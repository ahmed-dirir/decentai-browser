"""An agent's program: the standard's messages on its standard input and output. It says ready, runs each call of
the runtime's as a task on one loop of the agent's own, answers a cancel at once, and hands each answer to its own
calls back to the one waiting (agents.md §4, §9)."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import itertools
import json
import logging
import os
import sys
import threading
from concurrent.futures import Future
from typing import Any

from .agent import AgentBase, ResourceDenied
from .call import Call

log = logging.getLogger("standard")


class Program:
    LINE_MOST = 1024 * 1024

    def __init__(self, manifest: dict[str, Any]):
        self.manifest = manifest
        self.functions = {f["id"]: f for f in manifest["functions"]}
        self.out, sys.stdout = sys.stdout, sys.stderr  # standard output carries messages only
        self.writing = threading.Lock()
        self.ids = itertools.count(1)
        #: Calls of the runtime's in flight, by _id: (call, its task). Its own calls waiting, by _id.
        self.running: dict[str, tuple[Call, Future]] = {}
        self.waiting: dict[str, asyncio.Future] = {}
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="agent").start()

    def run(self) -> None:
        """Ready, then every message until standard input closes."""
        logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
        try:
            self.agent = self.made()
        except Exception as failed:
            self.send("error", {"message": f"The agent could not start: {failed}"[:1000]})
            raise SystemExit(1)
        self.send("ready", {"agent": self.manifest["agent"]["id"], "version": self.manifest["agent"]["version"],
                            "functions": list(self.functions)})
        for line in sys.stdin:
            message = json.loads(line)
            body = message.get("body") or {}
            self.heard(message.get("_id"), body.get("type"), body.get("data") or {})
        self.ended()

    def made(self) -> AgentBase:
        """The agent of agents/<id>/agent.py, each function of the manifest found among its tools' methods."""
        module = importlib.import_module(f"agents.{self.manifest['agent']['id']}.agent")
        found = [kind for kind in vars(module).values() if isinstance(kind, type) and issubclass(kind, AgentBase)
                 and kind.__module__ == module.__name__]
        agent = found[0]()
        missing = [function for function in self.functions if agent.function(function) is None]
        if missing:
            raise LookupError(f"no method for {', '.join(missing)}")
        return agent

    def heard(self, message_id: str, kind: str, data: dict[str, Any]) -> None:
        if kind == "call" and data.get("function") in self.functions:
            call = Call(self, message_id, data["function"], self.defaulted(data["function"], data.get("inputs") or {}))
            self.running[message_id] = (call, asyncio.run_coroutine_threadsafe(self.served(call), self.loop))
        elif kind == "cancel" and data.get("call") in self.running:
            call, task = self.running[data["call"]]
            self.answer(call, "error", {"code": "cancelled", "message": "Cancelled."})
            task.cancel()
        elif kind in ("result", "error") and data.get("call") in self.waiting:
            waiting = self.waiting.pop(data["call"])
            self.loop.call_soon_threadsafe(lambda: waiting.done() or waiting.set_result((kind, data)))
        elif kind == "note" and data.get("call") in self.running and isinstance(data.get("input"), list):
            self.loop.call_soon_threadsafe(self.running[data["call"]][0].screen.receive, data["input"])

    def defaulted(self, function: str, inputs: dict[str, Any]) -> dict[str, Any]:
        """The inputs, with the defaults the manifest gives those left out."""
        properties = (self.functions[function].get("inputs") or {}).get("properties") or {}
        return {**{name: p["default"] for name, p in properties.items() if isinstance(p, dict) and "default" in p},
                **inputs}

    async def served(self, call: Call) -> None:
        """A function run: its output and status, or what it raised, as the call's answer."""
        try:
            found = self.agent.function(call.function)(call)
            found = await found if inspect.isawaitable(found) else found
            output, status = found if isinstance(found, tuple) else (found, "success")
            if status == "success":
                self.answer(call, "result", {"output": output}, call.files)
            else:
                output = output if isinstance(output, dict) else {"error": str(output)}
                extra = {k: v for k, v in output.items() if k not in ("error", "kind")}
                words = str(output.get("error") or "It failed.") + (f" {json.dumps(extra)}" if extra else "")
                self.answer(call, "error", {**({"code": output["kind"]} if output.get("kind") else {}),
                                            "message": words[:1000]})
        except asyncio.CancelledError:
            pass
        except Exception as failed:
            log.exception(f"{call.function} failed")
            self.answer(call, "error", {**({"code": failed.code} if getattr(failed, "code", "") else {}),
                                        "message": (str(failed) or type(failed).__name__)[:1000]})

    def answer(self, call: Call, kind: str, data: dict[str, Any], files: list[str] | None = None) -> None:
        """A call's one answer; one too large for a line is an error saying so."""
        if self.running.pop(call.id, None) is None:
            return
        try:
            self.send(kind, {"call": call.id, **data}, files)
        except (TypeError, ValueError) as refused:
            self.send("error", {"call": call.id, "message": f"The answer can't be sent: {refused}"[:1000]})

    async def ask(self, within: str, function: str, inputs: dict[str, Any], files: list[str] | None) -> dict:
        """A call of its own to the runtime, within one of the runtime's: its output, or ResourceDenied."""
        message_id = f"p{next(self.ids)}"
        self.waiting[message_id] = answered = self.loop.create_future()
        self.send("call", {"within": within, "function": function, "inputs": inputs}, files, message_id)
        try:
            kind, data = await answered
        except asyncio.CancelledError:
            if self.waiting.pop(message_id, None) is not None:
                self.send("cancel", {"call": message_id})
            raise
        if kind == "error":
            raise ResourceDenied(str(data.get("message") or f"{function} failed."), str(data.get("code") or ""))
        return data.get("output") or {}

    def send(self, kind: str, data: dict[str, Any], files: list[str] | None = None, message_id: str = "") -> None:
        message = {"_id": message_id or f"p{next(self.ids)}", "body": {"type": kind, "data": data},
                   **({"files": files} if files else {})}
        line = json.dumps(message, separators=(",", ":")) + "\n"
        if len(line.encode()) > self.LINE_MOST:
            raise ValueError(f"The answer is {len(line.encode())} bytes, more than a message carries: return less, "
                             f"or return it as a file.")
        with self.writing:
            self.out.write(line)
            self.out.flush()

    def ended(self) -> None:
        """Standard input closed: the calls in flight stopped, the agent closed, and the program gone."""
        for _, task in list(self.running.values()):
            task.cancel()
        try:
            asyncio.run_coroutine_threadsafe(self.agent.close(), self.loop).result(3)
        except Exception:
            pass
        os._exit(0)
