# Bounded capability setup backend

`pi-shared capability` is the backend for the expanded `/setup` frontend. It is
separate from the broad installation workflow and standalone Peekaboo; it changes
neither their module selections nor receipts. It does not grant Pi project trust.

```sh
pi-shared capability COMPONENT plan --json \
  --project /absolute/canonical/project \
  --agent-dir /absolute/canonical/profile \
  --shared-root /absolute/canonical/pi-shared \
  --node-executable /absolute/canonical/node \
  --options '{}'
```

Components: `search`, `browser`, `mcp`, `development`, `documents`, `apple`,
`knowledge`, `models`, `diagnostics`. Actions: `plan`, `apply`, `check`.
All four context paths are required, even for components that only use some of
them. The shared root is the explicitly trusted distribution, not a path read
from project settings. Use canonical paths; symlink aliases are not resolved by
this backend. No discovery of an ancestor project or implicit active profile.

- **Plan:** offline filesystem inventory only; no processes, network, locks,
  prompts, credentials, or writes.
- **Apply:** the missing-only scaffolds below, plus explicit profile MCP migration. Repeat the identical options
  and context with `--yes --expected-plan <64-lowercase-hex-planId>`. No prompts.
  Unsupported apply, an existing target, or stale approval is an error, not a
  successful no-op. Plan IDs are absent when there is nothing to apply.
- **Check:** explicit bounded read-only prerequisite inspection, never project
  commands or language-server startup. Only Apple and diagnostics spawn probes;
  other entries perform static inventory. Check does not return a plan ID.

The Pi frontend canonicalizes its active context paths before invoking this backend.
Code-intel accepts standard Homebrew Node Cellar executables when only the prefix
or `Cellar` ancestor is admin-group-writable and the current user belongs to that
group. World-writable ancestors, other group-writable locations and executable
symlinks remain refused. This follows Homebrew's trust in other local admins.

## Options and ownership

Options must be an object. Unknown fields, duplicate keys, unsupported modes,
non-finite JSON, controls, and oversized values are rejected. Options are limited
to 16 KiB. Omit optional blank fields rather than supplying empty strings.

| Component | Options | Behavior |
| --- | --- | --- |
| `search` | `mode`: `guided` (default), `local`, `existing`; local requires absolute `keyFile`; existing requires `url`, permits absolute `keyFile` | Handoff to `pi-shared setup --guided`, `--search local --brave-key-file PATH`, or `--search existing --search-url URL [--search-key-file PATH]`. Local Brave keys and broker bearer keys use different flags. No key contents are opened. |
| `browser` | `mode`: `public` (default), `app` | Public handoff: `setup --with-browser`. Source app handoff: quoted `cd`, `npm ci --ignore-scripts`, then supplied Node/local Playwright CLI. Managed installs instead offer `pi-shared update` for dependencies and a separate local-CLI Chromium user-cache install. Never uncontrolled `npx` fetching. |
| `mcp` | `mode`: `native` (default), `migrate`; migration permits absolute `sourceConfig` | Native profile inventory, or digest-approved adapter conversion into missing `AGENT/mcp.json` with private backups and scoped `settings.json` edits. Source is unchanged; credentials are never printed/executed. Ambiguous policies and existing targets are refused. See [official MCP migration](mcp.md). |
| `development` | `mode`: `code-intel` (default), `verification`; verification optionally accepts `command`, `args`, `inputs` together | Missing-only project configuration, described below. No guessed command, install, execution or trust. |
| `documents` | `{}` | Static standard-location executable inventory for LibreOffice/soffice, Poppler/pdfinfo and pdftoppm. macOS handoffs: `brew install --cask libreoffice` and `brew install poppler`; other platforms receive package-manager guidance. No document conversion or rendering. |
| `apple` | `{}` | Plan inspects platform/standard Xcode location. Check performs the three bounded probes below. Full Xcode installation, developer-directory selection and simulator installation remain manual. |
| `knowledge` | `mode`: `initialize` (default), `ingest`; optional absolute `root` | Missing-only public metadata scaffold in a new external private root, or handoff to explicit private ingestion. |
| `models` | `mode`: `native` (default), `guided`, `gateway`, `omlx` | Native `/login` and `/model` handoffs; owning setup `--guided`, `--with model-gateway`, or `--with omlx --omlx guide`. No model calls, credentials, weights, or service startup. |
| `diagnostics` | `{}` | Check runs the trusted shared `bin/pi-doctor --json --agent-dir AGENT --timeout 5`, without any opt-in probe flags. Fixed capability/status labels and links to relevant `/setup` flows are shown; arbitrary child guidance is not reflected. |

Every setup handoff leaves approval with the existing owner. **None supplies
`--yes`.** Dependency-repair handoffs offer `pi-shared update --plan` first and
explicitly warn that `pi-shared update` applies immediately without confirmation,
can upgrade the CLI/runtime and all saved modules, and may rewire profiles/restart
shared services. It is not dependency-only repair. Broad setup preserves saved choices and can affect shared services and
profile wiring beyond the chosen capability; review its full plan and consent
there. Native MCP is built into Pi 0.99.1; the default MCP handoff no longer installs
an adapter. MCP servers and external packages run with full machine permissions:
review source before starting/installing them. Do not execute source dependency commands against
package-manager-owned installed artifacts; use the owning managed update/release
workflow instead. Ownership detection includes conventional managed roots and the
custom `code_root` from the existing CLI's fully validated saved receipt. Invalid
receipts fail closed rather than misclassifying an installation as a source tree.

Search URLs reject credentials, queries, fragments and control characters.
Bearer authentication requires HTTPS or loopback HTTP. Actual endpoint checks and
private key-file validation remain with the owning search setup flow. Handoffs
quote literal paths safely; they are displayed, never executed here.

## Missing-only scaffolds

### Project development

Targets are only `.pi/code-intel.json` or `.pi/verification.json` in the supplied
project. Existing files remain byte-for-byte untouched, even if malformed; their
presence is not a validation verdict. The backend refuses home/profile/system and
managed-runtime project destinations, unsafe ownership, symlink `.pi` paths and
unsafe targets. The frontend's separate Pi project-trust gate remains required.

Code intelligence requires installed TypeScript and typescript-language-server
at the shared extension's exact lockfile-pinned versions. It writes version 1,
`adapter: typescript-language-server`, `workspace: .`, the supplied absolute Node,
absolute shared TLS `lib/cli.mjs` plus `--stdio`, absolute TypeScript
`lib/tsserver.js`, and `timeoutMs: 15000`. Missing dependencies produce explicit
installation/update guidance, **not** an actionable apply. Installed manifests
must match exact lockfile pins; this is not an npm archive-integrity audit. Plans
show the destination and exact proposed JSON before approval. Missing dependencies
in managed installations offer the owning `pi-shared update`, never `npm ci` inside
package-manager-owned artifacts.

Verification with no explicit command selection is guidance only. Supplying all
three fields creates one check:

```json
{
  "version": 1,
  "checks": [{
    "id": "project-check",
    "command": "YOUR_EXECUTABLE",
    "args": [],
    "cwd": ".",
    "timeoutSeconds": 120,
    "inputs": {"paths": ["YOUR_EXISTING_INPUT"], "exclude": [], "untracked": "include"},
    "report": {"format": "exit"}
  }]
}
```

Inputs must be real existing literal project-relative file/directory paths, not
symlinks, traversal or globs. No source tree is executed or recursively audited
here; verification performs its own bounded source inventory later. An exit-only
check claims **no test count**. Review the new file before separate
`/verification-trust` approval in a Pi-trusted project. Setup never stores trust.

### Private knowledge metadata

Root precedence: explicit option, valid absolute `PI_SOFTWARE_KB_ROOT`, then
`~/.pi/knowledge/software-engineering`. Invalid environment path syntax is ignored
with a warning; unsafe filesystem paths fail closed. The new root must be outside
ordinary project workspaces, all recognized managed roots (including saved custom
module locations), the shared distribution, and agent profile. Launching Pi from
home or one of its ancestor directories does not turn that whole directory into
a project exclusion; the default private root remains usable there. Existing roots are
only inspected, never merged, repaired or replaced.

Only these bundled public metadata files are copied, byte-for-byte:

- `knowledge/software-engineering/sources.json`
- `knowledge/software-engineering/documents.json`
- `knowledge/software-engineering/corpus/source-cards.md`

New directories, including `corpus`, `corpus/pdf-downloads` and `private`, are
0700; files are 0600. No PDFs, private books, indexes or extracted text are copied
or downloaded. Metadata is **not an indexed corpus**. Private indexes/PDF content
are never opened during inventory.

Ingest is only a terminal handoff:

```sh
python3 SHARED/extensions/software-kb/ingest.py --private --root ROOT
# Optional, separately chosen macOS Vision OCR:
python3 SHARED/extensions/software-kb/ingest.py --private --root ROOT --ocr
```

Poppler is a prerequisite; OCR also needs local Swift/Vision. These commands are
not run by capability apply/check. Only ingest material you may use locally; no
cloud uploads. For a custom root, export `PI_SOFTWARE_KB_ROOT` in the shell that
launches Pi and restart/reload with that environment. Exporting in an unrelated
shell does not change an existing Pi process's environment.

## Checks, safety and evidence

Apple check runs only:

1. `/usr/bin/xcode-select -p` (requires a full app developer directory, not CLT).
2. `/usr/bin/xcodebuild -version`.
3. `/usr/bin/xcrun simctl list devices available -j` (requires a nonempty available
   device inventory for this specific check to pass).

No simulator boot, app build, signing, license acceptance, desktop action, browser
execution, inference or network authentication probe. Each probe is bounded to
15 seconds and 1 MiB combined stdout/stderr. Doctor has a 20-second outer deadline
and 5-second per-checker deadline. Probe groups are killed on timeout/cancellation.
A minimal environment excludes inherited secrets, injection variables, and
project PATH entries. Doctor additionally receives fixed standard Homebrew/system
PATH directories for static executable inventory; never arbitrary project scripts.
Doctor's usual static browser inventory uses the user's research configuration;
its selected profile inventory receives the explicit agent directory.

MCP migration reads source/profile JSON only to convert and back it up privately;
it never opens OAuth/keychain stores or executes credential references. Native
`pi mcp list` is a separate handoff because it connects every enabled server.

No raw subprocess output, configuration content, credentials, simulator names or
doctor guidance are printed. Doctor emits aggregate counts plus allowlisted
capability/status tokens and fixed actionable setup guidance. `verified`
means only the named check succeeded (e.g. executable path presence), not that a
capability is loaded, authenticated, active or globally ready. Other static checks
can succeed with `unknown` or `not-installed` status: inventory success is not
readiness.

Apply digests bind proposed bytes, context/options, dependency/metadata file
content and identities, relevant existing target state, and directory ancestry.
Apply validates the approval, acquires the existing per-user
`~/.config/pi-shared/update.lock`, then reinspects and compares the digest. Target
creation uses no-follow directory descriptors and exclusive files/directories,
never replacement. The explicit MCP migration exception narrowly replaces profile
settings only after private backups and compare-before-replace checks; native MCP
targets still must be missing. Its [rollback manifest](mcp.md#backups-and-rollback)
provides original/proposed hashes and exact backups. Existing config must be regular, single-link, safely owned and
not shared-writable; reads are bounded to 1 MiB (code-intel dependencies: 32 MiB).
No setup receipt is adopted or written. New scaffold files belong to the user;
manual cleanup must review only those files, not delete an existing directory.

The lock coordinates pi-shared writers, not hostile same-user processes. This is
not a filesystem sandbox, authenticity proof of the supplied shared distribution,
or atomic multi-file transaction. Do not concurrently edit targets during apply.
A failed/cancelled operation may leave new directories/partial scaffold files;
they are deliberately not overwritten on retry. Inspect them manually first.
MCP migration can also leave changed profile settings; use its private manifest
for hash-checked manual rollback, never overwrite later edits.

## JSON and exit contract

Every `--json` invocation (including argument failures, except ordinary help)
prints one object:

```json
{
  "schemaVersion": 1,
  "component": "development",
  "action": "plan",
  "ok": true,
  "summary": "...",
  "status": "needs-configuration",
  "evidence": [{"label": "Project configuration", "value": "missing"}],
  "actions": [],
  "warnings": [],
  "errors": [],
  "nextSteps": [],
  "handoffs": [{"label": "...", "command": "...", "kind": "terminal"}],
  "planId": "<64 lowercase hexadecimal characters; only for an actionable apply>"
}
```

Status is `not-installed`, `needs-configuration`, `configured-untested`, `verified`
or `unknown`. Handoff kind is `terminal` or `pi`. Each array has at most 40 items,
each string at most 4096 characters, and the whole JSON at most 64 KiB. Oversized
reports fail closed rather than returning truncated approval data. Invalid
component/action arguments use the valid fallback identity `diagnostics`/`plan`.
Exit 0 iff `ok: true`; operation failures return 1, argument failures 2,
cancellation 130. Successful apply/check omit the approval digest.

Tests (disposable fixtures, no live installation):

```sh
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests/test_capability_setup.py tests/test_mcp_setup.py
```
