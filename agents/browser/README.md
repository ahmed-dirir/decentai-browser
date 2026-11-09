# Browser

A real web browser, driven by the chat's model, with no site-specific
code. Given a goal and a page it looks, decides one action, acts, and
looks again — up to a step budget — while the person watches it live in
the chat and takes it over when a site wants a human.

## What the model sees every step

A screenshot of the page with a red numbered mark on every element it
could act on; the same elements as text (number, kind, label, value,
link); the page's readable words; its plan, with where each step of it
stands; the account of what is behind it; the records it has kept; its
newest actions and how they went; notes from earlier visits to the
site. It writes its reasoning, never shown, and one action.

How much of each it is shown is a share of the step's prompt, not a
count (`tools/memory.py`): the page's words take what the rest leaves.
Everything is kept; the actions no longer shown are folded, every ten
steps, into an account the model writes itself, and `recall` reads
back what left its sight. Where something is cut, the cut is said,
with how to get the rest. `DECENTAI_BROWSER_PROMPT_CHARS` (60,000) and
`DECENTAI_BROWSER_FOLD_EVERY` (10) are the two numbers a deployment
may change.

## Beneath the picture

The browser has no window, so there is no developer's window to open;
what that window shows is read through the driver and handed over as
words (`tools/inspector.py`).

| Action | What it gives | Rule |
|---|---|---|
| `source` | The HTML of an element or the page, without what draws or runs, and without any field's value | Free |
| `network` | What the page fetched since it opened: method, status, kind, address | Free; never a header |
| `response` | What came back for one of them, when it is data or text | Free; not while a sign-in is on the page, nor for what was fetched while the person held the browser |
| `console` | What the page logged, and its errors | Free |
| `request` | An address of the page's own site, fetched with the page's session | GET only, the same site only |
| `script` | JavaScript, run in the page | The person's approval every time, asked in the chat with the reason and the code whole. 600 characters at most |

Cookies, stored values and headers are never shown: they are the
person's session, not the page's content.

The model's replies are never capped in tokens: a reply is one JSON
object and the model stops when it is done. A reply that cannot be
read as an action is told back with why — cut off, or not one JSON
object — and three such replies in a row end the run.

## Functions

| Function | Level | What it does |
|---|---|---|
| `browse_run` | 2 | Does what the goal says from `start_url`, up to `max_steps` (60 by default, 200 at most). Without `start_url` it continues on the page the chat's browser was left at. `files` are what the person attached for a page to take (`upload`). Returns the outcome (`done`, `stopped`, `stopped_by_person` when the user answered Stop, `failed`, `budget`), a summary, the records found, the steps taken, the final URL, a screenshot's `file_ref`, what was approved, how many handoffs, and any downloads, each returned to the chat as a file |
| `browse_watch` | 0 | Shows the chat's browser in the live view before anything is asked of it, so the person can look, sign in or go somewhere; the model calls it when the person wants to see or use the browser, not as a step in a task. `action: quit` closes the browser and remembers where it was |
| `browse_screenshot` | 2 | Opens a page and returns a screenshot as a file, with its title and final URL |

A process serves one chat, so it holds one browser: it stays open after
a run, and a follow-up in the same chat continues where the last run
stopped. It goes with the process, which the runtime ends once the chat
stops using it.

## The moments that are the person's

- **A sign-in, in whatever shape the site gives it.** An email on one
  page and the password on the next, a company number beside both, a
  PIN, a code: on each page the model names the fields that take the
  person's own values, and the site's login (a `logins` secret, one
  per domain) is asked for exactly those — handed over when it holds
  them; when it does not, they are its form's questions, which the
  person fills in on the platform's form. A field is named by what
  the form says it is (a password input, the form's own marking of a
  user name or a one-time code) or by what the model says it is, never
  by the words on the page, which are in any language. A code is asked
  each time and never stored. The values reach the page and nothing
  else: not the model's prompt, not the history.
- **A press that cannot be taken back.** Before a button is pressed, a
  form sent, a choice made in a list or a file put in, a second model
  call — the judge (`tools/judge.py`) — is shown the control, its form
  and the screen around it, and says what the press does: it looks,
  it changes something the person can take back, or it commits. One
  that commits stops for the person's approval (asked in the chat), and so
  does any change when they asked only to look. An answer the judge
  did not give, or that cannot be read, is one that commits. A plain
  link is followed unjudged — a read, by the protocol's rule — unless
  the model itself expects more of it. No list of words decides any of
  this, so it holds in every language. Only the agent's own question
  approves a press: a question the model asked on its own does not.
- **A page that wants a human.** A CAPTCHA, an approval on the person's
  phone, a sign-in a script may not do: the model sees it and hands the
  browser over, the agent feeds the person's hand to the page, and
  continues when they press Done. A run whose last three actions
  changed nothing offers the browser too, once for a page, before it
  gives up.
- **A script.** See above: theirs to allow, each time.
- **The tabs.** A page a click opened in a new tab comes to the front.
  Every frame tells of the open tabs, the live view shows them, and the
  person goes to one, closes one or opens one — which takes the
  browser, as any hand on it does.

## What it keeps

- **Sessions** (`session` records, encrypted): a site's cookies after a
  sign-in or a handoff, keyed by domain and account, so the next run
  skips the form.
- **Site notes** (`notes` records, editable by the person), by
  section — signing in, finding things, what to avoid, what the person
  did themselves, and the names the sign-in's fields are saved under —
  each rewritten on its own after a run, read before the next on that
  site.
- **Sign-ins** (`logins` secrets): what each site's sign-in asks, by
  field name, under the site's domain; a one-time code is never kept.
- **Where the browser was**, in the process only: the address the
  chat's browser was at when it closed, so the next run or watch in
  that chat without an address starts there.

The final screenshot of every run, and any file the model chose to
download, are returned to the chat as its files; nothing keeps them.

## Where it may go

Every navigation and every request the page makes is checked: loopback
and private addresses are refused, so a browser on the runtime host
cannot reach a metadata service or the platform itself.
`DECENTAI_WEB_ALLOW_LOOPBACK=1` is the tests' escape. In the sandbox
the browser is handed the sandbox's way out (`HTTPS_PROXY`), which
looks each name up and makes the same check; a page it refused is said
with its reason, asked of the way out with a tunnel.

## What it carries

The program carries Playwright and its driver, pinned in
`requirements.txt`, and Chromium with them, under `chromium/` at the
program's root (`publish/chromium.sh` makes it for each machine): the
build Playwright wants, the system libraries it loads that the sandbox's
base lacks, and fonts. Outside a program, in a checkout, Playwright's own
install serves (`playwright install --no-shell chromium`). Chromium runs without its own sandbox
(`--no-sandbox`: the sandbox it runs in refuses the namespaces it would
make) and keeps its shared memory out of the small `/dev/shm` (`--disable-dev-shm-usage`). Each Playwright
release wants one browser build: Playwright 1.59.0 and `chromium-1217`
go together. A missing browser is reported in the run's summary, not
hidden. Set `DECENTAI_BROWSER_HEADLESS=0` to watch it on a machine with
a screen.

## Tests

`tests/test_browser.py` runs the agent as its own program with a real
Chromium against a loopback shop: a login, items, a basket, an order
button, and a page that wants a human. The chat's model is scripted;
the person is a scripted asker who fills in the platform's form.
`tests/test_browser_revision.py` proves the judge, the memory, the
looking beneath and the tabs: the shop has a sign-in over two pages in
Arabic, a list it fetches as data, a link that opens a new tab, and a
page where nothing moves. `tests/test_browser_proxy.py` runs it
behind the tests' way out, which refuses a name that resolves inside.
