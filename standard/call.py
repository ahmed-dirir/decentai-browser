"""One call of the runtime's, as a function sees it: its inputs, and the runtime's functions within it, record.*,
model, chat and network.open, with files as paths in the home (agents.md §6, §9)."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path, PurePosixPath
from typing import Any

from .agent import ResourceDenied

PICTURES = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpg", b"GIF8": "gif", b"RIFF": "webp"}


class Call:
    def __init__(self, program: Any, call_id: str, function: str, inputs: dict[str, Any]):
        self.program, self.id, self.function, self.inputs = program, call_id, function, inputs
        self.records = Records(self)
        self.screen = Screen(self)
        #: The files it returns, as paths in the home: named in its result's files.
        self.files: list[str] = []
        self._written = 0

    async def runtime(self, function: str, inputs: dict[str, Any], files: list[str] | None = None) -> dict[str, Any]:
        """One of the runtime's functions, within this call: its output, or ResourceDenied."""
        return await self.program.ask(self.id, function, inputs, files)

    async def progress(self, text: str) -> None:
        self.program.send("note", {"call": self.id, "progress": str(text)[:1000]})

    async def model(self, prompt: str, system: str | None = None, max_tokens: int | None = None,
                    pictures: list[bytes | str] | None = None) -> str:
        """The chat's model: its text. Pictures are paths in the home, or bytes written there."""
        messages = ([{"role": "system", "text": system}] if system else []) + [{"role": "user", "text": prompt}]
        paths = [self.written(f"picture.{self.kind(p)}", p) if isinstance(p, bytes) else str(p) for p in pictures or []]
        inputs = {"messages": messages, **({"max_tokens": max_tokens} if max_tokens else {}),
                  **({"pictures": paths} if paths else {})}
        return str((await self.runtime("model", inputs, paths))["text"] or "")

    async def say(self, text: str = "", table: dict[str, Any] | None = None,
                  chart: dict[str, Any] | None = None) -> None:
        """Words in the chat, with a table ({title, columns, rows}; rows may be mappings) or a chart."""
        if table is not None:
            rows, columns = list(table.get("rows") or []), list(table.get("columns") or [])
            columns = columns or (list(rows[0]) if rows and isinstance(rows[0], dict) else [])
            rows = [["" if row.get(c) is None else row[c] for c in columns] if isinstance(row, dict) else list(row)
                    for row in rows]
            table = {"title": str(table.get("title") or ""), "columns": columns, "rows": rows}
        inputs = {**({"text": text} if text else {}), **({"table": table} if table else {}),
                  **({"chart": chart} if chart else {})}
        await self.runtime("chat", inputs)

    async def ask(self, question: str, choices: list[str] | None = None, expects: str | None = None) -> str | None:
        """The person's answer: words, a choice, or a file's path in the home. None when nobody answered."""
        expects = expects or ("choice" if choices else "text")
        inputs = {"text": question, "expects": expects, **({"choices": list(choices)} if choices else {})}
        answer = (await self.runtime("chat", inputs)).get("answer")
        return None if answer is None else str(answer)

    async def open(self, hosts: list[str], purpose: str, code: str = "", language: str = "python") -> list[str]:
        """Hosts opened for this call once the person allowed them, with the code that needs them shown."""
        inputs = {"hosts": list(hosts), "purpose": purpose, **({"code": {"language": language, "text": code}}
                                                                if code else {})}
        return list((await self.runtime("network.open", inputs))["hosts"])

    def give(self, name: str, data: bytes | str) -> str:
        """A file returned with the result: its path, which the runtime turns into the chat's file."""
        path = self.written(name, data)
        self.files.append(path)
        return path

    async def file(self, given: str, resource: str | None = None) -> Path:
        """A file handed to this call, by its path in the home; or a record of the resource, read by its ref."""
        home = Path.cwd().resolve()
        path = (home / str(given)).resolve()
        if path.is_file() and path.is_relative_to(home):
            return path
        if resource is None:
            raise ResourceDenied(f"{given} is no file handed to this call.", "not_found")
        return home / (await self.records.read(resource, str(given)))["path"]

    def written(self, name: str, data: bytes | str) -> str:
        """Bytes written in the home, under a folder of this call's own: the path, relative to the home."""
        self._written += 1
        path = PurePosixPath("out", self.id, str(self._written), PurePosixPath(str(name)).name or "file")
        (Path.cwd() / path).parent.mkdir(parents=True, exist_ok=True)
        (Path.cwd() / path).write_bytes(data.encode() if isinstance(data, str) else data)
        return path.as_posix()

    @staticmethod
    def kind(picture: bytes) -> str:
        return next((kind for start, kind in PICTURES.items() if picture.startswith(start)), "png")


class Records:
    """record.* on the resources the manifest declares: each record ``{ref, fields}``, a file's ``{ref, name, type,
    size}`` and, read, its ``path`` in the home."""

    def __init__(self, call: Call):
        self.call = call

    async def list(self, resource: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Every record, oldest first, a page at a time."""
        found, after, narrowed = [], None, {"filters": filters} if filters else {}
        while True:
            page = await self.call.runtime("record.list", {"resource": resource, **narrowed,
                                                           **({"after": after} if after else {})})
            found += page["records"]
            after = page.get("next")
            if not after:
                return found

    async def read(self, resource: str, ref: str | None = None) -> dict[str, Any]:
        return await self.call.runtime("record.read", {"resource": resource, **({"ref": ref} if ref else {})})

    async def create(self, resource: str, fields: dict[str, Any]) -> str:
        return (await self.call.runtime("record.create", {"resource": resource, "fields": fields}))["ref"]

    async def create_file(self, resource: str, name: str, data: bytes | str) -> str:
        path = self.call.written(name, data)
        inputs = {"resource": resource, "name": PurePosixPath(path).name, "path": path}
        return (await self.call.runtime("record.create", inputs, [path]))["ref"]

    async def update(self, resource: str, ref: str, fields: dict[str, Any]) -> dict[str, Any]:
        return await self.call.runtime("record.update", {"resource": resource, "ref": ref, "fields": fields})

    async def delete(self, resource: str, ref: str) -> bool:
        return bool((await self.call.runtime("record.delete", {"resource": resource, "ref": ref})).get("deleted"))

    async def secret(self, resource: str, ref: str | None = None) -> dict[str, Any]:
        """A secret's fields. With none kept yet, the person is asked to fill it in, or to connect the account."""
        if ref:
            return (await self.read(resource, ref))["fields"]
        found = await self.list(resource)
        if not found:
            ref = await self.create(resource, {})
        elif len(found) == 1:
            ref = found[0]["ref"]
        else:
            named = ", ".join(str(r["fields"].get("account") or r["ref"]) for r in found)
            raise ResourceDenied(f"Several are kept for {resource} ({named}): name one.", "invalid")
        return (await self.read(resource, ref))["fields"]


class Screen:
    """What a call drives, shown to the person as pictures (``note`` with ``screen``); what they do on it comes back
    as events (``note`` with ``input``): mouse, keys, control taken and given back, and words said."""

    MOST = 300 * 1024
    TABS_MOST = 20

    def __init__(self, call: Call):
        self.call = call
        self.open = self.taken = self.closed = False
        self._took = False
        self._events: list[dict[str, Any]] = []
        self._said: list[str] = []
        self._arrived: asyncio.Event | None = None

    async def show(self, image: bytes, width: int, height: int, tabs: list[dict[str, Any]] | None = None) -> bool:
        """One picture, JPEG or PNG; False when it is too large to show."""
        encoded = base64.b64encode(bytes(image)).decode("ascii")
        if not image or len(encoded) > self.MOST:
            return False
        told = [{"index": int(t.get("index") or n), "title": str(t.get("title") or "")[:200],
                 "address": str(t.get("address") or "")[:300], "active": t.get("active") is True}
                for n, t in enumerate(tabs or [], 1)][:self.TABS_MOST]
        self.open = True
        self.call.program.send("note", {"call": self.call.id, "screen": {
            "image": encoded, "width": int(width), "height": int(height), "tabs": told}})
        return True

    async def close(self) -> None:
        if self.open:
            self.open = False
            self.call.program.send("note", {"call": self.call.id, "screen": None})

    def receive(self, events: list[dict[str, Any]]) -> None:
        """The person's events on this screen, as the runtime sent them."""
        for event in events:
            if event.get("type") == "say":
                self._said += [str(event.get("text") or "").strip()] if str(event.get("text") or "").strip() else []
                continue
            if event.get("type") == "control":
                self.taken = event.get("action") == "take"
                self._took = self._took or self.taken
                self.closed = self.closed or event.get("action") == "close"
            self._events.append(dict(event))
        if self._arrived is not None:
            self._arrived.set()

    def took(self) -> bool:
        """Whether the person took control since this was last asked, even if they gave it back since."""
        took, self._took = self._took, False
        return took

    def inputs(self) -> list[dict[str, Any]]:
        """What the person did since last asked, oldest first."""
        events, self._events = self._events, []
        return events

    async def wait_input(self, timeout: float = 1.0) -> list[dict[str, Any]]:
        if not self._events:
            self._arrived = asyncio.Event()
            try:
                await asyncio.wait_for(self._arrived.wait(), timeout)
            except asyncio.TimeoutError:
                pass
            finally:
                self._arrived = None
        return self.inputs()

    def said(self) -> list[str]:
        """What the person said to this call since last asked."""
        said, self._said = self._said, []
        return said
