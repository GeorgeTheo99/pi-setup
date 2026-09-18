#!/usr/bin/env bash
# Explicit network-enabled integration smoke. Uses npm registries and a trusted
# local pi-shared Git HEAD, but no providers, models, browsers or service installs.
# Usage: tests/smoke_minimal.sh /absolute/path/to/pi-shared
#
# Verifies the unified-CLI contract end to end: setup writes ~/.pi/launcher.json
# (no ~/.zshrc), status passes, and the shared launcher answers its read-only /
# offline management flags (--launcher-list / --launcher-check / --launcher-refresh)
# against the generated config. No shell startup is sourced and no model is run.
set -euo pipefail
[ "$#" -eq 1 ] || { echo "Usage: $0 /absolute/path/to/trusted/pi-shared" >&2; exit 2; }
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHARED="$(cd "$1" && pwd)"
REF="$(git -C "$SHARED" rev-parse --verify HEAD)"
[ -x "$SHARED/bin/pi-launch" ] || { echo "Not a pi-shared checkout (no bin/pi-launch)" >&2; exit 2; }
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/pi-shared-minimal.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/setup" "$STAGE/home" "$STAGE/tmp" "$STAGE/runtime" "$STAGE/bin"
# Match the packaged Python dependency; never rely on an unrelated ambient ABI.
PYTHON="$(uv python find --managed-python --no-python-downloads 3.12)"
env -i PATH=/usr/bin:/bin "$PYTHON" -c 'import plistlib'
ln -s "$PYTHON" "$STAGE/bin/python3"
cp -R "$ROOT/bin" "$ROOT/lib" "$ROOT/install.sh" "$STAGE/setup/"
cp "$ROOT/runtime/package.json" "$ROOT/runtime/package-lock.json" "$STAGE/runtime/"
# JSON strings are accepted as quoted scalar values by the minimal parser only
# for ordinary local paths; refuse special characters instead of producing YAML.
case "$SHARED" in *$'\n'*|*'"'*|*"'"*) echo "Unsupported source path" >&2; exit 2 ;; esac
printf 'modules:\n  pi-shared:\n    repo: %s\n    ref: %s\n    tier: required\n' "$SHARED" "$REF" > "$STAGE/setup/manifest.yaml"
git -C "$STAGE/setup" init -q -b main
git -C "$STAGE/setup" add .
git -C "$STAGE/setup" -c core.hooksPath=/dev/null -c user.name='Setup smoke' -c user.email='fixture@example.test' commit -qm fixture
git clone -q --bare "$STAGE/setup" "$STAGE/setup-origin.git"
git -C "$STAGE/setup" remote add origin "$STAGE/setup-origin.git"
git -C "$STAGE/setup" fetch -q origin
git -C "$STAGE/setup" branch -q --set-upstream-to=origin/main
printf 'Testing trusted pi-shared commit %s with isolated HOME; npm downloads may occur.\n' "$REF"
env -i HOME="$STAGE/home" TMPDIR="$STAGE/tmp" \
  PATH="$STAGE/bin:$STAGE/runtime/node_modules/.bin:$PATH" \
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_ALLOW_PROTOCOL=file \
  GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null \
  npm_config_userconfig=/dev/null npm_config_globalconfig="$STAGE/home/empty-npmrc" \
  npm_config_registry="${npm_config_registry:-https://registry.npmjs.org/}" \
  PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 \
  SHARED="$SHARED" \
  /bin/bash -c '
    set -euo pipefail
    stage="$1"
    (cd "$stage/runtime" && npm ci --ignore-scripts --no-audit --no-fund)
    # New setups always opt into the unified CLI at ~/.pi/launcher.json.
    "$stage/setup/bin/pi-shared" setup --mode later --without-browser --yes
    "$stage/setup/bin/pi-shared" status
    cli="$HOME/.pi/launcher.json"
    launch="$HOME/.local/share/pi-shared/modules/pi-shared/bin/pi-launch"
    # --launcher-check / --launcher-refresh / --launcher-list are read-only or
    # offline and need no PI_UPSTREAM_BIN; they must succeed against the config.
    PI_LAUNCHER_CONFIG="$cli" "$launch" --launcher-check
    PI_LAUNCHER_CONFIG="$cli" "$launch" --launcher-refresh
    PI_LAUNCHER_CONFIG="$cli" "$launch" --launcher-list
    PI_LAUNCHER_CONFIG="$cli" "$launch" --launcher-help
    python3 - <<PY
import json, os
from pathlib import Path
home = Path.home().resolve()
receipt = json.loads((home / ".config/pi-shared/setup.json").read_text())
assert receipt["status"] == "module-checks-passed", receipt
assert receipt["modules"] == ["pi-shared"], receipt
assert receipt["version"] == 2, receipt
# New unified-CLI receipt records cli_file and never depends on shell wiring.
assert Path(receipt["cli_file"]).resolve() == home / ".pi/launcher.json", receipt
assert Path(receipt["code_root"]).is_relative_to(home), receipt
assert (home / ".pi/launcher.json").is_file(), "unified CLI config not generated"
assert not (home / ".zshrc").exists(), "fresh setup must not write ~/.zshrc"
assert not (home / "Library/LaunchAgents").exists(), "unexpected service registration"
assert not (home / ".omlx").exists(), "unexpected oMLX state"
assert not (home / ".pi-fallback").exists(), "unexpected recovery state"
assert not (home / ".cache/ms-playwright").exists(), "unexpected browser download"
assert not (home / ".pi-omlx/agent/models.json").exists(), "unexpected placeholder models"
PY
    "$stage/setup/bin/pi-shared" update </dev/null
    "$stage/setup/bin/pi-shared" update --modules-only </dev/null
    "$stage/setup/bin/pi-shared" status
    # The offline management flags still work after the update-driven refresh.
    PI_LAUNCHER_CONFIG="$HOME/.pi/launcher.json" "$launch" --launcher-check
    echo "MINIMAL_SETUP_SMOKE_OK: setup, unified-CLI config, offline launcher management, source re-exec update, repeated module update and status passed; no service/model calls, no ~/.zshrc"
  ' smoke "$STAGE"
