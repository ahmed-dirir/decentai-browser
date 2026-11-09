"""The Browser agent behind the sandbox's way out.

In the sandbox a program can neither look a name up nor open a
connection of its own: its one way out is a proxy, named in
HTTPS_PROXY, with no pass. The browser is handed that proxy, and a name
is the way out's to look up and to refuse.

The way out here is the tests' own, one per program; its lookup is a
table, so the shop has a name and another name resolves inside.
"""

import pytest


from tests.harness import run, way_out
from tests.conftest import declaring
from tests.test_browser import browse, make, shop  # noqa: F401

INSIDE = "inside.sidra.example"


def port_of(shop):
    return shop.url.rsplit(":", 1)[1]


@pytest.fixture
def proxy(agents, shop, monkeypatch):
    """The test's own way out, with a lookup table of its own;
    ``admitted`` runs the agent as if it declared these hosts and no
    others."""
    found = way_out()
    found.names = {INSIDE: ["10.0.0.5"]}
    found.admitted = lambda hosts: monkeypatch.setitem(
        agents, "browser", declaring(agents["browser"], hosts))
    return found


class TestThePolicy:
    """What is checked without asking anybody, and what is left to the
    way out."""

    def policy(self, monkeypatch, proxy="http://127.0.0.1:3128"):
        from agents.browser.tools.policy import AddressPolicy

        monkeypatch.setenv("HTTPS_PROXY", proxy)
        monkeypatch.delenv("DECENTAI_WEB_ALLOW_LOOPBACK", raising=False)
        return AddressPolicy()

    def test_a_name_is_left_to_the_proxy(self, monkeypatch):
        policy = self.policy(monkeypatch)
        assert policy.refusal("https://a-name-that-is-nowhere.example/") is None

    @pytest.mark.parametrize("url", [
        "http://10.0.0.5/admin", "http://169.254.169.254/latest/meta-data",
        "http://127.0.0.1:8001/internal", "http://[::1]/", "http://192.168.1.1/",
    ])
    def test_an_address_written_as_one_is_refused_here(self, monkeypatch, url):
        assert self.policy(monkeypatch).refusal(url)

    def test_a_scheme_that_is_not_the_webs_is_refused_here(self, monkeypatch):
        assert "only http and https" in self.policy(monkeypatch).refusal("file:///etc/passwd")

    def test_the_browser_is_given_the_proxy_and_no_way_around_it(self, monkeypatch):
        assert self.policy(monkeypatch).for_the_browser() == {
            "server": "http://127.0.0.1:3128", "bypass": "<-loopback>"}

    def test_without_a_proxy_the_browser_is_given_none(self, monkeypatch):
        from agents.browser.tools.policy import AddressPolicy

        monkeypatch.delenv("HTTPS_PROXY", raising=False)
        assert AddressPolicy().for_the_browser() is None


class TestTheBrowser:
    def test_a_page_is_reached_through_it_by_name(self, agents, shop, proxy):
        proxy.admitted("any")
        executor, provider, _, _ = make(shop)
        result, status = run(executor.invoke(
            agents["browser"], "browse_screenshot",
            {"url": f"http://shop.sidra.example:{port_of(shop)}/"},
            chat_level=2))
        assert status == "success", result
        assert result["title"] == "Sidra Fitness" and result["file_ref"]
        assert proxy.refusals == 0

    def test_it_is_the_way_out_and_not_a_way_beside_one(self, agents, shop, proxy):
        """The shop by its own address, and a way out that opens
        nothing: had the browser gone around it, it would have arrived.
        The refusal is the way out's, and said with its reason."""
        proxy.admitted([])
        executor, provider, _, _ = make(shop)
        result, status = run(executor.invoke(
            agents["browser"], "browse_screenshot", {"url": shop.url},
            chat_level=2))
        assert status == "error" and "cannot be visited" in result["error"], result
        assert "not a host this agent declared" in result["error"]
        assert proxy.refusals >= 1

    def test_a_name_that_resolves_inside_is_refused_and_said(self, agents, shop, proxy):
        proxy.admitted("any")
        executor, provider, mind, person = make(shop)
        result, status = browse(agents, executor, "Read the admin page",
                                f"http://{INSIDE}/admin", max_steps=2)
        assert status == "success" and result["outcome"] == "failed", result
        assert "not a public address" in result["summary"], result

    def test_an_address_written_as_one_never_reaches_the_proxy(self, agents, shop, proxy):
        proxy.admitted("any")
        executor, provider, mind, person = make(shop)
        result, status = browse(agents, executor, "Read the instance metadata",
                                "http://169.254.169.254/latest/meta-data", max_steps=2)
        assert status == "success" and result["outcome"] == "failed", result
        assert "private network address" in result["summary"]
        assert proxy.refusals == 0
