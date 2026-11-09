"""Connections out of the sandbox, whose one way out is a proxy at the address its environment names (sandbox.md §7).
A library that speaks HTTP takes the proxy from the environment; a protocol that isn't the web's opens a tunnel."""

from __future__ import annotations

import os
import re
import socket
from urllib.parse import urlsplit


class TunnelRefused(ConnectionError):
    """The way out would not open the connection: its status, and its reason in its own words."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason or f"The way out answered {status}.")
        self.status, self.reason = status, reason


class Tunnel:
    """A socket to a host and port, through the way out (HTTP CONNECT); straight where there is none."""

    HEAD_MOST = 16 * 1024
    ANSWER = re.compile(r"^HTTP/\d\.\d (\d{3}) ?(.*)$")

    @classmethod
    def open(cls, host: str, port: int, timeout: float | None = 30) -> socket.socket:
        proxy = (os.environ.get("HTTPS_PROXY") or "").strip()
        if not proxy:
            return socket.create_connection((host, int(port)), timeout)
        where, target = urlsplit(proxy), f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        try:
            request = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode("latin-1")
        except UnicodeEncodeError:
            raise OSError(f"'{host}' is not a host name that can be connected to.") from None
        link = socket.create_connection((where.hostname, where.port), timeout)
        try:
            link.sendall(request)
            head = b""
            while not head.endswith(b"\r\n\r\n") and len(head) <= cls.HEAD_MOST:
                byte = link.recv(1)
                if not byte:
                    break
                head += byte
        except BaseException:
            link.close()
            raise
        answer = cls.ANSWER.match(head.split(b"\r\n", 1)[0].decode("latin-1", "replace"))
        if answer is None or answer.group(1) != "200":
            raise TunnelRefused(int(answer.group(1)) if answer else 502, cls.reason(link) or (
                answer.group(2).strip() if answer else "The way out gave no answer that can be read."))
        return link

    @staticmethod
    def reason(link: socket.socket) -> str:
        """A refusal's reason where the way out writes it after its answer's head: its first line; else ""."""
        body = b""
        try:
            link.settimeout(2)
            while len(body) < 2000 and (piece := link.recv(2000)):
                body += piece
        except OSError:
            pass
        finally:
            link.close()
        return (body.decode("utf-8", "replace").strip().splitlines() or [""])[0]


class BaseAddress:
    """An API's base address a secret names, so an agent's own tests can point it at a stand-in."""

    #: Set by the agents' tests; a deployment never sets it.
    UNDER_TEST = "AGENTS_UNDER_TEST"

    @classmethod
    def given(cls, value: object) -> str:
        """``value`` when it is https, or http under test; otherwise "", and the agent uses its own address."""
        value = str(value or "").strip()
        scheme = urlsplit(value).scheme.lower()
        return value if scheme == "https" or (scheme == "http" and os.environ.get(cls.UNDER_TEST)) else ""
