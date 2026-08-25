#!/usr/bin/env bash
# pi-setup — compose a working Pi coding-agent environment from independent modules.
#
# Usage:
#   ./install.sh                            # required + recommended modules
#   ./install.sh --minimal                  # required modules only
#   ./install.sh --with-local_web_search    # add an optional module
#   ./install.sh --overlay <dir-or-git-url> # apply an overlay (extra modules + hooks)
#   ./install.sh --update                   # git pull每 module + re-run installers
#
# Overlays: a directory (or repo) containing manifest.fragment.yaml and an
# optional setup.d/ of executable hooks run after module installs. Overlays
# extend the module list; they never replace installer logic.
#
# Prerequisites: macOS, git, curl. Module installers may additionally require
# Homebrew, uv, python3, and Node (they check for themselves).
set -euo pipefail

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_ROOT="${PI_SETUP_CODE_ROOT:-$HOME/local_code}"
MANIFEST="$SETUP_DIR/manifest.yaml"

MINIMAL=0
UPDATE=0
declare -a WITH_MODULES=()
declare -a OVERLAYS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --minimal) MINIMAL=1 ;;
    --update) UPDATE=1 ;;
    --with-*) WITH_MODULES+=("${1#--with-}") ;;
    --overlay) shift; OVERLAYS+=("${1:?--overlay requires a value}") ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "Unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

log()  { printf '\033[1;34m[pi-setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[pi-setup]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[pi-setup]\033[0m %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null || die "git is required"
command -v python3 >/dev/null || die "python3 is required (used to parse manifest.yaml)"

# ---------------------------------------------------------------------------
# Manifest parsing (python3 stdlib only — no yaml dependency for the installer)
# ---------------------------------------------------------------------------
parse_manifest() {
  # Emits: name<TAB>repo<TAB>ref<TAB>tier per module.
  python3 - "$1" <<'PY'
import re, sys

path = sys.argv[1]
text = open(path).read()
# Minimal YAML subset parser for our manifest shape (2-space indented maps).
modules = {}
current = None
in_modules = False
for raw in text.splitlines():
    line = raw.rstrip()
    if not line or line.lstrip().startswith("#"):
        continue
    if line == "modules:":
        in_modules = True
        continue
    if in_modules:
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            modules[current] = {}
            continue
        m = re.match(r"^    ([a-z_]+):\s*(.+)$", line)
        if m and current:
            key, val = m.group(1), m.group(2).strip()
            if not val.startswith(">"):
                modules[current][key] = val.strip("'\"")
            continue
        if re.match(r"^[A-Za-z]", line):
            in_modules = False
for name, mod in modules.items():
    print(f"{name}\t{mod.get('repo','')}\t{mod.get('ref','main')}\t{mod.get('tier','optional')}")
PY
}

wants_module() {
  local tier="$1" name="$2"
  case "$tier" in
    required) return 0 ;;
    recommended) [ "$MINIMAL" -eq 0 ] && return 0 ;;
  esac
  local w
  for w in "${WITH_MODULES[@]:-}"; do [ "$w" = "$name" ] && return 0; done
  return 1
}

install_module() {
  local name="$1" repo="$2" ref="$3"
  local dest="$CODE_ROOT/$name"
  if [ -d "$dest/.git" ]; then
    if [ "$UPDATE" -eq 1 ]; then
      log "Updating $name..."
      git -C "$dest" fetch -q origin
      git -C "$dest" checkout -q "$ref" 2>/dev/null || true
      git -C "$dest" pull -q --ff-only origin "$ref" 2>/dev/null || warn "$name: not fast-forwardable; left as-is"
    else
      log "$name already cloned; skipping fetch (use --update to pull)"
    fi
  else
    log "Cloning $name ($ref)..."
    git clone -q "$repo" "$dest"
    git -C "$dest" checkout -q "$ref" 2>/dev/null || true
  fi
  if [ -x "$dest/install.sh" ]; then
    log "Installing $name..."
    (cd "$dest" && ./install.sh) || die "$name installer failed"
  else
    warn "$name has no install.sh; cloned only"
  fi
}

run_overlay() {
  local src="$1" dir
  case "$src" in
    http*://*|git@*)
      dir="$CODE_ROOT/$(basename "$src" .git)"
      if [ -d "$dir/.git" ]; then
        [ "$UPDATE" -eq 1 ] && git -C "$dir" pull -q --ff-only || true
      else
        log "Cloning overlay $(basename "$dir")..."
        git clone -q "$src" "$dir"
      fi
      ;;
    *) dir="$(cd "$src" && pwd)" ;;
  esac
  if [ -f "$dir/manifest.fragment.yaml" ]; then
    log "Overlay modules from $(basename "$dir")..."
    while IFS=$'\t' read -r name repo ref tier; do
      [ -n "$name" ] || continue
      wants_module "$tier" "$name" && install_module "$name" "$repo" "$ref"
    done < <(parse_manifest "$dir/manifest.fragment.yaml")
  fi
  if [ -d "$dir/setup.d" ]; then
    local hook
    for hook in "$dir"/setup.d/*; do
      [ -x "$hook" ] || continue
      log "Overlay hook: $(basename "$hook")"
      "$hook" || die "Overlay hook failed: $hook"
    done
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
mkdir -p "$CODE_ROOT"
log "Modules root: $CODE_ROOT"

while IFS=$'\t' read -r name repo ref tier; do
  [ -n "$name" ] || continue
  if wants_module "$tier" "$name"; then
    install_module "$name" "$repo" "$ref"
  else
    log "Skipping $name ($tier)"
  fi
done < <(parse_manifest "$MANIFEST")

for o in "${OVERLAYS[@]:-}"; do
  [ -n "$o" ] && run_overlay "$o"
done

log "Running doctor..."
"$SETUP_DIR/bin/doctor" || warn "doctor reported issues (see above)"

log "Done. Start a new shell, then run: pi"
