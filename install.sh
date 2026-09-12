#!/usr/bin/env bash
# pi-setup — compose a working Pi coding-agent environment from independent modules.
#
# Usage:
#   ./install.sh                            # required + recommended modules
#   ./install.sh --minimal                  # required modules only
#   ./install.sh --with-local_web_search    # add an optional module
#   ./install.sh --overlay <dir-or-git-url> # apply an overlay (extra modules + hooks)
#   ./install.sh --update                   # fetch selected refs + re-run installers
#
# Overlays: a directory (or repo) containing manifest.fragment.yaml and an
# optional setup.d/ of executable hooks run after module installs. Overlays
# extend the module list; they never replace installer logic.
#
# Prerequisites: macOS, git, curl, python3, and the Pi CLI (`pi`) on PATH.
# Module installers may additionally require Homebrew, uv, and Node (they
# check for themselves and fail loudly when missing).
#
# Exit status: 0 only when every selected module/hook succeeded AND the final
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
declare -a DOCTOR_ARGS=()

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
if not modules:
    raise SystemExit(f"No modules found in {path}")
for name, mod in modules.items():
    repo, ref, tier = mod.get('repo', ''), mod.get('ref', 'main'), mod.get('tier', 'optional')
    if not repo or not ref or tier not in {'required', 'recommended', 'optional'} or any('\t' in v for v in (repo, ref, tier)):
        raise SystemExit(f"Invalid module {name} in {path}")
    print(f"{name}\t{repo}\t{ref}\t{tier}")
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

checkout_repo() {
  local repo="$1" dest="$2" ref="$3" fresh=0 top origin target branch='' head current status
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    top="$(git -C "$dest" rev-parse --show-toplevel 2>/dev/null)" || die "$dest is not a Git worktree"
    [ "$(cd "$dest" && pwd -P)" = "$(cd "$top" && pwd -P)" ] || die "$dest is not a repository root"
    origin="$(git -C "$dest" remote get-url origin)" || die "$dest has no origin"
    [ "$origin" = "$repo" ] || die "$dest origin does not match manifest; reconcile it manually (no changes made)"
    status="$(git -C "$dest" status --porcelain --untracked-files=all)" || die "$dest worktree status check failed"
    [ -z "$status" ] || die "$dest has local changes; commit/stash them before installing"
    if [ "$UPDATE" -eq 1 ]; then
      log "Fetching $(basename "$dest")..."
      git -C "$dest" fetch -q --prune --tags origin '+refs/heads/*:refs/remotes/origin/*' || die "$dest fetch failed; installer not run"
    fi
  else
    log "Cloning $(basename "$dest")..."
    git clone -q -- "$repo" "$dest" || die "$dest clone failed"
    fresh=1
  fi

  # Remote overlays follow origin's recorded default branch. Modules use the
  # explicit branch/tag/SHA in the manifest, never Git's checkout heuristics.
  if [ -z "$ref" ]; then
    ref="$(git -C "$dest" symbolic-ref --short refs/remotes/origin/HEAD)" || die "$dest has no origin default branch"
    ref="${ref#origin/}"
  fi
  if git -C "$dest" show-ref --verify --quiet "refs/remotes/origin/$ref"; then
    git -C "$dest" show-ref --verify --quiet "refs/tags/$ref" && die "$dest ref $ref is both a branch and tag"
    branch="$ref"
    target="refs/remotes/origin/$ref"
  elif git -C "$dest" show-ref --verify --quiet "refs/tags/$ref"; then
    target="refs/tags/$ref"
  elif [[ "$ref" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
    # Resolve object IDs, not revision names: a local branch named like a short
    # SHA must never silently select a different commit. Multiple matches fail.
    target="$(git -C "$dest" rev-parse --disambiguate="$ref")" || die "$dest SHA resolution failed"
    [[ "$target" =~ ^[0-9a-fA-F]{40}$ ]] || die "$dest SHA $ref is missing or ambiguous"
  else
    die "$dest ref $ref not found; use --update to fetch or correct the manifest"
  fi
  target="$(git -C "$dest" rev-parse --verify "$target^{commit}")" || die "$dest ref $ref is not a commit"
  head="$(git -C "$dest" rev-parse HEAD)" || die "$dest has no HEAD"
  current="$(git -C "$dest" symbolic-ref --short -q HEAD || true)"

  if [ "$fresh" -eq 0 ] && [ "$UPDATE" -eq 0 ]; then
    if [ "$head" != "$target" ] || { [ -n "$branch" ] && [ "$current" != "$branch" ]; }; then
      die "$dest checkout does not match $ref; use --update (local work is never reset)"
    fi
    log "$(basename "$dest") matches $ref (cached refs; no fetch)"
    return 0
  fi
  # Never abandon unreferenced detached work or reset an ahead/diverged branch.
  if [ "$fresh" -eq 0 ] && [ -z "$current" ] && [ "$head" != "$target" ]; then
    [ -n "$(git -C "$dest" for-each-ref --contains "$head" --format='%(refname)' refs/heads/ refs/tags/ refs/remotes/)" ] || die "$dest has detached work; create a branch before changing refs"
  fi
  if [ -n "$branch" ]; then
    if git -C "$dest" show-ref --verify --quiet "refs/heads/$branch"; then
      git -C "$dest" merge-base --is-ancestor "refs/heads/$branch" "$target" || die "$dest branch $branch is ahead or diverged; reconcile manually"
      git -C "$dest" checkout -q --no-overwrite-ignore "$branch" || die "$dest checkout failed"
      git -C "$dest" merge -q --ff-only --no-overwrite-ignore "$target" || die "$dest fast-forward failed"
    else
      git -C "$dest" checkout -q --no-overwrite-ignore -b "$branch" --track "origin/$branch" || die "$dest checkout failed"
    fi
  else
    git -C "$dest" checkout -q --no-overwrite-ignore --detach "$target^{commit}" || die "$dest checkout failed"
  fi
  [ "$(git -C "$dest" rev-parse HEAD)" = "$target" ] || die "$dest revision verification failed"
}

install_module() {
  local name="$1" repo="$2" ref="$3"
  local dest="$CODE_ROOT/$name"
  checkout_repo "$repo" "$dest" "$ref"
  [ -x "$dest/install.sh" ] || die "$name has no executable install.sh; installation incomplete"
  log "Installing $name..."
  (cd "$dest" && ./install.sh) || die "$name installer failed"
  [ "$name" != browser-worker ] || BROWSER_WORKER_INSTALLED=1
  DOCTOR_ARGS+=(--module "$name")
}

run_overlay() {
  local src="$1" dir rows
  case "$src" in
    *://*|git@*)
      dir="$CODE_ROOT/$(basename "$src" .git)"
      checkout_repo "$src" "$dir" ""
      ;;
    *) dir="$(cd "$src" && pwd)" ;;
  esac
  if [ -f "$dir/manifest.fragment.yaml" ]; then
    log "Overlay modules from $(basename "$dir")..."
    rows="$(parse_manifest "$dir/manifest.fragment.yaml")" || die "Invalid overlay manifest"
    while IFS=$'\t' read -r name repo ref tier; do
      [ -n "$name" ] || continue
      if wants_module "$tier" "$name"; then
        install_module "$name" "$repo" "$ref"
      fi
    done <<< "$rows"
  fi
  if [ -d "$dir/setup.d" ]; then
    local hook
    for hook in "$dir"/setup.d/*; do
      [ -x "$hook" ] || continue
      log "Overlay hook: $(basename "$hook")"
      "$hook" || die "Overlay hook failed: $hook"
    done
  fi
  DOCTOR_ARGS+=(--overlay "$dir")
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
    log "$rc already sources generated launchers"
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
    log "$rc already references pi-launchers.zsh (unmanaged line); leaving it"
    return 0
  fi
  if [ -f "$rc" ]; then
    local backup
    backup="$rc.bak-$(date +%Y%m%d-%H%M%S)-$$"
    (umask 077; cp "$rc" "$backup"; chmod 600 "$backup")
  fi
  local needs_newline=0
  if [ -s "$rc" ] && [ "$(tail -c1 "$rc" | od -An -c | tr -d ' ')" != '\n' ]; then
    needs_newline=1
  fi
  {
    if [ "$needs_newline" -eq 1 ]; then printf '\n'; fi
    # Preserve literal $HOME for the shell that later sources this file.
    # shellcheck disable=SC2016
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

rows="$(parse_manifest "$MANIFEST")" || die "Invalid module manifest"
while IFS=$'\t' read -r name repo ref tier; do
  [ -n "$name" ] || continue
  if wants_module "$tier" "$name"; then
    install_module "$name" "$repo" "$ref"
  else
    log "Skipping $name ($tier)"
  fi
done <<< "$rows"

for o in "${OVERLAYS[@]:-}"; do
  if [ -n "$o" ]; then run_overlay "$o"; fi
done

# Enable only the worker whose module installer succeeded. This deliberately
# runs after overlays, without changing their web_search/web_fetch endpoints.
if [ "$BROWSER_WORKER_INSTALLED" -eq 1 ]; then
  python3 "$SETUP_DIR/lib/configure_browser_worker.py" --worker-root "$CODE_ROOT/browser-worker" \
    || die "browser-worker installed but client configuration failed; see repair instructions above"
fi

install_shell_rc

log "Running doctor..."
# pi-shared installs into PI_SHARED_AGENT_DIR (default ~/.pi/agent), not the
# calling shell's PI_CODING_AGENT_DIR. Verify the profile we actually installed.
PI_CODING_AGENT_DIR="${PI_SHARED_AGENT_DIR:-$HOME/.pi/agent}" \
  "$SETUP_DIR/bin/doctor" "${DOCTOR_ARGS[@]}" || die "Installation checks failed; install did not complete. Review diagnostics above (completed module installs were not rolled back)"

log "Selected module checks passed (provider authentication/readiness is separate)."
if [ -s "$LAUNCHERS" ]; then
  log "Start a new shell and run \`pi-list\` to see available commands. Model launchers appear after a gateway alias catalog is configured."
  log "(Bare \`pi\` uses the active profile, not necessarily the generated model catalog.)"
else
  log "Start a new shell, then run: pi"
fi
