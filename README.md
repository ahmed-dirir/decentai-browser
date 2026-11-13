# DecentAI Browser

The browser agent for DecentAI, in a catalog of its own: a real web browser, driven by the chat's model, on any
site. What it does and how it keeps you in charge: [agents/browser/README.md](agents/browser/README.md). It is
written to the platform's agent standard (`documentation/agents.md` in
[the platform](https://github.com/decentai-io/decentai-engine)), with the same `standard` library and build as
[DecentAI Agents](https://github.com/decentai-io/decentai-official-agents).

## Install it

In DecentAI, an administrator adds this catalog's address as an agent source, with no credential:
`https://raw.githubusercontent.com/decentai-io/decentai-browser-agent/main/catalog/catalog.json`. It names each
version's programs at that version's GitHub release, and the deployment checks each by its digest.

## Build and release

```bash
publish/build.sh
gh release create v<version> catalog/browser/<version>/*.zip --title "Browser <version>"
```

Chromium, its libraries and fonts are made first for each machine (`publish/chromium.sh`, aarch64 under
emulation), then the program: the agent's code, the `standard` library, its pinned wheels and that Chromium, one
zip per machine under `catalog/browser/<version>/`. Each is over 200 MB, past git's 100 MB limit on GitHub, so the
zips stay out of git and are uploaded as the version's release; commit `catalog/catalog.json` once the release is
up. A version once published never changes: raise it for any change. In Git Bash on Windows, set
`MSYS_NO_PATHCONV=1` first.

## Run the tests

The browser runs as its own program against a loopback shop, a real Chromium, the chat's model scripted
([`tests/harness.py`](tests/harness.py)). The manifest is checked with the platform's own checks, from a checkout
of the platform beside this one (as `decentai-engine`, or `decentai`).

```bash
docker run -d --name browser-test -v "$PWD:/w" python:3.12-slim sleep infinity
docker exec browser-test sh -c '
  pip install -r /w/decentai-browser-agent/tests/requirements.txt -r /w/decentai-browser-agent/agents/browser/requirements.txt
  python -m playwright install --with-deps --no-shell chromium
  cd /w/decentai-browser-agent && python -m pytest tests -q -n 6 --timeout=300'
```
