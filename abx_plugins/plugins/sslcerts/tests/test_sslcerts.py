"""
Tests for the SSL plugin.

Tests the real SSL hook with an actual HTTPS URL to verify
certificate information extraction.
"""

import json
import hashlib
import shutil
import signal
import socket
import sys
import ssl
import subprocess
import tempfile
from pathlib import Path

import pytest

from abx_plugins.plugins.base.testing import (
    get_hook_script,
    get_plugin_dir,
    start_process_and_wait_for_file,
    wait_for_file,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    CHROME_NAVIGATE_HOOK,
    chrome_session,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


# Get the path to the SSL hook
PLUGIN_DIR = get_plugin_dir(__file__)
SSLCERTS_HOOK = get_hook_script(PLUGIN_DIR, "on_Snapshot__*_sslcerts.*")


class TestSSLPlugin:
    """Test the SSL plugin with real HTTPS URLs."""

    def test_sslcerts_hook_exists(self):
        """SSL hook script should exist."""
        assert SSLCERTS_HOOK is not None, "SSL hook not found in plugin directory"
        assert SSLCERTS_HOOK.exists(), f"Hook not found: {SSLCERTS_HOOK}"


class TestSSLWithChrome:
    """Integration tests for SSL plugin with Chrome."""

    def setup_method(self, _method=None):
        """Set up test environment."""
        self.temp_dir = Path(tempfile.mkdtemp())

    def teardown_method(self, _method=None):
        """Clean up."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @pytest.mark.parametrize("idle_tls", [False, True])
    def test_chain_timeout_preserves_browser_certificate(self, idle_tls):
        """A stalled native TLS peer must not crash an already-ready collector."""
        cert = self.temp_dir / "cert.pem"
        key = self.temp_dir / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        test_url = f"https://127.0.0.1:{port}/"
        (self.temp_dir / "index.html").write_text(
            "<h1>Real HTTPS certificate fixture</h1>",
        )
        with (self.temp_dir / "https.log").open("w") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    """
import http.server, os, signal, ssl, sys
class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def do_GET(self):
        super().do_GET()
        self.wfile.flush()
        print('HTTPS response sent; suspending origin', flush=True)
        os.kill(os.getpid(), signal.SIGSTOP)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(sys.argv[2], sys.argv[3])
server = http.server.ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler)
# The worker's first read performs TLS negotiation; a raw TCP preconnection
# must not block the listening thread before it dispatches other connections.
server.socket = context.wrap_socket(
    server.socket, server_side=True, do_handshake_on_connect=False,
)
print('HTTPS server ready', flush=True)
server.serve_forever()
""",
                    str(port),
                    str(cert),
                    str(key),
                ],
                cwd=self.temp_dir,
                stdout=log,
                stderr=log,
                text=True,
            )
            idle = None
            try:
                wait_for_file(
                    self.temp_dir / "https.log",
                    process=server,
                    ready=lambda p: "HTTPS server ready" in p.read_text(),
                )
                client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                client_context.check_hostname = False
                client_context.verify_mode = ssl.CERT_NONE
                # A browser may preconnect without sending an HTTP request.
                # Keep a real idle TCP/TLS connection open so server concurrency
                # is exercised instead of depending on Chrome's connection order.
                idle = socket.create_connection(("127.0.0.1", port), timeout=5)
                if idle_tls:
                    idle = client_context.wrap_socket(
                        idle,
                        server_hostname="localhost",
                    )
                with chrome_session(
                    self.temp_dir,
                    crawl_id="tls-timeout",
                    snapshot_id="tls-timeout",
                    test_url=test_url,
                    navigate=False,
                    timeout=30,
                    env_overrides={"CHROME_CHECK_SSL_VALIDITY": "false"},
                ) as (_, _, chrome_dir, env):
                    # Serve Chrome's real static response before suspending the
                    # origin process. Its independent chain handshake must stall
                    # without making the initial browser navigation race an idle
                    # preconnection for the only HTTP server thread.
                    ssl_dir = chrome_dir.parent / "sslcerts"
                    output = ssl_dir / "sslcerts.jsonl"
                    listener = start_process_and_wait_for_file(
                        [str(SSLCERTS_HOOK), f"--url={test_url}"],
                        output,
                        cwd=chrome_dir.parent,
                        env={**env, "SSLCERTS_TIMEOUT": "2"},
                    )
                    try:
                        navigation = subprocess.run(
                            [str(CHROME_NAVIGATE_HOOK), f"--url={test_url}"],
                            cwd=chrome_dir,
                            env=env,
                            capture_output=True,
                            text=True,
                            timeout=120,
                        )
                        assert navigation.returncode == 0, navigation.stderr
                        assert (
                            json.loads(
                                (chrome_dir / "navigation.json").read_text(),
                            )["status"]
                            == 200
                        )
                        assert (
                            "HTTPS response sent; suspending origin"
                            in (self.temp_dir / "https.log").read_text()
                        )
                        wait_for_file(
                            output,
                            process=listener,
                            ready=lambda p: p.exists() and p.stat().st_size > 0,
                        )
                        record = json.loads(output.read_text())
                        assert record["protocol"] in {"TLS 1.2", "TLS 1.3"}
                        assert record["subjectName"] == "localhost"
                        assert (
                            "Timed out fetching certificate chain"
                            in record["chainError"]
                        )
                        assert listener.poll() is None
                    finally:
                        listener.terminate()
                        stdout, stderr = listener.communicate(timeout=30)
                    assert listener.returncode == 0, stderr
                    assert "Unhandled 'error' event" not in stderr
                    results = [
                        json.loads(line)
                        for line in stdout.splitlines()
                        if line.startswith("{")
                    ]
                    assert len(results) == 1 and results[0]["status"] == "succeeded"
            finally:
                server.send_signal(signal.SIGCONT)
                if idle is not None:
                    idle.close()
                server.kill()
                server.wait(timeout=5)

    def test_failed_https_navigation_does_not_report_certificate_success(self):
        """A real failed Chrome navigation cannot produce a successful SSL result."""
        with socket.socket() as closed_socket:
            closed_socket.bind(("127.0.0.1", 0))
            closed_port = closed_socket.getsockname()[1]
        test_url = f"https://127.0.0.1:{closed_port}/"
        snapshot_id = "test-ssl-unreachable"

        with chrome_session(
            self.temp_dir,
            crawl_id="test-ssl-unreachable-crawl",
            snapshot_id=snapshot_id,
            test_url=test_url,
            navigate=False,
            timeout=30,
        ) as (_, _, snapshot_chrome_dir, env):
            ssl_dir = snapshot_chrome_dir.parent / "sslcerts"
            ssl_dir.mkdir(exist_ok=True)
            ssl_output = ssl_dir / "sslcerts.jsonl"
            listener = start_process_and_wait_for_file(
                [
                    str(SSLCERTS_HOOK),
                    f"--url={test_url}",
                    f"--snapshot-id={snapshot_id}",
                ],
                ssl_output,
                cwd=ssl_dir,
                env=env,
            )
            try:
                navigation = subprocess.run(
                    [
                        str(CHROME_NAVIGATE_HOOK),
                        f"--url={test_url}",
                        f"--snapshot-id={snapshot_id}",
                    ],
                    cwd=snapshot_chrome_dir,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=env,
                )
                assert navigation.returncode != 0
                navigation_state = json.loads(
                    (snapshot_chrome_dir / "navigation.json").read_text(),
                )
                assert "ERR_CONNECTION_REFUSED" in navigation_state["error"]
            finally:
                listener.terminate()
                stdout, stderr = listener.communicate(timeout=30)

            assert listener.returncode == 0, stderr
            assert ssl_output.read_text() == ""
            records = [
                json.loads(line) for line in stdout.splitlines() if line.startswith("{")
            ]
            assert len(records) == 1
            assert records[0]["type"] == "ArchiveResult"
            assert records[0]["status"] == "failed"
            assert "ERR_CONNECTION_REFUSED" in records[0]["output_str"]

    @pytest.mark.parametrize("use_public_url", [False, True])
    def test_ssl_extracts_certificate_from_https_url(
        self,
        chrome_test_https_url,
        use_public_url,
    ):
        """SSL hook should extract certificate info from a real HTTPS URL."""
        test_url = (
            "https://archivebox.io/tests/" if use_public_url else chrome_test_https_url
        )
        snapshot_id = "test-ssl-snapshot"

        with chrome_session(
            self.temp_dir,
            crawl_id="test-ssl-crawl",
            snapshot_id=snapshot_id,
            test_url=test_url,
            navigate=False,
            timeout=30,
            env_overrides={"CHROME_CHECK_SSL_VALIDITY": "false"},
        ) as (chrome_process, chrome_pid, snapshot_chrome_dir, env):
            ssl_dir = snapshot_chrome_dir.parent / "sslcerts"
            ssl_dir.mkdir(exist_ok=True)

            # Run SSL hook with the active Chrome session (background hook)
            ssl_output = ssl_dir / "sslcerts.jsonl"
            result = start_process_and_wait_for_file(
                [
                    str(SSLCERTS_HOOK),
                    f"--url={test_url}",
                    f"--snapshot-id={snapshot_id}",
                ],
                ssl_output,
                cwd=ssl_dir,
                env=env,
            )

            nav_result = subprocess.run(
                [
                    str(CHROME_NAVIGATE_HOOK),
                    f"--url={test_url}",
                    f"--snapshot-id={snapshot_id}",
                ],
                cwd=str(snapshot_chrome_dir),
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            assert nav_result.returncode == 0, f"Navigation failed: {nav_result.stderr}"

            wait_for_file(
                ssl_output,
                process=result,
                ready=lambda path: path.exists() and path.stat().st_size > 0,
            )
            result.terminate()
            stdout, stderr = result.communicate(timeout=30)

            ssl_data = None

            # Try parsing from file first
            if ssl_output.exists():
                with open(ssl_output) as f:
                    content = f.read().strip()
                    if content.startswith("{"):
                        ssl_data = json.loads(content)

            # Try parsing from stdout if not in file
            if not ssl_data:
                for line in stdout.split("\n"):
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            record = json.loads(line)
                            if (
                                "protocol" in record
                                or "issuer" in record
                                or record.get("type") == "SSL"
                            ):
                                ssl_data = record
                                break
                        except json.JSONDecodeError:
                            continue

            # Verify hook ran successfully
            assert "Traceback" not in stderr
            assert "Error:" not in stderr

            # HTTPS fixture page must produce SSL metadata.
            assert ssl_data is not None, "No SSL data extracted from HTTPS URL"

            # Verify we got certificate info
            assert "protocol" in ssl_data, f"SSL data missing protocol: {ssl_data}"
            # CDP SecurityDetails.protocol reports QUIC for HTTPS over HTTP/3.
            protocols = {"TLS 1.2", "TLS 1.3"}
            if use_public_url:
                protocols.add("QUIC")
            assert ssl_data["protocol"] in protocols, ssl_data
            assert ssl_data["schemeIsCryptographic"] is True

            if use_public_url:
                assert ssl_data["certificateChain"]
                leaf_pem = snapshot_chrome_dir.parent / ssl_data["leafPemPath"]
                leaf_der = ssl.PEM_cert_to_DER_cert(leaf_pem.read_text())
                leaf_fingerprint = hashlib.sha256(leaf_der).hexdigest()
                assert ssl_data["leafFingerprint256"] == leaf_fingerprint
                assert (
                    ssl_data["certificateChain"][0]["fingerprint256"]
                    == leaf_fingerprint
                )
                for certificate in ssl_data["certificateChain"]:
                    fingerprint = certificate["fingerprint256"].replace(":", "").lower()
                    assert certificate["ctSearchUrl"] == (
                        f"https://ctlogs.dev/search?q={fingerprint}"
                    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
