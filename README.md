# pi-setup

Compose a working [Pi](https://github.com/badlogic/pi-mono) coding-agent
environment from independent modules — without copying any of them into this
repo. Each module is its own git repository with its own installer; `pi-setup`
just clones, pins, installs, and checks local configuration/service evidence.
A successful check is not proof that provider authentication or inference works.

## Homebrew entry point: `pi-shared`

The public command is **`pi-shared`**; this repository owns its orchestration.
Start with the [main pi-shared README](https://github.com/GeorgeTheo99/pi-shared)
for the user-facing install → connect → use → update journey. This repository
is the installer backend, not a separately running sidecar.
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
pi models                             # configured model aliases, once a catalog exists
```

If Homebrew reports an **untrusted tap** (possibly followed by “invalid syntax
in tap”), review the formula and follow the
[formula-scoped trust recovery](docs/homebrew.md#homebrew-trust-errors) before
retrying installation. Do not disable trust checks globally.

Fresh setup wires the **unified `pi` CLI** — no shell startup file is written.
The packaged `pi` runs stock Pi until a setup receipt exists, then transparently
routes configured model aliases through the shared launcher. Model aliases are
stored in `~/.pi/launcher.json` (override with `PI_LAUNCHER_CONFIG`); list them
with `pi models` (also `pi --launcher-list`), validate with `pi --launcher-check`, regenerate
offline with `pi --launcher-refresh`, and see all subcommands with
`pi --launcher-help`. `pi <alias>` launches that route, `pi -- <prompt>` bypasses
aliases, and `pi list` remains the stock package command. Aliases appear once a
gateway/model catalog is configured; before that `pi` behaves as stock Pi.
Existing direct-launcher choices and explicit opt-outs are preserved, and no
generated shell functions are required or created.

Routine maintenance is now `pi-shared update`: it remembers the selected setup,
updates its owning package/source and selected modules, refreshes the launcher
config (offline, via `pi --launcher-refresh`), and verifies the result.
`pi-shared update --plan` is read-only. Updates never touch software, services,
or models beyond the recorded selection. See [update ownership and safety](docs/updates.md),
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
./install.sh --with-omnigent          # also require native-Pi prerequisites (no session launch)
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

### Connect an existing remote gateway (direct)

This is unreleased source functionality, not part of Homebrew 0.1.6. It requires
a matching pi-setup release and the updated shared `pi-gateway` helper; confirm
`existing-gateway` appears in `pi-shared setup --help` before using it.

Use your existing gateway endpoint and an already-provisioned client key file:

```bash
pi-shared setup --mode existing-gateway \
  --gateway-url https://gateway.example \
  --gateway-key-file "$HOME/.config/model-gateway/client.key" --plan
# Review the plan, then replace --plan with --yes to connect.
```

The key file must be an absolute, non-symlink, owner-only `0600` regular file.
Never pass the key itself in arguments or embed it in the URL. HTTPS is preferred;
trusted private/Tailscale **numeric IP** HTTP endpoints require the explicit
`--allow-private-http` opt-in. A trailing `/v1` is accepted and normalized.

This selects shared resources and browser-worker (omit browsing with
`--without-browser`), **not a local gateway or oMLX**. Setup reads the remote
`/v1/models/canonical` catalog with your existing key and configures `pi models`
and `pi <alias>`; it neither provisions credentials nor changes remote services,
uses federation, downloads weights, or tests inference. Failed discovery leaves
setup incomplete. The receipt stores only the endpoint, key-file reference and
HTTP opt-in. Rerunning `pi-shared setup --mode existing-gateway --yes` reuses the
saved connection and explicitly refreshes discovery; repeat optional module
selections as needed. Setup refuses to silently discard an external connection
or mix it with a previously selected local gateway/oMLX.

`pi-shared update` preserves the connection and checks it **offline only**;
`pi-shared status` also checks local configuration, not remote readiness.

### Optional Omnigent compatibility

The supported compatibility path is Omnigent's native Pi terminal with the
standard `~/.pi/agent` profile and explicit Pi provider/model arguments:

```bash
omnigent pi --provider <pi-provider> --model <pi-model-id>
```

Public CLI: `pi-shared setup --mode cloud --with-omnigent` (use `--plan` first).
Private adapter: `pi-databricks install --with-omnigent` (0.2.1+, with public
pi-shared 0.1.4+). Source: `./install.sh --with-omnigent`.

`--with-omnigent` adds prerequisite/package-import checks; it does not install
Omnigent, launch sessions, select a server, rewrite profiles, or verify inference.
Normal installations do not require or invoke Omnigent. See the
[compatibility contract and isolated smoke test](docs/omnigent-compatibility.md)
for the supported scope, limitations, and evidence required before publication.

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

Fresh setups write **no `~/.zshrc`**: the unified `pi` CLI needs no shell startup
wiring. Configured model aliases are available immediately as `pi <alias>` and
`pi models`. For backward compatibility, the installer still *removes*
any recognized legacy launcher block/source line it previously added to
`~/.zshrc`, keeping a private backup; it never writes or sources a shell rc. Set
`PI_SETUP_NO_SHELL_RC=1` to skip that cleanup, or `PI_SETUP_ZSHRC` to target
another rc file. Shell startup is never executed by installation or diagnostics.
Bare `pi` uses the active profile and routes through the launcher only when the
first argument is a configured alias.

## Health check

```bash
bin/doctor                              # known modules present under code root
bin/doctor --module pi-shared           # minimal composition only
bin/doctor --require-catalog           # explicitly require generated model artifacts
PI_CODING_AGENT_DIR="$HOME/.pi/agent" bin/doctor --module pi-shared --require-omnigent
# Checks native-Pi prerequisites; does not start Omnigent or test inference.
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
- When the unified CLI is selected (a `~/.pi/launcher.json`, `PI_SHARED_CLI_OUT`,
  or `PI_SHARED_BOOTSTRAP_LAUNCHERS=1`), the doctor runs `pi-launch
  --launcher-check` — a read-only, offline validation of the launcher config that
  contacts no provider and needs no stock runtime. Use `--require-catalog` to
  additionally require a generated `~/.pi-omlx/agent` profile with a nonempty
  model catalog. A fresh gateway install without a catalog remains a valid
  direct-provider setup; no overlay is mandatory. Legacy `pi-launchers.zsh`
  metadata is still syntax-checked and reported as migratable, but shell wiring
  is no longer required. An explicitly selected pi-databricks overlay is required
  to load as a package when the generated profile is checked.
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
| `pi --launcher-list` shows no aliases | Configure a gateway/model catalog, then `pi --launcher-refresh`; before that `pi` behaves as stock Pi |
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
providers, or require running services. `tests/test_bootstrap.py` covers the
packaged `bin/pi` wrapper (stock argv/env, `PI_UPSTREAM_BIN` validation,
delegating to the shared launcher after setup, and refusing unsafe/invalid
receipts) against fake stock and launcher executables.

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
