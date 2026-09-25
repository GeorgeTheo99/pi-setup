# Removing user setup

Public CLI 0.1.13 adds an explicit teardown separate from Homebrew package removal:

```sh
pi-shared uninstall --plan
pi-shared uninstall --yes
# Only when the packaged runtime is no longer needed:
brew uninstall georgetheo99/tap/pi-shared
```

Remove any separately installed overlays through their own uninstall commands
first. Public teardown has no knowledge of private integrations and does not
remove their resources. Homebrew removes its formula files; it does not invoke
this teardown or delete per-user configuration. Formulae do not offer the cask
`uninstall`/`zap` lifecycle.

`--plan` lists exact file actions and retained state without writes, authentication,
inference, or service changes. `--yes` applies the plan under the setup/update
lock. The schema-2 setup receipt identifies selected modules and service owners.
Unrecorded source installations and old receipts require explicit reconciliation.
No receipt means no automatic cleanup. Plans check on-disk ownership; applying
also verifies that launchd's loaded service points at the expected plist.

Teardown detaches matching shared package registrations, instruction links and
helper symlinks, stops/unregisters recorded gateway/browser/search services,
archives generated launcher routing, and retires the receipt last. Shared
service stops affect **all clients** of those services. Remote gateways are
never stopped. Homebrew oMLX service teardown is not supported by this initial
command and is refused before changes; reconcile that selection separately.

To prepare Pi's model configuration for a different setup mode:

```sh
pi-shared uninstall --archive-config --plan
pi-shared uninstall --archive-config --yes
```

This additionally detaches the `model-gateway` provider from regular profile model
files, or unlinks a catalog containing only that provider, and clears matching
default model selection. Other providers survive. Catalog link targets remain
intact. The option never follows a link to delete its target or resets credentials.

| Source of truth | Retained state |
|---|---|
| Recorded module root | Checkouts, dependencies, local changes |
| Standard/configured Pi profiles | Sessions, auth, custom settings; models unless explicitly archived |
| Gateway source paths reported in the plan | Provider/workspace pools, configuration, model-info, credentials, ledger and backups |
| Research settings and browser-worker data | Client configuration, token files and browser data |
| Other harnesses and shared alias catalog | All state; other clients may depend on it |
| Logs, downloads, package caches | Diagnostic/history data and reusable downloads |
| Setup directory | Inert `update.lock`; receipt is archived |
| Homebrew | Packages and dependencies until explicitly removed |

Retained research configuration may still select a now-stopped browser or search
service. Reinstall the selected service or explicitly update that configuration
before using those capabilities. Legacy shell wiring and custom helper links
are retained; restart old shells and review any manually managed wiring.

Before mutation, an owner-only recovery directory is created under
`~/.local/state/pi-shared/uninstall/<run>/`. Its `manifest.json` maps original
paths, types and modes to numbered backups. Backups for regular files contain
original bytes; backups for links contain link targets as text. Archives may
contain secrets from configuration and must not be published. Restoring requires
reviewing the manifest, restoring selected original files/links with their modes,
then starting intended services through their component operators. Never overwrite
new user changes during recovery.

The operation is not atomic across files and services. Failures preserve the
receipt and recovery archive; resolve the cause and retry. A second completed
uninstall is a no-op. Symlinked parents, unsafe/hardlinked files, malformed config,
changed service identities, and files changed after planning are refused.

This is user-setup teardown, not a clean-machine reset. For fresh-onboarding
acceptance, separately inventory and archive retained configuration and checkouts
within an approved reset boundary. Do not delete shared credentials, packages,
controller state, or another user's files to simulate a fresh account.
