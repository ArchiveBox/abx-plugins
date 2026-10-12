# ci-runner: hosted-linux
"""Expected verifier limits must not turn an otherwise usable capture into failure."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
import pytest

from abx_plugins.plugins.base.testing import install_required_binary_from_config
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    resolve_node_with_abxpkg,
)
from abx_plugins.plugins.tlsnotary.tests.test_navigation_headers import (
    CHECK_CAPTURE,
    PLUGIN_DIR,
    PREPARE_HOOK,
    SNAPSHOT_HOOK,
    _inspect_browser,
    verifier as verifier,
)

pytestmark = pytest.mark.usefixtures("ensure_chrome_test_prereqs")


def _prepare(env: dict[str, str], _chrome_dir: Path) -> None:
    install_required_binary_from_config(PLUGIN_DIR, "tlsnotary", env=env)
    prepared = subprocess.run(
        [str(PREPARE_HOOK)],
        cwd=env["CRAWL_DIR"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr


@pytest.fixture
def unavailable_gateway(tmp_path, request):
    """Use the shipped gateway and real refused sockets, never a substitute server."""
    node = Path(resolve_node_with_abxpkg(os.environ.copy()))
    server_dir = tmp_path / "gateway"
    server_dir.mkdir()
    for name in ("server.mjs", "memory.cjs", "package.json", "package-lock.json"):
        shutil.copy2(PLUGIN_DIR / "server" / name, server_dir / name)
    installed = subprocess.run(
        [str(node.with_name("npm")), "ci", "--omit=dev", "--no-audit", "--no-fund"],
        cwd=server_dir,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    key = tmp_path / "signing.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    with socket.socket() as backend:
        # A bound but non-listening socket exercises the OS refusal/timeout path
        # without borrowing another process's port or emulating the verifier.
        backend.bind(("127.0.0.1", 0))
        backend_port = backend.getsockname()[1]
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        env = {
            **os.environ,
            "SIGNING_KEY": str(key),
            "PORT": str(port),
            "WEBHOOK_PORT": "0",
            "VERIFIER_URL": f"ws://127.0.0.1:{backend_port}",
            "TLSNOTARY_MIN_AVAILABLE_MEMORY_MB": (
                "1048576" if request.param == "memory" else "512"
            ),
        }
        with (tmp_path / "gateway.log").open("w+") as log:
            gateway = subprocess.Popen(
                [str(node), str(server_dir / "server.mjs")],
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                url = f"http://127.0.0.1:{port}"
                deadline = time.monotonic() + 5
                while True:
                    assert gateway.poll() is None
                    try:
                        with urllib.request.urlopen(
                            url + "/health",
                            timeout=1,
                        ) as response:
                            assert json.load(response) == {"ok": True, "active": 0}
                        break
                    except urllib.error.URLError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
                if request.param == "offline":
                    url = f"http://127.0.0.1:{backend_port}"
                yield {"TLSNOTARY_VERIFIER_URL": url}
            finally:
                gateway.terminate()
                gateway.wait(timeout=10)
                log.seek(0)
                print(log.read())


@pytest.mark.parametrize(
    ("unavailable_gateway", "reason"),
    [
        ("memory", "low on memory"),
        ("offline", "unavailable"),
        ("backend_refused", "could not connect to the verifier"),
    ],
    indirect=["unavailable_gateway"],
)
def test_real_hook_reports_expected_verifier_absence(
    tmp_path,
    unavailable_gateway,
    reason,
):
    url = "https://example.com/"
    with chrome_session(
        tmp_path,
        test_url=url,
        timeout=45,
        env_overrides={
            **unavailable_gateway,
            "TLSNOTARY_ENABLED": "true",
            "TLSNOTARY_TIMEOUT": "30",
        },
        crawl_setup=_prepare,
    ) as (_process, _pid, snapshot_chrome_dir, env):
        output = Path(env["SNAP_DIR"]) / "tlsnotary"
        output.mkdir(exist_ok=True)
        # A failed attempt must not destroy a previously saved proof on rerun.
        previous = output / "previous-proof.txt"
        previous.write_text("previous capture remains available")
        result = subprocess.run(
            [str(SNAPSHOT_HOOK), f"--url={url}", "--snapshot-id=test-snapshot"],
            cwd=output,
            env=env,
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
        print(result.stdout, result.stderr)
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        ]
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(records) == 1, records
        assert records[0]["type"] == "ArchiveResult"
        assert records[0]["status"] == "noresults", records
        assert reason in records[0]["output_str"]
        assert "WARNING" in result.stderr
        assert reason in result.stderr
        assert previous.read_text() == "previous capture remains available"
        assert not (output / "receipt.json").exists()
        assert not (output / "response.http").exists()
        assert not (Path(env["CRAWL_DIR"]) / "tlsnotary/capture.lock").exists()
        state = _inspect_browser(
            env,
            (snapshot_chrome_dir / "cdp_url.txt").read_text().strip(),
            "",
        )
        assert state["pageUrls"].count(url) == 1, state
        assert all(
            not page.startswith("chrome-extension://") for page in state["pageUrls"]
        ), state


@pytest.mark.parametrize(
    ("setting", "limit", "direction"),
    [
        ("TLSNOTARY_MAX_RECV_BYTES", 1024, "response"),
        ("TLSNOTARY_MAX_SENT_BYTES", 512, "request"),
    ],
)
def test_real_hook_reports_explicit_transcript_size_limit(
    tmp_path,
    verifier,
    setting,
    limit,
    direction,
):
    # WHY: exercise the real TLS record-layer refusal, including Chrome's
    # request headers, instead of treating any crash or deadline as oversized.
    url = "https://www.rfc-editor.org/rfc/rfc8441.txt"
    with chrome_session(
        tmp_path,
        test_url=url,
        timeout=45,
        env_overrides={
            **verifier,
            "TLSNOTARY_ENABLED": "true",
            "TLSNOTARY_TIMEOUT": "60",
            setting: str(limit),
        },
        crawl_setup=_prepare,
    ) as (_process, _pid, snapshot_chrome_dir, env):
        output = Path(env["SNAP_DIR"]) / "tlsnotary"
        output.mkdir(exist_ok=True)
        previous = output / "previous-proof.txt"
        previous.write_text("previous capture remains available")
        result = subprocess.run(
            [str(SNAPSHOT_HOOK), f"--url={url}", "--snapshot-id=test-snapshot"],
            cwd=output,
            env=env,
            capture_output=True,
            text=True,
            timeout=70,
            check=False,
        )
        print(result.stdout, result.stderr)
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        ]
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(records) == 1, records
        assert records[0]["type"] == "ArchiveResult"
        assert records[0]["status"] == "noresults", records
        expected = f"{direction} exceeds the configured size limit ({setting}={limit})"
        assert expected in records[0]["output_str"], records
        assert "WARNING" in result.stderr
        assert expected in result.stderr
        assert previous.read_text() == "previous capture remains available"
        assert not (output / "receipt.json").exists()
        assert not (output / "response.http").exists()
        assert not (Path(env["CRAWL_DIR"]) / "tlsnotary/capture.lock").exists()
        state = _inspect_browser(
            env,
            (snapshot_chrome_dir / "cdp_url.txt").read_text().strip(),
            "",
        )
        assert state["pageUrls"].count(url) == 1, state
        assert all(
            not page.startswith("chrome-extension://") for page in state["pageUrls"]
        ), state


def test_real_hook_rejects_a_receipt_signed_by_an_untrusted_key(tmp_path, verifier):
    """Service availability does not relax cryptographic acceptance of a proof."""
    url = "https://example.com/"
    with chrome_session(
        tmp_path,
        test_url=url,
        timeout=45,
        env_overrides={
            **verifier,
            "TLSNOTARY_ENABLED": "true",
            "TLSNOTARY_TIMEOUT": "90",
        },
        crawl_setup=_prepare,
    ) as (_process, _pid, _chrome_dir, env):
        output = Path(env["SNAP_DIR"]) / "tlsnotary"
        output.mkdir(exist_ok=True)
        saved = {}
        for trusted in (True, False):
            proof_env = env.copy()
            if not trusted:
                # The bundled public service key is different from this fixture's
                # real signer. A valid proof from the wrong signer must fail.
                proof_env["TLSNOTARY_TRUSTED_KEY"] = ""
            result = subprocess.run(
                [str(SNAPSHOT_HOOK), f"--url={url}", "--snapshot-id=test-snapshot"],
                cwd=output,
                env=proof_env,
                capture_output=True,
                text=True,
                timeout=100,
                check=False,
            )
            print(result.stdout, result.stderr)
            records = [
                json.loads(line)
                for line in result.stdout.splitlines()
                if line.startswith("{")
            ]
            assert len(records) == 1, records
            if trusted:
                assert result.returncode == 0, result.stdout + result.stderr
                assert records[0]["status"] == "succeeded", records
                checked = subprocess.run(
                    [
                        env["NODE_BINARY"],
                        str(CHECK_CAPTURE),
                        str(output),
                        verifier["TLSNOTARY_TRUSTED_KEY"],
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                assert checked.returncode == 0, checked.stdout + checked.stderr
                saved = {
                    name: (output / name).read_bytes()
                    for name in ("receipt.json", "response.http")
                }
            else:
                assert result.returncode == 1, result.stdout + result.stderr
                assert records[0]["status"] == "failed", records
                assert "signature" in result.stderr.lower(), result.stderr
                assert {name: (output / name).read_bytes() for name in saved} == saved


def test_real_proof_fits_between_admission_and_stop_reserves(tmp_path, verifier):
    """A proof that is admitted must be able to finish above its stop reserve.

    Otherwise a host with just enough admission headroom spends minutes in MPC
    and is then stopped for low memory with no proof. Measure a real proof's
    memory growth across Chrome's process tree (resident set, an upper bound)
    against the hook's own reserves, read from the module the hook uses.
    """
    reserves = json.loads(
        subprocess.check_output(
            [
                resolve_node_with_abxpkg(os.environ.copy()),
                "-e",
                "const m = require(process.argv[1]); console.log(JSON.stringify({"
                "start: m.PROOF_START_HEADROOM_BYTES, stop: m.PROOF_STOP_HEADROOM_BYTES,"
                "ram: m.memoryHeadroomBytes({includeSwap: false})}))",
                str(PLUGIN_DIR / "server" / "memory.cjs"),
            ],
            text=True,
            timeout=30,
        ),
    )
    assert reserves["ram"] > 0, reserves

    url = "https://news.ycombinator.com/"
    with chrome_session(
        tmp_path,
        test_url=url,
        timeout=45,
        env_overrides={
            **verifier,
            "TLSNOTARY_ENABLED": "true",
            "TLSNOTARY_TIMEOUT": "90",
        },
        crawl_setup=_prepare,
    ) as (_process, chrome_pid, _chrome_dir, env):
        output = Path(env["SNAP_DIR"]) / "tlsnotary"
        output.mkdir(exist_ok=True)

        def resident_bytes() -> int:
            total = 0
            root = psutil.Process(chrome_pid)
            for process in (root, *root.children(recursive=True)):
                try:
                    total += process.memory_info().rss
                except psutil.Error:
                    pass
            return total

        baseline = resident_bytes()
        peak = baseline
        finished = threading.Event()

        def sample() -> None:
            nonlocal peak
            while not finished.is_set():
                peak = max(peak, resident_bytes())
                time.sleep(0.05)

        sampler = threading.Thread(target=sample)
        sampler.start()
        try:
            result = subprocess.run(
                [str(SNAPSHOT_HOOK), f"--url={url}", "--snapshot-id=test-snapshot"],
                cwd=output,
                env=env,
                capture_output=True,
                text=True,
                timeout=100,
                check=False,
            )
        finally:
            finished.set()
            sampler.join()
        print(result.stdout, result.stderr, f"growth={peak - baseline}", reserves)
        records = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        ]
        assert result.returncode == 0, result.stdout + result.stderr
        assert [record["status"] for record in records] == ["succeeded"], records
        assert (output / "receipt.json").is_file()
        assert peak - baseline > 0
        assert peak - baseline + reserves["stop"] <= reserves["start"], (
            peak - baseline,
            reserves,
        )
