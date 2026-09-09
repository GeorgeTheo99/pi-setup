"""Hermetic CLI regressions: local Git only; no installers/services/providers."""
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pi-setup-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.code = self.root / "code"
        self.setup = self.root / "setup"
        self.tools = self.root / "tools"
        for path in (self.home, self.code, self.setup / "bin", self.tools):
            path.mkdir(parents=True)
        for name in ("install.sh", "bin/doctor"):
            shutil.copy2(ROOT / name, self.setup / name)
        # Only these real commands are exposed. All other installation/network
        # entrypoints are unavailable or fail loudly. Git transport is file-only.
        for command in ("bash", "git", "python3", "dirname", "basename", "mkdir"):
            (self.tools / command).symlink_to(shutil.which(command))
        self.env = {
            "HOME": str(self.home), "PATH": str(self.tools),
            "PI_SETUP_CODE_ROOT": str(self.code), "LC_ALL": "C",
            "PI_SETUP_NO_SHELL_RC": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull, "GIT_ALLOW_PROTOCOL": "file",
            "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "CALLS": str(self.root / "calls"),
        }
        self.script(self.tools / "pi", 'test "$1" = --version || exit 97\nprintf "fixture-pi\\n"')
        for name in ("curl", "npm", "uv", "launchctl"):
            self.script(self.tools / name, f'echo "FORBIDDEN {name}" >> "$CALLS"; exit 98')

    def script(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/usr/bin/env bash\nset -eu\n" + body + "\n")
        path.chmod(0o755)

    def run_cli(self, script, *args, success=True):
        result = subprocess.run([str(script), *args], env=self.env, cwd=self.setup,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=20)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("Selected module checks passed", result.stdout)
        self.assertNotIn("FORBIDDEN", self.calls())
        return result.stdout

    def calls(self):
        path = Path(self.env["CALLS"])
        return path.read_text() if path.exists() else ""

    def clear_calls(self):
        Path(self.env["CALLS"]).unlink(missing_ok=True)

    def git(self, repo, *args):
        result = subprocess.run([str(self.tools / "git"), "-C", str(repo), *args],
                                env=self.env, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, repo, name="revision", contents="new"):
        (repo / name).write_text(contents)
        self.git(repo, "add", "--all")
        self.git(repo, "commit", "-qm", "fixture revision")
        return self.git(repo, "rev-parse", "HEAD")


class InstallerTests(Fixture):
    def setUp(self):
        super().setUp()
        self.origin = self.root / "origin"
        self.origin.mkdir()
        self.git(self.origin, "init", "-q", "-b", "main")
        self.script(self.origin / "install.sh", 'echo "installer $(git rev-parse HEAD)" >> "$CALLS"\nexit "${INSTALL_EXIT:-0}"')
        self.first = self.commit(self.origin)
        self.dest = self.code / "pi-shared"
        self.script(self.setup / "bin/doctor", 'echo "doctor $*" >> "$CALLS"\nexit "${DOCTOR_EXIT:-0}"')
        self.manifest()

    def manifest(self, ref="main", repo=None):
        (self.setup / "manifest.yaml").write_text(
            f"modules:\n  pi-shared:\n    repo: {repo or self.origin}\n    ref: {ref}\n    tier: required\n")

    def install(self, *args, success=True):
        return self.run_cli(self.setup / "install.sh", *args, success=success)

    def clone(self):
        self.git(self.code, "clone", "-q", str(self.origin), str(self.dest))

    def test_clone_default_branch_and_doctor_selection(self):
        self.install("--minimal")
        self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), self.first)
        self.assertIn(f"installer {self.first}", self.calls())
        self.assertIn("doctor --module pi-shared", self.calls())

    def test_installer_doctor_checks_installed_profile_not_calling_profile(self):
        self.script(self.setup / "bin/doctor", 'echo "profile $PI_CODING_AGENT_DIR" >> "$CALLS"')
        self.env["PI_CODING_AGENT_DIR"] = str(self.home / "calling-profile")
        self.install("--minimal")
        self.assertIn(f"profile {self.home / '.pi/agent'}", self.calls())
        self.clear_calls()
        self.env["PI_SHARED_AGENT_DIR"] = str(self.home / "installed-profile")
        self.install("--minimal")
        self.assertIn(f"profile {self.home / 'installed-profile'}", self.calls())
        self.assertNotIn("calling-profile", self.calls())

    def test_clone_nondefault_branch(self):
        self.git(self.origin, "checkout", "-qb", "release")
        target = self.commit(self.origin, contents="release")
        self.git(self.origin, "checkout", "-q", "main")
        self.manifest("release")
        self.install()
        self.assertEqual(self.git(self.dest, "branch", "--show-current"), "release")
        self.assertIn(target, self.calls())

    def test_missing_ref_never_runs_installer(self):
        self.manifest("does-not-exist")
        self.assertIn("not found", self.install(success=False))
        self.assertEqual(self.calls(), "")

    def test_tag_and_sha_are_exact_detached_pins(self):
        self.git(self.origin, "tag", "-a", "v1", "-m", "fixture tag")
        newer = self.commit(self.origin, contents="newer")
        self.manifest("v1")
        self.install()
        self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), self.first)
        self.assertEqual(self.git(self.dest, "branch", "--show-current"), "")
        self.manifest(newer)
        self.clear_calls()
        self.install("--update")
        self.assertIn(newer, self.calls())
        self.assertEqual(self.git(self.dest, "branch", "--show-current"), "")

    def test_consecutive_tag_upgrades_retain_previous_pin(self):
        self.git(self.origin, "tag", "v1")
        self.manifest("v1")
        self.install()
        for version in ("v2", "v3"):
            target = self.commit(self.origin, contents=version)
            self.git(self.origin, "tag", version)
            self.manifest(version)
            self.clear_calls()
            self.install("--update")
            self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), target)
            self.assertIn(f"installer {target}", self.calls())
        self.assertEqual(self.git(self.dest, "rev-parse", "refs/tags/v1"), self.first)

    def test_consecutive_sha_upgrades_accept_remote_retained_head(self):
        self.manifest(self.first)
        self.install()
        for version in ("two", "three"):
            target = self.commit(self.origin, contents=version)
            self.manifest(target)
            self.install("--update")
            self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), target)

    def test_sha_pin_is_not_shadowed_by_local_branch(self):
        newer = self.commit(self.origin, contents="different commit")
        self.clone()
        for ref in (self.first[:7], self.first):
            with self.subTest(ref=ref):
                self.git(self.dest, "branch", ref, newer)
                self.manifest(ref)
                self.clear_calls()
                self.install("--update")
                self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), self.first)
                self.assertIn(f"installer {self.first}", self.calls())
                self.assertEqual(self.git(self.dest, "rev-parse", f"refs/heads/{ref}"), newer)

    def test_ambiguous_sha_objects_fail_closed(self):
        self.clone()
        self.manifest(self.first[:7])
        real_git = (self.tools / "git").resolve()
        (self.tools / "git").unlink()
        self.script(self.tools / "git",
                    'if [ "${3:-}" = rev-parse ] && [[ "${4:-}" = --disambiguate=* ]]; then\n'
                    f'  printf "%s\\n%s\\n" {self.first} {self.first}; exit 0\n'
                    'fi\n'
                    f'exec {shlex.quote(str(real_git))} "$@"')
        self.assertIn("ambiguous", self.install("--update", success=False))
        self.assertEqual(self.calls(), "")
        self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), self.first)

    def test_ambiguous_branch_tag_rejected(self):
        self.git(self.origin, "tag", "main")
        self.assertIn("both a branch and tag", self.install(success=False))
        self.assertEqual(self.calls(), "")

    def test_no_update_validates_cached_revision_without_fetch(self):
        self.clone()
        self.commit(self.origin, contents="upstream moved")
        self.install()
        self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), self.first)
        self.git(self.dest, "fetch", "-q", "origin")
        self.clear_calls()
        self.assertIn("does not match", self.install(success=False))
        self.assertEqual(self.calls(), "")

    def test_existing_wrong_branch_requires_explicit_update(self):
        self.clone()
        self.git(self.dest, "checkout", "-qb", "local-work")
        local = self.commit(self.dest, contents="local branch work")
        self.assertIn("does not match", self.install(success=False))
        self.install("--update")
        self.assertEqual(self.git(self.dest, "rev-parse", "local-work"), local)
        self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), self.first)

    def test_fast_forward_updates_before_installing(self):
        self.clone()
        target = self.commit(self.origin, contents="remote update")
        self.install("--update")
        self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), target)
        self.assertIn(f"installer {target}", self.calls())

    def test_diverged_and_ahead_branches_preserve_work(self):
        self.clone()
        local = self.commit(self.dest, "local-work", "keep me")
        self.assertIn("ahead or diverged", self.install("--update", success=False))
        self.commit(self.origin, contents="divergence")
        self.assertIn("ahead or diverged", self.install("--update", success=False))
        self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), local)
        self.assertEqual((self.dest / "local-work").read_text(), "keep me")
        self.assertEqual(self.calls(), "")

    def test_dirty_and_untracked_files_block_install(self):
        self.clone()
        for name in ("revision", "untracked"):
            with self.subTest(name=name):
                path = self.dest / name
                previous = path.read_text() if path.exists() else None
                path.write_text("keep me")
                for args in ((), ("--update",)):
                    self.assertIn("local changes", self.install(*args, success=False))
                self.assertEqual(path.read_text(), "keep me")
                if previous is None:
                    path.unlink()
                else:
                    path.write_text(previous)
        (self.dest / "revision").write_text("staged work")
        self.git(self.dest, "add", "revision")
        self.assertIn("local changes", self.install("--update", success=False))
        self.assertIn("M  revision", self.git(self.dest, "status", "--porcelain"))
        self.assertEqual(self.calls(), "")

    def test_ignored_file_not_overwritten_by_fast_forward(self):
        (self.origin / ".gitignore").write_text("private-file\n")
        self.commit(self.origin, contents="ignore added")
        self.clone()
        (self.dest / "private-file").write_text("local private data")
        (self.origin / "private-file").write_text("remote tracked data")
        self.git(self.origin, "add", "-f", "private-file")
        self.git(self.origin, "commit", "-qm", "track formerly ignored file")
        before = self.git(self.dest, "rev-parse", "HEAD")
        self.install("--update", success=False)
        self.assertEqual((self.dest / "private-file").read_text(), "local private data")
        self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), before)
        self.assertEqual(self.calls(), "")

    def test_detached_local_commit_is_not_abandoned(self):
        self.clone()
        self.git(self.dest, "checkout", "-q", "--detach")
        local = self.commit(self.dest, contents="detached work")
        self.assertIn("detached work", self.install("--update", success=False))
        self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), local)
        self.assertEqual(self.calls(), "")

    def test_origin_mismatch_rejected_before_install(self):
        self.clone()
        self.git(self.dest, "remote", "set-url", "origin", str(self.root / "wrong"))
        for args in ((), ("--update",)):
            self.assertIn("origin does not match", self.install(*args, success=False))
        self.assertEqual(self.calls(), "")

    def test_fetch_failure_does_not_install_stale_checkout(self):
        self.clone()
        unavailable = str(self.root / "unavailable")
        self.git(self.dest, "remote", "set-url", "origin", unavailable)
        self.manifest(repo=unavailable)
        self.assertIn("fetch failed", self.install("--update", success=False))
        self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), self.first)
        self.assertEqual(self.calls(), "")

    def test_existing_nonrepo_is_not_repurposed(self):
        self.dest.mkdir()
        (self.dest / "keep").write_text("local data")
        self.assertIn("not a Git worktree", self.install(success=False))
        self.assertEqual((self.dest / "keep").read_text(), "local data")

    def test_git_status_and_checkout_failures_stop_install(self):
        self.clone()
        real_git = (self.tools / "git").resolve()
        (self.tools / "git").unlink()
        for command in ("status", "checkout"):
            with self.subTest(command=command):
                self.script(self.tools / "git",
                            f'if [ "${{3:-}}" = {command} ]; then exit 91; fi\n'
                            f'exec {shlex.quote(str(real_git))} "$@"')
                self.install("--update", success=False)
                self.assertEqual(self.calls(), "")
                self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), self.first)

    def test_missing_existing_ref_leaves_checkout_untouched(self):
        self.clone()
        self.manifest("missing-release")
        for args in ((), ("--update",)):
            self.assertIn("not found", self.install(*args, success=False))
            self.assertEqual(self.git(self.dest, "rev-parse", "HEAD"), self.first)
        self.assertEqual(self.calls(), "")

    def test_nested_directory_is_not_a_module_repository(self):
        self.git(self.code, "init", "-q", "-b", "main")
        self.dest.mkdir()
        self.assertIn("not a repository root", self.install(success=False))
        self.assertEqual(self.calls(), "")

    def test_git_worktree_supported(self):
        self.clone()
        separate = self.code / "original-clone"
        self.dest.rename(separate)
        self.git(separate, "checkout", "-q", "--detach")
        self.git(separate, "worktree", "add", "-q", str(self.dest), "main")
        self.assertTrue((self.dest / ".git").is_file())
        self.install()

    def test_missing_installer_and_failed_installer_do_not_succeed(self):
        self.env["INSTALL_EXIT"] = "7"
        self.assertIn("installer failed", self.install(success=False))
        self.assertNotIn("doctor", self.calls())
        (self.origin / "install.sh").unlink()
        self.commit(self.origin, contents="installer removed")
        self.clear_calls()
        self.assertIn("no executable install.sh", self.install("--update", success=False))
        self.assertEqual(self.calls(), "")

    def test_doctor_failure_propagates(self):
        self.env["DOCTOR_EXIT"] = "9"
        self.assertIn("Installation checks failed", self.install(success=False))
        self.assertIn("doctor --module pi-shared", self.calls())

    def test_invalid_manifest_cannot_silently_succeed(self):
        (self.setup / "manifest.yaml").write_text("modules:\n  pi-shared:\n    tier: required\n")
        self.assertIn("Invalid module manifest", self.install(success=False))
        self.assertEqual(self.calls(), "")

    def test_overlay_hook_and_doctor_are_explicit(self):
        overlay = self.root / "overlay"
        self.script(overlay / "setup.d/check", 'echo hook >> "$CALLS"')
        self.install("--overlay", str(overlay))
        self.assertIn("hook", self.calls())
        self.assertIn(f"--overlay {overlay}", self.calls())

    def test_remote_overlay_update_failure_stops_hooks(self):
        overlay = self.root / "overlay-origin"
        overlay.mkdir()
        self.git(overlay, "init", "-q", "-b", "main")
        self.script(overlay / "setup.d/check", 'echo overlay-hook >> "$CALLS"')
        self.commit(overlay)
        self.install("--overlay", overlay.as_uri())
        dest = self.code / "overlay-origin"
        local = self.commit(dest, "local-work", "keep overlay work")
        self.commit(overlay, contents="upstream overlay")
        self.clear_calls()
        self.install("--update", "--overlay", overlay.as_uri(), success=False)
        self.assertEqual(self.git(dest, "rev-parse", "HEAD"), local)
        self.assertNotIn("overlay-hook", self.calls())
        self.assertNotIn("doctor", self.calls())


class DoctorTests(Fixture):
    def setUp(self):
        super().setUp()
        self.shared = self.code / "pi-shared"
        self.script(self.shared / "bin/pi-shared-check-deps", 'echo deps >> "$CALLS"\nexit "${DEPS_EXIT:-0}"')
        self.script(self.shared / "bin/pi-profile-check", 'echo "profile $*" >> "$CALLS"\nexit "${PROBE_EXIT:-0}"')
        self.settings = self.home / ".pi/agent/settings.json"
        self.configure([str(self.shared)])

    def configure(self, packages, extra=None):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps({"packages": packages, **(extra or {})}))

    def doctor(self, *args, success=True):
        return self.run_cli(self.setup / "bin/doctor", *args, success=success)

    def service(self, name, checker):
        if name == "model-gateway":
            launchers = self.home / ".pi/generated/pi-launchers.zsh"
            launchers.parent.mkdir(parents=True, exist_ok=True)
            launchers.write_text("pi-list() { :; }\n")
            self.script(self.tools / "zsh", 'test "$1" = -n || exit 97\necho "launcher syntax" >> "$CALLS"')
        self.script(self.code / name / checker,
                    f'test "$1" = verify || exit 97\necho "{name} verify" >> "$CALLS"\nexit "${{SERVICE_EXIT:-0}}"')

    def test_exact_string_object_relative_and_tilde_package_paths(self):
        link = self.home / "shared-link"
        link.symlink_to(self.shared)
        sources = [str(self.shared), os.path.relpath(self.shared, self.settings.parent), "~/shared-link"]
        for source in sources:
            for entry in (source, {"source": source, "extensions": []}):
                with self.subTest(entry=entry):
                    self.configure([entry])
                    output = self.doctor()
                    self.assertIn("package configured", output)
                    self.assertIn("resource loading checked separately", output)
                    self.assertIn(f"profile --agent-dir {self.settings.parent}", self.calls())

    def test_substrings_wrong_shapes_and_invalid_json_fail(self):
        for packages in ([str(self.shared) + "-other"], [], "pi-shared", [{"source": 123}], None):
            with self.subTest(packages=packages):
                self.configure(packages, {"unrelated": "pi-shared"})
                self.doctor(success=False)
        for text in ("not JSON pi-shared", "[]", "null"):
            self.settings.write_text(text)
            self.doctor(success=False)
        self.assertNotIn("deps", self.calls())

    def test_active_profile_override(self):
        self.settings = self.home / "alternate/settings.json"
        self.env["PI_CODING_AGENT_DIR"] = str(self.settings.parent)
        self.configure([str(self.shared)])
        self.assertIn(str(self.settings), self.doctor())
        self.assertIn(f"profile --agent-dir {self.settings.parent}", self.calls())
        self.settings.write_text("{}")
        self.doctor(success=False)  # must not fall back to valid default profile

    def test_cli_and_dependency_failures_propagate(self):
        self.env["DEPS_EXIT"] = "4"
        self.assertIn("dependency resolution failed", self.doctor(success=False))
        self.env["DEPS_EXIT"] = "0"
        self.script(self.tools / "pi", "exit 5")
        self.assertIn("--version failed", self.doctor(success=False))

    def test_services_use_documented_verifiers_not_http_status(self):
        self.service("model-gateway", "bin/model-gateway")
        self.service("local_web_search", "scripts/local-search")
        output = self.doctor()
        self.assertIn("model-gateway verify", self.calls())
        self.assertIn("local_web_search verify", self.calls())
        self.assertIn(f"profile --agent-dir {self.home / '.pi-omlx/agent'}", self.calls())
        self.assertIn("--require-models", self.calls())
        self.assertIn("inference NOT tested", output)
        self.assertIn("Brave provider request NOT tested", output)
        self.env["SERVICE_EXIT"] = "3"
        self.assertIn("verification failed", self.doctor(success=False))

    def test_minimal_selection_does_not_probe_unselected_services(self):
        self.service("model-gateway", "bin/model-gateway")
        self.service("local_web_search", "scripts/local-search")
        self.env["SERVICE_EXIT"] = "3"
        output = self.doctor("--module", "pi-shared")
        self.assertIn("model-gateway not selected", output)
        self.assertNotIn("verify", self.calls())

    def test_selected_missing_service_and_missing_verifier_fail(self):
        self.doctor("--module", "local_web_search", success=False)
        (self.code / "model-gateway").mkdir()
        self.assertIn("verifier missing", self.doctor(success=False))

    def test_overlay_doctors_never_run_by_directory_discovery(self):
        overlay = self.code / "unrelated"
        self.script(overlay / "bin/doctor", 'echo overlay-doctor >> "$CALLS"\nexit 6')
        (overlay / "manifest.fragment.yaml").write_text("modules: {}\n")
        self.doctor()
        self.assertNotIn("overlay-doctor", self.calls())
        self.assertIn("Overlay doctor failed", self.doctor("--overlay", str(overlay), success=False))
        self.assertIn("overlay-doctor", self.calls())

    def test_unknown_module_reports_unverified_not_ready(self):
        self.assertIn("no built-in readiness check", self.doctor("--module", "custom"))


if __name__ == "__main__":
    unittest.main()
