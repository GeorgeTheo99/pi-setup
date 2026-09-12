# One-command updates

After setup with this version, routine maintenance is:

```sh
pi-shared update
```

There is no repeated component questionnaire and no manual `pi-regen` step.
`pi-shared update --plan` reads the saved plan without commands, downloads or
writes. `--modules-only` is an advanced/testing option that skips updating the
owning CLI/runtime. The shell shortcut `pi-shared-update` delegates to the same
command when the managed setup/CLI exists. A resource-only installation without
that coordinator keeps its explicitly labelled, limited Git updater.

## Remembered configuration

The owner-only `~/.config/pi-shared/setup.json` receipt uses schema 2. It records
selected modules, profile/module/launcher paths, supported non-secret installer
settings, successful applied revisions, runtime versions and service ownership
fingerprints. It does not copy model weights, provider credentials, or complete
service environments. Model choices and authentication remain in their existing
configuration files. Regeneration arguments in the private generated launcher
are authoritative for model endpoint/output settings and direct-shortcut choices.
Remembered custom paths must be absolute (a leading `~` is expanded during setup).

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
5. Regenerate from the current launcher metadata, run the selected doctor checks,
   and save success only after all selected work succeeds.

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

## Automatic shell refresh

New generated launchers register one zsh `precmd` hook for interactive shells.
At the next prompt it checks bounded local file fingerprints. The initial prompt
establishes a refresh baseline; unchanged inputs cause no rendering or writes.
Changes to the generated launcher, alias catalog, or configured local status file
refresh model shortcuts and remove retired *generated* model functions. Personal
functions outside that recorded set are not removed. A new shell or explicit
setup/source is needed once to load this new hook into an older shell.

The helper `pi-shared/bin/pi-launchers-refresh` reads JSON argument metadata;
it never evaluates the existing shell body. Only recognized options, matching
installation/output identities and safe owned files are accepted. It invokes
the trusted renderer with arguments over stdin, so stored gateway credentials
are not exposed as subprocess arguments. Outputs are private files. Cached,
minimal previously observed oMLX hints are preserved; automatic refresh never
contacts status URLs, providers, GitHub, or model endpoints.

The model-output digest protects edits made outside the generator. Automatic
refresh pauses rather than overwriting manual changes or taking ownership of a
pre-existing manual model file. Inspect/reconcile generator settings before an
explicit `pi-regen`. Missing/invalid catalogs still cannot erase configured
launchers. Unchanged failures warn once and retry when inputs change. Shell
error-exit mode is not allowed to turn a refresh failure into an exited shell.
Prompt refresh waits while explicit setup/update holds its lock.

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
