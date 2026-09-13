# pi-setup

Compose a working [Pi](https://github.com/badlogic/pi-mono) coding-agent
environment from independent modules — without copying any of them into this
repo. Each module is its own git repository with its own installer; `pi-setup`
just clones, pins, installs, and checks local configuration/service evidence.
A successful check is not proof that provider authentication or inference works.

## Homebrew entry point: `pi-shared`

The public command is **`pi-shared`**; this repository owns its orchestration.
The companion `homebrew-tap` repository owns the formula, not another installer.
See [Homebrew setup](docs/homebrew.md) for installation, local testing,
service ownership, and the release gate. The tap is
[GeorgeTheo99/homebrew-tap](https://github.com/GeorgeTheo99/homebrew-tap).

Review [the formula](https://github.com/GeorgeTheo99/homebrew-tap/blob/main/Formula/pi-shared.rb)
and its source before granting trust. The first command persistently trusts only
this formula, including future revisions—not the entire tap. On older Homebrew
versions without `brew trust`, omit that first command.

```bash
brew trust --formula georgetheo99/tap/pi-shared
brew install georgetheo99/tap/pi-shared
pi-shared setup                         # interactive choices and final approval
pi-shared setup --mode cloud --plan     # read-only preview; no downloads or commands
pi-shared setup --local                 # opt into oMLX options, now or later
pi-shared status                        # selected module checks, not inference proof
pi                                     # authenticate with /login and select /model
```

If Homebrew reports an **untrusted tap** (possibly followed by “invalid syntax
in tap”), review the formula and follow the
[formula-scoped trust recovery](docs/homebrew.md#homebrew-trust-errors) before
retrying installation. Do not disable trust checks globally.

Fresh setup enables `pi-list`, `pi-regen`, `pi-shared-update`, `pi-restart`,
`pi-default`, and `pi-openai` even before a gateway alias catalog exists. Open a
new shell after setup. `pi-list` explains the unconfigured state; model shortcuts
appear after configuring the gateway alias export and running `pi-regen`.
Existing direct-launcher choices and explicit opt-outs are preserved.

Routine maintenance is now `pi-shared update`: it remembers the selected setup,
updates its owning package/source and selected modules, refreshes dependencies
and shortcuts, and verifies the result. `pi-shared update --plan` is read-only.
Interactive zsh prompts automatically refresh changed local catalog/launcher
data—never software, services, or models. See [update ownership and safety](docs/updates.md),
including the one-time setup capture required for older receipts.

Homebrew installs Node, Python, uv, Git and a pinned, lockfile-backed Pi runtime.
It does **not** run setup, start services, modify Pi profiles, or download LLM
weights during package installation. `setup` explicitly delegates to the same
module installers used below. The new CLI defaults writable module checkouts to
`~/.local/share/pi-shared/modules`; `PI_SETUP_CODE_ROOT` overrides this. The legacy
`./install.sh` keeps its existing `~/local_code` default.

## Modules

| Module | Tier | What it provides |
|---|---|---|
| [pi-shared](https://github.com/GeorgeTheo99/pi-shared) | required | Pi extensions (memory, subagents, browser automation, deep research, work plans, tool bundles), skills, prompts, themes, workflows |
| [model-gateway](https://github.com/GeorgeTheo99/model-gateway) | recommended | Local model router on `:9111` — one endpoint for cloud providers (BYO keys) and local oMLX MLX models, with protocol translation |
| [browser-worker](https://github.com/GeorgeTheo99/browser-worker) | recommended | Rendered public browsing on `:8890`, with local production credentials and a provisioned browser; independent of web search |
| [local_web_search](https://github.com/GeorgeTheo99/local_web_search) | optional | Loopback web search / page retrieval broker on `:8889` (needs a personal Brave Search API key) |

## Prerequisites

- macOS with [Homebrew](https://brew.sh)
- `git`, `curl`, `python3`
- [uv](https://docs.astral.sh/uv) (`brew install uv`) — used by the service modules
- Node.js/npm for Pi and pi-shared's locked extension dependencies
- **Pi itself, installed first**: `npm install -g --ignore-scripts @earendil-works/pi-coding-agent`
  (see the [Pi quickstart](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/quickstart.md)).
  `install.sh` refuses to start until `pi` is on `PATH`.

## Install

```bash
git clone https://github.com/GeorgeTheo99/pi-setup.git ~/local_code/pi-setup
cd ~/local_code/pi-setup

./install.sh --minimal                # pi-shared only; no local services
# OR:
./install.sh                          # pi-shared + model-gateway + browser-worker
./install.sh --minimal --with-browser-worker # shared resources + rendered browsing
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

## What "done" means

`install.sh` exits `0` **only** when every selected module and overlay hook
succeeded *and* the final doctor passes. Any selected failure exits non-zero
with the failing check and its repair command — the installer never prints a
success banner over a broken environment.

After module/overlay installation it adds one marked, file-existence-guarded
block to `~/.zshrc` that sources generated launchers
(`~/.pi/generated/pi-launchers.zsh`). When an overlay has generated them, a
**new shell** has `pi-list` and its `pi-<alias>` commands (e.g. `pi-sonnet`).
Existing content is preserved with a private backup; re-running never duplicates
the block. Set `PI_SETUP_NO_SHELL_RC=1` to opt out, or `PI_SETUP_ZSHRC` to target
another rc file. Shell startup is never executed by installation or diagnostics.
Bare `pi` uses the active profile, not necessarily the generated catalog.

## Health check

```bash
bin/doctor                              # known modules present under code root
bin/doctor --module pi-shared           # minimal composition only
bin/doctor --require-catalog           # explicitly require generated model artifacts
bin/doctor --overlay /path/to/overlay   # explicitly trust and run its doctor
bin/doctor --smoke-model sonnet         # opt into one real completion via pi-sonnet
```

- Parses the active profile's `settings.json` package entries and resolves the
  exact pi-shared path; checks declared runtime dependencies, then delegates
  extension loading to `pi-shared/bin/pi-profile-check`. Uses
  `PI_CODING_AGENT_DIR`, then `PI_SHARED_AGENT_DIR`, then
  `~/.pi/agent` for a standalone doctor invocation. During installation,
  pi-setup explicitly checks `PI_SHARED_AGENT_DIR` (default `~/.pi/agent`),
  matching pi-shared's install target rather than the calling Pi profile.
- Delegates known service checks to `model-gateway verify` (service identity
  and liveness only) and `scripts/local-search verify` (local readiness,
  telemetry when enabled, and the exact MCP tool list). These read-only checks
  honor each module's persisted port configuration and make no provider calls.
- Checks the selected/default profile through the **installed Pi SDK resource
  loader**, not `pi --list-models`. Extension registration code executes, as at
  normal Pi startup; it may have side effects. The helper inspects extension
  errors and fails on timeouts, missing reports, or nonzero exits.
- When model-gateway is selected and generated launchers/profile settings already
  exist, also checks launcher syntax/shell wiring and the generated
  `~/.pi-omlx/agent` profile with a nonempty model catalog. Use `--require-catalog`
  to require these artifacts explicitly. A fresh gateway install without a
  catalog remains a valid direct-provider setup; no overlay is mandatory.
  An explicitly selected pi-databricks overlay is required to load as a package
  when the generated profile is checked.
- Before invoking the pinned browser-worker verifier, checks for installed uv
  and Python 3.12 with `uv python find --no-python-downloads`. Missing prerequisites
  fail without calling the operator's provisioning path; provisioning belongs
  to the explicit installer. Verification runs with uv offline/downloads disabled.
- Does not test model inference unless `--smoke-model` is explicitly passed;
  that opt-in requires a standalone `PI_OK` response. Browser execution is not
  tested. Source presence alone is not reported as installed/ready.
- The installer checks only selected modules and explicitly passed overlays.
  Any failed installer or doctor exits nonzero; earlier completed installs
  are not rolled back. No success message follows failed checks.

Inside Pi, `/mcp` lists servers managed by the optional MCP adapter only.
The native browser/search wrappers connect to their independent MCP services
outside that adapter. With the environment-doctor extension loaded, use
`/mcp-connections` (or `dev_doctor`) to see both paths without connecting to
services. This does not change server registration or make tools service-ready.

### Browser dependency

Normal installation includes the recommended browser-worker dependency. Its
installer provisions the browser/runtime and a private, per-install
`pi-production` token; users do not need an external API key or a token from
another machine. After overlays complete, pi-setup verifies the worker and wires
its URL/token **path** into the research configuration. No token value is stored
in that configuration, and existing web-search endpoints are unchanged.

The doctor checks the selected worker; an installed, enabled but broken dependency
is a failure, not optional success. Unselected modules are not probed, even when
present. `--minimal` skips recommended modules;
`--minimal --with-browser-worker` installs only shared resources plus browsing.
Explicitly deselected browser capability remains clearly marked `DISABLED`.
The Databricks search shim stays on `8891`; browser-worker defaults to `8890`.
Re-running the Databricks web hook preserves an independently enabled worker.

### Recovery

| Symptom | Fix |
|---|---|
| `Cannot find module 'yaml'` / `'patchright'` on `pi` start | `~/local_code/pi-shared/install.sh` (runs locked `npm ci` per extension) |
| duplicate `enterprise_*` tool errors | overlay wiring hook, e.g. `~/local_code/pi-databricks/setup.d/30-pi-wiring.sh` |
| `pi-list: command not found` | re-run `./install.sh` (adds the `~/.zshrc` block), then open a new shell |
| doctor: model catalog/profile missing | Re-run the overlay's model hook; it repairs artifacts even after activation succeeded. |
| doctor: AI Dev Kit source or Python imports missing | Run `pi-databricks/setup.d/26-enterprise-runtime.sh`. |
| Browser-worker unavailable | Re-run the installer (or `browser-worker/install.sh`), then `pi-shared/bin/pi-browser-check`. Custom deployments can set `browserWorkerMcpUrl`/`browserWorkerTokenFile` or the corresponding environment overrides. |

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
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests
python3 -B -m unittest discover -s tests -p test_setup.py -v
bash -n install.sh
```

The full suite requires pytest (the unittest command covers only `test_setup.py`).
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
