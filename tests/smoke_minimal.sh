#!/usr/bin/env bash
# Explicit network-enabled integration smoke. Uses npm registries and a trusted
# local pi-shared Git HEAD, but no providers, models, browsers or service installs.
# Usage: tests/smoke_minimal.sh /absolute/path/to/pi-shared
set -euo pipefail
[ "$#" -eq 1 ] || { echo "Usage: $0 /absolute/path/to/trusted/pi-shared" >&2; exit 2; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHARED="$(cd "$1" && pwd)"
REF="$(git -C "$SHARED" rev-parse --verify HEAD)"
[ -f "$SHARED/bin/pi-shared-install" ] || { echo "Not a pi-shared checkout" >&2; exit 2; }
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/pi-shared-minimal.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/setup" "$STAGE/home" "$STAGE/tmp" "$STAGE/runtime"
cp -R "$ROOT/bin" "$ROOT/lib" "$ROOT/install.sh" "$STAGE/setup/"
cp "$ROOT/runtime/package.json" "$ROOT/runtime/package-lock.json" "$STAGE/runtime/"
# JSON strings are accepted as quoted scalar values by the minimal parser only
# for ordinary local paths; refuse special characters instead of producing YAML.
case "$SHARED" in *$'\n'*|*'"'*|*"'"*) echo "Unsupported source path" >&2; exit 2 ;; esac
printf 'modules:\n  pi-shared:\n    repo: %s\n    ref: %s\n    tier: required\n' "$SHARED" "$REF" > "$STAGE/setup/manifest.yaml"
printf 'Testing trusted pi-shared commit %s with isolated HOME; npm downloads may occur.\n' "$REF"
env -i HOME="$STAGE/home" TMPDIR="$STAGE/tmp" \
  PATH="$STAGE/runtime/node_modules/.bin:$PATH" \
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_ALLOW_PROTOCOL=file \
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null \
  npm_config_userconfig=/dev/null npm_config_globalconfig="$STAGE/home/empty-npmrc" \
  PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 \
  /bin/bash -c '
    set -euo pipefail
    stage="$1"
    npm ci --prefix "$stage/runtime" --ignore-scripts --no-audit --no-fund
    "$stage/setup/bin/pi-shared" setup --mode later --without-browser --yes
    "$stage/setup/bin/pi-shared" status
    python3 - <<PY
import json, os
from pathlib import Path
home = Path.home().resolve()
receipt = json.loads((home / ".config/pi-shared/setup.json").read_text())
assert receipt["status"] == "module-checks-passed", receipt
assert receipt["modules"] == ["pi-shared"], receipt
assert Path(receipt["code_root"]).is_relative_to(home), receipt
assert not (home / "Library/LaunchAgents").exists(), "unexpected service registration"
assert not (home / ".omlx").exists(), "unexpected oMLX state"
assert not (home / ".pi-fallback").exists(), "unexpected recovery state"
assert not (home / ".cache/ms-playwright").exists(), "unexpected browser download"
print("MINIMAL_SETUP_SMOKE_OK: actual dependency install/profile loading/status passed; no service/model setup")
PY
  ' smoke "$STAGE"
