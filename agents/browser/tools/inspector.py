"""Beneath the picture: what a page is made of, what it fetched, what
it logged — and the two ways to ask it for more.

A browser a person holds has a developer's window for this. This one
has no window at all, so what that window would show is read through
the driver and handed over as words: the page's HTML, the requests it
made and what came back, its console. None of it is more than the page
already sent to this browser.

Three things are never handed over, because they are the person's
session itself and not the page's content: a request's or a response's
headers, the cookies, and what the page stored. And what was fetched
while a sign-in was on the page, or while the person held the browser,
is listed but not opened.

Two ways ask for more than was sent. ``request`` fetches an address of
the page's own site with the page's session — by GET and nothing else,
which by the protocol's own rule is a read. ``script`` runs JavaScript
in the page, which can do anything the person can do there and cannot
be held to reading; whether it runs is the person's to say, each time,
and asking them is the tool's business, not this class's.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from .policy import same_site

#: The element, or the page, as HTML a reader can use: what draws and
#: what runs is taken out, a field's value is never in it (a hidden
#: field's value is as often a token as not), and white space is one
#: space.
SOURCE_JS = r"""
(n) => {
  const root = n ? document.querySelector('[data-dai-mark="' + n + '"]') : document.documentElement;
  if (!root) return null;
  const copy = root.cloneNode(true);
  copy.querySelectorAll('script, style, noscript, template, svg, canvas, link, meta, #dai-marks')
    .forEach(e => e.remove());
  copy.querySelectorAll('[data-dai-mark]').forEach(e => e.removeAttribute('data-dai-mark'));
  copy.querySelectorAll('input, textarea').forEach(e => {
    e.removeAttribute('value'); if (e.tagName === 'TEXTAREA') e.textContent = '';
  });
  copy.querySelectorAll('[style]').forEach(e => e.removeAttribute('style'));
  const walker = document.createTreeWalker(copy, NodeFilter.SHOW_COMMENT);
  const comments = []; while (walker.nextNode()) comments.push(walker.currentNode);
  comments.forEach(c => c.remove());
  return (copy.outerHTML || '').replace(/\s+/g, ' ').slice(0, 400000);
}
"""

#: What a control is and what it belongs to, for whoever judges a press
#: on it: the control, its form by its fields' names and never their
#: values, the words around it, and where it is on the screen.
ABOUT_JS = r"""
(n) => {
  const el = n ? document.querySelector('[data-dai-mark="' + n + '"]') : document.activeElement;
  if (!el || el === document.body) return null;
  const words = (e) => e ? (e.innerText || e.textContent || '').trim().replace(/\s+/g, ' ') : '';
  const labelOf = (e) => {
    const tied = e.id ? document.querySelector('label[for="' + CSS.escape(e.id) + '"]') : null;
    return (e.getAttribute('aria-label') || words(tied || e.closest('label')) ||
      e.getAttribute('placeholder') || e.getAttribute('title') || e.getAttribute('name') || '').slice(0, 80);
  };
  const form = el.closest('form');
  const fields = form ? Array.from(form.elements)
    .filter(f => f.type !== 'hidden' && f.tagName !== 'FIELDSET').slice(0, 30)
    .map(f => ({ name: (f.getAttribute('name') || f.id || '').slice(0, 60),
                 type: (f.getAttribute('type') || f.tagName).toLowerCase(),
                 label: f.tagName === 'BUTTON' ? words(f).slice(0, 80) : labelOf(f) })) : [];
  const around = el.closest('form, dialog, [role=dialog], [role=alertdialog], li, tr, article, section') ||
    el.parentElement;
  let heading = '';
  for (let e = el; e && !heading; e = e.parentElement) {
    const found = e.querySelector ? e.querySelector('h1, h2, h3, h4, [role=heading], legend') : null;
    if (found) heading = words(found).slice(0, 120);
  }
  const box = el.getBoundingClientRect();
  const tag = el.tagName.toLowerCase();
  return {
    control: { tag, type: (el.getAttribute('type') || '').toLowerCase(), role: el.getAttribute('role') || '',
               text: (words(el) || el.value || '').slice(0, 120), label: labelOf(el),
               name: (el.getAttribute('name') || el.id || '').slice(0, 60),
               href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 200) : '' },
    form: form ? { method: (form.getAttribute('method') || 'get').toLowerCase(),
                   action: (form.getAttribute('action') || '').slice(0, 200), fields } : null,
    heading, around: words(around).slice(0, 700), title: document.title,
    box: [box.left, box.top, box.width, box.height],
  };
}
"""

#: One read of an address, as the page itself would make it.
FETCH_JS = r"""
async (address) => {
  const answer = await fetch(address, { method: 'GET', credentials: 'include',
    headers: { 'Accept': 'application/json, text/plain, */*' } });
  const text = await answer.text();
  return { status: answer.status, address: answer.url,
           type: answer.headers.get('content-type') || '', size: text.length,
           text: text.slice(0, 400000) };
}
"""


class InspectError(Exception):
    pass


class Inspector:
    #: How much of what happened is kept to be asked about.
    FETCHED_KEPT = 400
    LOGGED_KEPT = 200
    #: A script longer than this is not run: the person must be able to
    #: read what they allow.
    SCRIPT_CHARS = 600
    SCRIPT_SECONDS = 20
    REQUEST_SECONDS = 25
    #: What is opened as words. Pictures, fonts and the rest are listed
    #: and not read.
    READABLE = ("json", "text/", "xml", "javascript", "csv", "x-www-form-urlencoded")
    #: How far around a control the judge is shown the screen.
    AROUND = (220, 150)

    def __init__(self, driver):
        self.driver = driver
        self.fetched: List[Dict[str, Any]] = []
        self.logged: List[Dict[str, str]] = []
        self._count = 0
        #: a sign-in is on the page, or the person holds the browser:
        #: what is fetched now is listed, never opened
        self.guarded = False

    # -- what is recorded, as it happens ---------------------------------------
    def watch(self, page) -> None:
        page.on("request", lambda request: self._asked(page, request))
        page.on("response", lambda response: self._answered(page, response))
        page.on("console", lambda message: self._logged(page, message.type, message.text))
        page.on("pageerror", lambda error: self._logged(page, "error", str(error)))

    def forget(self, page) -> None:
        self.fetched = [f for f in self.fetched if f["page"] is not page]
        self.logged = [entry for entry in self.logged if entry["page"] is not page]

    def _asked(self, page, request) -> None:
        # A page that goes somewhere else begins again: what is kept is
        # what this page fetched, since it opened.
        try:
            if request.is_navigation_request() and request.frame is page.main_frame:
                self.forget(page)
        except Exception:
            pass

    def _answered(self, page, response) -> None:
        try:
            request = response.request
            headers = response.headers
            self._count += 1
            self.fetched.append({
                "id": self._count, "page": page, "method": request.method, "address": response.url,
                "status": response.status, "kind": request.resource_type,
                "type": str(headers.get("content-type") or "").split(";")[0].strip(),
                "size": int(headers.get("content-length") or 0),
                "guarded": self.guarded, "response": response,
            })
            del self.fetched[:-self.FETCHED_KEPT]
        except Exception:
            pass

    def _logged(self, page, kind: str, text: str) -> None:
        self.logged.append({"page": page, "kind": str(kind), "text": str(text)[:600]})
        del self.logged[:-self.LOGGED_KEPT]

    # -- what is handed over ---------------------------------------------------------
    def network(self, wanted: str = "") -> List[str]:
        """What the page in front fetched since it opened, oldest first:
        one line each, and the number to open it by."""
        wanted = str(wanted or "").strip().lower()
        lines = []
        for entry in self.fetched:
            if entry["page"] is not self.driver.page:
                continue
            line = (f"{entry['id']}  {entry['method']} {entry['status']}  {entry['kind']}"
                    f"{' ' + entry['type'] if entry['type'] else ''}"
                    f"{' ' + str(entry['size']) + ' bytes' if entry['size'] else ''}  {entry['address'][:300]}")
            if not wanted or wanted in line.lower():
                lines.append(line)
        return lines

    async def response(self, number: int) -> str:
        """What came back for one of them, as words."""
        entry = next((f for f in self.fetched if f["id"] == int(number or 0)), None)
        if entry is None:
            raise InspectError(f"nothing was fetched under the number {number}; network lists them")
        if entry["guarded"] or (self.guarded and entry["page"] is self.driver.page):
            raise InspectError("what was fetched while a sign-in was on the page, or while the "
                               "person held the browser, is not opened")
        if not any(mark in entry["type"] for mark in self.READABLE):
            raise InspectError(f"it is {entry['type'] or 'of no stated kind'}, which is not read as words")
        try:
            return str(await entry["response"].text())
        except Exception as exc:
            raise InspectError(f"its body is no longer held by the browser: {self._short(exc)}")

    def console(self) -> List[str]:
        return [f"{entry['kind']}: {entry['text']}" for entry in self.logged
                if entry["page"] is self.driver.page]

    async def source(self, n: int = 0) -> str:
        """The HTML of one element, or of the page."""
        try:
            found = await self.driver.page.evaluate(SOURCE_JS, int(n or 0))
        except Exception as exc:
            raise InspectError(f"the page's HTML could not be read: {self._short(exc)}")
        if found is None:
            raise InspectError(f"no element {n} is on the page")
        return str(found)

    async def request(self, address: str) -> Dict[str, Any]:
        """An address of the page's own site, read with the page's
        session. GET and nothing else: a read, by the protocol's rule —
        and the same site and nothing else, since the session is that
        site's alone."""
        page = self.driver.page
        target = urljoin(page.url, str(address or "").strip())
        if not same_site(page.url, target):
            raise InspectError(f"{target} is not of the site the page is on; only the page's "
                               f"own site is read with its session — goto opens another")
        why = self.driver.policy.refusal(target)
        if why:
            raise InspectError(f"{target} cannot be read: {why}")
        try:
            return dict(await asyncio.wait_for(page.evaluate(FETCH_JS, target), self.REQUEST_SECONDS))
        except asyncio.TimeoutError:
            raise InspectError(f"{target} did not answer in {self.REQUEST_SECONDS} seconds")
        except Exception as exc:
            raise InspectError(f"{target} could not be read: {self._short(exc)}")

    async def script(self, code: str) -> str:
        """JavaScript, run in the page, and what it returned as JSON.
        The code is the body of a function: what it returns is the
        answer. Whoever calls this has asked the person."""
        try:
            answer = await asyncio.wait_for(
                self.driver.page.evaluate("(async () => {\n" + str(code) + "\n})()"),
                self.SCRIPT_SECONDS)
        except asyncio.TimeoutError:
            raise InspectError(f"the script did not end in {self.SCRIPT_SECONDS} seconds")
        except Exception as exc:
            raise InspectError(f"the script failed: {self._short(exc)}")
        try:
            return json.dumps(answer, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(answer)

    async def about(self, n: int = 0) -> Optional[Dict[str, Any]]:
        """A control and what it belongs to, with the screen around it
        as a picture — or None when there is no such control. With no
        mark, the field the keyboard is in."""
        page = self.driver.page
        try:
            found = await page.evaluate(ABOUT_JS, int(n or 0))
        except Exception:
            return None
        if not found:
            return None
        found["picture"] = b""
        try:
            left, top, width, height = (float(v) for v in found.pop("box"))
            size = page.viewport_size or {"width": 1280, "height": 800}
            x = max(0.0, left - self.AROUND[0])
            y = max(0.0, top - self.AROUND[1])
            clip = {"x": x, "y": y,
                    "width": max(1.0, min(size["width"] - x, width + 2 * self.AROUND[0])),
                    "height": max(1.0, min(size["height"] - y, height + 2 * self.AROUND[1]))}
            found["picture"] = await page.screenshot(type="jpeg", quality=60, clip=clip)
        except Exception:
            pass
        return found

    @staticmethod
    def _short(exc: Exception) -> str:
        return str(exc).splitlines()[0][:200] if str(exc) else exc.__class__.__name__
