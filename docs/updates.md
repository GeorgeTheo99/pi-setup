# One-command updates

After setup with this version, routine maintenance is:

```sh
pi-shared update
# Package 0.1.11+ also accepts:
pi update
```

There is no repeated component questionnaire and no manual regeneration step.
Starting with package 0.1.11, **bare `pi update`** forwards to the same owning
`pi-shared update` command, updating the pinned runtime and saved modules. It
prints that scope before delegation, preserves the updater's exit status, and
never searches PATH for a different updater. The updater still validates the
saved receipt; before setup it reports that setup is required.

Only the exact bare command is redirected. Explicit arguments, including
`pi update --extensions`, `--models`, `--extension <source>`, and `--help`, retain
stock routing. Stock self-update forms (`--self`, `self`, `pi`, `--all`, `--force`)
remain unsupported for the Homebrew-owned runtime; use the bare command instead.
Use `pi-shared update --plan` or `--modules-only` for managed update options.
`pi -- update` remains a literal prompt, not an update request.

Homebrew owns the pinned runtime; never use global npm to mutate it. Updates
install the latest published package, not necessarily the latest npm Pi version.
Packages 0.1.10 and 0.1.11 pin stock Pi 0.87.1. Restart running Pi sessions after a runtime upgrade; `/reload` only
reloads extensions and does not replace the running runtime.
`pi-shared update --plan` reads the saved plan without commands, downloads or
writes. `--modules-only` is an advanced/testing option that skips updating the
owning CLI/runtime. A resource-only source installation without that coordinator
keeps its explicitly labelled, limited Git updater.

## Remembered configuration

The owner-only `~/.config/pi-shared/setup.json` receipt uses schema 2. It records
selected modules, profile/module paths, the unified-CLI config path (`cli_file`,
default `~/.pi/launcher.json`), supported non-secret installer settings,
successful applied revisions, runtime versions and service ownership
fingerprints. Legacy receipts additionally retain `launchers_file` as a
migration input; it is never evaluated as shell. It does not copy model weights,
provider credentials, or complete service environments. Model choices and
authentication remain in their existing configuration files. The launcher
config's generation metadata is authoritative for model endpoint/output settings
and direct-shortcut choices. Remembered custom paths must be absolute (a leading
`~` is expanded during setup).

`setup --mode direct` (Homebrew **0.1.8+** with an updated shared module) keeps
schema 2 and records `PI_SHARED_DIRECT_ONLY=1` in settings. Update/status reject receipts
that mix this policy with gateway/oMLX selections, inconsistent paths or disabled
CLI bootstrap. Updates preserve the policy and custom paths rather than adopting
calling-shell overrides. Launcher generation metadata is checked offline before
updates/status and after installation/update: it must be direct-only, without
catalog, model output or gateway profile arguments. Older shared installers that
ignore the policy cannot complete successfully. Native Pi authentication/model
settings remain native; no provider calls are made. Setup reruns reuse saved paths
and shortcut opt-outs; prior gateway/oMLX selections, including incomplete
receipts, require manual reconciliation rather than automatic migration/removal.

`existing-gateway` setups additionally record `external_gateway`: the
normalized endpoint, absolute key-file reference and explicit private-HTTP
opt-in. This optional schema-2 field is backward-compatible; key contents are
never copied into receipts. Update preserves that connection and its managed
outputs and invokes shared `pi-gateway check` **offline only**. It does not fetch
the remote catalog, provision credentials, claim remote service ownership,
restart remote services, or test remote readiness/inference. To explicitly
refresh authenticated discovery, rerun `pi-shared setup --mode existing-gateway --yes`
(repeat optional module selections); omitted connection arguments and
saved custom paths are retained. `status` uses the same offline connection
check. Connection/check failures cannot produce a completed receipt.

Receipts from 0.1.2 and earlier lack some of this information. They remain readable
by `status`, but `update` refuses to guess their missing settings. Run setup once
with the new version, supplying original custom overrides where applicable.
This is a one-time capture, not a questionnaire on every update. Existing oMLX
connections remain separately managed unless installed/recorded as owned by this
setup; do not choose a fresh oMLX installation over an already running manager.

## What updates

1. Check saved selections and current private service ownership/settings.
2. Update the owning package. A Homebrew-installed CLI verifies its marker and
   installed prefix, runs `brew update` and upgrades that exact formula with
   `--fetch-HEAD` (refreshing existing HEAD installs without switching stable
   installations to HEAD). A source
   installation requires a clean, attached Git checkout with an upstream and
   uses a fast-forward-only pull. The process re-executes the updated CLI while
   retaining the per-user operation lock.
3. Fetch only the selected component checkouts and follow manifest refs. A new
   recommended module is not silently selected. Dirty or mismatched repositories
   are never reset, cleaned, forced or adopted.
4. Always synchronize shared extension dependencies. Reapply service installers
   only when their source differs from the last successful applied revision.
   Runtime prerequisite changes also trigger reapplication of selected services.
   Component installers own their restart/verification; there is no duplicate
   orchestration restart pass. An unchanged, healthy service is not restarted.
5. Regenerate the unified-CLI config offline from its recorded generation
   metadata (`pi --launcher-refresh`; never a shell eval), run the selected
   doctor checks, and save success only after all selected work succeeds. New
   setups always carry `PI_SHARED_CLI_OUT`, so this path replaces the old shell
   launcher refresh. Legacy receipts without `cli_file` keep the limited Git
   launcher updater until they are migrated by running setup once with this
   version.

Owned Homebrew oMLX is upgraded separately. Its active package version is compared
to the last successfully applied version, so a failed restart is retried even
when the package upgrade already succeeded. The applied version is checkpointed
only after restart/readiness checks and ownership capture succeed. Its current local port is read from its settings; authenticated
endpoints may establish liveness without asserting model readiness. Existing
external oMLX connections and the independent pi-fallback recovery product are
not upgraded by this workflow. No LLM weights or model completions are requested.
Homebrew still applies its ordinary dependency-upgrade semantics.

Gateway and search settings are recovered from current owned private plists,
including custom config/catalog/ledger/log/backup paths. Unsupported custom
environment entries fail closed rather than being discarded. Browser-worker
recovers its own persisted settings. A replaced service identity is not adopted.

## Unified CLI refresh

New installs route model aliases through the packaged `pi` command instead of
generated shell functions, so there is no `~/.zshrc` hook to load and no new
shell to open. The alias catalog lives in `~/.pi/launcher.json` (override with
`PI_LAUNCHER_CONFIG`). With package 0.1.9+ and an updated shared module,
`pi models` groups aliases by local/cloud/direct routing and accepts `--local`,
`--cloud`, `--direct`, `--verbose`, and `--json`. `pi --launcher-list` retains
legacy tab-separated routes. The trusted shared capability manifest declares
`modelsCommand: 1` before the bootstrap forwards the command unchanged; older
modules retain legacy list/help translation and reject new options with update
guidance. Unconfigured `models` never reaches stock Pi as a prompt.
`pi --launcher-check` validates the config read-only, and `pi --launcher-refresh`
regenerates it offline from its recorded generation metadata. None of these
contact status URLs, providers, GitHub, or model endpoints, and none run
inference; `--launcher-check`/`--launcher-refresh` do not even require the stock
runtime.

`pi <alias>` launches that route on demand, resolving the current config each
time, so newly added shortcuts appear and removed ones disappear without any
regeneration step. `pi -- <prompt>` bypasses aliases and `pi list` stays the
stock package command. The launcher reads only recognized generation options and
safe owned files, and never evaluates a shell body; stored gateway credentials
are not exposed as subprocess arguments and generated outputs are private files.
Missing or invalid catalogs cannot erase a configured launcher. Explicit
setup/update holds the per-user operation lock while it regenerates the config.

## Failure boundaries

Package and component changes are not one cross-repository transaction. Completed
steps are not rolled back automatically. An interrupted/failed application keeps
an incomplete receipt and prior successful revisions; repeating `update` repairs
selected modules rather than incorrectly skipping an already-advanced checkout.
Service identity changes still require explicit reconciliation instead of blind
adoption. The operation lock coordinates these tools, not arbitrary external
editors or other service managers. Missing ownership metadata or lost/custom generation configuration may
require setup/manual repair. Individual generated files are atomically replaced,
not a multi-file transaction; an interruption between output replacements can
require explicit regeneration.

There are no background software upgrades, service restarts, model downloads or
provider/model calls. Only local shortcut refresh is automatic. The update
command itself is the user's explicit authorization to upgrade selected software
and restart affected services.
