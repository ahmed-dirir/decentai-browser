"""The run: look, decide, act, look again — with the person's cards at
the moments that are theirs, the live view throughout, and what was
learned kept for next time."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from standard import ResourceDenied, ToolBase

from . import guards
from .brain import Brain
from .driver import VIEWPORT, Driver, DriverError, Snapshot
from .inspector import InspectError
from .judge import Judge
from .memory import SiteNotes, cut
from .policy import domain_of

#: A frame this often while a browser is handed to the person and
#: their hand is off it.
HANDOFF_FRAME_SECONDS = 0.35
#: While the person drives, frames on their own clock, this often:
#: their hand is never made to wait for a picture.
HANDOFF_FRAME_MIN_SECONDS = 0.1
#: How long the input loop waits for the person's next action before
#: looking again at whether it should end.
INPUT_WAIT_SECONDS = 0.05
#: What the notes of one site may hold (the platform's limit on a plain
#: field is a little above it).
NOTES_CHARS = 8000
#: What is kept of one stretch of the person's own doing, on the notes,
#: and how much of it the mind is told at once.
TRAIL_NOTE_MAX = 400
TRAIL_SHOWN = 12


async def show_frame(call, driver, image: bytes, width: int, height: int) -> None:
    """One frame to the live view, with the tabs that are open: a
    person who cannot see the tabs cannot tell where a click went, nor
    close the one they do not want."""
    await call.screen.show(image, width, height, await driver.tabs())


async def drive(call, driver, until, idle_interval: float = HANDOFF_FRAME_SECONDS,
                hear=None, may_drive=None):
    """The person's hand on a browser: inputs go to the page the moment
    they arrive, and frames flow on their own clock beside them — one
    every HANDOFF_FRAME_MIN_SECONDS while they hold control, lighter
    pictures, and one every ``idle_interval`` otherwise. Ends when
    ``until()`` says why. ``may_drive()`` false means the person only
    watches for now — a run holds the browser — and their input is
    dropped rather than fed under the run's hand. Returns (why, what
    they did in words)."""
    trail: List[str] = []
    stop = asyncio.Event()

    async def frames() -> None:
        while not stop.is_set():
            taken = call.screen.taken
            started = time.monotonic()
            if taken:
                # Their hand is on the browser: what the page fetches
                # now may be their sign-in, and is never opened.
                driver.inspector.guarded = True
            try:
                image = await driver.frame(fast=taken)
                if image and driver.page is not None:
                    size = driver.page.viewport_size
                    width, height = (size["width"], size["height"]) if size else VIEWPORT
                    await show_frame(call, driver, image, width, height)
            except Exception:
                pass
            interval = HANDOFF_FRAME_MIN_SECONDS if taken else idle_interval
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))

    pictures = asyncio.get_running_loop().create_task(frames())
    try:
        while True:
            # What is already waiting is the person's, and reaches the
            # page before it is asked whether they still hold it: a
            # take, a few actions and a release can all arrive inside
            # one of the run's steps.
            events = call.screen.inputs()
            if not events:
                why = until()
                if why:
                    return why, trail
                events = await call.screen.wait_input(INPUT_WAIT_SECONDS)
            if events and (may_drive is None or may_drive()):
                try:
                    trail += await driver.dispatch(events)
                except Exception:
                    pass
            if hear is not None:
                for text in call.screen.said():
                    hear(text)
    finally:
        stop.set()
        pictures.cancel()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: A watch (the browser shown on request) ends on its own after this.
WATCH_MAX_SECONDS = 3600
#: While the person only watches, a frame this often; taking over
#: makes frames follow their hand (HANDOFF_FRAME_MIN_SECONDS).
WATCH_IDLE_FRAME_SECONDS = 1.0


class Kept:
    """The chat's browser, kept open between its runs: the next run
    finds the page the last one left. One at a time — a second run
    waits its turn on the lock."""

    def __init__(self, driver: Driver):
        self.driver = driver
        self.lock = asyncio.Lock()


class Browser:
    """The one browser of this process. A process serves one chat, so
    nothing here is ever handed across chats; it goes when the process
    does, which the runtime ends once the chat stops using it. Where it
    was is kept as long as the process lives, so the next run or watch
    without an address starts there after it closed."""

    def __init__(self):
        self.kept: Optional[Kept] = None
        self.was = ""

    def get(self) -> Optional[Kept]:
        if self.kept is not None and self.kept.driver.page is None:
            self.kept = None
        return self.kept

    def keep(self, driver: Driver) -> Kept:
        if self.kept is None or self.kept.driver is not driver:
            self.kept = Kept(driver)
        return self.kept

    def remember(self, url: str) -> None:
        if str(url or "").startswith(("http://", "https://")):
            self.was = str(url)

    async def drop(self) -> None:
        kept, self.kept = self.kept, None
        if kept is not None:
            await kept.driver.stop()


BROWSER = Browser()


class BrowseTool(ToolBase):
    id = "browse"

    #: The whole run stops when the page has not changed this many times
    #: in a row after an action.
    STUCK_AFTER = 3
    #: A run that keeps circling after being told so ends: this many
    #: circling actions in a row (Brain.CIRCLE_AFTER says when the
    #: model is told; this is when nobody is listening).
    CIRCLE_STOP = 14
    #: This many answers in a row that could not be read as an action
    #: end the run: the model is told each time what was wrong, and a
    #: model that cannot answer three times will not on the tenth.
    UNREADABLE_STOP = 3
    #: Actions meant to change the page. Only these count towards a
    #: page that "stopped changing": reading, recording and asking
    #: leave it as it was, and that is not being stuck.
    MOVING = frozenset(("click", "type", "select", "press", "scroll", "goto", "back",
                        "tab", "login", "code", "download", "upload", "handoff"))

    # -- browse_screenshot ---------------------------------------------------
    async def screenshot(self, call):
        url = str(call.inputs["url"]).strip()
        driver = Driver(headless=self._headless())
        try:
            await call.progress("Starting the browser")
            await driver.start()
            await call.progress(f"Opening {url}")
            await driver.goto(url)
            snapshot = await driver.snapshot(marks=False)
            await call.progress("Saving the screenshot")
            saved = self._save_image(call, snapshot, "screenshot")
            await call.progress("Closing the browser")
            return {"file_ref": saved, "title": snapshot.title,
                    "final_url": snapshot.url}, "success"
        except DriverError as exc:
            return {"error": str(exc), "kind": "browser"}, "error"
        finally:
            await driver.stop()

    # -- browse_run ------------------------------------------------------------
    async def run(self, call):
        goal = str(call.inputs["goal"]).strip()
        start_url = str(call.inputs.get("start_url") or "").strip()
        if start_url and "://" not in start_url:
            start_url = "https://" + start_url
        max_steps = int(call.inputs.get("max_steps") or 60)

        # The chat's browser, if the last run left one open: the run
        # continues on its page unless sent elsewhere.
        kept = BROWSER.get()
        if kept is not None:
            async with kept.lock:
                return await self._run_in(call, kept.driver, goal, start_url,
                                          max_steps, continuing=True)
        if not start_url:
            # No browser and no address: where the chat's browser was
            # when it last closed, if anywhere.
            start_url = BROWSER.was
            if start_url:
                await call.progress(f"Resuming at {start_url}")
        if not start_url:
            return {"outcome": "failed", "steps": 0, "final_url": "", "records": [],
                    "summary": "No browser is open in this chat and no "
                               "start_url was given: say where to start."}, "success"
        driver = Driver(headless=self._headless())
        return await self._run_in(call, driver, goal, start_url, max_steps, continuing=False)

    async def _run_in(self, call, driver: Driver, goal: str, start_url: str,
                      max_steps: int, continuing: bool):
        here = driver.page.url if continuing and driver.page is not None else ""
        domain = domain_of(start_url or here)
        brain = Brain(call)
        # The files the person handed the run, by their paths in the
        # program's home: what upload may put into a page.
        brain.files = [str(given) for given in call.inputs.get("files") or []]
        run = _Run(call, driver, brain, goal, domain)
        try:
            brain.notes = await run.load_notes()
            if not continuing:
                await driver.start(storage_state=await run.load_session())
            if start_url and (not continuing or domain_of(here) != domain
                              or start_url.rstrip("/") != here.rstrip("/")):
                await call.progress(f"Opening {start_url}")
                await driver.goto(start_url)
            elif continuing:
                await call.progress(f"Continuing at {here}")
                brain.continuing = here
            await brain.make_plan(goal, start_url or here)
            outcome = await run.loop(max_steps)
        except DriverError as exc:
            outcome = {"outcome": "failed", "summary": str(exc)}
        except Exception as exc:  # the browser died, the model refused
            outcome = {"outcome": "failed", "summary": f"The run stopped: {exc}"}
        finally:
            result = await run.close(outcome)
            if driver.page is not None:
                BROWSER.keep(driver)
                BROWSER.remember(result.get("final_url", ""))
            else:
                await driver.stop()
        return result, "success"

    async def watch(self, call):
        """The browser shown on request, before anything is asked: the
        chat's kept browser, or a fresh one at a blank page that is kept
        from now on. Frames flow, the person's hand reaches the page,
        and whatever they did — a sign-in above all — is the browser's
        state for the next run. Ends when they close the panel or on its
        own clock. Never holds the browser's lock: a run takes it
        whenever it needs to, and the person watches until the run is
        done."""
        kept = BROWSER.get()
        if str(call.inputs.get("action") or "open") == "quit":
            # The person closed the browser itself, not the panel: where
            # it was is remembered, and it goes now.
            url = kept.driver.page.url if kept is not None and kept.driver.page is not None else ""
            if kept is not None:
                BROWSER.remember(url)
                await BROWSER.drop()
                await call.progress("The browser is closed")
            return {"outcome": "quit", "final_url": url,
                    "summary": "The browser was closed" + (f" at {url}" if url else "") + "."}, "success"
        if kept is None:
            resume = BROWSER.was
            driver = Driver(headless=self._headless())
            await driver.start(storage_state=await _Run.session_for(call, domain_of(resume)) if resume else None)
            if resume:
                try:
                    await call.progress(f"Resuming at {resume}")
                    await driver.goto(resume)
                except DriverError as exc:
                    await call.progress(str(exc))
            kept = BROWSER.keep(driver)
        driver = kept.driver
        await call.progress("Showing the browser")
        started = time.monotonic()
        opened_at = driver.page.url if driver.page is not None else ""

        def until() -> str:
            if call.screen.closed:
                return "closed"
            if time.monotonic() - started > WATCH_MAX_SECONDS:
                return "timeout"
            if driver.page is None:
                return "browser_closed"
            return ""

        # A run of the same chat may take the browser while the person
        # watches: the picture goes on, their hand waits until the run
        # is done with it. A typed address goes through the same policy
        # a run's goto obeys.
        outcome, trail = await drive(call, driver, until, idle_interval=WATCH_IDLE_FRAME_SECONDS,
                                     may_drive=lambda: not kept.lock.locked())
        for line in trail:
            if "cannot be visited" in line:
                await call.progress(line)
        await self._note_person(call, driver, trail)
        if driver.page is not None:
            BROWSER.remember(driver.page.url)
            if trail:
                # What the person did by hand — a sign-in above all — is
                # the browser's alone until it is kept: a browser that
                # closes before the next run would take it along. Kept
                # under the site they began at and the one they ended
                # at, since a sign-in often starts on one and lands on
                # another, and the next run may start at either.
                for domain in dict.fromkeys((domain_of(driver.page.url), domain_of(opened_at))):
                    await _Run.keep_session(call, driver, domain)
        try:
            await call.screen.close()
        except Exception:
            pass
        url = driver.page.url if driver.page is not None else ""
        return {"outcome": outcome, "final_url": url,
                "summary": {"closed": "The person closed the live view.",
                            "timeout": "The watch ended on its own clock.",
                            "browser_closed": "The browser closed."}.get(outcome, outcome)}, "success"

    async def _note_person(self, call, driver: Driver, trail: List[str]) -> None:
        """What the person did in a watched browser, kept on the site's
        notes so the next run there can try it itself: the sign-in they
        did by hand is the sign-in the agent will know."""
        lines = [t for t in trail if "cannot be visited" not in t]
        url = driver.page.url if driver.page is not None else ""
        domain = domain_of(url) if url and not url.startswith("about:") else ""
        if not lines or not domain:
            return
        try:
            rows = await call.records.list("notes", {"domain": domain})
            notes = SiteNotes(str(rows[0]["fields"].get("notes") or "") if rows else "")
            notes.add("person", now_iso()[:10] + ": " + "; ".join(lines)[:TRAIL_NOTE_MAX])
            fields = {"domain": domain, "updated_at": now_iso(),
                      "notes": notes.text()[:NOTES_CHARS]}
            if rows:
                await call.records.update("notes", rows[0]["ref"], fields)
            else:
                await call.records.create("notes", fields)
        except Exception:
            pass

    @staticmethod
    def _headless() -> bool:
        return os.environ.get("DECENTAI_BROWSER_HEADLESS", "1") != "0"

    @staticmethod
    def _save_image(call, snapshot: Snapshot, name: str) -> str:
        """The picture, returned to the chat as a file: its path, which the runtime makes the file's ref."""
        if not snapshot.image:
            return ""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        return call.give(f"{name}-{stamp}.jpg", snapshot.image)

    async def close(self) -> None:
        """The process ends, and the chat's browser with it."""
        await BROWSER.drop()


class _Run:
    """One browse_run from start to result."""

    #: What one reading action may bring into the run's memory. How
    #: much of it the mind is shown at a step is the memory's budget.
    TAKEN_CHARS = 40000
    #: What a question may hold (the standard's limit on chat text),
    #: and each of its choices.
    CARD_CHARS = 4000
    CHOICE_CHARS = 100

    def __init__(self, call, driver: Driver, brain: Brain, goal: str, domain: str):
        self.call = call
        self.driver = driver
        self.brain = brain
        self.judge = Judge(call)
        self.goal = goal
        self.domain = domain
        self.steps = 0
        #: answers in a row that could not be read as an action
        self.unreadable = 0
        self.approvals: List[str] = []
        self.handoffs = 0
        self.downloads: List[Dict[str, Any]] = []
        self.account = ""
        #: the names the person's values were asked under on this run,
        #: with their labels — kept on the site's notes, so the next
        #: run asks for the same field under the same name
        self.login_names: Dict[str, str] = {}
        self.session_dirty = False
        self.last: Optional[Snapshot] = None
        #: the pages where the browser was already offered to the
        #: person because the run was stuck: offered once, not for ever
        self.offered: List[str] = []

    # -- what is kept between runs -----------------------------------------
    async def load_notes(self) -> SiteNotes:
        try:
            rows = await self.call.records.list("notes", {"domain": self.domain})
        except Exception:
            return SiteNotes()
        return SiteNotes(str(rows[0]["fields"].get("notes") or "") if rows else "")

    async def save_notes(self, notes: SiteNotes) -> None:
        text = notes.text()
        if not text.strip():
            return
        fields = {"domain": self.domain, "updated_at": now_iso(), "notes": text[:NOTES_CHARS]}
        try:
            rows = await self.call.records.list("notes", {"domain": self.domain})
            if rows:
                await self.call.records.update("notes", rows[0]["ref"], fields)
            else:
                await self.call.records.create("notes", fields)
        except Exception:
            pass

    async def load_session(self) -> Optional[Dict[str, Any]]:
        return await self.session_for(self.call, self.domain)

    @staticmethod
    async def session_for(call, domain: str) -> Optional[Dict[str, Any]]:
        """A site's saved sign-in, as a browser context takes it."""
        if not domain:
            return None
        try:
            rows = await call.records.list("session", {"domain": domain})
            if not rows:
                return None
            state = (await call.records.read("session", rows[0]["ref"]))["fields"].get("state")
            return state if isinstance(state, dict) and state.get("cookies") else None
        except Exception:
            return None

    async def save_session(self) -> None:
        await self.keep_session(self.call, self.driver, self.domain, self.account)

    @staticmethod
    async def keep_session(call, driver: Driver, domain: str, account: str = "") -> None:
        """The browser's sign-ins, kept under a site's name for the next
        browser that opens there. Without an account to name, the one
        already on the row stays."""
        if not domain:
            return
        try:
            state = await driver.storage_state()
            if not state.get("cookies"):
                return
            rows = await call.records.list("session", {"domain": domain})
            if rows and not account:
                account = str(rows[0]["fields"].get("account") or "")
            fields = {"domain": domain, "account": account,
                      "saved_at": now_iso(), "state": state}
            if rows:
                await call.records.update("session", rows[0]["ref"], fields)
            else:
                await call.records.create("session", fields)
        except Exception:
            pass

    # -- the loop ------------------------------------------------------------
    async def loop(self, max_steps: int) -> Dict[str, Any]:
        memory = self.brain.memory
        unchanged, last_signature, moved = 0, "", False
        while self.steps < max_steps:
            # The person may speak, or take the browser, at any step.
            for text in self.call.screen.said():
                self.brain.hear(text)
            # ...and may have handed it back already, inside the step
            # before this one: that is still a take, and what they did
            # is still told.
            took = getattr(self.call.screen, "took", lambda: False)()
            if took or self.call.screen.taken:
                await self.yield_to_person()
            snapshot = await self.driver.snapshot()
            self.last = snapshot
            # While a sign-in is on the page, what the page fetches is
            # listed and never opened.
            self.driver.inspector.guarded = any(
                e.get("tag") == "input" and str(e.get("type") or "").lower() == "password"
                for e in snapshot.elements)
            await self.show(snapshot)
            if snapshot.signature != last_signature:
                unchanged = 0
            elif moved:
                unchanged += 1
            last_signature = snapshot.signature
            if unchanged >= BrowseTool.STUCK_AFTER:
                ended = await self.stuck(snapshot)
                if ended is not None:
                    return ended
                unchanged, moved, last_signature = 0, False, ""
                continue
            circle = self.brain.circling(memory.actions[-BrowseTool.CIRCLE_STOP:]) \
                if len(memory.actions) >= BrowseTool.CIRCLE_STOP else ""
            if circle and all(
                    str(a.get("action") or "") in circle.split(" / ")
                    for a in memory.actions[-BrowseTool.CIRCLE_STOP:]):
                return {"outcome": "failed",
                        "summary": f"The run went in circles at {snapshot.url}: the last "
                                   f"{BrowseTool.CIRCLE_STOP} actions were {circle}, by turns, "
                                   f"after {self.steps} steps."}

            self.steps += 1
            await self.brain.fold(self.goal, self.steps)
            decision = await self.brain.decide(self.goal, snapshot, self.steps, max_steps, unchanged)
            action = dict(decision.get("action") or {})
            what = str(action.get("do") or "").lower()
            await self.call.progress(self._line(action, snapshot))
            self.unreadable = self.unreadable + 1 if what == "invalid" else 0
            if self.unreadable >= BrowseTool.UNREADABLE_STOP:
                how = ("were cut off before they ended" if action.get("cut")
                       else "could not be read as an action")
                return {"outcome": "failed",
                        "summary": f"The model's last {self.unreadable} answers {how}, at "
                                   f"{snapshot.url} after {self.steps} steps."}
            moved = what in BrowseTool.MOVING
            try:
                ended = await self.act(what, action, snapshot)
            except (DriverError, InspectError) as exc:
                memory.did(self._line(action, snapshot), f"failed: {exc}")
                continue
            if ended is not None:
                return ended
            if moved:
                # What the action did, shown at once — the next step's
                # frame comes only after the model has thought.
                await self.show()
        summary = f"Ran out of steps ({max_steps}) at {self.last.url if self.last else ''}."
        return {"outcome": "budget", "summary": summary}

    async def stuck(self, snapshot: Snapshot) -> Optional[Dict[str, Any]]:
        """The last actions changed nothing. The person is offered the
        browser, once for a page: what stops a script is often a thing a
        person sees at a glance. When they decline, or nobody is there
        to take it, the run ends as it always did."""
        failed = {"outcome": "failed",
                  "summary": f"The page stopped changing after {self.steps} steps at "
                             f"{snapshot.url}; the last actions did nothing."}
        if snapshot.signature in self.offered:
            return failed
        self.offered.append(snapshot.signature)
        took = await self.handoff(
            f"The last {BrowseTool.STUCK_AFTER} actions changed nothing on this page, and the "
            f"way on is not clear from here.")
        return None if took else failed

    async def act(self, what: str, action: Dict[str, Any], snapshot: Snapshot) -> Optional[Dict[str, Any]]:
        """One action; a dict ends the run."""
        driver, memory, inspector = self.driver, self.brain.memory, self.driver.inspector
        line = self._line(action, snapshot)
        result = "ok"
        n = int(action.get("n") or 0)
        element = snapshot.element(n) if n else None

        if Judge.asked_for(what, action, element):
            ended = await self.judged(what, action, element, snapshot, line)
            if ended is not None:
                return ended

        if what == "click":
            await driver.click(n)
        elif what == "type":
            await driver.type(n, str(action.get("text") or ""), bool(action.get("submit")))
        elif what == "select":
            await driver.select(n, str(action.get("option") or ""))
        elif what == "press":
            await driver.press(str(action.get("key") or "Enter"))
        elif what == "scroll":
            await driver.scroll(str(action.get("direction") or "down"))
        elif what == "hover":
            await driver.hover(n)
        elif what == "goto":
            await driver.goto(str(action.get("url") or ""))
        elif what == "back":
            await driver.back()
        elif what == "tab":
            await driver.tab(str(action.get("action") or "new"), action.get("index"), str(action.get("url") or ""))
        elif what == "read":
            text = await driver.read(n)
            result = f"read {len(text)} characters:\n" + cut(
                text, self.TAKEN_CHARS, "read of one element gives less")
        elif what == "remember":
            records = action.get("records")
            if not isinstance(records, list):
                records = [action.get("record")]
            kept = memory.keep(records)
            result = (f"kept {kept} ({len(memory.records)} records)" if kept
                      else "nothing to keep: give a record object, or records as a list")
        elif what == "recall":
            result = memory.recall(str(action.get("query") or ""), memory.share("actions"))
        elif what == "source":
            text = await inspector.source(n)
            result = f"the HTML of {'element ' + str(n) if n else 'the page'}, {len(text)} characters:\n" + cut(
                text, self.TAKEN_CHARS, "source of one element gives less")
        elif what == "network":
            lines = inspector.network(str(action.get("filter") or ""))
            result = (f"{len(lines)} fetched by this page (number, method, status, kind, address):\n"
                      + cut("\n".join(lines), self.TAKEN_CHARS, "a filter gives fewer")) if lines \
                else "nothing this page fetched matches; without a filter, all of it is listed"
        elif what == "response":
            text = await inspector.response(int(action.get("id") or 0))
            result = f"{len(text)} characters came back:\n" + cut(
                text, self.TAKEN_CHARS, "request with a narrower address gives less")
        elif what == "console":
            lines = inspector.console()
            result = cut("\n".join(lines), self.TAKEN_CHARS) if lines else "the page logged nothing"
        elif what == "request":
            got = await inspector.request(str(action.get("url") or ""))
            result = (f"{got['status']} {got['type']} from {got['address']}, {got['size']} characters:\n"
                      + cut(str(got["text"]), self.TAKEN_CHARS, "a narrower address gives less"))
        elif what == "script":
            result = await self.script(action, snapshot)
        elif what == "login":
            result = await self.login(action, snapshot)
        elif what == "code":
            # The older way to ask for a one-time code: one field, asked
            # every time.
            result = await self.login({"fields": [{"n": n, "name": "otp", "once": True,
                                                   "label": "One-time code"}]}, snapshot)
        elif what == "download":
            filename, content = await driver.download(n)
            # Returned to the chat with the result, as the chat's file.
            self.downloads.append({"filename": filename, "file_ref": self.call.give(filename, content),
                                   "size": len(content)})
            result = f"downloaded {filename} ({len(content)} bytes); it goes to the person with the result"
        elif what == "upload":
            # A file the person handed this run, by its path in the home.
            given = str(action.get("file") or "")
            if given not in self.brain.files:
                result = f"nothing was uploaded: {given or 'no file'} is not one of FILES"
            else:
                path = await self.call.file(given)
                await driver.upload(n, path.name, path.read_bytes())
                result = f"uploaded {path.name} ({path.stat().st_size} bytes)"
        elif what == "ask":
            choices = [str(c)[:self.CHOICE_CHARS] for c in (action.get("choices") or [])][:8]
            question = str(action.get("question") or "Which one?")[: self.CARD_CHARS]
            answer = await self.call.ask(question, choices=choices or None)
            result = f"the person answered: {answer}" if answer is not None else "nobody answered"
        elif what == "handoff":
            if not await self.handoff(str(action.get("reason") or "the page needs a person")):
                return {"outcome": "stopped", "summary": "Stopped at a handoff the person did not complete."}
            result = "the person handed the browser back"
        elif what == "finish":
            summary = str(action.get("summary") or "").strip()
            claimed = str(action.get("outcome") or "done")
            if claimed == "done":
                verdict = await self.brain.verify(self.goal, summary, snapshot)
                if not verdict["done"]:
                    memory.did(line, f"not finished: {verdict['summary']}")
                    return None
                summary = verdict["summary"]
            return {"outcome": "done" if claimed == "done" else "failed", "summary": summary}
        elif what == "invalid":
            if action.get("cut"):
                result = ("your answer was cut off before it ended, so nothing was done: "
                          "answer with less — fewer and shorter records in one remember, "
                          "shorter thinking — and never the same again")
            else:
                result = ("your answer could not be read as one JSON object with an action, "
                          "so nothing was done: answer with the JSON object only")
        else:
            result = f"unknown action {what!r}; use one of the actions listed"
        memory.did(line, result)
        return None

    # -- the guarded moments ---------------------------------------------------
    async def judged(self, what: str, action: Dict[str, Any], element: Optional[Dict[str, Any]],
                     snapshot: Snapshot, line: str) -> Optional[Dict[str, Any]]:
        """A press, judged before it lands. One that commits — or one
        that changes anything, when the person asked only for looking —
        stops for their say, on the platform's own card with its two
        answers. Nothing else approves a press: not a question the mind
        asked on its own, not a word in an answer."""
        n = int((element or {}).get("n") or 0)
        about = await self.driver.inspector.about(0 if what == "press" else n)
        if about is None and element is not None:
            about = {"control": {k: element.get(k) for k in
                                 ("tag", "type", "role", "text", "label", "name", "href")},
                     "title": snapshot.title}
        found = await self.judge.verdict(self.goal, what, action, about, snapshot.url)
        if not Judge.stops(found):
            return None
        control = (about or {}).get("control") or {}
        words = str(control.get("text") or control.get("label") or control.get("name")
                    or (f"element {n}" if n else "the field in use")).strip()[:120]
        if what in ("press", "type"):
            words = f"Enter in {words}"
        where = snapshot.url.split("?", 1)[0][:200]
        answer = await self.call.ask(
            f"About to press “{words}” on {where}. {found['why']} Go ahead?"[: self.CARD_CHARS],
            choices=["Go ahead", "Stop"])
        if answer != "Go ahead":
            self.brain.memory.did(line, "the person did not approve it")
            # Their own Stop is said as that, and not as a run that
            # merely ended, where a plain "stopped" reads as a result to
            # try again. Nobody answering is the other case.
            return {"outcome": "stopped_by_person" if answer == "Stop" else "stopped",
                    "summary": f"Stopped before pressing “{words}” on {where}, as the person asked."
                    if answer == "Stop" else
                    f"Stopped before pressing “{words}” on {where}: nobody approved it."}
        self.approvals.append(words)
        return None

    async def script(self, action: Dict[str, Any], snapshot: Snapshot) -> str:
        """JavaScript in the page, when the person allows it — asked
        every time, shown the reason and the code whole, with what the
        assistant made of it. A script can do whatever the person can
        do on the site, so the last word on one is theirs."""
        inspector = self.driver.inspector
        code = str(action.get("code") or "").strip()
        why = " ".join(str(action.get("why") or "").split())[:200]
        if not code:
            return "nothing was run: give the code, as the body of a function, and the why"
        if len(code) > inspector.SCRIPT_CHARS:
            return (f"nothing was run: a script of more than {inspector.SCRIPT_CHARS} characters "
                    f"is not put to the person, who must be able to read what they allow; "
                    f"source, network, response and request read without one")
        if not await self._allows(code, why, snapshot):
            return "the person did not allow the script; do not reach for the same by another way"
        self.approvals.append(f"script: {why or code[:60]}")
        self.session_dirty = True
        return "the script returned:\n" + cut(await inspector.script(code), self.TAKEN_CHARS)

    async def _allows(self, code: str, why: str, snapshot: Snapshot) -> bool:
        """The person's answer to a script, asked in the chat with the
        code whole and the reason beside it."""
        where = snapshot.url.split("?", 1)[0][:120]
        answer = await self.call.ask(
            f"Run this script on {where}?\nWhy: {why or 'no reason was given'}\n\n{code}"[: self.CARD_CHARS],
            choices=["Run it", "Stop"])
        return answer == "Run it"

    def _own_fields(self, action: Dict[str, Any], snapshot: Snapshot) -> List[Dict[str, Any]]:
        """The fields on this page that take the person's own values,
        as the model named them — whatever the form's shape: an email
        alone, a password alone on the next page, a company number
        beside both, a code. The older shapes (username and password by
        mark, a code by mark) are read as the fields they are; and with
        nothing named, the form's own password field and the field
        before it."""
        said = [dict(f) for f in (action.get("fields") or []) if isinstance(f, dict)]
        if not said:
            marks = {name: int(action.get(name) or 0) for name in ("username", "password")}
            if not any(marks.values()):
                marks = guards.login_fields(snapshot.elements)
            said = [{"n": marks[name], "name": name}
                    for name in ("username", "password") if marks.get(name)]
        fields, seen = [], set()
        for item in said:
            element = snapshot.element(int(item.get("n") or 0))
            if element is None:
                continue
            field = guards.field_of(element, item)
            if field["name"] not in seen:
                seen.add(field["name"])
                fields.append(field)
        return fields

    async def login(self, action: Dict[str, Any], snapshot: Snapshot) -> str:
        """The person's own values, put into the fields the model named.
        The site's login is asked for exactly those: what it holds it
        hands over, what it lacks the person fills in on the platform's
        form — once, or every time for a code. The values reach the page
        and nothing else: not the model, not the history."""
        fields = self._own_fields(action, snapshot)
        if not fields:
            return ("nothing was filled: name the fields on this page that take the person's "
                    "own values — login {fields: [{n, name, label}]} — by their marks")
        page = self.driver.page
        values = await self._values(domain_of(snapshot.url), fields)
        if values is None:
            return "the person did not provide it; do not try to sign in another way"
        if self.driver.page is not page or domain_of(page.url) != domain_of(snapshot.url):
            # The form may wait minutes: a popup or a redirect meanwhile must not be handed site A's password.
            return ("nothing was filled: the page changed while the person answered; "
                    "look at the page again and ask for the login there")
        self.account = str(values.get("username") or self.account)
        filled = []
        for field in fields:
            value = str(values.get(field["name"]) or "")
            if value:
                await self.driver.fill(field["n"], value)
                filled.append(field["label"])
                self.login_names[field["name"]] = field["label"]
        if not filled:
            return "the person's login holds nothing for these fields; nothing was filled"
        submit = action.get("submit")
        if submit is False:
            pass  # more of the form is the model's to fill before it goes
        elif int(submit or 0):
            await self.driver.click(int(submit))
        else:
            await self.driver.press("Enter")
        self.session_dirty = True
        return (f"filled {', '.join(filled)} from the person's own values"
                + ("" if submit is False else " and sent the form") + "; check the page")

    async def _values(self, site: str, fields: List[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        """The person's values for these fields, from the site's login
        (one per site, its answers by field name): those it keeps, and
        the rest asked as its form's questions, a code every time and
        never kept. None when the person declined."""
        asked = {f["name"]: {"type": "secret" if f["secret"] else "text", "remember": not f["once"]}
                 for f in fields}
        records = self.call.records
        try:
            found = await records.list("logins", {"site": site})
            if found:
                ref = found[0]["ref"]
                answers = (await records.read("logins", ref))["fields"].get("answers") or {}
            else:
                ref, answers = await records.create("logins", {"site": site, "answers": {}}), {}
            missing = {name: question for name, question in asked.items()
                       if not question["remember"] or "value" not in (answers.get(name) or {})}
            if missing:
                kept = {name: answer for name, answer in answers.items() if name not in missing}
                answers = (await records.update("logins", ref, {"answers": {**kept, **missing}}))[
                    "fields"].get("answers") or {}
        except ResourceDenied as exc:
            if exc.code != "refused":
                raise
            return None
        return {name: str((answers.get(name) or {}).get("value") or "") for name in asked}

    async def yield_to_person(self) -> None:
        """The person took the browser in the live view without being
        asked. The run waits: frames flow, their hand is fed to the
        page, until they hand it back — then the model is told and looks
        again at the page as they left it."""
        await self.call.progress("The person took the browser")
        _why, trail = await drive(
            self.call, self.driver,
            until=lambda: "" if self.call.screen.taken else "released",
            hear=self.brain.hear)
        self.session_dirty = True
        # What they did, in words, is the run's to learn from and the
        # site's notes to keep: the next run tries it itself.
        self._person_did(trail)
        self.brain.memory.did(
            "the person took the browser for a while",
            ("they " + "; ".join(trail[-TRAIL_SHOWN:]) + "; then handed it back — look again"
             if trail else
             "they handed it back; the page is as they left it — look again"))
        await self.call.progress("The person handed the browser back")

    async def handoff(self, reason: str) -> bool:
        """The person takes the browser over. Frames keep flowing and
        their hand is fed to the page until they press Done."""
        self.handoffs += 1
        await self.call.progress(f"Handing the browser to you: {reason}")
        before = (await self.driver.snapshot(marks=False)).signature
        asking = asyncio.ensure_future(self.call.ask(
            (f"{reason} Take over the browser in the live view, do what it needs, "
             f"then choose Done.")[: self.CARD_CHARS], choices=["Done", "Stop"]))
        try:
            _why, trail = await drive(
                self.call, self.driver,
                until=lambda: "answered" if asking.done() else "",
                hear=self.brain.hear)
        finally:
            if not asking.done():
                asking.cancel()
        answer = asking.result() if asking.done() and not asking.cancelled() else None
        if answer == "Done":
            self.session_dirty = True
            snapshot = await self.driver.snapshot(marks=False)
            if snapshot.signature == before:
                # Nothing visibly changed in this browser: the person may
                # have done what was needed elsewhere (their phone, another
                # window). Reload so the site's new state shows.
                try:
                    await self.driver.page.reload(wait_until="domcontentloaded", timeout=15000)
                    await self.driver.settle()
                except Exception:
                    pass
            self._person_did(trail)
            self.brain.memory.did(
                "handoff",
                ("the person took over and " + "; ".join(trail[-TRAIL_SHOWN:]) + "; then handed back"
                 if trail else "the person took over and handed back"))
            return True
        return False

    def _person_did(self, trail: List[str]) -> None:
        """What the person did themselves, on the site's notes, so the
        next run tries it itself."""
        lines = [t for t in trail if "cannot be visited" not in t]
        if lines:
            self.brain.notes.add("person", now_iso()[:10] + ": " + "; ".join(lines)[:TRAIL_NOTE_MAX])

    # -- the live view -------------------------------------------------------------
    async def show(self, snapshot: Optional[Snapshot] = None) -> None:
        """One frame to the live view, the size the browser's viewport
        is — the snapshot's when one is at hand — with the open tabs."""
        image = await self.driver.frame()
        if not image:
            return
        if snapshot is not None:
            width, height = snapshot.width, snapshot.height
        else:
            size = self.driver.page.viewport_size if self.driver.page else None
            width, height = (size["width"], size["height"]) if size else VIEWPORT
        await show_frame(self.call, self.driver, image, width, height)

    # -- the end -----------------------------------------------------------------------
    async def close(self, outcome: Dict[str, Any]) -> Dict[str, Any]:
        memory = self.brain.memory
        result: Dict[str, Any] = {
            "outcome": str(outcome.get("outcome") or "failed"),
            "summary": str(outcome.get("summary") or ""),
            "steps": self.steps, "final_url": "", "records": list(memory.records),
            "approvals": list(self.approvals), "handoffs": self.handoffs,
            "downloads": list(self.downloads),
        }
        try:
            if self.driver.page is not None:
                final = await self.driver.snapshot(marks=False)
                result["final_url"] = final.url
                result["file_ref"] = BrowseTool._save_image(self.call, final, "final")
                if self.session_dirty:
                    await self.save_session()
                if self.steps:
                    notes = await self.brain.write_notes(self.domain)
                    notes.names.update(self.login_names)
                    await self.save_notes(notes)
        except Exception:
            pass
        try:
            await self.call.screen.close()
        except Exception:
            pass
        if len(result["records"]) > 1:
            columns = sorted({k for r in result["records"] for k in r if isinstance(r, dict)})[:8]
            try:
                await self.call.say(table={"title": "What was found", "columns": columns,
                                           "rows": result["records"][:500]})
            except Exception:
                pass
        return result

    @staticmethod
    def _line(action: Dict[str, Any], snapshot: Snapshot) -> str:
        what = str(action.get("do") or "?")
        if what == "invalid":
            return ("the model's answer was cut off before it ended" if action.get("cut")
                    else "the model's answer could not be read")
        n = action.get("n")
        element = snapshot.element(int(n)) if n else None
        words = (element or {}).get("text") or (element or {}).get("label") or ""
        parts = [what]
        if n:
            parts.append(f"[{n}] {words!r}" if words else f"[{n}]")
        for key in ("text", "url", "option", "key", "direction", "question", "reason", "outcome",
                    "query", "filter", "id", "why"):
            if action.get(key):
                parts.append(f"{key}={str(action[key])[:60]!r}")
        return " ".join(parts)
