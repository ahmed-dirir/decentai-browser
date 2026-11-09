#!/bin/sh
# The Chromium the browser agent's program carries, for the machine this runs on: Playwright's own build, the system
# libraries it loads that the sandbox's base (python:3.12-slim) lacks, and fonts. Run in python:3.12-slim of that
# machine, with this repository at /src (publish/build.sh does):
#
#   sh /src/publish/chromium.sh <out>      -> <out>/chromium/{browsers,lib,fonts,fonts.conf}
set -e
out="$1/chromium"
find / -xdev -name '*.so*' 2>/dev/null | xargs -n1 basename | sort -u > /tmp/base
pip install -q --root-user-action=ignore "$(grep '^playwright==' /src/agents/browser/requirements.txt)"
PLAYWRIGHT_BROWSERS_PATH="$out/browsers" python -m playwright install --with-deps --no-shell chromium > /dev/null
rm -rf "$out"/browsers/ffmpeg-* "$out/browsers/.links"
chrome=$(find "$out/browsers" -type f -name chrome | head -1)
mkdir -p "$out/lib" "$out/fonts"
# Every library Chromium and what it carries beside it link to, outside the base; and NSS's modules, loaded by name.
modules=$(find /usr/lib -name libsoftokn3.so -o -name 'libfreebl*.so' -o -name libnssckbi.so)
for file in "$chrome" $(find "$(dirname "$chrome")" -name '*.so*') $modules; do
    ldd "$file" 2>/dev/null | awk '/=> \// {print $3}'
done | sort -u | while read -r library; do
    grep -qx "$(basename "$library")" /tmp/base || cp -L "$library" "$out/lib/"
done
cp -L $modules "$out/lib/"
find /usr/share/fonts -name 'Liberation*.ttf' -o -name 'DejaVu*.ttf' | xargs -I{} cp -L {} "$out/fonts/"
cat > "$out/fonts.conf" <<'EOF'
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir prefix="relative">fonts</dir>
  <cachedir>/tmp/fontconfig</cachedir>
</fontconfig>
EOF
du -sh "$out"
