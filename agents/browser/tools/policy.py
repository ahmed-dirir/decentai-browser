"""Where the browser may go.

A browser on the runtime host can reach whatever the host can: a
metadata service, a database on the private network, the platform
itself. So every navigation and every request the page makes is checked
against its address, and one to a loopback or private address is
refused. ``DECENTAI_WEB_ALLOW_LOOPBACK=1`` is the tests' escape — a
loopback site and nothing else — the same variable the web agents use.

BEHIND THE SANDBOX'S WAY OUT. In the sandbox a program cannot look a
name up or open a connection of its own: its one way out is a proxy
named in ``HTTPS_PROXY``. The browser is handed that proxy, and the way
out makes the check made here — every address a name resolves to, then
a connection to the address it checked. So a name is left to it, and
what is checked here is what can be checked without asking anybody: the
scheme, and an address written as an address.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Dict, Optional
from urllib.parse import urlsplit

LOOPBACK_VARIABLE = "DECENTAI_WEB_ALLOW_LOOPBACK"
#: The sandbox's way out, where there is one.
PROXY_VARIABLE = "HTTPS_PROXY"


class AddressPolicy:
    SCHEMES = ("http", "https")

    def __init__(self):
        self._cache: Dict[str, Optional[str]] = {}
        self.allow_loopback = os.environ.get(LOOPBACK_VARIABLE) == "1"
        self.proxy = os.environ.get(PROXY_VARIABLE) or ""

    def for_the_browser(self) -> Optional[Dict[str, str]]:
        """The way out as a browser is given a proxy, or None where
        there is none. Nothing goes around it: a browser left to itself
        reaches this machine's own addresses directly, and behind the
        way out that is the way out's to refuse."""
        if not self.proxy:
            return None
        parts = urlsplit(self.proxy)
        if not parts.hostname or not parts.port:
            return None
        return {"server": f"{parts.scheme or 'http'}://{parts.hostname}:{parts.port}", "bypass": "<-loopback>"}

    def refusal(self, url: str) -> Optional[str]:
        """Why this address may not be visited, or None."""
        parts = urlsplit(str(url or ""))
        if parts.scheme not in self.SCHEMES:
            return f"only http and https addresses can be visited, not {parts.scheme or 'none'!r}"
        host = (parts.hostname or "").lower()
        if not host:
            return "the address names no host"
        if host in self._cache:
            return self._cache[host]
        why = self._check(host)
        self._cache[host] = why
        return why

    def _check(self, host: str) -> Optional[str]:
        if self.proxy:
            # A name is the proxy's to look up and to refuse. An
            # address written as one needs no looking up.
            try:
                ipaddress.ip_address(host.strip("[]"))
            except ValueError:
                return None
            addresses = [host.strip("[]")]
        else:
            try:
                infos = socket.getaddrinfo(host, None)
            except socket.gaierror:
                return f"{host} does not resolve"
            addresses = [str(info[4][0]).split("%", 1)[0] for info in infos]
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if ip.is_loopback:
                if self.allow_loopback:
                    continue
                return f"{host} is a loopback address (this machine)"
            # The escape permits loopback and nothing else: a link-local
            # address (a cloud metadata service) or a private range is
            # refused whatever the environment says.
            if ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
                return f"{host} is a private network address"
        return None


def domain_of(url: str) -> str:
    """The host a login, a session and a site's notes are kept under:
    the page's host, lowercase, with a non-standard port kept."""
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        return f"{host}:{parts.port}"
    return host


def same_site(one: str, other: str) -> bool:
    """Whether two addresses are of one site: the same host, or hosts
    under the same name — ``x.example.com`` and ``api.example.com``.
    Under a country's ending with a short name before it (``co.uk``),
    three labels make the site rather than two; a wrong guess there
    refuses a read that would have been allowed, never the reverse."""
    hosts = [(urlsplit(str(u or "")).hostname or "").lower() for u in (one, other)]
    if not all(hosts):
        return False
    if hosts[0] == hosts[1]:
        return True
    names = []
    for host in hosts:
        labels = host.split(".")
        if len(labels) < 2 or labels[-1].isdigit():
            return False  # an address, or a bare name: itself only
        short = len(labels[-1]) == 2 and len(labels[-2]) <= 3
        kept = 3 if len(labels) > 2 and short else 2
        names.append(".".join(labels[-kept:]))
    return names[0] == names[1]
