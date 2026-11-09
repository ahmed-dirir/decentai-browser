"""Every manifest passes the platform's own checks, as an install runs them (its api_server/src/agents/manifest.py,
from the platform's checkout beside this repository); and catalog/catalog.json is schema 3, offers every agent at
its folder's version and manifest, and names files that are the bytes it says (each at its version's release, as
built in catalog/), each program a zip with `start` at its root."""

import hashlib
import importlib.util
import json
import zipfile

import pytest
import yaml

from publish.build import Catalog
from tests.harness import REPOSITORY, ROOT

CATALOG = REPOSITORY / "catalog"
FOLDERS = sorted(path for path in ROOT.iterdir() if (path / "manifest.yaml").is_file())


@pytest.fixture(scope="module")
def standard():
    """The platform's Standard, loaded by its file's path."""
    spec = importlib.util.spec_from_file_location(
        "platform_manifest", REPOSITORY.parent / "decentai" / "api_server" / "src" / "agents" / "manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Standard


def manifest(folder):
    return yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))


def built(entry, file):
    """The file a version's release offers, as the build left it in catalog/<id>/<version>/."""
    agent = entry["manifest"]["agent"]
    name = file["url"].rsplit("/", 1)[1]
    assert file["url"] == f"{Catalog.RELEASES}/v{agent['version']}/{name}"
    return CATALOG / agent["id"] / agent["version"] / name


@pytest.mark.parametrize("folder", FOLDERS, ids=[folder.name for folder in FOLDERS])
def test_the_manifest_passes_the_platforms_checks(standard, folder):
    found = manifest(folder)
    assert found["agent"]["id"] == folder.name
    assert standard.manifest_errors(found) == []
    for function in found["functions"]:
        if function.get("schedulable"):
            assert function["level"] <= 1 and "model" not in (function.get("calls") or []), function["id"]


def test_the_catalog_offers_every_agent_as_its_folder_says(standard):
    catalog = json.loads((CATALOG / "catalog.json").read_text(encoding="utf-8"))
    assert standard.catalog_errors(catalog) == []
    offered = {entry["manifest"]["agent"]["id"]: entry for entry in catalog["agents"]}
    assert sorted(offered) == [folder.name for folder in FOLDERS]
    for folder in FOLDERS:
        entry = offered[folder.name]
        assert entry["manifest"] == manifest(folder) and entry["base"] == "python-3.12"
        assert set(entry["programs"]) == {"x86_64", "aarch64"}
        for file in [*entry["programs"].values(), *([entry["samples"]] if "samples" in entry else [])]:
            body = built(entry, file).read_bytes()
            assert (len(body), "sha256:" + hashlib.sha256(body).hexdigest()) == (file["size"], file["digest"])
        with zipfile.ZipFile(built(entry, entry["programs"]["x86_64"])) as program:
            assert json.loads(program.read("manifest.json")) == entry["manifest"]
            assert program.getinfo("start").external_attr >> 16 & 0o100
