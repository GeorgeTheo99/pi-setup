# Omnigent native-Pi compatibility

## Supported path

Use Omnigent's **native Pi terminal** with Pi's existing standard profile and an
explicit provider/model selection:

```bash
omnigent pi --provider <pi-provider> --model <pi-model-id>
```

For a configured model-gateway/Databricks installation, the reference command is:

```bash
omnigent pi --provider model-gateway --model databricks-glm-5-3
```

This is an optional access path, not a replacement for `pi` or generated `pi-*`
launchers. The compatibility implementation does not change those launchers,
profile repair, saved models, or the Pi runtime.

| Included in the compatibility scope | Boundary |
|---|---|
| Native Pi launch on macOS | Installed `pi`, `omnigent`, and `tmux` must be on the host's PATH |
| Standard Pi configuration | `~/.pi/agent`; no automatic switch to `~/.pi-omlx/agent` |
| Shared tools | `pi-shared` package must load through Pi's normal resource loader |
| Optional enterprise overlay | When configured, `pi-databricks` must load before `pi-shared` |
| Explicit model selection | Provider/model must resolve in this profile, not only in a generated alternate profile |
| Basic prompts and tools | Requires an independently validated model route and a separately authorized inference smoke |
| Existing model-gateway | Must already be configured and running on the harness host; no automatic start/restart |

Omnigent's server may be remote, but the native CLI launches Pi through the
calling host's daemon/runner. `localhost` in a Pi model URL refers to that
**harness host**, not the control-plane server.

Explicit `--provider`/`--model` arguments avoid Omnigent's managed-provider
injection in the inspected Omnigent 0.14.0 implementation. A bare command may
select a different configuration path; it is not the reference compatibility
command. An already-running terminal must be restarted explicitly to apply new
launch arguments. Do not assume a calling shell's `PI_CODING_AGENT_DIR` or a
shell function such as `pi-glm53` is forwarded to the runner.

## Prerequisites, not automatic activation

```bash
# During an explicitly authorized installation:
./install.sh --with-omnigent

# Or inspect an existing standard installation:
PI_CODING_AGENT_DIR="$HOME/.pi/agent" \
  bin/doctor --module pi-shared --require-omnigent
```

The flag requires the executables and checks configured package imports and
package order. It does **not** install Omnigent, choose a server, launch a
session, generate a profile, start services, repair configuration, or test a
model. The normal install still installs its selected modules; the Omnigent
flag only adds checks. Without the flag, Omnigent is neither required nor
invoked. Existing direct-provider installations remain supported.

The package-import probe executes trusted extension initialization, which can
have side effects. It is not a strict read-only filesystem inspection. For
read-only declarations, use `pi-shared/bin/pi-profile-check --static` instead.

Model-gateway health and model listing are separate, non-inference evidence:

```bash
curl -fsS --max-time 3 http://localhost:9111/health
curl -fsS --max-time 3 http://localhost:9111/v1/models
```

A health response is not proof that credentials, the selected upstream route,
or model inference work. Use configured endpoints if they differ from these
reference defaults; never paste credentials into diagnostic commands or logs.

## Explicit isolated native-launch smoke

This is a developer/release check, **not an installer or doctor side effect**:

```bash
python3 -B tests/omnigent_native_smoke.py \
  --agent-dir "$HOME/.pi/agent" \
  --package "$HOME/local_code/pi-shared" \
  --provider model-gateway --model databricks-glm-5-3

# When testing the enterprise composition, preserve package order:
python3 -B tests/omnigent_native_smoke.py \
  --agent-dir "$HOME/.pi/agent" \
  --package "$HOME/local_code/pi-databricks" \
  --package "$HOME/local_code/pi-shared" \
  --provider model-gateway --model databricks-glm-5-3
```

Only run it against reviewed local packages. It:

1. Checks that the requested identity is declared in the source `models.json`.
2. Creates a short, private temporary HOME under `/tmp` (macOS tmux socket paths
   are length-limited) and a minimal standard-profile fixture using the supplied
   package paths. A restricted PATH exposes only the selected Pi, Omnigent,
   Node, tmux, and OS tools: Omnigent's startup discovery must not probe unrelated
   third-party launchers. It does not copy `auth.json`, real API keys, provider
   commands, or the real model endpoint. The selected model has an inert
   loopback endpoint and synthetic fixture authentication.
3. Checks package imports through the installed Pi SDK.
4. Starts its own foreground loopback Omnigent server on an ephemeral port,
   then requests a native terminal with explicit provider/model and `--offline`.
5. Uses a test extension to record the actual Pi executable, TUI mode, profile,
   selected model, package registrations, basic tool presence, and Omnigent
   bridge command. It submits no prompt and requests Pi shutdown.
6. Stops only its isolated host target and owned foreground processes, and waits
   for captured fixture descendants/tmux to exit. It never uses the broad
   `omnigent server stop`/`omnigent stop` commands. Successful
   temporary state is removed; failed diagnostics are retained privately.

This proves **native startup and model identity**, not inference, use of a real
provider transport, every extension's behavior, or compatibility of an arbitrary
user profile. The fixture intentionally does not clone all user settings. Test
with clean source worktrees and existing installed dependencies; do not point
production profiles at development branches.

## Not certified in the initial scope

- Browser/mobile parity for custom Pi terminal UI or dialogs.
- Web-driven `/self-handoff`, goal transfer, or advanced delegation workflows.
  These are not deliberately disabled, but are not part of the initial claim.
- Omnigent-generated provider/profile configurations or alternate Pi profiles.
- Headless/direct harness mode, containers, remote Linux hosts, or all Omnigent
  policies/sandbox configurations.
- Automatic gateway lifecycle, credential refresh, catalog regeneration, or
  profile repair by the compatibility layer.

## Development validation — 2026-09-16

These are development-worktree results, not a published release or an inference
certificate. No changes were activated in the daily Pi profiles.

| Check | Result |
|---|---|
| Host | macOS 26.6.2 |
| Installed runtime | Pi 0.85.1; Omnigent 0.14.0; Node 26.8.2; tmux 3.5a |
| Source bases | pi-setup `5e5e9eb`; pi-shared `5927e57`; pi-databricks `f37e906` |
| Offline pi-setup suite | 128 tests and 17 subtests passed |
| Native launch with pi-shared only | Passed: executable, standard-profile fixture, TUI, bridge, package/tools, explicit model identity |
| Native launch with both packages | Passed with pi-databricks before pi-shared |
| Explicit selection | `model-gateway/databricks-glm-5-3`, despite deliberately different fixture defaults |
| Disposable process cleanup | Passed for both final restricted-PATH runs |
| Real model transport, credentials, inference | Not tested; model endpoint replaced with an inert fixture endpoint |
| Browser/mobile and advanced workflows | Not tested |

The restricted PATH is essential: isolating HOME alone does not prevent
Omnigent's capability discovery from executing unrelated installed launchers.

## Release evidence and wording

Record the exact Pi/Omnigent/Node/tmux versions, package revisions, OS, test
results, and inference status for each validated release. Do not turn a passing
prerequisite check into a full compatibility or inference certificate.

After native-launch validation, bounded wording is:

> Compatible with Omnigent's native Pi launch path. Advanced Pi-specific
> workflows may require the native terminal. Model access requires a configured
> provider; inference verification is reported separately.

The stronger claim **"tested for basic agent operation"** additionally requires
an explicitly authorized minimal real-model prompt/tool smoke. Do not run that
request automatically during installation or these compatibility checks.

Public references: [Omnigent supported harnesses](https://omnigent.ai/docs/build/harnesses/supported),
[Pi extensions](https://pi.dev/docs/latest/extensions),
[Pi RPC limitations](https://pi.dev/docs/latest/rpc).
