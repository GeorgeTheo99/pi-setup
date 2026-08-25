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
| [local_web_search](https://github.com/GeorgeTheo99/local_web_search) | optional | Loopback web search / page retrieval broker on `:8889` (needs a personal Brave Search API key) |

## Prerequisites

- macOS with [Homebrew](https://brew.sh)
- `git`, `curl`, `python3`
- [uv](https://docs.astral.sh/uv) (`brew install uv`) — used by the service modules
- Pi itself: `npm install -g @mariozechner/pi-coding-agent` (or see the Pi docs)

## Install

```bash
git clone https://github.com/GeorgeTheo99/pi-setup.git ~/local_code/pi-setup
cd ~/local_code/pi-setup

./install.sh                          # required + recommended
./install.sh --minimal                # pi-shared only
./install.sh --with-local_web_search  # add the search broker
./install.sh --update                 # pull all modules + re-run installers
```

Post-install (module-specific):

- **model-gateway** needs a `model-info.json` catalog and provider keys in its
  gitignored `config/config.yaml` — see its `docs/deployment.md`.
- **local_web_search** needs your Brave Search API key (env var or secret
  file — see its README).

## Health check

```bash
bin/doctor
```

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
