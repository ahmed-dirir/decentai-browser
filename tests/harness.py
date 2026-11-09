"""The runtime's side of an agent's program, for the tests: the program started as a sandbox starts it, in a home of
its own, spoken to in the standard's messages (documentation/agents.md §9 of the platform); its own calls answered by
fakes of record.*, model and chat, held to what each function declares; its connections through a way out that
opens only the hosts it may reach, each name found on this machine, where the stubs are.

What is checked here is what an agent's code meets: its manifest, the messages, the gate's first step (declared in
``calls``) and the runtime's limits. The platform's own gates are tested with the platform.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import itertools
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import jsonschema
import yaml

REPOSITORY = Path(__file__).resolve().parents[1]
ROOT = REPOSITORY / "agents"
PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
START = ("import sys, yaml; sys.path.insert(0, {repository!r}); from standard import Program; "
         "Program(yaml.safe_load(open({manifest!r}, encoding='utf-8'))).run()")
LEVELS = {"record.list": 0, "record.read": 0, "record.create": 1, "record.update": 1, "record.delete": 2, "model": 0,
          "chat": 0, "network.open": 3}
PICTURES = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")


class Refusal(Exception):
    """A call of the program's refused or failed, as the runtime answers it: its code and its words."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def swapped(value: Any, found: dict[str, str]) -> Any:
    """Every text equal to a key of ``found`` replaced by its value, however deep."""
    if isinstance(value, str):
        return found.get(value, value)
    if isinstance(value, list):
        return [swapped(item, found) for item in value]
    if isinstance(value, dict):
        return {key: swapped(item, found) for key, item in value.items()}
    return value


def strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    return set().union(*(strings(item) for item in items))


def problem(schema: Any, value: Any) -> str:
    found = jsonschema.exceptions.best_match(jsonschema.Draft202012Validator(schema or {}).iter_errors(value))
    return "" if found is None else f"{'.'.join(map(str, found.absolute_path)) or '(root)'}: {found.message}"


#: What the platform names by extension whatever the interpreter's table holds (shared/file_types.py).
KNOWN_TYPES = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
               ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
               ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
               ".md": "text/markdown", ".csv": "text/csv", ".json": "application/json", ".yaml": "application/yaml"}


def file_type(name: str) -> str:
    known = KNOWN_TYPES.get(PurePosixPath(name.lower()).suffix)
    return known or mimetypes.guess_type(name)[0] or "application/octet-stream"


# -- the way out ---------------------------------------------------------------------------------------------------

class Way:
    """The way out, as the runtime runs it: a proxy per process, opening a host it declared, one network.open opened
    for a call in flight, or one a secret it read names; each name found in ``names``, on this machine unless that
    says otherwise, and reached only at a public address (this machine counts while ``allow_loopback``)."""

    def __init__(self):
        self.web = {80, 443}
        self.names: dict[str, list[str]] = {}
        self.allow_loopback = True
        self.refusals = 0
        #: Where a process is pointed instead of its own way out: "" for none at all.
        self.pointed: Optional[str] = None
        self.loop = asyncio.new_event_loop()
        self.servers: list[asyncio.AbstractServer] = []
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    def serve(self, agent: "Installed") -> int:
        async def started():
            server = await asyncio.start_server(lambda r, w: self.carried(agent, r, w), "127.0.0.1", 0)
            self.servers.append(server)
            return server.sockets[0].getsockname()[1]
        return asyncio.run_coroutine_threadsafe(started(), self.loop).result(10)

    def end(self) -> None:
        for server in self.servers:
            self.loop.call_soon_threadsafe(server.close)
        self.loop.call_soon_threadsafe(self.loop.stop)

    def stands_for(self, host: str, server: Any) -> str:
        """The address a stub gives out under a service's name: its port counts as one the web answers on."""
        port = server.server_address[1]
        self.web.add(port)
        return f"http://{host}:{port}"

    def refusal(self, agent: "Installed", host: str, port: int) -> str:
        declared = [h for h in agent.hosts if isinstance(h, str)]
        if "any" in declared:
            return ""
        opened = {h for call in agent.calls.values() for h in call.opened}
        for pattern in [*declared, *opened, *agent.secret_hosts]:
            name, _, wanted = pattern.partition(":")
            if (int(wanted) == port if wanted else port in self.web) and (
                    host == name or (name.startswith("*.") and host.endswith(name[1:]))):
                return ""
        return f"{host}:{port} is not a host this agent declared, or was opened for this call."

    def address(self, host: str) -> tuple[str, str]:
        """(the address to reach, or "", and why not)."""
        try:
            addresses = [str(ipaddress.ip_address(host.strip("[]")))]
        except ValueError:
            addresses = self.names.get(host, ["127.0.0.1"])
        if not addresses:
            return "", f"{host} does not resolve."
        found = ipaddress.ip_address(addresses[0])
        if not found.is_global and not (found.is_loopback and self.allow_loopback):
            return "", f"{host} is not a public address; only public internet addresses are reached."
        return addresses[0], ""

    async def carried(self, agent: "Installed", reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
            method, target, version = head.split("\r\n", 1)[0].split(" ", 2)
            tunnel = method.upper() == "CONNECT"
            url = urlsplit(f"//{target}" if tunnel else target)
            host, port = (url.hostname or "").lower(), url.port or 80
            why = self.refusal(agent, host, port)
            address, unreachable = self.address(host) if not why else ("", "")
            if why or unreachable:
                self.refusals += bool(why)
                writer.write(f"HTTP/1.1 {403 if why else 502} {why or unreachable}\r\n\r\n".encode("latin-1"))
                return await writer.drain()
            upstream_reader, upstream = await asyncio.open_connection(address, port)
            if tunnel:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                lines = head.split("\r\n")
                path = target[target.find("/", target.find("//") + 2):] if "//" in target else target
                kept = [line for line in lines[1:] if line and not line.lower().startswith(("proxy-", "connection"))]
                upstream.write(("\r\n".join([f"{method} {path or '/'} {version}", *kept, "Connection: close"])
                                + "\r\n\r\n").encode("latin-1"))
            await asyncio.gather(self.piped(reader, upstream), self.piped(upstream_reader, writer))
        except (OSError, ValueError, IndexError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def piped(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                writer.close()


# -- one test's place ----------------------------------------------------------------------------------------------

class Place:
    """What one test has: its loop, its way out, the programs it started."""

    current: Optional["Place"] = None

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.way = Way()
        self.installed: list["Installed"] = []
        Place.current = self

    def end(self) -> None:
        for agent in self.installed:
            self.loop.run_until_complete(agent.end())
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()
        self.way.end()
        Place.current = None


def run(awaitable: Any) -> Any:
    """One coroutine on the test's own loop, where its programs' pipes are."""
    return Place.current.loop.run_until_complete(awaitable)


def way_out() -> Way:
    return Place.current.way


def stands_for(host: str, server: Any) -> str:
    return way_out().stands_for(host, server)


# -- the program ---------------------------------------------------------------------------------------------------

class Called:
    """A call of the runtime's to the program, in flight: whose chat it is, and what it opened and said."""

    def __init__(self, chat: "Chat", function: dict[str, Any], call_id: str, attended: bool):
        self.chat, self.function, self.id, self.attended = chat, function, call_id, attended
        self.done: asyncio.Future = asyncio.get_running_loop().create_future()
        self.opened: set[str] = set()
        self.said = 0


class Installed:
    """One agent's program as a runtime holds it for a chat: one process, started at its first call, in its home."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.manifest = yaml.safe_load((self.folder / "manifest.yaml").read_text(encoding="utf-8"))
        self.agent_id = self.manifest["agent"]["id"]
        self.hosts: list[Any] = list((self.manifest.get("network") or {}).get("hosts") or [])
        self.secret_hosts: set[str] = set()
        self.calls: dict[str, Called] = {}
        self.serving: dict[str, asyncio.Task] = {}
        self.ids = itertools.count(1)
        self.home = Path(tempfile.mkdtemp(prefix=f"home-{self.agent_id}-"))
        self.process: Optional[asyncio.subprocess.Process] = None
        self.ready: Optional[asyncio.Future] = None
        self.failed = ""
        self.port = way_out().serve(self)
        Place.current.installed.append(self)

    def function(self, function_id: str) -> Optional[dict[str, Any]]:
        return next((f for f in self.manifest["functions"] if f["id"] == function_id), None)

    def resource(self, resource_id: str) -> tuple[str, dict[str, Any]]:
        for kind in ("secrets", "data", "files"):
            for resource in (self.manifest.get("resources") or {}).get(kind) or []:
                if resource["id"] == resource_id:
                    return kind, resource
        raise Refusal("not_found", f"{self.agent_id} declares no resource {resource_id}.")

    def declaring(self, hosts: Any) -> "Installed":
        """As if its manifest declared these hosts: "any", or names with :<port> where the port isn't the web's."""
        self.hosts = ["any"] if hosts == "any" else list(hosts)
        return self

    def environment(self) -> dict[str, str]:
        pointed = way_out().pointed
        address = f"http://127.0.0.1:{self.port}" if pointed is None else pointed
        found = {k: v for k, v in os.environ.items() if k not in PROXY_NAMES}
        return {**found, "HOME": str(self.home), **(dict.fromkeys(PROXY_NAMES, address) if address else {})}

    async def started(self) -> None:
        if self.process is not None:
            return await asyncio.wait_for(asyncio.shield(self.ready), 60)
        self.ready = asyncio.get_running_loop().create_future()
        start = START.format(repository=str(REPOSITORY), manifest=str(self.folder / "manifest.yaml"))
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", start, cwd=self.home, env=self.environment(), limit=4 * 1024 * 1024,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        asyncio.ensure_future(self.read())
        await asyncio.wait_for(asyncio.shield(self.ready), 60)

    async def read(self) -> None:
        """Every message the program writes, each a line of JSON, handed where it belongs."""
        while line := await self.process.stdout.readline():
            assert len(line) <= 1024 * 1024, f"{self.agent_id} wrote a line longer than a message may be"
            message = json.loads(line)
            assert set(message) <= {"_id", "body", "files"}, message
            body = message["body"]
            kind, data = body["type"], body.get("data") or {}
            if kind == "ready" and not self.ready.done():
                expected = {"agent": self.agent_id, "version": self.manifest["agent"]["version"],
                            "functions": sorted(f["id"] for f in self.manifest["functions"])}
                said = {**data, "functions": sorted(data.get("functions") or [])}
                self.ready.set_result(None) if said == expected else self.ready.set_exception(
                    AssertionError(f"its ready {said} doesn't match its manifest {expected}"))
            elif kind == "error" and "call" not in data:
                self.failed = str(data.get("message"))
                if not self.ready.done():
                    self.ready.set_exception(AssertionError(f"{self.agent_id} failed: {self.failed}"))
            elif kind in ("result", "error") and data.get("call") in self.calls:
                done = self.calls[data["call"]].done
                done.done() or done.set_result(message)
            elif kind == "call":
                called = self.calls.get(data.get("within"))
                self.serving[message["_id"]] = asyncio.ensure_future(self.served(called, message))
            elif kind == "cancel" and data.get("call") in self.serving:
                self.serving[data["call"]].cancel()
            elif kind == "note" and data.get("call") in self.calls:
                await self.calls[data["call"]].chat.noted(self, data)
        for called in self.calls.values():
            called.done.done() or called.done.set_result(
                {"body": {"type": "error", "data": {"code": "failed", "message": "The agent's process ended."}}})
        if self.ready and not self.ready.done():
            self.ready.set_exception(AssertionError(f"{self.agent_id} ended before it was ready"))

    async def served(self, called: Optional[Called], message: dict[str, Any]) -> None:
        """One of the program's calls, answered with its result or its error."""
        try:
            if called is None:
                raise Refusal("refused", "A call is made within a call of the runtime's that is in flight.")
            output, files = await called.chat.serve(self, called, message)
            self.send("result", {"call": message["_id"], "output": output}, files)
        except Refusal as refused:
            self.send("error", {"call": message["_id"], "code": refused.code, "message": str(refused)[:1000]})
        except asyncio.CancelledError:
            self.send("error", {"call": message["_id"], "code": "cancelled", "message": "Cancelled."})
        finally:
            self.serving.pop(message["_id"], None)

    def send(self, kind: str, data: dict[str, Any], files: Optional[list] = None, message_id: str = "") -> str:
        message_id = message_id or f"r{next(self.ids)}"
        message = {"_id": message_id, "body": {"type": kind, "data": data}, **({"files": files} if files else {})}
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        return message_id

    def handed(self, name: str, content: bytes, folder: str) -> str:
        """A file written into the home, as the sandbox writes one in: its path there."""
        path = PurePosixPath("in", folder, PurePosixPath(name).name)
        (self.home / path).parent.mkdir(parents=True, exist_ok=True)
        (self.home / path).write_bytes(content)
        return path.as_posix()

    def kept(self, path: str) -> bytes:
        """A file the program named in a message: a plain file inside its home."""
        found = (self.home / str(path)).resolve()
        assert found.is_file() and found.is_relative_to(self.home.resolve()) and not found.is_symlink(), path
        return found.read_bytes()

    async def cancel(self, call_id: str) -> None:
        if call_id in self.calls:
            self.send("cancel", {"call": call_id})

    async def end(self) -> None:
        """Its input closed, as the sandbox ends a process: 5 seconds to exit, then ended."""
        if self.process is not None and self.process.returncode is None:
            with contextlib.suppress(OSError):
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        shutil.rmtree(self.home, ignore_errors=True)


class Agents(dict):
    """Every agent of the repository by its id, each started when a test first calls it."""

    def __missing__(self, agent_id: str) -> Installed:
        folder = ROOT / agent_id
        if not (folder / "manifest.yaml").is_file():
            raise KeyError(agent_id)
        self[agent_id] = Installed(folder)
        return self[agent_id]

    @property
    def folders(self) -> dict[str, Path]:
        return {path.name: path for path in sorted(ROOT.iterdir()) if (path / "manifest.yaml").is_file()}

    def __iter__(self):
        return iter(self.folders)

    def __len__(self) -> int:
        return len(self.folders)


def probe(agent: Installed) -> list[str]:
    """Started alone, as an install's check starts it: what is wrong with its ready, if anything."""
    fresh = Installed(agent.folder)
    try:
        run(fresh.started())
        return []
    except (AssertionError, asyncio.TimeoutError) as wrong:
        return [str(wrong) or "it said nothing within 60 seconds"]


def requirements(folder: Path) -> list[str]:
    """The pinned dependencies an agent's program carries."""
    found = Path(folder) / "requirements.txt"
    return [line.strip() for line in found.read_text().splitlines() if line.strip()] if found.is_file() else []


# -- records -------------------------------------------------------------------------------------------------------

class Records:
    """A person's records, as the data layer keeps them: data and secrets as ``{keys, values}`` by the storage their
    resource declares, files as their name, type and content; each under its resource's id, by ref. A secret given
    here is its fields, or a list of them for several."""

    def __init__(self, secrets: Optional[dict[str, Any]] = None):
        self.given = dict(secrets or {})
        self.secrets: dict[str, dict[str, dict]] = {}
        self.data: dict[str, dict[str, dict]] = {}
        self.files: dict[str, dict[str, dict]] = {}
        self.ids = itertools.count(1)

    def ref(self, kind: str) -> str:
        return f"{kind}_{next(self.ids)}"

    def kept(self, kind: str, resource: dict[str, Any]) -> dict[str, dict]:
        """The records of one resource; secrets given at the start split as the resource declares them."""
        if kind == "secrets" and resource["id"] in self.given:
            given = self.given.pop(resource["id"])
            for fields in given if isinstance(given, list) else [given]:
                fields = dict(fields)
                ref = str(fields.pop("ref", "") or self.ref("secret"))
                keys, values = self.split(resource, fields, oauth=bool(resource.get("oauth")))
                self.secrets.setdefault(resource["id"], {})[ref] = {"keys": keys, "values": values}
        return getattr(self, kind).setdefault(resource["id"], {})

    @staticmethod
    def split(resource: dict[str, Any], fields: dict[str, Any], oauth: bool = False) -> tuple[dict, dict]:
        """Fields into keys and values as declared; checked as the data layer checks them."""
        declared = {f["name"]: f for f in resource.get("fields") or []}
        unknown = sorted(set(fields) - set(declared) - ({"access_token", "expires_at", "account"} if oauth else set()))
        if unknown:
            raise Refusal("invalid", f"No field is declared as {', '.join(unknown)}.")
        keys, values = {}, {}
        for name, value in fields.items():
            field = declared.get(name, {"type": "string", "storage": "keys" if name == "account" else "values"})
            if value is None:
                continue
            kind = field["type"]
            if (kind == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool))) or (
                    kind in ("string", "secret") and not isinstance(value, str)) or (
                    kind == "select" and value not in (field.get("options") or [])) or (
                    kind in ("object", "form") and not isinstance(value, dict)):
                raise Refusal("invalid", f"{field.get('label', name)} is not a {kind} it may hold: {value!r}")
            (keys if field.get("storage") == "keys" else values)[name] = value
        return keys, values

    def create_data(self, resource: str, keys: dict[str, Any], values: Optional[dict[str, Any]] = None) -> str:
        """A record the person already has, for a test to start from."""
        ref = self.ref("data")
        self.data.setdefault(resource, {})[ref] = {"keys": dict(keys), "values": dict(values or {})}
        return ref

    def create_file(self, resource: str, name: str, content: bytes | str) -> str:
        """A file the person already has, for a test to start from."""
        ref = self.ref("file")
        data = content.encode() if isinstance(content, str) else bytes(content)
        self.files.setdefault(resource, {})[ref] = {"name": name, "type": file_type(name), "size": len(data),
                                                     "content": data}
        return ref


# -- the chat ------------------------------------------------------------------------------------------------------

@dataclass
class ChatSinks:
    """The person and the model, as a test plays them; each None where nobody is there."""

    llm: Optional[Callable] = None        # (messages, max_tokens, pictures) -> text, or (text, stop)
    ask: Optional[Callable] = None        # (text, choices, expects) -> the answer, or None
    say: Optional[Callable] = None        # (inputs) -> None: chat that expects nothing
    approve: Optional[Callable] = None    # (request) -> bool: a function above the chat's level
    form: Optional[Callable] = None       # (resource id, fields asked) -> fields, or None: a secret left empty
    open: Optional[Callable] = None       # (hosts, purpose, code) -> bool: network.open
    screen: Optional[Callable] = None     # (call id, screen) -> None
    progress: Optional[Callable] = None   # (text) -> None


async def answered(sink: Optional[Callable], *args: Any) -> Any:
    if sink is None:
        return None
    found = sink(*args)
    return await found if asyncio.iscoroutine(found) else found


class Chat:
    """What stands around an agent's call in a chat: its inputs and outputs held to their schemas, its level against
    the chat's, the files it is handed and returns, and the runtime's functions it calls."""

    def __init__(self, provider: Optional[Records] = None, sinks: Optional[ChatSinks] = None):
        self.provider = provider if provider is not None else Records()
        self.sinks = sinks or ChatSinks()
        #: The chat's files, by ref: those handed in and those the agents returned.
        self.files: dict[str, dict[str, Any]] = {}
        self.progress: list[str] = []
        self.said: list[dict[str, Any]] = []
        self.screens: list[Any] = []
        self.running: dict[str, Installed] = {}
        self.ids = itertools.count(1)

    def file(self, name: str, content: bytes | str) -> str:
        """A file of the chat's, as a person attaches one: its ref, which a call's inputs may name."""
        ref = f"chatfile_{next(self.ids)}"
        self.files[ref] = {"name": name, "content": content.encode() if isinstance(content, str) else bytes(content)}
        return ref

    async def invoke(self, agent: Installed, function_id: str, inputs: Optional[dict[str, Any]] = None,
                     chat_level: int = 1, attended: bool = True) -> tuple[dict[str, Any], str]:
        """The function's output and "success", or ``{error, kind?}`` and "error"."""
        inputs = dict(inputs or {})
        function = agent.function(function_id)
        if function is None:
            return {"error": f"{agent.agent_id} has no function {function_id}.", "kind": "not_found"}, "error"
        wrong = problem(function.get("inputs"), inputs)
        if wrong:
            return {"error": f"The inputs don't fit {function_id}: {wrong}", "kind": "invalid"}, "error"
        for scope, binding in (function.get("authorization") or {}).items():
            if binding.get("required", True) and inputs.get(binding["from_input"]) in (None, ""):
                return {"error": f"{function_id} must be told its {scope}.", "kind": "invalid"}, "error"
        if function["level"] > chat_level and not await answered(self.sinks.approve, {
                "agent": agent.agent_id, "function": function_id, "level": function["level"], "inputs": inputs}):
            return {"error": f"{function_id} needs level {function['level']} and was not approved.", "denied": True,
                    "kind": "refused"}, "error"
        named = {ref: agent.handed(self.files[ref]["name"], self.files[ref]["content"], ref) for ref in self.files
                 if ref in strings(inputs)}
        await agent.started()
        called = Called(self, function, f"c{next(agent.ids)}", attended)
        agent.calls[called.id] = called
        self.running[called.id] = agent
        try:
            agent.send("call", {"function": function_id, "inputs": swapped(inputs, named)}, list(named.values()),
                       called.id)
            message = await asyncio.wait_for(called.done, int(function.get("timeout_seconds") or 60) + 60)
        finally:
            agent.calls.pop(called.id, None)
            self.running.pop(called.id, None)
        data = message["body"]["data"]
        if message["body"]["type"] == "error":
            return {"error": str(data.get("message")), **({"kind": data["code"]} if data.get("code") else {})}, "error"
        made = {}
        for path in message.get("files") or []:
            made[path] = self.file(PurePosixPath(path).name, agent.kept(path))
        output = swapped(data["output"], made)
        wrong = problem(function.get("outputs"), output)
        if wrong:
            return {"error": f"{agent.agent_id} answered outside its outputs: {wrong}", "kind": "invalid"}, "error"
        return output, "success"

    async def screen_input(self, call_id: str, events: list[dict[str, Any]]) -> bool:
        """What the person did on a call's screen, sent to its program as a note."""
        agent = self.running.get(call_id)
        if agent is None:
            return False
        assert len(events) <= 64
        agent.send("note", {"call": call_id, "input": events})
        return True

    async def noted(self, agent: Installed, data: dict[str, Any]) -> None:
        assert len(data) == 2, data
        if "progress" in data:
            assert len(data["progress"]) <= 1000
            self.progress.append(data["progress"])
            await answered(self.sinks.progress, data["progress"])
        elif "screen" in data:
            screen = data["screen"]
            assert screen is None or (len(screen["image"]) <= 300 * 1024 and len(screen.get("tabs") or []) <= 20)
            self.screens.append(screen)
            await answered(self.sinks.screen, data["call"], screen)

    async def serve(self, agent: Installed, called: Called, message: dict[str, Any]) -> tuple[Any, list]:
        """One of the program's calls, through the gate's first step: what its function declared."""
        data = message["body"]["data"]
        name, inputs = str(data.get("function") or ""), data.get("inputs") or {}
        if name not in LEVELS:
            raise Refusal("not_found", f"The runtime has no function {name}.")
        declared = f"{name}:{inputs.get('resource')}" if name.startswith("record.") else name
        if declared not in (called.function.get("calls") or []):
            raise Refusal("refused", f"{agent.agent_id}.{called.function['id']} doesn't declare {declared}.")
        files = [agent.kept(path) for path in message.get("files") or []]
        if name.startswith("record."):
            return await self.record(agent, called, name.split(".")[1], inputs, files)
        return await {"model": self.model, "chat": self.chat, "network.open": self.open}[name](
            agent, called, inputs, files)

    async def record(self, agent: Installed, called: Called, operation: str, inputs: dict, files: list) -> tuple:
        kind, resource = agent.resource(str(inputs.get("resource")))
        kept, ref = self.provider.kept(kind, resource), str(inputs.get("ref") or "")
        if operation == "list":
            filters = inputs.get("filters") or {}
            found = [self.listed(kind, r, record) for r, record in kept.items()
                     if kind == "files" or all(record["keys"].get(k) == v for k, v in filters.items())]
            start = next((n + 1 for n, record in enumerate(found) if record["ref"] == inputs.get("after")), 0)
            page = found[start:start + 100]
            return {"records": page, "next": page[-1]["ref"] if len(page) == 100 else None}, []
        if operation == "create":
            if kind == "files":
                assert len(files) == 1 and inputs.get("path"), "a file is created from a path named in files"
                name = PurePosixPath(inputs["path"]).name
                wrong = self.unfit(resource, name, len(files[0]))
                if wrong:
                    raise Refusal("invalid", wrong)
                return {"ref": self.provider.create_file(resource["id"], name, files[0])}, []
            fields = await self.filled(resource, kind, inputs.get("fields") or {}, whole=True)
            keys, values = self.provider.split(resource, fields, oauth=bool(resource.get("oauth")))
            ref = self.provider.ref("secret" if kind == "secrets" else "data")
            kept[ref] = {"keys": keys, "values": values}
            return {"ref": ref}, []
        if not ref and (resource.get("binding") or {}).get("cardinality") == "one" and kept:
            ref = next(iter(kept))
        if ref not in kept:
            raise Refusal("not_found" if ref else "invalid", "No such record listing this agent." if ref
                          else "ref names the record.")
        if operation == "delete":
            del kept[ref]
            return {"deleted": True}, []
        if operation == "update":
            fields = await self.filled(resource, kind, inputs.get("fields") or {}, whole=False)
            keys, values = self.provider.split(resource, {k: v for k, v in fields.items() if v is not None},
                                               oauth=bool(resource.get("oauth")))
            kept[ref]["keys"].update(keys)
            kept[ref]["values"].update(values)
        if kind == "files":
            file = kept[ref]
            path = agent.handed(file["name"], file["content"], ref)
            return {"ref": ref, "name": file["name"], "type": file["type"], "size": file["size"], "path": path}, [path]
        fields = {**kept[ref]["keys"], **kept[ref]["values"]}
        if kind == "secrets":
            for host in agent.hosts:
                if isinstance(host, dict) and host["from_secret"].startswith(f"{resource['id']}."):
                    value = fields.get(host["from_secret"].split(".", 1)[1])
                    agent.secret_hosts.update([str(value).lower()] if value else [])
        return {"ref": ref, "fields": fields}, []

    async def filled(self, resource: dict[str, Any], kind: str, fields: dict[str, Any], whole: bool) -> dict:
        """A secret's fields, with those left empty asked of the person on its declared form: a secret field left out
        (on create) or set to null, a form's question with no value, an account to connect. Declined: refused."""
        if kind != "secrets":
            return fields
        empty = [f["name"] for f in resource.get("fields") or [] if (
            f["type"] == "secret" and fields.get(f["name"]) is None and (whole or f["name"] in fields)) or (
            f["type"] == "form" and isinstance(fields.get(f["name"]), dict)
            and any("value" not in q for q in fields[f["name"]].values()))]
        if not empty and not (whole and resource.get("oauth")):
            return fields
        given = await answered(self.sinks.form, resource["id"], {"fields": empty, "given": fields})
        if not given:
            raise Refusal("refused", "Its person declined.")
        return {**fields, **given}

    @staticmethod
    def listed(kind: str, ref: str, record: dict[str, Any]) -> dict[str, Any]:
        if kind == "files":
            return {"ref": ref, "name": record["name"], "type": record["type"], "size": record["size"]}
        return {"ref": ref, "fields": dict(record["keys"])}

    @staticmethod
    def unfit(resource: dict[str, Any], name: str, size: int) -> str:
        constraints = resource.get("constraints") or {}
        kind, kinds = file_type(name), constraints.get("mime_types")
        if kinds and not any(kind == k or (k.endswith("/*") and kind.startswith(k[:-1])) for k in kinds):
            return f"{resource['id']} takes {', '.join(kinds)}, not {kind} ({name})."
        if constraints.get("max_size_mb") and size > constraints["max_size_mb"] * 1024 * 1024:
            return f"{resource['id']} takes files up to {constraints['max_size_mb']} MB."
        return ""

    async def model(self, agent: Installed, called: Called, inputs: dict, files: list) -> tuple:
        messages = inputs.get("messages") or []
        assert all(m.get("role") in ("system", "user", "assistant") and isinstance(m.get("text"), str)
                   for m in messages), messages
        assert len(files) <= 16 and all(f.startswith(PICTURES) and len(f) <= 5 * 1024 * 1024 for f in files)
        if self.sinks.llm is None:
            raise Refusal("failed", "This chat has no model.")
        reply = await answered(self.sinks.llm, messages, inputs.get("max_tokens"), files)
        text, stop = reply if isinstance(reply, tuple) else (reply, "end")
        return {"text": str(text), "stop": stop}, []

    async def chat(self, agent: Installed, called: Called, inputs: dict, files: list) -> tuple:
        assert set(inputs) <= {"text", "table", "chart", "choices", "expects"}, inputs
        assert len(inputs.get("text") or "") <= 4000 and len(inputs.get("choices") or []) <= 8
        assert all(len(c) <= 100 for c in inputs.get("choices") or [])
        table = inputs.get("table")
        assert table is None or (len(table["columns"]) <= 24 and len(table["rows"]) <= 500
                                 and all(len(row) == len(table["columns"]) for row in table["rows"])), table
        expects = inputs.get("expects", "nothing")
        assert expects in ("nothing", "text", "choice", "file")
        if expects == "nothing":
            called.said += 1
            if called.said > 5:
                raise Refusal("refused", "A call says at most 5 things in the chat.")
            self.said.append(inputs)
            await answered(self.sinks.say, inputs)
            return {"answer": None}, []
        if not called.attended:
            return {"answer": None}, []
        answer = await answered(self.sinks.ask, inputs.get("text", ""), inputs.get("choices") or [], expects)
        if expects == "file" and answer:
            path = agent.handed(self.files[answer]["name"], self.files[answer]["content"], answer)
            return {"answer": path}, [path]
        return {"answer": answer}, []

    async def open(self, agent: Installed, called: Called, inputs: dict, files: list) -> tuple:
        hosts = [str(h).lower() for h in inputs.get("hosts") or []]
        if not hosts:
            raise Refusal("invalid", "Name the hosts to open.")
        if not called.attended:
            raise Refusal("refused", "Nobody is there to approve this.")
        if not await answered(self.sinks.open, hosts, inputs.get("purpose", ""), inputs.get("code")):
            raise Refusal("refused", "Its person declined.")
        called.opened.update(hosts)
        return {"hosts": hosts}, []


__all__ = ["Agents", "Chat", "ChatSinks", "Installed", "Place", "REPOSITORY", "ROOT", "Records", "Refusal", "Way",
           "probe", "requirements", "run", "stands_for", "way_out"]
