"""The hand and the eyes: Playwright driving Chromium.

One page at a time in one browser context, with tabs. ``snapshot``
gives the model what a person sees — a screenshot with a numbered mark
on every element it could act on, the same elements as text, the
page's words — and what the guards need: whether a password field is
on the page, whether the page wants a human. Actions name elements by
their mark. ``frame`` is the plain picture for the live view, and
``dispatch`` feeds the person's own hand to the page when they take
over.

Nothing here talks to the platform; the tool does that.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from standard import Tunnel, TunnelRefused

from .inspector import Inspector
from .policy import AddressPolicy

#: The Chromium the program carries beside its code (publish/chromium.sh): its build, the libraries the sandbox's
#: base lacks, and fonts. Not there in a checkout, where Playwright's own install serves.
CARRIED = Path(__file__).resolve().parents[3] / "chromium"
if CARRIED.is_dir():
    os.environ.update(PLAYWRIGHT_BROWSERS_PATH=str(CARRIED / "browsers"), LD_LIBRARY_PATH=str(CARRIED / "lib"),
                      FONTCONFIG_FILE=str(CARRIED / "fonts.conf"))

VIEWPORT = (1280, 800)
#: What is read of the page's words, and of one element. How much of it
#: the model is shown is the memory's budget to say, not a number here.
TEXT_CHARS = 40000
READ_CHARS = 40000
#: How many elements are marked on one picture: more, and the numbers
#: cover the page they label. The rest are counted, and said.
MARKS_MAX = 150
#: How many tabs are told of, and how often their names are read again.
TABS_MAX = 20
TABS_FRESH_SECONDS = 1.0
#: How long a click is given to open its new tab, if it opens one.
NEW_TAB_SECONDS = 0.25
#: Reads of a page before it is shown as unreadable: one being replaced
#: by the next (a redirect after a form) is waited for and read again.
READ_ATTEMPTS = 3

#: Finds what a person could act on in the viewport, numbers it, and
#: draws the number on it. Returns the elements as data; the marks stay
#: on the elements (a data attribute) so an action can find them, and
#: the drawn labels are removed after the screenshot.
MARK_JS = r"""
(limit) => {
  const old = document.getElementById('dai-marks');
  if (old) old.remove();
  document.querySelectorAll('[data-dai-mark]').forEach(e => e.removeAttribute('data-dai-mark'));
  const selector = 'a[href], button, input:not([type=hidden]), select, textarea, summary, ' +
    '[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], ' +
    '[role=option], [role=combobox], [role=switch], [role=textbox], [contenteditable=true], [onclick]';
  const vw = window.innerWidth, vh = window.innerHeight;
  const seen = new Set();
  const out = [];
  let more = 0;
  const layer = document.createElement('div');
  layer.id = 'dai-marks';
  layer.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
  let n = 0;
  for (const el of document.querySelectorAll(selector)) {
    if (seen.has(el)) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || Number(style.opacity) === 0) continue;
    const box = el.getBoundingClientRect();
    if (box.width < 2 || box.height < 2) continue;
    if (box.bottom < 0 || box.right < 0 || box.top > vh || box.left > vw) continue;
    seen.add(el);
    if (out.length >= limit) { more += 1; continue; }
    n += 1;
    el.setAttribute('data-dai-mark', String(n));
    const tag = el.tagName.toLowerCase();
    const text = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ');
    // What a person reads beside the field comes before what the form
    // calls it inside: its label, then its hints, then its own name.
    const labelled = (el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null) ||
      (tag === 'input' || tag === 'select' || tag === 'textarea' ? el.closest('label') : null);
    const label = el.getAttribute('aria-label') || (labelled ? labelled.innerText.trim() : '') ||
      el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt') ||
      el.getAttribute('name') || '';
    out.push({
      n, tag, role: el.getAttribute('role') || '',
      type: tag === 'input' ? (el.getAttribute('type') || 'text') : '',
      text: text.slice(0, 80),
      label: label.replace(/\s+/g, ' ').slice(0, 80),
      value: (tag === 'input' || tag === 'textarea' || tag === 'select') ? String(el.value || '').slice(0, 80) : '',
      href: tag === 'a' ? String(el.getAttribute('href') || '').slice(0, 160) : '',
      // What the form itself says a field is for: its own name and the
      // standard autocomplete token, in whatever language the page is.
      name: String(el.getAttribute('name') || el.id || '').slice(0, 60),
      autocomplete: String(el.getAttribute('autocomplete') || '').toLowerCase().slice(0, 40),
      checked: (el.type === 'checkbox' || el.type === 'radio') ? !!el.checked : undefined,
      disabled: !!el.disabled,
      box: [Math.round(box.left), Math.round(box.top), Math.round(box.width), Math.round(box.height)],
    });
    const tag_ = document.createElement('div');
    tag_.textContent = String(n);
    tag_.style.cssText = 'position:fixed;left:' + Math.max(0, box.left) + 'px;top:' + Math.max(0, box.top - 14) +
      'px;background:#d6336c;color:#fff;font:bold 11px/14px system-ui,sans-serif;padding:0 4px;border-radius:3px;' +
      'box-shadow:0 0 0 1px #fff;';
    layer.appendChild(tag_);
    const outline = document.createElement('div');
    outline.style.cssText = 'position:fixed;left:' + box.left + 'px;top:' + box.top + 'px;width:' + box.width +
      'px;height:' + box.height + 'px;outline:2px solid #d6336c;outline-offset:-1px;';
    layer.appendChild(outline);
  }
  document.body.appendChild(layer);
  // Only frames a person could see and act in: an invisible captcha
  // frame (a login page's silent risk check, a v3 badge parked off the
  // edge) is not a human check, and stopping for it hands the person a
  // page with nothing to solve.
  const frames = Array.from(document.querySelectorAll('iframe')).filter(f => {
    const r = f.getBoundingClientRect(); const s = getComputedStyle(f);
    return r.width >= 200 && r.height >= 60 && s.display !== 'none' && s.visibility !== 'hidden'
      && s.opacity !== '0' && r.right <= window.innerWidth + 1 && r.left >= -1;
  }).map(f => f.getAttribute('src') || '').filter(Boolean);
  return { elements: out, more, text: (document.body.innerText || '').slice(0, 40000), frames,
           scroll: [Math.round(window.scrollY), Math.round(document.documentElement.scrollHeight), vh] };
}
"""
UNMARK_JS = "() => { const l = document.getElementById('dai-marks'); if (l) l.remove(); }"


@dataclass
class Snapshot:
    url: str
    title: str
    elements: List[Dict[str, Any]]
    text: str
    frames: List[str]
    image: bytes
    width: int
    height: int
    scroll: Tuple[int, int, int] = (0, 0, 0)
    tabs: List[str] = field(default_factory=list)
    #: elements in view that were not marked, past MARKS_MAX
    more: int = 0

    @property
    def signature(self) -> str:
        """What 'the page did not change' is judged by."""
        digest = hashlib.sha1()
        digest.update(self.url.encode("utf-8"))
        digest.update(str(self.scroll[0]).encode("utf-8"))
        for element in self.elements:
            digest.update(f"{element.get('tag')}|{element.get('text')}|{element.get('value')}".encode("utf-8"))
        digest.update(self.text[:2000].encode("utf-8"))
        return digest.hexdigest()

    def element(self, n: int) -> Optional[Dict[str, Any]]:
        return next((e for e in self.elements if int(e.get("n") or 0) == int(n)), None)


class DriverError(Exception):
    pass


class Driver:
    """One browser, one context, the page in front."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.policy = AddressPolicy()
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None
        self.downloads: List[Dict[str, Any]] = []
        self._tmp = tempfile.mkdtemp(prefix="dai-browser-")
        #: what the pages fetched and logged, and the ways to look
        #: beneath the picture
        self.inspector = Inspector(self)
        self._tabs: List[Dict[str, Any]] = []
        self._tabs_read = 0.0
        #: counts every tab that came, went or came to the front
        self._tabs_changed = 0
        self._tabs_as_of = -1
        self._adopted: List[Any] = []

    # -- life --------------------------------------------------------------
    async def start(self, storage_state: Optional[Dict[str, Any]] = None) -> None:
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        try:
            # A sandbox's /dev/shm is small and Chromium fills it with
            # tabs' shared memory; told not to, it uses ordinary memory
            # instead. Chromium's own sandbox is off: the sandbox it runs
            # in refuses the namespaces it would make. The automation
            # flag is off: a browser that announces it is driven fails
            # every human check the person then solves by hand, however
            # right.
            args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
            # In the sandbox, the browser's one way out is the sandbox's.
            way_out = self.policy.for_the_browser()
            through = {"proxy": way_out} if way_out else {}
            try:
                # Chromium's full build in its new headless mode: the one
                # whose client hints, mime types and permissions read as
                # an ordinary browser's. The headless shell Playwright
                # launches by default names itself HeadlessChrome in the
                # brands it sends with every request, and a site's human
                # check fails a person on that alone.
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless, channel="chromium", args=args, **through)
            except Exception:
                self._browser = await self._playwright.chromium.launch(
                    headless=self.headless, args=args, **through)
        except Exception as exc:
            raise DriverError(
                f"The browser could not start: {str(exc).splitlines()[0]}. The program carries "
                f"Chromium beside its code, under chromium/.")
        self._context = await self._browser.new_context(
            viewport={"width": VIEWPORT[0], "height": VIEWPORT[1]},
            storage_state=storage_state or None,
            accept_downloads=True,
            locale="en-US",
            user_agent=await self._plain_user_agent(),
        )
        self._context.set_default_timeout(8000)
        await self._context.add_init_script(self.PLAIN_JS)
        await self._context.add_init_script(self.NO_AUTHENTICATOR_JS)
        await self._context.route("**/*", self._gate)
        self._context.on("page", self._adopt)
        self._adopt(await self._context.new_page())

    #: What a headless browser says about itself that an ordinary one
    #: does not, said the ordinary way. Not a disguise of who is acting
    #: — the person watches every step and approves what matters — but
    #: the difference between a human check that can be passed by a
    #: person and one that fails them on sight.
    PLAIN_JS = """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        if (!window.chrome) { window.chrome = { runtime: {} }; }
        if (navigator.plugins && navigator.plugins.length === 0) {
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
        }
    """

    #: This browser has no authenticator: no Windows Hello, no
    #: fingerprint, no security key — those are on the person's own
    #: device, and this browser runs on the runtime's host. Left alone,
    #: a passkey request here waits for a window that never opens, and
    #: the page waits with it, whoever drives: a run, or the person in
    #: the live view. So the browser says what is true — no platform
    #: authenticator — and a passkey request is refused at once, the
    #: way it is when a person presses Cancel; the site then offers its
    #: other ways to sign in. Password credentials are left as they are.
    NO_AUTHENTICATOR_JS = """
        (() => {
            if (!window.PublicKeyCredential || !navigator.credentials) return;
            const none = () => Promise.resolve(false);
            try { PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable = none; } catch (e) {}
            try { PublicKeyCredential.isConditionalMediationAvailable = none; } catch (e) {}
            const refuse = (real) => function (options) {
                if (options && options.publicKey) {
                    return Promise.reject(new DOMException(
                        'The operation either timed out or was not allowed.', 'NotAllowedError'));
                }
                return real.apply(this, arguments);
            };
            try {
                const credentials = Object.getPrototypeOf(navigator.credentials);
                credentials.get = refuse(credentials.get);
                credentials.create = refuse(credentials.create);
            } catch (e) {}
        })();
    """

    async def _plain_user_agent(self) -> str:
        """The browser's own user agent with the word that names a
        headless build taken out: what the same Chromium says when it
        has a window."""
        try:
            probe = await self._browser.new_context()
            page = await probe.new_page()
            agent = str(await page.evaluate("navigator.userAgent") or "")
            await probe.close()
        except Exception:
            return ""
        return agent.replace("HeadlessChrome", "Chrome")

    async def _gate(self, route, request) -> None:
        why = self.policy.refusal(request.url)
        if why:
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    def _on_download(self, download) -> None:
        self.downloads.append({"download": download, "filename": download.suggested_filename})

    def _adopt(self, page) -> None:
        """A page that opened — the first, one the agent asked for, or
        one a site opened in a new tab or window — comes to the front,
        as it does in a browser a person holds: what a click opened is
        what they see next. When it closes, the one before it returns."""
        self.page = page
        self._tabs_changed += 1
        if page in self._adopted:
            return
        self._adopted.append(page)
        page.on("download", self._on_download)
        page.on("close", self._left)
        self.inspector.watch(page)

    def _left(self, page) -> None:
        if page in self._adopted:
            self._adopted.remove(page)
        self.inspector.forget(page)
        self._tabs_changed += 1
        if self.page is page:
            still = [p for p in (self._context.pages if self._context else []) if p is not page]
            self.page = still[-1] if still else None

    async def stop(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer is not None:
                    await closer.close()
            except Exception:
                pass
        try:
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception:
            pass
        self._playwright = self._browser = self._context = self.page = None

    async def storage_state(self) -> Dict[str, Any]:
        return await self._context.storage_state() if self._context else {}

    # -- eyes --------------------------------------------------------------
    async def settle(self, seconds: float = 2.0) -> None:
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        try:
            await self.page.wait_for_load_state("networkidle", timeout=int(seconds * 1000))
        except Exception:
            pass

    async def _read(self, page, marks: bool) -> Dict[str, Any]:
        if marks:
            return await page.evaluate(MARK_JS, MARKS_MAX)
        return {"elements": [], "text": await page.evaluate("() => document.body ? document.body.innerText : ''"),
                "frames": [], "scroll": [0, 0, VIEWPORT[1]]}

    async def snapshot(self, marks: bool = True) -> Snapshot:
        """The page as the model sees it: marked picture, elements, words."""
        page = self.page
        found = None
        for attempt in range(READ_ATTEMPTS):
            try:
                found = await self._read(page, marks)
                break
            except Exception as exc:
                # A form sent a moment ago may still be on its way to
                # the next page — a redirect after a sign-in, say — and
                # a page being replaced cannot be read. Wait for the new
                # one and look again, rather than show the model nothing.
                if attempt + 1 < READ_ATTEMPTS:
                    await self.settle(1.0)
                    continue
                found = {"elements": [], "text": f"(the page could not be read: {exc})",
                         "frames": [], "scroll": [0, 0, VIEWPORT[1]]}
        try:
            image = await page.screenshot(type="jpeg", quality=65)
        except Exception:
            image = b""
        if marks:
            try:
                await page.evaluate(UNMARK_JS)
            except Exception:
                pass
        tabs = []
        for other in (self._context.pages if self._context else []):
            try:
                tabs.append(f"{'* ' if other is page else ''}{await other.title() or other.url}")
            except Exception:
                pass
        return Snapshot(
            url=page.url, title=await self._title(),
            elements=list(found.get("elements") or []),
            text=str(found.get("text") or "")[:TEXT_CHARS],
            frames=list(found.get("frames") or []),
            image=image, width=VIEWPORT[0], height=VIEWPORT[1],
            scroll=tuple(found.get("scroll") or (0, 0, VIEWPORT[1])),
            tabs=tabs, more=int(found.get("more") or 0),
        )

    #: What the live view accepts for one frame (the standard's 300 KB
    #: of base64, with room), and the qualities tried to fit a
    #: picture-heavy page under it. A frame over the cap is not shown,
    #: and a shop's front page at quality 50 is over it — the person
    #: then watched nothing at all.
    FRAME_MAX_BYTES = 225_000
    FRAME_QUALITIES = (50, 35, 25, 15)
    #: While the person drives, a lighter picture: capture and transfer
    #: are what stand between their hand and what they see.
    FAST_QUALITIES = (30, 20, 12)

    async def frame(self, fast: bool = False) -> bytes:
        """The plain picture, for the live view, small enough to send."""
        image = b""
        for quality in (self.FAST_QUALITIES if fast else self.FRAME_QUALITIES):
            try:
                image = await self.page.screenshot(type="jpeg", quality=quality)
            except Exception:
                return b""
            if len(image) <= self.FRAME_MAX_BYTES:
                return image
        return image if len(image) <= self.FRAME_MAX_BYTES else b""

    async def _title(self) -> str:
        try:
            return await self.page.title()
        except Exception:
            return ""

    async def read(self, n: int = 0) -> str:
        """One element's words in full, or the page's."""
        if n:
            try:
                return str(await self._locator(n).inner_text(timeout=3000))[:READ_CHARS]
            except Exception as exc:
                raise DriverError(f"element {n} could not be read: {self._short(exc)}")
        try:
            return str(await self.page.evaluate("() => document.body ? document.body.innerText : ''"))[:READ_CHARS]
        except Exception as exc:
            raise DriverError(f"the page could not be read: {self._short(exc)}")

    # -- hand --------------------------------------------------------------
    def _locator(self, n: int):
        return self.page.locator(f'[data-dai-mark="{int(n)}"]').first

    async def goto(self, url: str) -> None:
        why = self.policy.refusal(url)
        if why:
            raise DriverError(f"{url} cannot be visited: {why}.")
        try:
            answer = await self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
        except Exception as exc:
            # A refused tunnel, or a refusal with nothing in it to show.
            refused = await self._refused_by_the_way_out(url) if any(
                failure in str(exc) for failure in self.REFUSED_FAILURES) else ""
            raise DriverError(f"{url} cannot be visited: {refused}." if refused
                              else f"{url} did not load: {self._short(exc)}")
        refused = await self._refused_by_the_way_out(answer.url) if answer is not None \
            and answer.status in (403, 502) else ""
        if refused:
            raise DriverError(f"{url} cannot be visited: {refused}.")
        await self.settle()

    #: How Chromium says a page failed that the way out may have refused.
    REFUSED_FAILURES = ("ERR_TUNNEL_CONNECTION_FAILED", "ERR_HTTP_RESPONSE_CODE_FAILURE")

    async def _refused_by_the_way_out(self, url: str) -> str:
        """Why the sandbox's way out refuses this address, or '': asked
        with a tunnel to it, whose refusal is always the way out's own
        answer and never a site's — a page that failed, or came back
        forbidden, may have been either."""
        if not self.policy.proxy:
            return ""
        parts = urlsplit(url)
        try:
            link = await asyncio.to_thread(Tunnel.open, parts.hostname or "",
                                           parts.port or (443 if parts.scheme == "https" else 80), 15)
        except TunnelRefused as refused:
            return refused.reason.strip().rstrip(".") or "the way out refused it"
        except OSError:
            return ""
        link.close()
        return ""

    async def back(self) -> None:
        try:
            await self.page.go_back(wait_until="domcontentloaded", timeout=10000)
        except Exception as exc:
            raise DriverError(f"could not go back: {self._short(exc)}")
        await self.settle()

    async def click(self, n: int) -> None:
        target = self._locator(n)
        try:
            await target.scroll_into_view_if_needed(timeout=3000)
            await target.click(timeout=5000)
        except Exception as exc:
            raise DriverError(f"element {n} could not be clicked: {self._short(exc)}")
        # A page the click opened in a new tab arrives a moment after
        # it: waited for, so what is looked at next is what was opened.
        await asyncio.sleep(NEW_TAB_SECONDS)
        await self.settle(1.5)

    async def type(self, n: int, text: str, submit: bool = False) -> None:
        target = self._locator(n)
        try:
            await target.scroll_into_view_if_needed(timeout=3000)
            await target.click(timeout=5000)
            await target.fill("", timeout=3000)
            await target.type(str(text), delay=25, timeout=30000)
            if submit:
                await target.press("Enter")
        except Exception as exc:
            raise DriverError(f"could not type into element {n}: {self._short(exc)}")
        await self.settle(1.0)

    async def fill(self, n: int, text: str) -> None:
        """Set a field's value at once — for a login, where pacing buys nothing."""
        target = self._locator(n)
        try:
            await target.fill(str(text), timeout=5000)
        except Exception as exc:
            raise DriverError(f"could not fill element {n}: {self._short(exc)}")

    async def select(self, n: int, option: str) -> None:
        target = self._locator(n)
        try:
            try:
                await target.select_option(label=str(option), timeout=5000)
            except Exception:
                await target.select_option(value=str(option), timeout=5000)
        except Exception as exc:
            raise DriverError(f"could not choose {option!r} in element {n}: {self._short(exc)}")
        await self.settle(1.0)

    async def press(self, key: str) -> None:
        try:
            await self.page.keyboard.press(str(key))
        except Exception as exc:
            raise DriverError(f"could not press {key!r}: {self._short(exc)}")
        await self.settle(1.0)

    async def scroll(self, direction: str = "down", amount: int = 600) -> None:
        delta = int(amount) if direction == "down" else -int(amount)
        try:
            await self.page.mouse.wheel(0, delta)
            await asyncio.sleep(0.4)
        except Exception as exc:
            raise DriverError(f"could not scroll: {self._short(exc)}")

    async def hover(self, n: int) -> None:
        try:
            await self._locator(n).hover(timeout=5000)
            await asyncio.sleep(0.4)
        except Exception as exc:
            raise DriverError(f"element {n} could not be hovered: {self._short(exc)}")

    # -- tabs --------------------------------------------------------------
    async def tab(self, action: str, index: Optional[int] = None, url: str = "") -> None:
        pages = self._context.pages
        if action == "new":
            self._adopt(await self._context.new_page())
            if url:
                await self.goto(url)
        elif action == "switch":
            if index is None or not 1 <= int(index) <= len(pages):
                raise DriverError(f"no tab {index}; there are {len(pages)}")
            self.page = pages[int(index) - 1]
            await self.page.bring_to_front()
        elif action == "close":
            if len(pages) <= 1:
                raise DriverError("the last tab stays open")
            if index is not None and not 1 <= int(index) <= len(pages):
                raise DriverError(f"no tab {index}; there are {len(pages)}")
            closing = self.page if index is None else pages[int(index) - 1]
            front = self.page
            await closing.close()
            if closing is not front:
                self.page = front
        else:
            raise DriverError(f"unknown tab action {action!r}")
        self._tabs_changed += 1
        await self.settle(1.0)

    async def tabs(self) -> List[Dict[str, Any]]:
        """The open tabs, in their order, for whoever watches: a name,
        where it is, and which one is in front. Read again at most
        every TABS_FRESH_SECONDS — a frame ten times a second does not
        ask every page its name."""
        if self._context is None:
            return []
        if self._tabs and self._tabs_as_of == self._tabs_changed \
                and time.monotonic() - self._tabs_read < TABS_FRESH_SECONDS:
            return self._tabs
        as_of = self._tabs_changed
        named = []
        for page in self._context.pages[:TABS_MAX]:
            try:
                named.append((page, await page.title()))
            except Exception:
                named.append((page, ""))
        found = []
        for index, (page, title) in enumerate(named, 1):
            address = "" if page.url.startswith("about:") else page.url
            found.append({"index": index, "title": (title or address or "New tab")[:120],
                          "address": address[:300], "active": page is self.page})
        self._tabs, self._tabs_read, self._tabs_as_of = found, time.monotonic(), as_of
        return found

    # -- files -------------------------------------------------------------
    async def download(self, n: int) -> Tuple[str, bytes]:
        """Click and keep what comes down."""
        try:
            async with self.page.expect_download(timeout=30000) as waiting:
                await self._locator(n).click(timeout=5000)
            download = await waiting.value
            path = os.path.join(self._tmp, download.suggested_filename or "download")
            await download.save_as(path)
            with open(path, "rb") as handle:
                return download.suggested_filename or "download", handle.read()
        except Exception as exc:
            raise DriverError(f"nothing was downloaded from element {n}: {self._short(exc)}")

    async def upload(self, n: int, filename: str, content: bytes) -> None:
        path = os.path.join(self._tmp, os.path.basename(filename) or "upload")
        with open(path, "wb") as handle:
            handle.write(content)
        try:
            await self._locator(n).set_input_files(path, timeout=5000)
        except Exception as exc:
            raise DriverError(f"element {n} does not take a file: {self._short(exc)}")
        await self.settle(1.0)

    # -- the person's hand -------------------------------------------------
    #: What the page says about an element: enough to name it in a
    #: note ("clicked 'Sign in'"), never what was typed into it.
    DESCRIBE_JS = """(point) => {
        const el = point ? document.elementFromPoint(point.x, point.y) : document.activeElement;
        if (!el || el === document.body) return null;
        const it = el.closest('a, button, input, select, textarea, [role=button], [role=link], label') || el;
        const label = (it.labels && it.labels[0] && it.labels[0].innerText)
            || it.getAttribute('aria-label') || it.getAttribute('placeholder')
            || it.getAttribute('name') || it.getAttribute('title') || '';
        const words = (it.tagName === 'INPUT' || it.tagName === 'TEXTAREA' || it.tagName === 'SELECT')
            ? label : (it.innerText || it.value || label || '');
        return {tag: it.tagName.toLowerCase(), type: (it.getAttribute('type') || '').toLowerCase(),
                words: String(words).trim().replace(/\\s+/g, ' ').slice(0, 60)};
    }"""

    async def describe(self, x: Optional[float] = None, y: Optional[float] = None) -> str:
        """The element under a point, or the focused one, in words."""
        try:
            found = await self.page.evaluate(
                self.DESCRIBE_JS, {"x": x, "y": y} if x is not None else None)
        except Exception:
            return ""
        if not found:
            return ""
        words = found.get("words") or ""
        tag = found.get("tag") or ""
        kind = {"a": "link", "button": "button", "input": found.get("type") or "field",
                "textarea": "field", "select": "choice"}.get(tag, tag)
        return f"'{words}' ({kind})" if words else f"a {kind}"

    async def dispatch(self, events: List[Dict[str, Any]]) -> List[str]:
        """What the person did on the live view, done to the page — and
        said back in words, so a run can learn from it and the site's
        notes can keep it. Never what they typed: a field is named, a
        password is "the password", the characters are nobody's."""
        page = self.page
        trail: List[str] = []
        before = page.url if page is not None else ""
        typing_into = ""
        for event in events:
            kind, action = event.get("type"), event.get("action")
            # The page in front now: a click a moment ago may have
            # opened another.
            page = self.page
            if page is None:
                break
            try:
                if kind == "tab":
                    # The person's hand on the tabs: to one, away with
                    # one, a new one.
                    index = event.get("index")
                    names = {t["index"]: t["title"] for t in await self.tabs()}
                    named = f" '{names[int(index)]}'" if index and int(index) in names else ""
                    try:
                        await self.tab(str(action or ""), int(index) if index else None)
                        trail.append({"switch": f"went to tab {index}{named}",
                                      "close": f"closed tab {index or 'in front'}{named}",
                                      "new": "opened a new tab"}.get(str(action), f"tab {action}"))
                        page = self.page
                        before = page.url if page is not None else ""
                    except DriverError as exc:
                        trail.append(str(exc))
                elif kind == "navigate":
                    url = str(event.get("url") or "")
                    try:
                        await self.goto(url)
                        trail.append(f"went to {url}")
                    except DriverError as exc:
                        trail.append(str(exc))
                elif kind == "mouse":
                    x, y = float(event.get("x") or 0), float(event.get("y") or 0)
                    if action == "move":
                        await page.mouse.move(x, y)
                    elif action == "down":
                        await page.mouse.move(x, y)
                        target = await self.describe(x, y)
                        await page.mouse.down(button=str(event.get("button") or "left"))
                        if target:
                            trail.append(f"clicked {target}")
                            typing_into = ""
                    elif action == "up":
                        await page.mouse.up(button=str(event.get("button") or "left"))
                    elif action == "wheel":
                        await page.mouse.move(x, y)
                        await page.mouse.wheel(float(event.get("delta_x") or 0),
                                               float(event.get("delta_y") or 0))
                elif kind == "key":
                    key = str(event.get("key") or "")
                    if not key:
                        continue
                    if action == "down":
                        text = str(event.get("text") or "")
                        if text and not event.get("modifiers"):
                            await page.keyboard.insert_text(text)
                            if not typing_into:
                                # Named once per burst of typing: asking
                                # the page on every key would slow the
                                # person's hand for nothing.
                                field = await self.describe() or "a field"
                                typing_into = field
                                trail.append("entered the password" if "(password)" in field
                                             else f"typed into {field}")
                        else:
                            await page.keyboard.down(self._key(key))
                            if key == "Enter":
                                trail.append("pressed Enter")
                            if key in ("Tab", "Enter"):
                                typing_into = ""
                    elif action == "up":
                        if not (str(event.get("text") or "") and not event.get("modifiers")):
                            await page.keyboard.up(self._key(key))
            except Exception:
                continue
        after = page.url if page is not None else ""
        if after and after != before:
            trail.append(f"the page became {after}")
        return trail

    @staticmethod
    def _key(key: str) -> str:
        return {" ": "Space", "Esc": "Escape"}.get(key, key)

    @staticmethod
    def _short(exc: Exception) -> str:
        return str(exc).splitlines()[0][:200] if str(exc) else exc.__class__.__name__
