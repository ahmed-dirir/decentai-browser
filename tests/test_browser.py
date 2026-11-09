"""The Browser agent, run the way production runs it: its own program,
a real Chromium against a loopback shop, the chat's model scripted, the
person a scripted asker who fills in the platform's form.

What is proven: the login is asked for once and the session reused;
the marks are what the model clicks by; an order stops for approval; a
page that wants a human hands the browser over; what was found comes
back as records; the site's notes are written for next time."""

import asyncio
import json
import os
import re
from pathlib import Path

import pytest


from tests.harness import Chat, ChatSinks, Installed, Records, probe, run
from tests.browser_site import ACCOUNT, PASSWORD, ShopSite


@pytest.fixture
def shop():
    os.environ["DECENTAI_WEB_ALLOW_LOOPBACK"] = "1"
    # The program has a home of its own: Playwright is told where this
    # machine keeps its browsers.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))
    site = ShopSite().start()
    yield site
    site.stop()


class Shopper:
    """A scripted mind: reads the page the way the model would (the
    element lines) and answers with the action a careful shopper takes
    on this site. It never sees a password."""

    def __init__(self):
        self.prompts = []
        self.pictures = 0
        self.asked_order = False
        self.items_steps = 0
        self.accounts = 0
        self.judged = []
        #: what the judge says of the person's request, for a test
        self.reading_only = False

    def judge(self, text):
        """The judge, scripted: by the control it is shown, as a model
        would read it."""
        control = json.loads(re.search(r"^THE CONTROL: (.*)$", text, re.M).group(1))
        words = str(control.get("text") or control.get("label") or "")
        verdict = ("commits" if "Place order" in words
                   else "changes" if words.startswith("Add ") else "looks")
        self.judged.append((words, verdict))
        return json.dumps({"verdict": verdict, "why": f"It would do what “{words}” says.",
                           "reading_only": self.reading_only})

    async def __call__(self, messages, max_tokens=None, pictures=None):
        text = messages[-1]["text"]
        self.prompts.append(text)
        if pictures:
            self.pictures += len(pictures)
        if text.startswith("The person asked:"):
            return '["Sign in", "Open the shop", "Add the dumbbell under 500", "Check the basket", "Confirm"]'
        if text.startswith("The goal was:"):
            return '{"done": true, "summary": "The basket shows the 20 kg dumbbell and the order is placed."}'
        if text.startswith("You just finished a run"):
            return "Sign in at /login; the shop lists dumbbells with Add buttons; Place order is on /basket."
        if text.startswith("THE PERSON ASKED:"):
            return self.judge(text)
        if text.startswith("You are partway through a task"):
            self.accounts += 1
            return f"DONE — account {self.accounts}. LEARNED — the shop. LEFT — the rest."
        page = re.search(r"PAGE: .* — (\S+)", text)
        url = page.group(1) if page else ""
        marks = {m.group(2): int(m.group(1))
                 for m in re.finditer(r"^\[(\d+)\] [^:]+: '([^']*)'", text, re.M)}

        def mark(word):
            return next((n for label, n in marks.items() if word.lower() in label.lower()), None)

        if "Verify you are human" in text.split("PAGE:")[-1]:
            return ('{"thinking": "a check only a person passes", "action": {"do": "handoff", '
                    '"reason": "The page asks to verify that you are human: press the button."}}')
        if mark("Use your password instead"):
            return (f'{{"thinking": "no passkey here; the page offers the password", "action": '
                    f'{{"do": "click", "n": {mark("Use your password instead")}, "effect": "looks"}}}}')
        if url.endswith("/login") or "input password" in text:
            return ('{"thinking": "a login form", "action": {"do": "login", '
                    f'"username": {mark("Email") or 1}, "password": {marks.get("", 2) if False else mark("Password") or 2}, '
                    f'"submit": {mark("Sign in") or 3}}}}}')
        if "/items" in url:
            # Three steps that leave the page as it is — a page of records
            # in one call, a read, one more record — before the click.
            # The page has not changed for three snapshots, and that is
            # not being stuck.
            self.items_steps += 1
            if self.items_steps == 1:
                return ('{"thinking": "the 20 kg is under 500", "action": {"do": "remember", "records": ['
                        '{"kind": "item", "name": "Adjustable dumbbell 20 kg", "price": 380}, '
                        '{"kind": "item", "name": "Adjustable dumbbell 30 kg", "price": 520}]}}')
            if self.items_steps == 2:
                return '{"thinking": "the whole page", "action": {"do": "read", "n": 0}}'
            if self.items_steps == 3:
                return ('{"thinking": "one more", "action": {"do": "remember", '
                        '"record": {"kind": "item", "name": "Flat dumbbell 15 kg", "price": 140}}}')
            # The page's own words, below the history: a read's result
            # quotes the page as it was, and must not pass for it.
            if "Basket: 0" in text.split("PAGE:")[-1]:
                return f'{{"thinking": "add it", "action": {{"do": "click", "n": {mark("Add Adjustable dumbbell 20")}}}}}'
            return f'{{"thinking": "to the basket", "action": {{"do": "click", "n": {mark("Go to basket")}}}}}'
        if "/basket" in url:
            if not self.asked_order:
                # A cautious model asks on its own, against the rules;
                # the platform's guard must not ask a second time.
                self.asked_order = True
                return ('{"thinking": "check first", "action": {"do": "ask", "question": '
                        '"The basket holds the 20 kg dumbbell at 380. May I click Place order?", '
                        '"choices": ["Approve placing the order", "Do not place the order"]}}')
            return f'{{"thinking": "order it", "action": {{"do": "click", "n": {mark("Place order")}}}}}'
        if "/ordered" in url:
            return '{"thinking": "done", "action": {"do": "finish", "outcome": "done", "summary": "Ordered the 20 kg dumbbell."}}'
        if "Sidra Fitness" in text and "Shop" in marks:
            return f'{{"thinking": "open the shop", "action": {{"do": "click", "n": {marks["Shop"]}}}}}'
        return '{"thinking": "lost", "action": {"do": "finish", "outcome": "failed", "summary": "Could not find the shop."}}'


class Person:
    """The scripted person: gives the login once on the platform's form,
    approves the order, and presses the human check when handed the
    browser."""

    def __init__(self, shop):
        self.shop = shop
        self.asked = []
        self.logins_given = 0

    async def form(self, resource, asked):
        self.logins_given += 1
        questions = asked["given"]["answers"]
        assert resource == "logins" and list(questions) == ["username", "password"], asked
        values = {"username": ACCOUNT, "password": PASSWORD}
        return {"answers": {name: {**question, "value": values[name]} for name, question in questions.items()}}

    async def ask(self, question, choices, expects):
        self.asked.append(question)
        if choices and "Approve placing the order" in choices:
            return "Approve placing the order"
        if "Take over" in question:
            # The person does the human check themselves — here, by the
            # site's own door, which is what a click in the live view does.
            import urllib.request
            # The person's own browser, which no proxy stands in front of.
            person = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            person.open(f"{self.shop.url}/human", data=b"").read()
            return "Done"
        return "Go ahead"


async def _progress(text):
    """The agent's own lines, printed so a slow run can be followed
    with -s."""
    print(f"  [browser] {text}", flush=True)


def make(shop, provider=None):
    provider = provider or Records()
    mind = Shopper()
    person = Person(shop)
    executor = Chat(provider, ChatSinks(llm=mind, ask=person.ask, form=person.form, progress=_progress))
    return executor, provider, mind, person


def browse(agents, executor, goal, url, **inputs):
    return run(executor.invoke(agents["browser"], "browse_run",
                               {"goal": goal, "start_url": url, **inputs}, chat_level=2))


def another_chat(agents):
    """The agent as another chat has it: a process of its own."""
    return Installed(agents["browser"].folder)


def showing(executor, seen):
    """A progress sink that keeps the lines, and the id of the call that
    shows the browser: the newest in flight when it says so."""
    async def progress(text):
        await _progress(text)
        seen.setdefault("progress", []).append(text)
        if text == "Showing the browser":
            seen["call_id"] = list(executor.running)[-1]
    return progress


class TestPasskey:
    def test_a_passkey_request_is_refused_at_once(self, shop):
        """The person signed in by hand in the live view and Microsoft
        sent them to a passkey page that waited for ever: a passkey
        request in a browser with no authenticator never answers, and
        no run was there to take the way back. The browser refuses the
        request the way a person pressing Cancel does, and the page
        offers its other way in — whoever drives."""
        pytest.importorskip("playwright")
        from agents.browser.tools.driver import Driver

        async def visit():
            driver = Driver()
            try:
                await driver.start()
                # By name: a passkey is never asked of a bare address.
                await driver.goto(shop.url.replace("127.0.0.1", "localhost") + "/passkey")
                await driver.page.wait_for_selector("text=Use your password instead", timeout=3000)
                available = await driver.page.evaluate(
                    "PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable()")
                return await driver.read(), available
            finally:
                await driver.stop()

        words, available = run(visit())
        assert "NotAllowedError" in words
        assert available is False


class SignerIn(Shopper):
    """A scripted mind that signs in the way the prompt asks: on every
    page that shows fields for the person's own values, it names them
    by their marks — and says what a field is only where the form
    itself does not."""

    def __init__(self, named=None):
        super().__init__()
        self.named = named or {}

    async def __call__(self, messages, max_tokens=None, pictures=None):
        text = messages[-1]["text"]
        page = text.split("ELEMENTS (number, kind, text):")[-1]
        inputs = re.findall(r"^\[(\d+)\] input (\w+)[^:]*: '([^']*)'", page, re.M)
        if inputs and text.startswith("GOAL:"):
            self.prompts.append(text)
            fields = []
            for n, _kind, words in inputs:
                said = {"n": int(n)}
                said.update(next((v for k, v in self.named.items() if k in words), {}))
                fields.append(said)
            return json.dumps({"thinking": "the person's own values",
                               "action": {"do": "login", "fields": fields}})
        return await super().__call__(messages, max_tokens=max_tokens, pictures=pictures)


class Vault:
    """The person's saved values for the shop, given on the platform's
    form field by field as they are asked for, and a record of each
    asking: the questions the site's login has no answer to."""

    def __init__(self, **held):
        self.held = {"username": ACCOUNT, "password": PASSWORD, **held}
        self.asked = []

    async def form(self, resource, asked):
        questions = asked["given"]["answers"]
        self.asked.append([(name, q["type"]) for name, q in questions.items() if "value" not in q])
        return {"answers": {name: {**q, **({"value": self.held[name]} if name in self.held else {})}
                            for name, q in questions.items()}}

    async def ask(self, question, choices, expects):
        return "Approve placing the order" if choices and "Approve placing the order" in choices \
            else "Go ahead"


class TestSigningIn:
    def signer(self, mind, vault):
        provider = Records()
        executor = Chat(provider, ChatSinks(llm=mind, ask=vault.ask, form=vault.form, progress=_progress))
        return executor, provider

    def test_a_field_is_named_by_what_the_form_says_it_is(self):
        """Not by its words, which are in any language: a password
        input is the password, a field the form marks as the user name
        is the username, a code is asked every time; anything else is
        kept under the form's own name for it, unless the model said
        what it is."""
        from agents.browser.tools import guards

        def field(element, **said):
            return guards.field_of({"n": 1, "tag": "input", **element}, said)

        assert field({"type": "password", "label": "كلمة المرور"}) == {
            "n": 1, "name": "password", "label": "كلمة المرور", "secret": True, "once": False}
        assert field({"type": "email", "name": "f1"})["name"] == "username"
        assert field({"type": "text", "autocomplete": "section-a username"})["name"] == "username"
        code = field({"type": "text", "autocomplete": "one-time-code", "name": "c"})
        assert (code["name"], code["secret"], code["once"]) == ("otp", True, True)
        assert field({"type": "text", "name": "Org-ID"})["name"] == "org_id"
        assert field({"type": "text", "name": "org"}, name="Company ID", label="Company number") == {
            "n": 1, "name": "company_id", "label": "Company number", "secret": False, "once": False}
        assert field({"type": "text", "name": "7x"})["name"] == "f_7x"
        assert field({"type": "text"})["name"] == "field_1"

    def test_a_sign_in_over_two_pages_in_another_language(self, agents, shop):
        """Atlassian's sign-in asks for the email on one page and the
        password on the next. The login step wanted both on one page,
        did nothing three times, and the run ended at the first page.
        Each page is asked for what it shows — here in Arabic, with
        nothing a list of English words would know."""
        shop.two_step = True
        mind, vault = SignerIn(), Vault()
        executor, provider = self.signer(mind, vault)
        result, status = browse(agents, executor, "Put the dumbbell under 500 in my basket and order it", shop.url)
        assert status == "success" and result["outcome"] == "done", result
        assert vault.asked == [[("username", "text")], [("password", "secret")]]
        assert shop.logins == [ACCOUNT] and shop.orders == [["d20"]]
        # What was filled is nobody's but the page's.
        assert not any(PASSWORD in p or ACCOUNT in p for p in mind.prompts)
        [notes] = [row["keys"]["notes"] for row in provider.data["notes"].values()]
        assert "NAMES OF THE SIGN-IN FIELDS" in notes and "username = " in notes and "password = " in notes

    def test_a_form_that_asks_for_more_than_a_login(self, agents, shop):
        """A company number beside the email and the password: three
        fields on one page, one of them named by the model because the
        form's own name for it says little."""
        from tests.browser_site import COMPANY

        shop.company_field = True
        mind = SignerIn(named={"Company": {"name": "company_id", "label": "Company number"}})
        vault = Vault(company_id=COMPANY)
        executor, provider = self.signer(mind, vault)
        result, status = browse(agents, executor, "Put the dumbbell under 500 in my basket and order it", shop.url)
        assert status == "success" and result["outcome"] == "done", result
        assert vault.asked == [[("company_id", "text"), ("username", "text"), ("password", "secret")]]
        assert shop.logins == [ACCOUNT] and shop.orders == [["d20"]]
        assert not any(COMPANY in p for p in mind.prompts)


class TestAPageOnItsWay:
    def test_a_page_being_replaced_is_waited_for_and_read_again(self):
        """A sign-in form posts and the site redirects to the next page.
        The click's wait can end on the old page before the redirect
        starts, so the snapshot meets a page being replaced, which
        cannot be read. That happened in CI: the model was shown no
        fields on the password page and gave up. The driver waits for
        the new page and reads it again."""
        from agents.browser.tools.driver import Driver

        class Replacing:
            url = "http://shop.test/login/password"

            def __init__(self, fails):
                self.fails, self.reads = fails, 0

            async def evaluate(self, script, *args):
                if "dai-marks" in script and not args:
                    return None  # taking the marks down
                self.reads += 1
                if self.reads <= self.fails:
                    raise RuntimeError("Execution context was destroyed, "
                                       "most likely because of a navigation")
                return {"elements": [{"n": 1, "tag": "input", "type": "password"}],
                        "text": "Password", "frames": [], "scroll": [0, 0, 800]}

            async def screenshot(self, **_):
                return b"jpeg"

            async def title(self):
                return "Sign in"

            async def wait_for_load_state(self, *_args, **_kw):
                return None

        driver = Driver()
        driver.page = Replacing(fails=1)
        shot = asyncio.run(driver.snapshot())
        assert driver.page.reads == 2
        assert [e["type"] for e in shot.elements] == ["password"]

        # A page that never becomes readable is said to be unreadable,
        # after a bounded number of tries — not waited on for ever.
        driver.page = Replacing(fails=99)
        shot = asyncio.run(driver.snapshot())
        assert shot.elements == [] and "could not be read" in shot.text
        assert driver.page.reads == 3


class TestAnUnreadableAnswer:
    def test_a_cut_reply_is_an_invalid_action_that_says_so(self):
        """A reply the model's token cap cut short becomes an invalid
        action marked cut, and the run's line and feedback say it in
        words."""
        from agents.browser.tools.brain import Brain
        from agents.browser.tools.browse_tool import _Run

        class Cut(str):
            cut = True

        class Mind:
            async def model(self, prompt, system=None, max_tokens=None, pictures=None):
                assert max_tokens is None
                return Cut('{"thinking": "record all three jobs", "action": {"do": "remem')

        class Call:
            model = Mind().model

        class Page:
            url, title, text, image, elements, tabs, scroll = "https://x.example/", "", "", b"", [], [], (0, 0, 0)

        decision = run(Brain(Call()).decide("goal", Page(), 1, 10, 0))
        assert decision["action"]["do"] == "invalid" and decision["action"]["cut"] is True
        assert _Run._line(decision["action"], Page()) == "the model's answer was cut off before it ended"
        assert _Run._line({"do": "invalid"}, Page()) == "the model's answer could not be read"

    def test_three_unreadable_answers_end_the_run(self, agents, shop):
        """A model that answers prose three times in a row ends the run
        as failed, with the reason, instead of spending the budget."""
        provider = Records()

        async def mumbling(messages, max_tokens=None, pictures=None):
            return "I would click the shop link, I think."

        person = Person(shop)
        executor = Chat(provider, ChatSinks(llm=mumbling, ask=person.ask, form=person.form, progress=_progress))
        result, status = browse(agents, executor, "Buy a dumbbell", shop.url, max_steps=30)
        assert status == "success", result
        assert result["outcome"] == "failed"
        assert "could not be read" in result["summary"]
        assert result["steps"] == 3


class TestCircling:
    def test_alternating_and_repeated_actions_are_seen_as_circles(self):
        from agents.browser.tools.brain import Brain

        down, up = {"action": "scroll direction='down'"}, {"action": "scroll direction='up'"}
        assert Brain.circling([down, up] * 3) == "scroll direction='down' / scroll direction='up'"
        assert Brain.circling([down] * 6) == "scroll direction='down'"
        assert Brain.circling([down, up] * 2) == ""
        assert Brain.circling([down, up, down, {"action": "read"}, down, up]) == ""


class TestThePackage:
    def test_the_program_says_ready_with_every_declared_function(self, agents):
        agent = agents["browser"]
        assert probe(agent) == []

    def test_a_screenshot_is_a_file(self, agents, shop):
        executor, provider, _, _ = make(shop)
        result, status = run(executor.invoke(agents["browser"], "browse_screenshot",
                                             {"url": shop.url}, chat_level=2))
        assert status == "success", result
        assert result["title"] == "Sidra Fitness" and result["file_ref"]
        stored = executor.files[result["file_ref"]]
        assert stored["name"].startswith("screenshot-") and stored["content"][:2] == b"\xff\xd8"


class TestShopping:
    def test_a_login_the_marks_an_approval_and_a_record(self, agents, shop):
        executor, provider, mind, person = make(shop)
        result, status = browse(agents, executor, "Put the dumbbell under 500 in my basket and order it", shop.url)
        assert status == "success", result
        assert result["outcome"] == "done", result
        # The person typed nothing; the platform's form gave the login once.
        assert person.logins_given == 1 and shop.logins == [ACCOUNT]
        # The order stopped for approval on the agent's own question,
        # which says what the press does. The mind had asked on its own
        # first, against the rules: its question approves nothing,
        # whatever the answer's words.
        asked = [q for q in person.asked if "Place order" in q]
        assert asked[0] == "The basket holds the 20 kg dumbbell at 380. May I click Place order?"
        assert len(asked) == 2 and asked[1].startswith("About to press “Place order” on ")
        assert "It would do what “Place order” says." in asked[1]
        assert result["approvals"] == ["Place order"]
        # Judged: what was pressed, not what was followed. A plain link
        # is a read by the protocol's rule.
        assert ("Place order", "commits") in mind.judged
        assert not any(words == "Shop" for words, _ in mind.judged)
        assert shop.orders == [["d20"]]
        # What was found came back as a record, and the model saw pictures.
        assert [r["name"] for r in result["records"]] == [
            "Adjustable dumbbell 20 kg", "Adjustable dumbbell 30 kg", "Flat dumbbell 15 kg"]
        assert mind.pictures >= result["steps"]
        assert result["final_url"].endswith("/ordered") and result["file_ref"] in executor.files
        # ...and as a table in the chat.
        [table] = [said["table"] for said in executor.said if "table" in said]
        assert table["title"] == "What was found" and len(table["rows"]) == 3
        # The session and the site's notes are kept for next time.
        [session] = provider.data["session"].values()
        assert session["keys"]["account"] == ACCOUNT and session["values"]["state"]["cookies"]
        [notes] = provider.data["notes"].values()
        assert "Place order" in notes["keys"]["notes"]

    def test_the_next_run_of_a_chat_continues_in_the_open_browser(self, agents, shop):
        """The browser stays open after a run; a follow-up in the same
        chat, without a start_url, finds the page the last run left —
        no new browser, no new login. Another chat, a process of its
        own, never sees it, and without a start_url has nowhere to
        begin. The browser lives in the chat's process, so this is
        judged by what the runs report, not by looking inside."""
        executor, provider, mind, person = make(shop)
        other = Chat(provider, ChatSinks(llm=mind, ask=person.ask, form=person.form, progress=_progress))

        async def scenario():
            first = await executor.invoke(agents["browser"], "browse_run", {
                "goal": "Put the dumbbell under 500 in my basket and order it",
                "start_url": shop.url}, chat_level=2)
            again = await executor.invoke(agents["browser"], "browse_run", {
                "goal": "Confirm the order went through"}, chat_level=2)
            elsewhere = await other.invoke(another_chat(agents), "browse_run", {
                "goal": "Confirm the order went through"}, chat_level=2)
            return first, again, elsewhere

        (first, s1), (again, s2), (elsewhere, s3) = run(scenario())
        assert s1 == "success" and first["outcome"] == "done", first
        assert first["final_url"].endswith("/ordered")
        assert s2 == "success" and again["outcome"] == "done", again
        assert again["final_url"].endswith("/ordered")
        assert person.logins_given == 1 and shop.logins == [ACCOUNT]
        assert any("You continue in the browser" in p for p in mind.prompts)
        assert s3 == "success" and elsewhere["outcome"] == "failed", elsewhere
        assert "start_url" in elsewhere["summary"]

    def test_a_watch_shows_the_browser_until_closed_or_a_run_takes_it(self, agents, shop):
        """The browser on request: a watch streams the chat's browser,
        ends when the person closes the panel, and yields when a run of
        the same chat takes the browser — which then continues from the
        page the person left."""
        executor, provider, mind, person = make(shop)
        seen = {}
        executor.sinks.progress = showing(executor, seen)

        async def scenario():
            watching = asyncio.ensure_future(executor.invoke(
                agents["browser"], "browse_watch", {}, chat_level=2))
            while "call_id" not in seen:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.5)
            # The person types the shop's address, then closes the panel.
            assert await executor.screen_input(seen["call_id"], [
                {"type": "navigate", "url": shop.url},
                {"type": "control", "action": "take"},
                {"type": "control", "action": "release"}])
            await asyncio.sleep(1.0)
            assert await executor.screen_input(seen["call_id"], [
                {"type": "control", "action": "close"}])
            closed = await asyncio.wait_for(watching, 20)
            # What the person did in the watched browser is on the
            # site's notes, before any run rewrites them.
            noted = [row["keys"]["notes"] for row in provider.data.get("notes", {}).values()]
            seen.pop("call_id")
            # A second watch stays up while a run takes the browser; the
            # person keeps watching, and closes it when the run is done.
            watching = asyncio.ensure_future(executor.invoke(
                agents["browser"], "browse_watch", {}, chat_level=2))
            while "call_id" not in seen:
                await asyncio.sleep(0.05)
            ran = await executor.invoke(agents["browser"], "browse_run", {
                "goal": "Put the dumbbell under 500 in my basket and order it",
                "start_url": shop.url}, chat_level=2)
            assert not watching.done(), "the watch outlives the run"
            assert await executor.screen_input(seen["call_id"], [
                {"type": "control", "action": "close"}])
            after = await asyncio.wait_for(watching, 20)
            return closed, after, ran, noted

        (closed, s1), (after, s2), (ran, s3), noted = run(scenario())
        assert s1 == "success" and closed["outcome"] == "closed", closed
        assert closed["final_url"].startswith(shop.url), "the typed address was opened"
        assert any("WHAT THE PERSON DID THEMSELVES" in n and "went to " + shop.url in n
                   for n in noted), noted
        assert s3 == "success" and ran["outcome"] == "done", ran
        assert s2 == "success" and after["outcome"] == "closed", after
        assert after["final_url"].endswith("/ordered"), "the watch showed the run's browser to the end"

    def test_a_sign_in_by_hand_outlasts_the_browser(self, agents, shop):
        """The person signed in to Jira by hand in the live view, and
        the browser closed afterwards: the next run opened a fresh
        browser at the login page. What they signed in to is kept when
        the view closes, so a browser that goes later takes nothing."""
        executor, provider, mind, person = make(shop)
        seen = {}
        executor.sinks.progress = showing(executor, seen)

        def typed(text):
            return [{"type": "key", "action": "down", "key": ch, "text": ch} for ch in text]

        def pressed(key):
            return [{"type": "key", "action": "down", "key": key, "text": ""},
                    {"type": "key", "action": "up", "key": key, "text": ""}]

        async def scenario():
            watching = asyncio.ensure_future(executor.invoke(
                agents["browser"], "browse_watch", {}, chat_level=2))
            while "call_id" not in seen:
                await asyncio.sleep(0.05)
            call_id = seen["call_id"]
            await executor.screen_input(call_id, [{"type": "navigate", "url": shop.url + "/login"},
                                                  {"type": "control", "action": "take"}])
            await asyncio.sleep(1.0)
            await executor.screen_input(call_id, pressed("Tab") + typed(ACCOUNT)
                                        + pressed("Tab") + typed(PASSWORD) + pressed("Enter"))
            for _ in range(200):
                if shop.logins:
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(1.5)
            await executor.screen_input(call_id, [{"type": "control", "action": "release"},
                                                  {"type": "control", "action": "close"}])
            closed = await asyncio.wait_for(watching, 20)
            # The browser goes — closed on purpose here, or with its
            # process elsewhere — and a run opens a fresh one.
            await executor.invoke(agents["browser"], "browse_watch", {"action": "quit"}, chat_level=2)
            ran = await executor.invoke(agents["browser"], "browse_run", {
                "goal": "Put the dumbbell under 500 in my basket and order it",
                "start_url": shop.url}, chat_level=2)
            return closed, ran

        (closed, s1), (ran, s2) = run(scenario())
        assert s1 == "success" and closed["outcome"] == "closed", closed
        assert closed["final_url"].endswith("/items"), closed
        kept = [row["keys"]["domain"] for row in provider.data.get("session", {}).values()]
        assert kept, "the sign-in by hand was not kept"
        assert s2 == "success" and ran["outcome"] == "done", ran
        # One sign-in, the person's own; the run asked for none.
        assert shop.logins == [ACCOUNT] and person.logins_given == 0

    def test_a_quit_browser_is_resumed_where_it_was(self, agents, shop):
        """Closing the browser itself remembers where it was; the next
        open in that chat starts there, with the site's sign-in."""
        executor, provider, mind, person = make(shop)
        seen = {}
        executor.sinks.progress = showing(executor, seen)

        async def scenario():
            watching = asyncio.ensure_future(executor.invoke(
                agents["browser"], "browse_watch", {}, chat_level=2))
            while "call_id" not in seen:
                await asyncio.sleep(0.05)
            await executor.screen_input(seen["call_id"], [{"type": "navigate", "url": shop.url + "/login"}])
            await asyncio.sleep(1.0)
            await executor.screen_input(seen["call_id"], [{"type": "control", "action": "close"}])
            await asyncio.wait_for(watching, 20)
            quit_ = await executor.invoke(agents["browser"], "browse_watch", {"action": "quit"}, chat_level=2)
            seen.pop("call_id")
            watching = asyncio.ensure_future(executor.invoke(
                agents["browser"], "browse_watch", {}, chat_level=2))
            while "call_id" not in seen:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.5)
            await executor.screen_input(seen["call_id"], [{"type": "control", "action": "close"}])
            resumed = await asyncio.wait_for(watching, 20)
            return quit_, resumed

        (quit_, s1), (resumed, s2) = run(scenario())
        assert s1 == "success" and quit_["outcome"] == "quit" and quit_["final_url"].endswith("/login"), quit_
        assert s2 == "success" and resumed["final_url"].endswith("/login"), resumed
        assert any(p.startswith("Resuming at ") for p in seen["progress"])

    def test_the_next_chat_reuses_the_session_and_the_notes(self, agents, shop):
        """Another chat opens a browser of its own, signed in by the
        session the first one kept, and reads the site's notes."""
        executor, provider, mind, person = make(shop)
        browse(agents, executor, "Order the dumbbell under 500", shop.url)
        assert person.logins_given == 1
        shop.orders.clear()

        again, provider2, mind2, person2 = make(shop, provider)
        result, status = run(again.invoke(another_chat(agents), "browse_run", {
            "goal": "Order the dumbbell under 500", "start_url": shop.url}, chat_level=2))
        assert status == "success" and result["outcome"] == "done", result
        # No login asked the second time: the cookie did it.
        assert person2.logins_given == 0 and shop.logins == [ACCOUNT]
        assert any("SITE NOTES" in p for p in mind2.prompts)
        assert shop.orders == [["d20"]]

    def test_a_page_that_wants_a_human_is_handed_over(self, agents, shop):
        shop.captcha_on_items = True
        executor, provider, mind, person = make(shop)
        result, status = browse(agents, executor, "Order the dumbbell under 500", shop.url)
        assert status == "success", result
        assert result["outcome"] == "done", result
        assert result["handoffs"] == 1 and shop.human_checks == 1
        handoff = next(q for q in person.asked if "Take over" in q)
        assert "human" in handoff.lower()

    def test_an_invisible_captcha_frame_is_not_a_human_check(self, agents, shop):
        """Real sign-in pages carry a silent risk-check frame. On
        Atlassian the guard read it as a human check and handed the
        person a page with nothing to solve, twice. Only a frame the
        person can see is one."""
        shop.invisible_captcha_on_login = True
        executor, provider, mind, person = make(shop)
        result, status = browse(agents, executor, "Put the dumbbell under 500 in my basket and order it", shop.url)
        assert status == "success" and result["outcome"] == "done", result
        assert result["handoffs"] == 0 and not any("Take over" in q for q in person.asked)
        assert shop.logins == [ACCOUNT]

    def test_a_passkey_prompt_is_passed_by_the_pages_own_way(self, agents, shop):
        """Microsoft's sign-in sent the run to a passkey page, and the
        agent handed the browser to the person, who could do nothing
        there: the authenticator is on their device, not on the host.
        The browser refuses the passkey, the page offers its other way
        in, and the mind takes it — no list of words or addresses
        tells a passkey page from another."""
        shop.passkey_first = True
        executor, provider, mind, person = make(shop)
        result, status = browse(agents, executor, "Put the dumbbell under 500 in my basket and order it", shop.url)
        assert status == "success" and result["outcome"] == "done", result
        assert result["handoffs"] == 0 and not any("Take over" in q for q in person.asked)
        assert shop.logins == [ACCOUNT]
        assert any("click" in a and "Use your password instead" in a
                   for p in mind.prompts for a in re.findall(r"^- (.*)$", p, re.M))

    def test_the_person_steers_in_words_and_takes_the_browser_mid_run(self, agents, shop):
        """Words sent while the run works reach the model at its next
        step; taking the browser in the live view pauses the run until
        it is handed back, and the model is told to look again."""
        executor, provider, mind, person = make(shop)
        seen = {"call_id": "", "steered": False, "paused": None}

        async def progress(text):
            await _progress(text)
            if seen["paused"] is None:
                seen["paused"] = asyncio.Event()
            if text == "The person took the browser":
                seen["paused"].set()
            if text == "login" and not seen["steered"]:
                seen["steered"] = True
                seen["call_id"] = call_id = next(iter(executor.running))
                assert await executor.screen_input(call_id, [
                    {"type": "say", "text": "Only the 20 kg one, please."},
                    {"type": "control", "action": "take"},
                ])

                async def hand_back():
                    # Only once the run has paused for the person: a
                    # fixed wait raced the login act, which is as long
                    # as the page takes, and the hand-back landed on
                    # either side of the pause.
                    await asyncio.wait_for(seen["paused"].wait(), timeout=30)
                    # While they hold it, the person goes to the items
                    # page themselves; the run is told in words.
                    await executor.screen_input(call_id, [
                        {"type": "navigate", "url": shop.url + "/items"},
                        {"type": "mouse", "action": "move", "x": 5, "y": 5},
                        {"type": "control", "action": "release"},
                    ])
                asyncio.get_running_loop().create_task(hand_back())

        executor.sinks.progress = progress
        result, status = browse(agents, executor, "Put the dumbbell under 500 in my basket and order it", shop.url)
        assert status == "success" and result["outcome"] == "done", result
        assert seen["steered"]
        assert any("THE PERSON SAID" in p and "Only the 20 kg one" in p for p in mind.prompts)
        assert any("took the browser for a while" in p and "went to " + shop.url + "/items" in p
                   for p in mind.prompts), "what the person did reached the model in words"

    def test_a_refused_approval_stops_the_run(self, agents, shop):
        executor, provider, mind, person = make(shop)

        async def refuse(question, choices, expects):
            person.asked.append(question)
            return "Stop"
        executor.sinks.ask = refuse
        result, status = browse(agents, executor, "Order the dumbbell under 500", shop.url)
        assert status == "success" and result["outcome"] == "stopped_by_person", result
        assert "Place order" in result["summary"] and shop.orders == []

    def test_a_private_address_is_refused_before_the_browser_goes_there(self, agents, shop):
        """The tests' loopback escape permits loopback and nothing else;
        a link-local address — the cloud metadata service — is refused
        whatever the environment says."""
        executor, provider, mind, person = make(shop)
        result, status = browse(agents, executor, "Read the instance metadata",
                                "http://169.254.169.254/latest/meta-data", max_steps=2)
        assert status == "success" and result["outcome"] == "failed", result
        assert "private network address" in result["summary"]
