#!/usr/bin/env bash
# pi-setup — compose a working Pi coding-agent environment from independent modules.
#
# Usage:
#   ./install.sh                            # required + recommended modules
#   ./install.sh --minimal                  # required modules only
#   ./install.sh --with-omnigent            # also require native-Pi prerequisites
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
# unified CLI needs no shell startup wiring. Recognized obsolete generated
# launcher lines are removed; PI_SETUP_NO_SHELL_RC=1 opts out of that cleanup.
set -euo pipefail

SETUP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODE_ROOT="${PI_SETUP_CODE_ROOT:-$HOME/local_code}"
MANIFEST="$SETUP_DIR/manifest.yaml"

MINIMAL=0
UPDATE=0
UPDATE_CHANGED=0
ONLY_MODULES=""
BROWSER_WORKER_INSTALLED=0
REQUIRE_OMNIGENT=0
declare -a WITH_MODULES=()
declare -a OVERLAYS=()
declare -a DOCTOR_ARGS=()

while [ $# -gt 0 ]; do
  case "$1" in
    --minimal) MINIMAL=1 ;;
    --update) UPDATE=1 ;;
    --update-changed) UPDATE=1; UPDATE_CHANGED=1 ;;
    --only) shift; ONLY_MODULES="${1:?--only requires comma-separated module names}" ;;
    --with-omnigent) REQUIRE_OMNIGENT=1 ;;
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
if [ "$REQUIRE_OMNIGENT" -eq 1 ]; then
  python3 - "${PI_SHARED_AGENT_DIR:-$HOME/.pi/agent}" "$HOME/.pi/agent" <<'PY'
from pathlib import Path
import sys
if Path(sys.argv[1]).expanduser().resolve() != Path(sys.argv[2]).resolve():
    raise SystemExit("--with-omnigent requires the standard ~/.pi/agent profile; no configuration was changed.")
PY
fi
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
  if [ -n "$ONLY_MODULES" ]; then
    case ",$ONLY_MODULES," in *",$name,"*) return 0 ;; *) return 1 ;; esac
  fi
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
  if [ "$UPDATE_CHANGED" -eq 1 ] && [ "$name" != pi-shared ]; then
    local installed revision
    installed="$(python3 - "$name" <<'PY'
import json, os, sys
print(json.loads(os.environ.get('PI_SETUP_INSTALLED_REVISIONS', '{}')).get(sys.argv[1], ''))
PY
)" || die "Invalid installed revision receipt"
    revision="$(git -C "$dest" rev-parse HEAD)"
    if [ "$installed" = "$revision" ]; then
      log "$name unchanged since successful installation; not restarting"
      DOCTOR_ARGS+=(--module "$name")
      return 0
    fi
  fi
  log "Installing $name..."
  if [ "$UPDATE_CHANGED" -eq 1 ] && [ "$name" = pi-shared ] && [ -z "${PI_SHARED_CLI_OUT:-}" ]; then
    # Keep the current launcher metadata/profile selection for the final refresh.
    (cd "$dest" && ./install.sh --no-catalog) || die "$name installer failed"
  else
    (cd "$dest" && ./install.sh) || die "$name installer failed"
  fi
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
# Remove only exact legacy wiring; never create or source a shell rc.
# ---------------------------------------------------------------------------
LAUNCHERS="$HOME/.pi/generated/pi-launchers.zsh"

install_shell_rc() {
  # Legacy source-only orchestrators have not installed the replacement CLI.
  # Leave their existing shell wiring intact until they opt into JSON routing.
  [ -n "${PI_SHARED_CLI_OUT:-}" ] || return 0
  [ "${PI_SETUP_NO_SHELL_RC:-0}" = "1" ] && { log "Skipping shell cleanup (PI_SETUP_NO_SHELL_RC=1)"; return 0; }
  python3 - "${PI_SETUP_ZSHRC:-$HOME/.zshrc}" <<'PY'
import os, stat, sys, tempfile
from pathlib import Path
rc = Path(sys.argv[1]).expanduser()
if not rc.exists() and not rc.is_symlink():
    raise SystemExit(0)
info = rc.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
    raise SystemExit(f"Unsafe shell rc; review manually or opt out: {rc}")
original = rc.read_bytes()
text = original.decode('utf-8')
begin = '# >>> pi-setup generated launchers >>>'
end = '# <<< pi-setup generated launchers <<<'
source = '[ -f "$HOME/.pi/generated/pi-launchers.zsh" ] && source "$HOME/.pi/generated/pi-launchers.zsh"'
text = text.replace(begin + '\n' + source + '\n' + end + '\n', '')
known = {source}
legacy_paths = ('$HOME/.pi/generated/pi-launchers.zsh', '$HOME/.pi/model-gateway/pi-launchers.zsh')
for path in legacy_paths:
    known.add(f'source "{path}"')
    known.add(f'[ -f "{path}" ] && source "{path}"')
# Modified marked blocks are user content: preserve them in their entirety.
lines, inside = [], False
pending = iter(text.splitlines(keepends=True))
for line in pending:
    value = line.rstrip('\n')
    if not inside and value in {f'[ -f "{path}" ] && ' + chr(92) for path in legacy_paths}:
        following = next(pending, '')
        path = next(path for path in legacy_paths if f'[ -f "{path}" ]' in value)
        if following.rstrip('\n') == f'  source "{path}"':
            continue
        lines.extend((line, following))
        continue
    if value == begin:
        inside = True
    if inside or value not in known:
        lines.append(line)
    if value == end:
        inside = False
result = ''.join(lines).encode('utf-8')
if result != original:
    fd, backup = tempfile.mkstemp(prefix=rc.name + '.bak-', dir=rc.parent)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(original)
    fd, temporary = tempfile.mkstemp(prefix=rc.name + '.tmp-', dir=rc.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(result)
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
        os.replace(temporary, rc)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f'Removed recognized legacy launcher wiring from {rc}; private backup: {backup}')
    print('Restart existing shells to discard loaded functions; shell startup was not executed.')
PY
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
rows="$(parse_manifest "$MANIFEST")" || die "Invalid module manifest"
if [ -n "$ONLY_MODULES" ]; then
  python3 - "$ONLY_MODULES" "$rows" <<'PY' || die "Invalid exclusive module selection"
import sys
names = sys.argv[1].split(',')
known = {row.split('\t')[0] for row in sys.argv[2].splitlines()}
if not names or len(names) != len(set(names)) or any(name not in known for name in names):
    raise SystemExit(1)
PY
fi
mkdir -p "$CODE_ROOT"
log "Modules root: $CODE_ROOT"
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

if [ "$UPDATE_CHANGED" -eq 1 ] && [ "${PI_SETUP_REFRESH_LAUNCHERS:-1}" = 1 ]; then
  # Regenerate from the saved generated metadata, not reset endpoint/model choices.
  if [ -n "${PI_SHARED_CLI_OUT:-}" ]; then
    PI_LAUNCHER_CONFIG="$PI_SHARED_CLI_OUT" "$CODE_ROOT/pi-shared/bin/pi-launch" --launcher-refresh \
      || die "CLI refresh failed; selected module updates were not rolled back"
  else
    "$CODE_ROOT/pi-shared/bin/pi-launchers-refresh" --launcher "${PI_SHARED_LAUNCHERS_OUT:-$LAUNCHERS}" \
      || die "Launcher refresh failed; selected module updates were not rolled back"
  fi
fi

log "Running doctor..."
if [ "$REQUIRE_OMNIGENT" -eq 1 ]; then
  DOCTOR_ARGS+=(--require-omnigent)
fi
# pi-shared installs into PI_SHARED_AGENT_DIR (default ~/.pi/agent), not the
# calling shell's PI_CODING_AGENT_DIR. Verify the profile we actually installed.
PI_CODING_AGENT_DIR="${PI_SHARED_AGENT_DIR:-$HOME/.pi/agent}" \
  "$SETUP_DIR/bin/doctor" "${DOCTOR_ARGS[@]}" || die "Installation checks failed; install did not complete. Review diagnostics above (completed module installs were not rolled back)"

log "Selected module checks passed (provider authentication/readiness is separate)."
if [ "$REQUIRE_OMNIGENT" -eq 1 ]; then
  log "Omnigent native-Pi prerequisites checked; native launch and inference were not tested."
  log "Compatibility scope and explicit launch command: $SETUP_DIR/docs/omnigent-compatibility.md"
fi
log "Run \`pi\` for stock Pi, or \`pi --launcher-list\` for configured aliases."
log "Restart older shells to discard previously loaded launcher functions; no shell rc was sourced."
