#!/usr/bin/env bash

set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ "$#" -ne 1 ]]; then
    echo "Usage: $0 <version>" >&2
    exit 2
fi

uv run --no-cache --no-project python - "$1" <<'PY'
from pathlib import Path
import json
import re
import sys

version = sys.argv[1]
if not re.fullmatch(r'\d+\.\d+\.\d+(?:-?rc\d+)?', version):
    raise SystemExit(f'Unsupported version format: {version}')
path = Path('pyproject.toml')
text = path.read_text()
match = re.search(r'^version = "([^"]+)"$', text, re.MULTILINE)
if not match:
    raise SystemExit('Failed to find version in pyproject.toml')
def parse(value):
    base, _, rc = value.replace('-rc', 'rc').partition('rc')
    major, minor, patch = map(int, base.split('.'))
    return major, minor, patch, 0 if rc else 1, int(rc or 0)
if parse(version) <= parse(match.group(1)):
    raise SystemExit(f'New version {version} must be greater than {match.group(1)}')
updated, count = re.subn(r'^version = "[^"]+"$', f'version = "{version}"', text, count=1, flags=re.MULTILINE)
if count != 1:
    raise SystemExit('Failed to update version in pyproject.toml')
path.write_text(updated)

abxbus_version = next(
    dependency.removeprefix('abxbus==')
    for dependency in re.findall(r'^\s+"([^"]+)",?$', updated, re.MULTILINE)
    if dependency.startswith('abxbus==')
)
config_path = Path('abx_plugins/plugins/chrome/config.json')
config = json.loads(config_path.read_text())
record = next(item for item in config['required_binaries'] if item['name'] == 'abxbus')
record['min_version'] = abxbus_version
record['overrides']['pnpm']['install_args'] = [f'abxbus@{abxbus_version}']
record['overrides']['pnpm']['version'] = abxbus_version
config_path.write_text(json.dumps(config, indent=2) + '\n')

lock_path = Path('uv.lock')
lock, count = re.subn(
    r'(?m)^(name = "abx-plugins"\nversion = ")[^"]+("$)',
    rf'\g<1>{version}\2',
    lock_path.read_text(),
    count=1,
)
if count != 1:
    raise SystemExit('Failed to update abx-plugins version in uv.lock')
lock_path.write_text(lock)
print(version)
PY

uv lock --check --offline --no-cache
