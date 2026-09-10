#!/usr/bin/env bash
#
# Build the .mcpb bundle for MCP hosts and directories that distribute a local
# stdio server as a downloadable artifact (Claude Desktop, Smithery).
#
# The bundle is flat on purpose. manifest.json must sit at the bundle root, and
# two assertions in the suite read files *beside* the server: README.md (the
# variables the docs tell you to export are the ones the code reads) and
# manifest.json (the host contract agrees with the code). Put the code in a
# server/ subdirectory and those checks do not fail - they silently skip, and
# mutation_test.sh cannot even copy its inputs, so `bash mutation_test.sh`
# reports 23 x "cp failed" while looking like a run. A flat bundle keeps every
# claim in manifest.json runnable by whoever downloads it.
#
# The gate below is the point: this script refuses to emit a bundle whose own
# long_description overstates it. The numbers are read out of the manifest, not
# hardcoded here, so they cannot drift apart.
#
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-$here/shop-mcp.mcpb}"
py="${PYTHON:-python3}"

# Exactly what ships. The gates below execute inside $work, and running the
# suite there leaves __pycache__ behind - packing the directory wholesale would
# ship the build's own leftovers, so the bundle is written from this list only.
files=(shop_mcp.py self_test.py mutation_test.sh README.md manifest.json LICENSE)

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

for f in "${files[@]}"; do cp "$here/$f" "$work/$f"; done

# What the manifest promises a downloader they will see.
claimed_assertions="$("$py" -c '
import json, re, sys
d = json.load(open(sys.argv[1]))["long_description"]
m = re.search(r"to see (\d+) assertions", d)
print(m.group(1) if m else "")' "$work/manifest.json")"
claimed_defects="$("$py" -c '
import json, re, sys
d = json.load(open(sys.argv[1]))["long_description"]
m = re.search(r"to see (\d+) known defects", d)
print(m.group(1) if m else "")' "$work/manifest.json")"

if [ -z "$claimed_assertions" ] || [ -z "$claimed_defects" ]; then
  echo "build refused: manifest long_description no longer states both counts" >&2
  exit 3
fi

# What it actually does, run the way a downloader would run it. The verdict
# arrives on stderr: stdout is the JSON-RPC wire and this suite asserts it
# stays byte-empty, so a summary read off stdout would always be empty.
measured_assertions="$(cd "$work" && "$py" shop_mcp.py --self-test 2>&1 >/dev/null \
  | sed -n 's/^all green: \([0-9]*\) assertions$/\1/p')"
if [ "$measured_assertions" != "$claimed_assertions" ]; then
  echo "build refused: manifest claims $claimed_assertions assertions, bundle runs ${measured_assertions:-none (suite is red)}" >&2
  exit 3
fi

mutation_out="$(cd "$work" && bash mutation_test.sh 2>&1)" || {
  echo "build refused: mutation suite failed inside the bundle" >&2
  printf '%s\n' "$mutation_out" | tail -20 >&2
  exit 3
}
measured_defects="$(printf '%s\n' "$mutation_out" | sed -n 's/^  mutations: \([0-9]*\)$/\1/p')"
caught="$(printf '%s\n' "$mutation_out" | sed -n 's/^  caught: \([0-9]*\)$/\1/p')"
missed="$(printf '%s\n' "$mutation_out" | sed -n 's/^  missed: \([0-9]*\)$/\1/p')"

if [ "$measured_defects" != "$claimed_defects" ]; then
  echo "build refused: manifest claims $claimed_defects defects, bundle injects ${measured_defects:-none}" >&2
  exit 3
fi
if [ "$caught" != "$measured_defects" ] || [ "$missed" != "0" ]; then
  echo "build refused: $caught/$measured_defects caught, $missed missed - the manifest says each one is caught" >&2
  exit 3
fi

# .mcpb is a zip. Written with zipfile so the result does not depend on which
# zip(1) is installed, and sorted so two builds of one tree are identical.
"$py" - "$work" "$out" "${files[@]}" <<'PY'
import pathlib, sys, zipfile

src, out, names = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sorted(sys.argv[3:])
out.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for name in names:
        p = src / name
        info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (0o755 if p.suffix == ".sh" else 0o644) << 16
        z.writestr(info, p.read_bytes())
PY

echo "$out"
echo "  $measured_assertions assertions, $caught/$measured_defects defects caught, $missed missed - both as claimed"
