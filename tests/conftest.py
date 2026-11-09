import os
import sys

import pytest

from standard.net import BaseAddress
from tests.harness import ROOT, Agents, Installed, Place, stands_for  # noqa: F401

# The stubs serve plain http under the services' own names: a secret's base address may be one, here alone.
os.environ[BaseAddress.UNDER_TEST] = "1"
# The platform's checks are loaded from its checkout, which the tests never write in.
sys.dont_write_bytecode = True


@pytest.fixture
def agents():
    """Every agent, by its own id: each a program of its own, started at its first call, as a chat starts one."""
    return Agents()


@pytest.fixture(autouse=True)
def _place():
    """Each test runs on one loop, with a way out of its own; its programs end with it."""
    place = Place()
    yield place
    place.end()


def resource(manifest: dict, kind: str, resource_id: str) -> dict:
    """One declared resource of the manifest's, by kind and id."""
    return next(r for r in manifest["resources"][kind] if r["id"] == resource_id)


def declaring(agent: Installed, hosts) -> Installed:
    """The agent as if its manifest declared these hosts, where a test's stub is not where the real service is."""
    return agent.declaring(hosts)
