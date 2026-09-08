# pi-setup

Compose a working [Pi](https://github.com/badlogic/pi-mono) coding-agent
environment from independent modules — without copying any of them into this
repo. Each module is its own git repository with its own installer; `pi-setup`
just clones, pins, installs, and health-checks them.

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
- Node.js / `npm` (`brew install node`) — Pi extensions have locked npm dependencies
- **Pi itself, installed first**: `npm install -g @mariozechner/pi-coding-agent`
  (or see the Pi docs). `install.sh` refuses to start until `pi` is on `PATH`.

## Install

```bash
git clone https://github.com/GeorgeTheo99/pi-setup.git ~/local_code/pi-setup
cd ~/local_code/pi-setup

./install.sh                          # required + recommended
./install.sh --minimal                # pi-shared only
./install.sh --minimal --with-browser-worker # shared resources + rendered browsing
./install.sh --with-local_web_search  # add the search broker
./install.sh --update                 # pull all modules + re-run installers
```

Post-install (module-specific):

- **model-gateway** needs a `model-info.json` catalog and provider keys in its
  gitignored `config/config.yaml` — see its `docs/deployment.md`.
- **local_web_search** needs your Brave Search API key (env var or secret
  file — see its README).

## What "done" means

`install.sh` exits `0` **only** when every required module and overlay hook
succeeded *and* the final doctor passes. Any required failure exits non-zero
with the failing check and its repair command — the installer never prints a
success banner over a broken environment.

On success it also adds one marked block to `~/.zshrc` that sources the
generated launchers (`~/.pi/generated/pi-launchers.zsh`), so a **new shell**
has `pi-list` and the `pi-<alias>` commands (e.g. `pi-sonnet`). Re-running never
duplicates the block; set `PI_SETUP_NO_SHELL_RC=1` to opt out and source it
yourself. Bare `pi` starts the default profile without the generated catalog.

## Health check

```bash
bin/doctor                        # no model calls, shell startup, or repair
bin/doctor --smoke-model sonnet   # + one real no-tools completion via pi-sonnet
```

The doctor checks the Pi version, dependency resolution, and both the default
and generated model profiles through the **installed Pi SDK resource loader**.
It inspects collected extension errors directly; `pi --list-models` is not an
extension-readiness test. Timeouts, missing verification reports, and nonzero
exits fail the check. The generated profile must contain models and load its
required packages, independent of `~/.local/bin` being on `PATH`.

It also checks service identity, launcher syntax and shell-hook configuration,
and each overlay's doctor. It does not execute `.zshrc`, run profile repair, or
claim to have tested a fresh interactive shell. Third-party extension registration
code does execute, as it does at normal Pi startup. `--smoke-model` explicitly
opts into an end-to-end completion and requires a standalone `PI_OK` response.

Normal installation includes the recommended browser-worker dependency. Its
installer provisions the browser/runtime and a private, per-install
`pi-production` token; users do not need an external API key or a token from
another machine. After overlays complete, pi-setup verifies the worker and wires
its URL/token **path** into the research configuration. No token value is stored
in that configuration, and existing web-search endpoints are unchanged.

The doctor checks the selected worker; an installed but broken dependency is a
failure, not optional success. `--minimal` skips recommended modules;
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
- `bin/doctor` — optional extra health checks picked up by the main doctor

```bash
./install.sh --overlay <path-or-git-url>
```

## Design

- **No vendored code.** Modules stay independent repos; this repo owns only
  composition. Pinning happens via `ref:` in `manifest.yaml`.
- **Idempotent.** Re-running is safe; `--update` is the explicit upgrade path.
- **Overlay, don't fork.** Installer logic lives here once; overlays are data
  plus hooks.

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
