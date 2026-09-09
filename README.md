# pi-setup

Compose a working [Pi](https://github.com/badlogic/pi-mono) coding-agent
environment from independent modules — without copying any of them into this
repo. Each module is its own git repository with its own installer; `pi-setup`
just clones, pins, installs, and checks local configuration/service evidence.
A successful check is not proof that provider authentication or inference works.

## Modules

| Module | Tier | What it provides |
|---|---|---|
| [pi-shared](https://github.com/GeorgeTheo99/pi-shared) | required | Pi extensions (memory, subagents, browser automation, deep research, work plans, tool bundles), skills, prompts, themes, workflows |
| [model-gateway](https://github.com/GeorgeTheo99/model-gateway) | recommended | Local model router on `:9111` — one endpoint for cloud providers (BYO keys) and local oMLX MLX models, with protocol translation |
| [local_web_search](https://github.com/GeorgeTheo99/local_web_search) | optional | Loopback web search / page retrieval broker on `:8889` (needs a personal Brave Search API key) |

## Prerequisites

- macOS with [Homebrew](https://brew.sh)
- `git`, `curl`, `python3`
- [uv](https://docs.astral.sh/uv) (`brew install uv`) — used by the service modules
- Node.js/npm for Pi and pi-shared's locked extension dependencies
- Pi itself: `npm install -g --ignore-scripts @earendil-works/pi-coding-agent`
  (see the [Pi quickstart](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/quickstart.md))

## Install

```bash
git clone https://github.com/GeorgeTheo99/pi-setup.git ~/local_code/pi-setup
cd ~/local_code/pi-setup

./install.sh --minimal                # pi-shared only; no local services
# OR:
./install.sh                          # pi-shared + model-gateway
./install.sh --update                 # update selected refs + re-run installers
```

The minimal path installs shared resources and their dependencies, not a model
provider. Start `pi`, authenticate with `/login` (or configure a supported
provider key), and select `/model`. No gateway, oMLX, search broker, browser
runtime, or generated model catalog is required for this direct-provider path.
Optional shared tools still need their own prerequisites.

**model-gateway:** its installer creates an empty provider config and starter
`model-info.json` catalog when absent. A passing `/health` means only that the
gateway process answers. Onboard a provider/model separately before expecting
inference; see its README and `docs/provider-onboarding.md`.

### Add search (credential first)

The search installer starts its service and requires an owner-only Brave key
**before** installation. Pre-clone it, create the ignored credential file using
its README instructions, then enable it in pi-setup:

```bash
git clone https://github.com/GeorgeTheo99/local_web_search.git ~/local_code/local_web_search
# Follow local_web_search/README.md: create data/brave_key with mode 0600
# (data directory mode 0700), or use its documented custom data directory.
./install.sh --minimal --with-local_web_search
```

Do not put credentials in manifests or shell history. A key exported only in
an interactive shell is not necessarily available to the launchd service.
Include `--with-local_web_search` again when updating that optional module.
`PI_SETUP_CODE_ROOT` overrides the default `~/local_code` module directory.

## Health check

```bash
bin/doctor                              # known modules present under code root
bin/doctor --module pi-shared           # minimal composition only
bin/doctor --overlay /path/to/overlay   # explicitly trust and run its doctor
```

- Parses the active profile's `settings.json` package entries and resolves the
  exact pi-shared path; checks declared runtime dependencies without importing
  extensions. Uses `PI_CODING_AGENT_DIR`, then `PI_SHARED_AGENT_DIR`, then
  `~/.pi/agent` for a standalone doctor invocation. During installation,
  pi-setup explicitly checks `PI_SHARED_AGENT_DIR` (default `~/.pi/agent`),
  matching pi-shared's install target rather than the calling Pi profile.
- Delegates known service checks to `model-gateway verify` (service identity
  and liveness only) and `scripts/local-search verify` (local readiness,
  telemetry when enabled, and the exact MCP tool list). These read-only checks
  honor each module's persisted port configuration and make no provider calls.
- Does not test model inference, browser execution, or Pi extension loading.
  A directory is reported as source present, not as an installed/ready service.
- The installer checks only selected modules and explicitly passed overlays.
  Any failed installer or doctor exits nonzero; earlier completed installs
  are not rolled back. No success message follows failed checks.

Inside Pi, `/mcp` lists servers managed by the optional MCP adapter only.
The native browser/search wrappers connect to their independent MCP services
outside that adapter. With the environment-doctor extension loaded, use
`/mcp-connections` (or `dev_doctor`) to see both paths without connecting to
services. This does not change server registration or make tools service-ready.

## Overlays

Organizations or individuals can extend the module set without forking this
repo. An overlay is a directory or repo containing:

- `manifest.fragment.yaml` — extra modules in the same schema as
  `manifest.yaml`
- `setup.d/` — optional executable hooks run after module installs
- `bin/doctor` — optional read-only checks run only for an explicitly selected
  overlay (`install.sh --overlay ...` or `bin/doctor --overlay ...`)

Overlay hooks and doctors execute code with your permissions. Review them
before selecting an overlay; unrelated sibling folders are never auto-executed.

```bash
./install.sh --overlay <path-or-git-url>
```

## Design

- **No vendored code.** Modules stay independent repos; this repo owns only
  composition. Pinning happens via `ref:` in `manifest.yaml`.
- **Fail-closed refs.** Branches track `origin/<branch>` with fast-forward-only
  updates; tags/SHAs are checked out exactly, detached. Missing/ambiguous refs,
  origin mismatches, dirty worktrees (including untracked files), and
  ahead/diverged target branches stop installation. Ignored files are not
  overwritten by checkout/merge. No reset, clean, stash, or force checkout is
  performed; reconcile local work yourself before retrying. Origin URLs must
  match the manifest exactly (including local mirror vs. HTTPS differences).
- **Explicit updates.** Without `--update`, existing checkouts must match the
  requested ref using cached remote refs; no freshness claim or fetch is made.
  `--update` fetches before ref selection and reruns selected installers.
  Remote overlays follow their recorded `origin/HEAD` default branch.
- **Overlay, don't fork.** Installer logic lives here once; overlays are data
  plus hooks.

## Offline regression tests

```bash
python3 -B -m unittest discover -s tests -v
bash -n install.sh bin/doctor
```

Tests use temporary Git repositories, an isolated HOME, and stub installers and
service commands. They do not install packages, edit real profiles, contact
providers, or require running services.

## License

Apache-2.0

## Secret scanning

Secret scanning is enforced in three layers: tracked pre-commit/pre-push hooks,
the local bare repository's pre-receive hook, and the pinned GitHub Gitleaks
workflow. Install Gitleaks and activate the tracked worktree hooks once per
clone:

```bash
brew install gitleaks
git config core.hooksPath .githooks
```

Do not bypass a failed scan. `.gitleaksignore` contains only exact fingerprints
for synthetic test credentials; never allowlist an entire file or credential
pattern.
