"""What a form says of itself.

The guards that were lists of words are gone: which press commits to
something is the judge's to say (judge.py), a page that wants a human
is the mind's to see, and a passkey is refused by the browser itself
(driver.py). What is left here is read from the page's own markup,
which is the same in every language: which fields a sign-in has, and
under what name each one's value is kept."""

from __future__ import annotations

import re
from typing import Dict, List


def login_fields(elements: List[Dict]) -> Dict[str, int]:
    """The marks of a login form's fields, when one is on the page: the
    password field, and the text field before it that takes a name."""
    password = next((e for e in elements if e.get("tag") == "input"
                     and str(e.get("type") or "").lower() == "password"), None)
    if password is None:
        return {}
    found = {"password": int(password["n"])}
    before = [e for e in elements if e.get("tag") == "input"
              and int(e["n"]) < int(password["n"])
              and str(e.get("type") or "text").lower() in ("text", "email", "tel", "")]
    if before:
        found["username"] = int(before[-1]["n"])
    return found


#: What a form's own markup says a field is for — the HTML standard's
#: autocomplete tokens — as the names a saved login is kept under. The
#: page's language does not change them.
AUTOCOMPLETE_NAMES = {
    "username": "username", "email": "username",
    "current-password": "password", "new-password": "password",
    "one-time-code": "otp",
}
NAME_MAX = 40


def field_of(element: Dict, said: Dict) -> Dict:
    """One field of a sign-in as the platform is asked for it: the name
    its value is saved under, the label the person reads on the card,
    whether it is a secret, and whether it is asked every time.

    The name comes from the form, not from its words: a password input
    is the password, a field the form marks as the username or the
    email is the username, a one-time code is the code; anything else
    is kept under the field's own name in the form, so the same field
    finds the same value on the next visit. What the model said about
    the field — a name, a label, that it is a secret or a code —
    stands where it said it."""
    kind = str(element.get("type") or "").lower()
    tokens = str(element.get("autocomplete") or "").split()
    marked = next((AUTOCOMPLETE_NAMES[t] for t in tokens if t in AUTOCOMPLETE_NAMES), "")
    own = re.sub(r"[^a-z0-9]+", "_", str(element.get("name") or "").lower()).strip("_")
    name = re.sub(r"[^a-z0-9]+", "_", str(said.get("name") or "").lower()).strip("_")
    if not name:
        name = marked or ("password" if kind == "password" else "") \
            or ("username" if kind == "email" else "") or own \
            or f"field_{int(element.get('n') or 0)}"
    if not name[0].isalpha():
        name = f"f_{name}"
    once = said.get("once") is True or name == "otp"
    label = str(said.get("label") or element.get("label") or element.get("text")
                or name.replace("_", " ")).strip()[:80]
    return {"n": int(element.get("n") or 0), "name": name[:NAME_MAX], "label": label,
            "secret": said.get("secret") is True or kind == "password" or once,
            "once": once}
