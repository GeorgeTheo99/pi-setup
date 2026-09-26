"""Opt-in cross-repository smoke: real shared installer/helper, disposable HOME.

Set PI_SHARED_TEST_ROOT to a trusted pi-shared checkout. Git provisioning,
extension dependency installation and doctor are excluded; no remote services
or providers are contacted. Only the fake loopback gateway is contacted.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

from test_cli import ROOT, cli, isolated, options
from test_update import args as update_args


@pytest.mark.skipif(not os.environ.get("PI_SHARED_TEST_ROOT"), reason="opt-in sibling shared integration")
@pytest.mark.parametrize("prefix", ["", "/model-gateway", "/one/two_3.~-X"])
def test_real_shared_remote_setup_rerun_update_and_status(isolated, monkeypatch, prefix):
    home, calls = isolated
    shared = Path(os.environ["PI_SHARED_TEST_ROOT"]).resolve()
    for key in list(os.environ):
        if key.startswith(("PI_SHARED_", "PI_SETUP_", "PI_LAUNCHER_", "PI_OMLX_", "PI_DATABRICKS_")):
            monkeypatch.delenv(key)
    code_root = home / "modules"
    code_root.mkdir()
    (code_root / "pi-shared").symlink_to(shared, target_is_directory=True)
    key_file = home.resolve() / "gateway.key"
    key_file.write_text("fixture-only-token\n")
    key_file.chmod(0o600)
    monkeypatch.setenv("PI_SETUP_CODE_ROOT", str(code_root))
    monkeypatch.setenv("PI_SHARED_CLI_OUT", str(home / "custom/launcher.json"))
    monkeypatch.setenv("PI_SHARED_MODELS_OUT", str(home / "gateway-profile/models.json"))
    monkeypatch.setenv("PI_SHARED_CATALOG_PI_AGENT_DIR", str(home / "gateway-profile"))
    monkeypatch.setattr(cli.update_support, "runtime_versions", lambda: {})
    monkeypatch.setattr(cli.update_support, "revisions", lambda *a: {})
    monkeypatch.setattr(cli.update_support, "service_snapshot", lambda *a: {})
    monkeypatch.setattr(cli.update_support, "service_environment", lambda *a: {})
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"data": [{"id": "test-model", "available": True,
                "vision": False, "thinking_levels": [], "context_length": 32768,
                "max_output_tokens": 4096}]}).encode())

        def log_message(self, *_):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()

    def run(command, *, env=None):
        argv = list(map(str, command))
        calls.append(argv)
        if argv[0] == str(ROOT / "bin/doctor"):
            return  # SDK imports/dependencies are covered separately.
        if argv[0] == str(ROOT / "install.sh"):
            assert "model-gateway" not in " ".join(argv)
            argv = [str(shared / "bin/pi-shared-install"), "--no-deps"]
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert "fixture-only-token" not in result.stdout + result.stderr

    monkeypatch.setattr(cli, "run", run)
    try:
        assert cli.setup(options(mode=None, gateway_url=f"http://127.0.0.1:{http.server_port}{prefix}/v1/",
                                 gateway_key_file=str(key_file), allow_private_http=True)) == 0
        receipt = cli.read_state()
        assert receipt["modules"] == ["pi-shared"]
        assert receipt["model_access"] == ["direct", "existing-gateway"]
        assert receipt["external_gateway"]["url"] == f"http://127.0.0.1:{http.server_port}{prefix}"
        assert receipt["services"] == {}
        assert "fixture-only-token" not in cli.state_path().read_text()
        config = Path(receipt["cli_file"])
        models = home / "gateway-profile/models.json"
        original = (config.read_bytes(), models.read_bytes())
        # Re-run from saved connection and paths, then update/status offline.
        monkeypatch.delenv("PI_SETUP_CODE_ROOT")
        monkeypatch.delenv("PI_SHARED_CLI_OUT")
        monkeypatch.delenv("PI_SHARED_MODELS_OUT")
        monkeypatch.delenv("PI_SHARED_CATALOG_PI_AGENT_DIR")
        assert cli.setup(options(mode=None)) == 0
        assert cli.update(update_args(modules_only=True)) == 0
        assert cli.status() == 0
        assert (config.read_bytes(), models.read_bytes()) == original
        assert requests == [(f"{prefix}/v1/models/canonical", "Bearer fixture-only-token")] * 2
        assert cli.read_state()["external_gateway"] == receipt["external_gateway"]
        assert (home / ".local/bin/pi-gateway").resolve() == shared / "bin/pi-gateway"
    finally:
        http.shutdown()
        thread.join()
        http.server_close()
