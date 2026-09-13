# Homebrew distribution: pi-shared

`pi-shared` is the public package and command name. `pi-setup` remains the source
of installation orchestration; `pi-shared` remains the independent shared-resource
repository. The companion
[`GeorgeTheo99/homebrew-tap`](https://github.com/GeorgeTheo99/homebrew-tap) owns
stable source archive/checksum pins and the macOS Homebrew package CI.
**Publication is a separate approval/release step.** Maintainers must publish
source/tap refs and pass that CI before promoting a new package release.

## Installation contract

The stable tap pins a source release archive and its SHA-256. Development
installation adds `--HEAD`. Review
[the formula](https://github.com/GeorgeTheo99/homebrew-tap/blob/main/Formula/pi-shared.rb)
and its source before granting trust. The first command persistently trusts only
this formula, including future revisions—not the entire tap. On older Homebrew
versions without `brew trust`, omit that first command. For stable installation:

```bash
brew trust --formula georgetheo99/tap/pi-shared
brew install georgetheo99/tap/pi-shared
pi-shared setup
pi
```

Homebrew owns the orchestration code and a pinned Pi npm runtime. It installs
Node, Python, uv and Git; npm uses `runtime/package-lock.json` and disables
lifecycle scripts. It does not execute the module installer, edit HOME, register
services, ask for credentials or download LLM weights during `brew install`.
An existing `pi` binary must be reconciled explicitly if Homebrew reports a link
conflict; never use `brew link --overwrite` blindly.

The command can also run from a trusted source checkout without Homebrew:

```bash
./bin/pi-shared setup --mode later --without-browser --plan
```

Source users must already have the prerequisites listed in the README. The
Homebrew-owned Pi runtime is not installed by this source command.

## Homebrew trust errors

If installation refuses to load `georgetheo99/tap/pi-shared` from an untrusted
tap, first review [the formula and its source](https://github.com/GeorgeTheo99/homebrew-tap/blob/main/Formula/pi-shared.rb).
If you trust this package, explicitly trust only the formula and retry:

```bash
brew trust --formula georgetheo99/tap/pi-shared
brew install georgetheo99/tap/pi-shared
pi-shared setup
```

`brew trust --formula` accepts the fully qualified name before the tap exists;
run it before retrying installation even if `brew tap` itself failed. A separate
`brew tap` is unnecessary. This is persistent permission to load this formula,
including future revisions, not a checksum verification or trust grant for the
entire tap. Do not disable Homebrew's trust checks globally.

On Homebrew 6.0.22, tap validation catches formula-load failures across simulated
platforms. Trust rejections can therefore produce repeated `Invalid formula`
messages followed by `Cannot tap georgetheo99/tap: invalid syntax in tap!`.
When the preceding error is an untrusted-tap rejection, the final message is not
evidence of invalid Ruby syntax or platform incompatibility. Failed tap
validation may remove the newly cloned tap; formula-scoped trust can still be
recorded before the next install attempt.

If a different error remains after granting trust, investigate that error rather
than trusting the whole tap.

## Setup choices

| Choice | What setup selects | What still requires user configuration |
|---|---|---|
| Cloud | Shared resources, gateway, browser-worker | Pi subscription login/API key; gateway provider onboarding if desired |
| Local | Same modules; oMLX existing/install/guidance choice | Model selection/download, reviewed Pi/gateway route, actual inference test |
| Both | Cloud and local choices | Both sets of model configuration |
| Later | Shared resources and browser-worker | Model/provider setup later |

`--without-browser` omits browser-worker and its Chromium download. This does
not install the separate private `app_*` browser binaries: those are an optional
Pi browser-capture prerequisite. Other optional features, such as PowerPoint
preview (LibreOffice and Poppler), remain separately installed.

Each interactive plan ends with approval. EOF, Ctrl-C, or declining approval
stops setup; completed actions are never claimed to have been rolled back.
Non-interactive use requires explicit `--mode` and `--yes`. A read-only `--plan`
performs no provisioning, executable probes, network calls, or state writes.

```bash
pi-shared setup --mode cloud --plan
pi-shared setup --mode cloud --yes
pi-shared setup --mode later --without-browser --yes
pi-shared setup --local
pi-shared setup --mode both --omlx guide --recovery guide --plan
```

Writable modules live under `~/.local/share/pi-shared/modules`, outside the
Homebrew Cellar. `PI_SETUP_CODE_ROOT` overrides that location for both setup and
the receipt. Existing checkouts must satisfy the original installer's exact
origin/ref/clean-worktree rules. No developer checkout is silently adopted,
reset, overwritten or replaced. Selection is additive: skipping a module does
not stop or uninstall an earlier selection.

## Shell commands before model configuration

Open a new shell after setup and run `pi-list`. Fresh setups include `pi-list`,
`pi-regen`, `pi-shared-update`, `pi-restart`, `pi-default`, and `pi-openai` without
requiring a gateway catalog. Sourcing defines commands, appends `~/.local/bin` to PATH once and registers
an interactive local-file refresh hook. It never upgrades software, contacts
models or starts services. Changed catalog/launcher data refreshes at the next
prompt; manually edited model outputs require reconciliation rather than being
overwritten. Existing generated direct-launcher choices and explicit
`PI_SHARED_DIRECT_LAUNCHERS=0` opt-outs survive reruns.

Until the alias export is configured, `pi-list` says so and no placeholder
`models.json` is written. The future gateway profile is wired in advance.
Configure the gateway to export the alias catalog at the path shown by `pi-list`,
then run `pi-regen`; it creates model shortcuts and reloads them into the current
shell. Direct Pi `/login` alone does not create gateway aliases. Missing or
invalid input never replaces a configured launcher with an empty list.

Setup opts into this compatible shared-installer feature with
`PI_SHARED_BOOTSTRAP_LAUNCHERS=1`; setting it to `0` opts out. Status remembers
whether command bootstrap was requested and verifies the shell/profile without
requiring nonexistent gateway models. `--require-catalog` on the underlying
doctor remains strict. New setup must be paired with a pi-shared version that
supports bootstrap; missing generated launchers fail the requested check.

`pi-restart` still requires an installed service. For oMLX it uses the existing
server-ci manager, or oMLX's own app/Homebrew CLI when server-ci is absent. It
never installs a service as a side effect of restart. `pi-shared-update` updates
the writable shared-resource checkout, not Homebrew's packaged Pi runtime.

## oMLX: optional, never an implicit model download

Setup offers three actions:

- **Existing:** explicitly supply a numeric loopback OpenAI base URL. Query only
  `/v1/models`, with bounded responses, no redirects/proxies, no credentials and
  no inference. Do not change its process manager, configuration or models.
- **Install:** Apple Silicon/macOS 15+ only. Show total RAM and free disk, refuse
  an existing oMLX executable/settings/known service or an occupied default
  port, and use the upstream Homebrew tap and service. Wait for model discovery
  before declaring that startup check passed. Failure leaves the setup receipt
  incomplete and reports that the requested service was not stopped.
- **Guide:** do not install or start oMLX. Show its official installation and
  model-management documentation. This is the non-interactive default.

```bash
pi-shared setup --mode local --omlx existing \
  --omlx-url http://127.0.0.1:8000/v1 --yes
# Only for a new installation; never adopts an existing uv/app/launchd server:
pi-shared setup --mode local --omlx install --yes
```

Fresh oMLX installation delegates to upstream:

```bash
brew tap jundot/omlx https://github.com/jundot/omlx
brew install jundot/omlx/omlx
brew services start jundot/omlx/omlx
```

Source: upstream [formula](https://github.com/jundot/omlx/blob/main/Formula/omlx.rb)
and [documentation](https://github.com/jundot/omlx). Its installer may download
substantial Python/audio dependencies. No LLM weights are requested by this
wrapper. Fresh upstream defaults are loopback port 8000, models at
`~/.omlx/models`, and model management at `http://127.0.0.1:8000/admin`.

Choose model files in oMLX only after reviewing their download size and memory
requirements. Total physical RAM is not available RAM; context and caches also
consume memory. Setup makes no universal model-fit promise and does not pick a
model on your behalf. Configure a reviewed Pi/gateway route with exact model
ID and verified limits, then test an actual response. **The gateway's cloud
onboarding generator requires HTTPS and must not be used for a local HTTP
endpoint.** Automatic local route generation is not part of this release.
Authenticated/remote discovery also remains manual; never embed keys in URLs.

## Search and recovery

Search is opt-in and requires a credential **before** its module installer runs:

```bash
# Pre-clone local_web_search under the selected module root and follow its
# README to create owner-only data/brave_key (0600, parent directory 0700).
# A custom LOCAL_SEARCH_DATA_DIR is also supported by the module installer.
pi-shared setup --mode cloud --with-search --plan
pi-shared setup --mode cloud --with-search --yes
```

Setup does not collect, log or store API keys. A missing credential causes the
search installer to fail, not a false success. Earlier module installs may
already have completed; retries use the original idempotent installer.

`pi-fallback` remains a **separate optional recovery prototype**, independent of
Pi profiles, oMLX, gateway, cloud auth and the shared package. `--recovery guide`
shows preparation instructions; it neither downloads nor installs it. There is
currently no verified public recovery release/formula contract to depend on.
When a trusted contained release becomes available, install it separately,
review the curated model size/RAM estimates, install compatible weights and
verify an actual tool call. A doctor exit code alone is insufficient if it says
the model test was skipped. Offline recovery is not ready until those checks pass.

## Status, upgrades and uninstall

Use **`pi-shared update`** for routine updates of the complete saved selection.
It wraps the owning Homebrew package upgrade, re-executes the new CLI, updates
selected modules/dependencies, regenerates commands and verifies the result.
See [one-command updates](updates.md) for receipt migration, ownership checks,
unchanged-service behavior, automatic prompt refresh and failure boundaries.
The lower-level commands below remain available for explicit partial operations.

`~/.config/pi-shared/setup.json` is an owner-only receipt recording the selected
module root, profile and modules, not credentials. It is marked incomplete before
installation and completed only after the selected module checks and requested
oMLX action succeed. `pi-shared status` refuses incomplete/unsafe receipts and
re-runs the existing doctor for the last selected modules. It executes trusted
extension registration checks; it is not a static/sandboxed inspection. It does
not prove authentication, inference, browser execution, oMLX health or recovery
readiness. Use the component-specific checks for those capabilities.

- `brew upgrade pi-shared` updates the packaged orchestration/Pi runtime only.
  Do not use `pi update` or global npm to mutate the Homebrew-managed runtime.
- `pi-shared setup --mode ... --update` explicitly fetches and reruns selected
  module installers. Include optional selections again. There is no unattended
  service restart on Homebrew upgrade; modules may restart during explicit setup.
- `brew uninstall pi-shared` removes the formula, not your modules, services,
  credentials, profiles, sessions or models. Stop/manage these using their own
  documented operators before uninstalling runtime prerequisites. No recursive
  HOME cleanup or destructive uninstall is supplied.
- Gateway/browser/search retain their own per-user service managers. There is
  no competing `brew services start pi-shared` service. Only opt-in oMLX uses
  its upstream Homebrew service in this flow.

The orchestration source/Pi runtime can be pinned for a release, but module refs
currently include `main` (and a pinned browser-worker commit). A fresh install
can therefore select newer module code than a prior one. Full-stack immutable
release pinning and binary bottles are not claimed.

## Verification and release gate

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests
bash -n install.sh
./bin/pi-shared setup --mode later --without-browser --plan
```

The CLI suite isolates HOME and stubs all provisioning and model/service calls.
For an explicit network-enabled integration smoke, run
`tests/smoke_minimal.sh /absolute/path/to/trusted/pi-shared`. It installs the
pinned Pi runtime and real shared extension dependencies in a disposable HOME,
uses the local shared repository's committed HEAD, and checks real profile
loading and status. It never selects services, browsers or models; temporary
files are removed on exit. npm registry downloads may occur.
The tap's tests cover packaging and release pinning separately. Before public
promotion: review changes, publish approved source/tap refs, pin a verified
source archive, run Homebrew installation/tests in a disposable Mac environment,
and exercise selected service setup there. Do not test fresh installation over
an existing production service stack.
