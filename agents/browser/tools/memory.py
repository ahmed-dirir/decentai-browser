"""What a run remembers, and how much of it the mind is shown.

Everything is kept: every action and how it went, every record, every
word the person said. What is *shown* at a step is budgeted — a share
of the step's prompt each — rather than counted: "the last eight
actions" forgot the first client of seven by the time of the third,
whatever the model could have read.

Three things make a long run one piece of work:

- **The account.** The newest actions are shown as they happened. The
  older ones are folded, every so many steps, into an account the mind
  writes itself: what is done, what was learned, what remains. Nothing
  is forgotten; it is said in fewer words.
- **The plan.** Each step of it has a state, so where the run stands is
  in front of the mind at every step.
- **Recall.** What is no longer shown can be asked for.

Wherever something is cut, the cut is said, with how to get the rest.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

FOLD_PROMPT = """You are partway through a task in a web browser. Older steps are about to leave your sight; write the account that replaces them.

THE TASK: {goal}

THE ACCOUNT SO FAR:
{account}

THE STEPS SINCE, oldest first:
{steps}

Write the account anew, as three short parts, in plain words:
DONE — what has been finished, item by item, so nothing is done twice.
LEARNED — what is true of this site that the work depends on: addresses that work, where things are, what a search wants, what failed and why.
LEFT — what remains, in the order to do it.
At most {limit} characters. Answer with the account only."""


def cut(text: str, limit: int, rest: str = "") -> str:
    """The text within its limit — and when that cut it, a line saying
    how much is missing and how to get it."""
    text = str(text or "")
    limit = max(0, int(limit))
    if len(text) <= limit:
        return text
    note = f"\n[cut here: {len(text) - limit} more characters" + (f" — {rest}" if rest else "") + "]"
    return text[:max(0, limit)] + note


class Plan:
    """The steps of the work, each with where it stands."""

    STATES = ("todo", "doing", "done", "dropped")
    STEPS_MAX = 20

    def __init__(self):
        self.steps: List[Dict[str, str]] = []

    def take(self, raw: Any) -> None:
        """The plan as the mind wrote it: a list of steps, each a
        sentence or {"step", "state"}. A step already known keeps its
        state when none is given."""
        if not isinstance(raw, list):
            return
        known = {s["step"]: s["state"] for s in self.steps}
        steps = []
        for item in raw[: self.STEPS_MAX]:
            if isinstance(item, dict):
                text = str(item.get("step") or item.get("text") or "").strip()
                state = str(item.get("state") or "").strip().lower()
            else:
                text, state = str(item or "").strip(), ""
            if not text:
                continue
            steps.append({"step": text[:300],
                          "state": state if state in self.STATES else known.get(text, "todo")})
        if steps:
            self.steps = steps

    def shown(self) -> str:
        return "\n".join(f"{i}. [{s['state']}] {s['step']}" for i, s in enumerate(self.steps, 1))


class SiteNotes:
    """What is known of a site, by section, so that one long section
    does not push out the others. Kept as text a person can read and
    edit: a line naming the section, then its words."""

    SECTIONS = (
        ("signing_in", "SIGNING IN"),
        ("finding", "FINDING THINGS"),
        ("avoid", "WHAT TO AVOID"),
        ("person", "WHAT THE PERSON DID THEMSELVES"),
    )
    SECTION_CHARS = 900
    #: The names the person's values are saved under on this site: a
    #: field asked under another name would put a card in front of
    #: them for a value they already gave.
    NAMES = "NAMES OF THE SIGN-IN FIELDS"

    def __init__(self, text: str = ""):
        self.sections: Dict[str, str] = {key: "" for key, _ in self.SECTIONS}
        self.names: Dict[str, str] = {}
        self._read(str(text or ""))

    def _read(self, text: str) -> None:
        titles = {title: key for key, title in self.SECTIONS}
        current, lines = "", {key: [] for key in self.sections}
        loose: List[str] = []
        for line in text.splitlines():
            head = line.strip().rstrip(":")
            if head in titles:
                current = titles[head]
            elif head == self.NAMES:
                current = self.NAMES
            elif current == self.NAMES:
                name, _, label = line.partition(" = ")
                if name.strip() and label.strip():
                    self.names[name.strip()] = label.strip()
            elif current:
                lines[current].append(line)
            elif line.strip():
                loose.append(line)
        for key in self.sections:
            self.sections[key] = "\n".join(lines[key]).strip()
        if loose:
            # Notes from before there were sections: what is known of
            # the site, kept where finding things is.
            self.sections["finding"] = ("\n".join(loose) + "\n" + self.sections["finding"]).strip()

    def write(self, key: str, words: str) -> None:
        """One section anew. Nothing said keeps what was there."""
        words = str(words or "").strip()
        if key in self.sections and words:
            self.sections[key] = words[: self.SECTION_CHARS]

    def add(self, key: str, words: str) -> None:
        """More under a section, the oldest giving way when it is full."""
        words = str(words or "").strip()
        if key in self.sections and words:
            self.sections[key] = (self.sections[key] + "\n" + words).strip()[-self.SECTION_CHARS:]

    def text(self) -> str:
        parts = [f"{title}:\n{self.sections[key]}" for key, title in self.SECTIONS
                 if self.sections[key]]
        if self.names:
            parts.append(f"{self.NAMES}:\n" + "\n".join(
                f"{name} = {label}" for name, label in self.names.items()))
        return "\n\n".join(parts)


class WorkingMemory:
    #: What one step's prompt may hold, in characters — the model's to
    #: read, not the agent's to guess: a deployment with a larger or a
    #: smaller model says so.
    PROMPT_CHARS = int(os.environ.get("DECENTAI_BROWSER_PROMPT_CHARS") or 60000)
    #: What each part may take of it. The page's words take what the
    #: others leave, and never less than their own share.
    SHARES = {"actions": 0.24, "account": 0.08, "records": 0.10, "notes": 0.07,
              "said": 0.03, "elements": 0.22, "page": 0.20}
    #: The newest actions are never folded: they are what just happened.
    UNFOLDED = 6
    #: The account is written again every this many steps.
    FOLD_EVERY = int(os.environ.get("DECENTAI_BROWSER_FOLD_EVERY") or 10)
    ACCOUNT_CHARS = 2400

    def __init__(self):
        self.actions: List[Dict[str, str]] = []
        self.records: List[Dict[str, Any]] = []
        self.said: List[str] = []
        self.account = ""
        #: how many of the actions the account already holds
        self.folded = 0

    def share(self, part: str) -> int:
        return int(self.PROMPT_CHARS * self.SHARES[part])

    # -- what is kept ------------------------------------------------------------------
    def did(self, action: str, result: str) -> None:
        self.actions.append({"action": str(action), "result": str(result)})

    def heard(self, text: str) -> None:
        self.said.append(str(text)[:2000])
        self.did("the person said", str(text)[:600])

    def keep(self, records: List[Dict[str, Any]]) -> int:
        kept = [r for r in records if isinstance(r, dict) and r]
        self.records.extend(kept)
        return len(kept)

    # -- the account ---------------------------------------------------------------------
    def due(self, step: int) -> bool:
        return step > 0 and step % self.FOLD_EVERY == 0 \
            and len(self.actions) - self.folded > self.UNFOLDED

    async def fold(self, call, goal: str) -> None:
        """The actions older than the newest few, folded into the
        account. When the mind cannot write it, the steps themselves
        are kept in short, so nothing is lost to a failed call."""
        upto = len(self.actions) - self.UNFOLDED
        leaving = self.actions[self.folded:upto]
        if not leaving:
            return
        steps = "\n".join(f"- {a['action']} → {cut(a['result'], 700)}" for a in leaving)
        written = ""
        try:
            written = str(await call.model(FOLD_PROMPT.format(
                goal=goal, account=self.account or "(none yet)", steps=steps,
                limit=self.ACCOUNT_CHARS)) or "").strip()
        except Exception:
            written = ""
        if written:
            self.account = written[: self.ACCOUNT_CHARS]
        else:
            short = "\n".join(f"- {a['action']} → {a['result'][:120]}" for a in leaving)
            self.account = (self.account + "\n" + short).strip()[-self.ACCOUNT_CHARS:]
        self.folded = upto

    # -- what is shown ---------------------------------------------------------------------
    def actions_shown(self) -> str:
        """The actions not yet in the account, oldest first, within
        their share: the newest in full, the older ones given less when
        room runs short."""
        waiting = self.actions[self.folded:]
        if not waiting:
            return ""
        room = self.share("actions")
        lines: List[str] = []
        for index, action in enumerate(reversed(waiting)):
            # The newest action may take half of what is left; each one
            # before it, half again — a read a moment ago is whole, a
            # read ten steps ago is its beginning.
            allowed = max(200, room // 2)
            line = f"- {action['action']} → " + cut(
                action["result"], allowed, "recall finds it again")
            if len(line) > room and index >= self.UNFOLDED:
                lines.append(f"[{len(waiting) - index} older actions are not shown: recall finds them]")
                break
            lines.append(line)
            room = max(0, room - len(line))
        return "\n".join(reversed(lines))

    def records_shown(self) -> str:
        """How many records are kept, by kind, and the newest within
        their share."""
        if not self.records:
            return ""
        kinds: Dict[str, int] = {}
        for record in self.records:
            kind = str(record.get("kind") or "record")
            kinds[kind] = kinds.get(kind, 0) + 1
        head = f"{len(self.records)} kept: " + ", ".join(f"{n} {kind}" for kind, n in kinds.items())
        room, lines = self.share("records") - len(head), []
        for record in reversed(self.records):
            line = json.dumps(record, ensure_ascii=False)
            if len(line) > room:
                break
            lines.append(line)
            room -= len(line) + 1
        if len(lines) < len(self.records):
            head += f" — the newest {len(lines)} are shown; recall finds the others"
        return head + ("\n" + "\n".join(reversed(lines)) if lines else "")

    def said_shown(self) -> str:
        room, lines = self.share("said"), []
        for text in reversed(self.said):
            if len(text) > room and lines:
                break
            lines.append(f"- {cut(text, room)}")
            room -= len(text)
        return "\n".join(reversed(lines))

    def recall(self, query: str, limit: int) -> str:
        """What was kept that holds every word asked for: records
        first, then actions, oldest first. With no words, the oldest
        actions — what left sight first."""
        words = [w for w in re.split(r"\s+", str(query or "").lower()) if w]
        found: List[str] = []
        for record in self.records:
            line = json.dumps(record, ensure_ascii=False)
            if words and all(w in line.lower() for w in words):
                found.append("record: " + line)
        for number, action in enumerate(self.actions, 1):
            line = f"{action['action']} → {action['result']}"
            if not words or all(w in line.lower() for w in words):
                found.append(f"action {number}: " + line)
        if not found:
            return f"nothing kept holds {query!r}"
        each = max(300, limit // max(1, min(len(found), 12)))
        shown = [cut(line, each) for line in found[:12]]
        more = len(found) - len(shown)
        return "\n".join(shown) + (f"\n[{more} more match: ask with more words]" if more > 0 else "")
