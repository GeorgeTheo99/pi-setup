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
# Prerequisites: macOS, git, curl, python3, and the Pi CLI (`pi`) on PATH.
# Module installers may additionally require Homebrew, uv, and Node (they
# check for themselves and fail loudly when missing).
#
# Exit status: 0 only when every required module/hook succeeded AND the final
# doctor passes. Otherwise non-zero with the failing check(s) printed. The
# generated model launchers (`pi-list`, `pi-sonnet`, ...) are sourced from
# ~/.zshrc via a marked block; set PI_SETUP_NO_SHELL_RC=1 to opt out.
set -euo pipefail

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_ROOT="${PI_SETUP_CODE_ROOT:-$HOME/local_code}"
MANIFEST="$SETUP_DIR/manifest.yaml"

MINIMAL=0
UPDATE=0
BROWSER_WORKER_INSTALLED=0
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
command -v pi >/dev/null || die "the Pi CLI is not on PATH — install Pi first (see README prerequisites), then re-run"

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
      git -C "$dest" fetch -q origin "$ref" || die "$name: fetch failed; check repository access and retry"
      git -C "$dest" checkout -q "$ref" || die "$name: could not select the requested revision; preserve/reconcile local changes, then retry"
      git -C "$dest" pull -q --ff-only origin "$ref" || die "$name: update is not fast-forwardable; reconcile the checkout before retrying"
    else
      log "$name already cloned; skipping fetch (use --update to pull)"
    fi
  else
    log "Cloning $name ($ref)..."
    git clone -q "$repo" "$dest" || die "$name: clone failed; check repository access/authentication and retry"
    git -C "$dest" checkout -q "$ref" || die "$name: requested revision is unavailable; fetch the published revision and retry"
  fi
  # Exact manifest pins must also be honored by existing checkouts, even on a
  # normal rerun. Never install an older dependency merely because it is cloned.
  if [[ "$ref" =~ ^[0-9a-f]{40}$ ]] && [ "$(git -C "$dest" rev-parse HEAD)" != "$ref" ]; then
    [ -z "$(git -C "$dest" status --porcelain)" ] \
      || die "$name: pinned revision differs and checkout has local changes; preserve/reconcile them before retrying"
    git -C "$dest" cat-file -e "$ref^{commit}" 2>/dev/null \
      || git -C "$dest" fetch -q origin "$ref" \
      || die "$name: cannot fetch pinned revision"
    git -C "$dest" checkout -q --detach "$ref" || die "$name: cannot select pinned revision"
  fi
  if [ -x "$dest/install.sh" ]; then
    log "Installing $name..."
    (cd "$dest" && ./install.sh) || die "$name installer failed"
    [ "$name" != browser-worker ] || BROWSER_WORKER_INSTALLED=1
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
        if [ "$UPDATE" -eq 1 ]; then
          git -C "$dir" pull -q --ff-only || die "overlay update failed; reconcile its checkout before retrying"
        fi
      else
        log "Cloning overlay $(basename "$dir")..."
        git clone -q "$src" "$dir" || die "overlay clone failed; check repository access/authentication and retry"
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
# Shell integration: source generated launchers (pi-list, pi-sonnet, ...)
# ---------------------------------------------------------------------------
LAUNCHERS="$HOME/.pi/generated/pi-launchers.zsh"
RC_BEGIN="# >>> pi-setup generated launchers >>>"
RC_END="# <<< pi-setup generated launchers <<<"

install_shell_rc() {
  [ "${PI_SETUP_NO_SHELL_RC:-0}" = "1" ] && { log "Skipping ~/.zshrc integration (PI_SETUP_NO_SHELL_RC=1)"; return 0; }
  local rc="${PI_SETUP_ZSHRC:-$HOME/.zshrc}"
  if [ -f "$rc" ] && grep -qF "$RC_BEGIN" "$rc"; then
    log "~/.zshrc already sources generated launchers"
    return 0
  fi
  if [ -f "$rc" ] && python3 - "$rc" <<'PY'
import sys
from pathlib import Path
lines = Path(sys.argv[1]).read_text().splitlines()
sys.exit(0 if any('pi-launchers.zsh' in line and not line.lstrip().startswith('#')
                  and ('source ' in line or '. ' in line) for line in lines) else 1)
PY
  then
    log "~/.zshrc already references pi-launchers.zsh (unmanaged line); leaving it"
    return 0
  fi
  if [ -f "$rc" ]; then
    local backup="$rc.bak-$(date +%Y%m%d-%H%M%S)-$$"
    (umask 077; cp "$rc" "$backup"; chmod 600 "$backup")
  fi
  {
    [ -f "$rc" ] && [ -s "$rc" ] && [ "$(tail -c1 "$rc" | od -An -c | tr -d ' ')" != '\n' ] && echo
    printf '%s\n[ -f "$HOME/.pi/generated/pi-launchers.zsh" ] && source "$HOME/.pi/generated/pi-launchers.zsh"\n%s\n' \
      "$RC_BEGIN" "$RC_END"
  } >> "$rc"
  log "Added generated-launcher block to $rc"
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

# Enable only the worker whose module installer succeeded. This deliberately
# runs after overlays, without changing their web_search/web_fetch endpoints.
if [ "$BROWSER_WORKER_INSTALLED" -eq 1 ]; then
  python3 "$SETUP_DIR/lib/configure_browser_worker.py" --worker-root "$CODE_ROOT/browser-worker" \
    || die "browser-worker installed but client configuration failed; see repair instructions above"
fi

install_shell_rc

log "Running doctor..."
if ! "$SETUP_DIR/bin/doctor"; then
  die "install did not complete: fix the FAIL items above (each names its repair command), then re-run ./install.sh"
fi

if [ -s "$LAUNCHERS" ]; then
  log "Install complete. Start a new shell, run \`pi-list\` to see your models, then launch one, e.g. \`pi-sonnet\`."
  log "(Bare \`pi\` starts the default profile without the generated Databricks model catalog.)"
else
  log "Install complete. Start a new shell, then run: pi"
fi
