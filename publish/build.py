"""The catalog a deployment reads, built from the agents' folders (the platform's documentation/agents.md §10):

    python -m publish.build --out catalog [agent ...]

Each agent's program, for each machine, is a zip the sandbox runs on its base python-3.12: ``start`` at its root,
the manifest as JSON, this repository's ``standard`` library, the agent's code under ``agents/<id>/``, the wheels
its ``requirements.txt`` pins unpacked under ``site/``, a native one of that machine's, and what ``--carried`` holds
for it and that machine (the browser's Chromium: publish/chromium.sh). The same files make the same bytes, so a
version once published is never different: a build that would change one refuses.

Run it in Docker from a clean export (publish/build.sh). It needs PyYAML, pip and the network to fetch wheels.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

import yaml

REPOSITORY = Path(__file__).resolve().parents[1]
MACHINES = ("x86_64", "aarch64")
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
TEXT = (".py", ".yaml", ".yml", ".json", ".md", ".txt", ".html", ".css", ".js", ".csv", ".toml", ".cfg")
START = b'''#!/usr/local/bin/python3 -I
"""An agent's program, as the sandbox starts it: its code, the standard library and its dependencies beside this."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path[0:0] = [str(ROOT / "site"), str(ROOT)]

from standard import Program  # noqa: E402

Program(json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))).run()
'''


class BuildError(RuntimeError):
    """What stops a build, for whoever runs it."""


class Wheels:
    """An agent's pinned dependencies, as wheels for Python 3.12 on a glibc machine: every one pinned, and nothing
    unpinned pulled in."""

    PLATFORMS = [f"manylinux_2_{minor}" for minor in range(17, 42)] + ["manylinux2014", "manylinux2010", "manylinux1"]

    @classmethod
    def fetched(cls, requirements: Path, machine: str, into: Path) -> list[Path]:
        pins = [line.strip() for line in requirements.read_text().splitlines() if line.strip()]
        if not pins:
            return []
        command = [sys.executable, "-m", "pip", "download", "--quiet", "--no-input", "--disable-pip-version-check",
                   "--only-binary=:all:", "--python-version=3.12", "--implementation=cp", "--abi=cp312",
                   *[f"--platform={p}_{machine}" for p in cls.PLATFORMS], f"--dest={into}", "-r", str(requirements)]
        done = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=900)
        if done.returncode != 0:
            raise BuildError(f"{requirements}: no wheels for {machine}: {done.stderr.strip()[-500:]}")
        wheels = sorted(into.glob("*.whl"))
        named = {wheel.name.split("-")[0].lower().replace("_", "-") for wheel in wheels}
        pinned = {pin.split("==")[0].strip().lower().replace("_", "-") for pin in pins}
        if named != pinned:
            raise BuildError(f"{requirements}: pin these too, or drop them: {sorted(named ^ pinned)}")
        return wheels

    @staticmethod
    def unpacked(wheel: Path):
        """A wheel's files as installed, each with whether it runs: ``.data`` purelib and platlib at the top."""
        with zipfile.ZipFile(wheel) as unpacking:
            for item in unpacking.infolist():
                path = PurePosixPath(item.filename)
                if item.filename.endswith("/") or path.is_absolute() or ".." in path.parts:
                    continue
                if path.parts[0].endswith(".data"):
                    if len(path.parts) < 3 or path.parts[1] not in ("purelib", "platlib"):
                        continue
                    path = PurePosixPath(*path.parts[2:])
                yield path.as_posix(), unpacking.read(item), bool(item.external_attr >> 16 & 0o100)


def zipped(entries: dict[str, tuple[bytes, bool]]) -> bytes:
    """The same entries make the same bytes: sorted, at one fixed time, Unix modes."""
    written = io.BytesIO()
    with zipfile.ZipFile(written, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(entries):
            data, runs = entries[name]
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.create_system = 3
            info.external_attr = (0o100755 if runs else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return written.getvalue()


def read(path: Path) -> bytes:
    """A file's bytes; a text file's with Unix line endings, wherever it was checked out."""
    data = path.read_bytes()
    return data.replace(b"\r\n", b"\n") if path.suffix.lower() in TEXT else data


def files(folder: Path, leave_out: set[str]) -> list[Path]:
    found = []
    for path in sorted(folder.rglob("*")):
        parts = path.relative_to(folder).parts
        if path.is_symlink():
            raise BuildError(f"{path} is a link: a program holds plain files only.")
        if path.is_file() and not leave_out.intersection(parts) and "__pycache__" not in parts \
                and path.suffix not in (".pyc", ".pyo"):
            found.append(path)
    return found


class Agent:
    """One agent's folder: its manifest, its program for each machine, its samples and its readme."""

    #: What describes the code or travels beside it, never in the program.
    BESIDE = {"manifest.yaml", "README.md", "requirements.txt", "samples.yaml", "samples", "tests"}

    def __init__(self, folder: Path):
        self.folder = folder
        self.manifest = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
        self.id, self.version = self.manifest["agent"]["id"], self.manifest["agent"]["version"]
        if self.id != folder.name:
            raise BuildError(f"{folder} holds the agent {self.id}: a folder is named for its agent.")

    def program(self, machine: str, scratch: Path, carried: Path | None) -> bytes:
        entries = {"start": (START, True),
                   "manifest.json": (json.dumps(self.manifest, ensure_ascii=False).encode(), False)}
        for path in files(REPOSITORY / "standard", set()):
            entries[f"standard/{path.relative_to(REPOSITORY / 'standard').as_posix()}"] = (read(path), False)
        for path in files(self.folder, self.BESIDE):
            entries[f"agents/{self.id}/{path.relative_to(self.folder).as_posix()}"] = (read(path), False)
        if (self.folder / "requirements.txt").is_file():
            for wheel in Wheels.fetched(self.folder / "requirements.txt", machine, scratch / machine):
                for name, data, runs in Wheels.unpacked(wheel):
                    entries.setdefault(f"site/{name}", (data, runs))
        beside = carried / self.id / machine if carried else None
        if beside is not None and beside.is_dir():
            for path in files(beside, set()):
                entries[path.relative_to(beside).as_posix()] = (path.read_bytes(), bool(path.stat().st_mode & 0o100))
        return zipped(entries)

    def samples(self) -> bytes | None:
        """samples.yaml and the files under samples/, zipped."""
        if not (self.folder / "samples.yaml").is_file():
            return None
        found = [self.folder / "samples.yaml", *files(self.folder / "samples", set())] \
            if (self.folder / "samples").is_dir() else [self.folder / "samples.yaml"]
        return zipped({path.relative_to(self.folder).as_posix(): (read(path), False) for path in found})

    @property
    def readme(self) -> str:
        found = self.folder / "README.md"
        return found.read_text(encoding="utf-8")[:20000] if found.is_file() else ""


class Catalog:
    """catalog.json, and each version's files at <id>/<version>/, kept for good: uploaded as its GitHub release
    v<version>, since git takes no file over 100 MB, and named in catalog.json by the release's address."""

    ID, NAME = "decentai_browser", "DecentAI Browser"
    RELEASES = "https://github.com/ahmed-dirir/decentai-browser/releases/download"

    def __init__(self, out: Path, carried: Path | None):
        self.out, self.carried = out, carried
        self.entries: list[dict] = []

    def add(self, agent: Agent) -> None:
        with tempfile.TemporaryDirectory(prefix="build-") as scratch:
            programs = {machine: self.kept(agent, f"{machine}.zip",
                                           agent.program(machine, Path(scratch), self.carried))
                        for machine in MACHINES}
        entry = {"manifest": agent.manifest, "programs": programs, "base": "python-3.12"}
        samples = agent.samples()
        if samples is not None:
            entry["samples"] = self.kept(agent, "samples.zip", samples)
        if agent.readme:
            entry["readme"] = agent.readme
        self.entries.append(entry)

    def kept(self, agent: Agent, name: str, body: bytes) -> dict:
        url = f"{agent.id}/{agent.version}/{name}"
        path = self.out / url
        if path.exists() and path.read_bytes() != body:
            raise BuildError(f"{url} is published already with other bytes: raise {agent.id}'s version.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return {"url": f"{self.RELEASES}/v{agent.version}/{name}",
                "digest": f"sha256:{hashlib.sha256(body).hexdigest()}", "size": len(body)}

    def write(self, keep: list[dict]) -> Path:
        """catalog.json: these entries, and those kept of agents not built this time."""
        built = {entry["manifest"]["agent"]["id"] for entry in self.entries}
        entries = sorted([*self.entries, *[e for e in keep if e["manifest"]["agent"]["id"] not in built]],
                         key=lambda e: e["manifest"]["agent"]["id"])
        path = self.out / "catalog.json"
        path.write_text(json.dumps({"schema": 3, "catalog": {"id": self.ID, "name": self.NAME}, "agents": entries},
                                   indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m publish.build", description=__doc__.split("\n\n")[0])
    parser.add_argument("agents", nargs="*", help="agents to build; every one when none is named")
    parser.add_argument("--out", default=str(REPOSITORY / "catalog"))
    parser.add_argument("--carried", help="a folder of <agent>/<machine>/ trees each program carries at its root")
    asked = parser.parse_args(argv)
    out = Path(asked.out)
    folders = sorted(p for p in (REPOSITORY / "agents").iterdir() if (p / "manifest.yaml").is_file())
    folders = [f for f in folders if not asked.agents or f.name in asked.agents]
    previous = json.loads((out / "catalog.json").read_text(encoding="utf-8")) if (out / "catalog.json").exists() else {}
    keep = previous.get("agents", []) if previous.get("schema") == 3 and asked.agents else []
    catalog = Catalog(out, Path(asked.carried) if asked.carried else None)
    try:
        for folder in folders:
            catalog.add(Agent(folder))
            print(f"{folder.name} {catalog.entries[-1]['manifest']['agent']['version']}", flush=True)
    except BuildError as failed:
        print(f"Not built: {failed}", file=sys.stderr)
        return 1
    print(catalog.write(keep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
