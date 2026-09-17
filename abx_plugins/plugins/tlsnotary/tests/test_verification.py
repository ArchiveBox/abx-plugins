"""Verify a real Cabbage-signed public capture using the native CLI."""

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
ARTIFACT = Path(__file__).parent / "fixtures" / "hacker-news.tlsn"
KEY = json.loads((PLUGIN / "web/trust.json").read_text())["public_key"]


@pytest.fixture(scope="module")
def binary():
    configured = os.environ.get("TLSNOTARY_BINARY")
    if configured:
        return configured
    subprocess.run(
        [
            "cargo",
            "build",
            "--release",
            "--locked",
            "--manifest-path",
            str(PLUGIN / "runtime/Cargo.toml"),
        ],
        check=True,
    )
    return str(PLUGIN / "runtime/target/release/abx-tlsnotary")


def run_verify(binary, artifact, key=KEY, *args):
    return subprocess.run(
        [binary, "verify", str(artifact), "--trusted-key", key, *args],
        capture_output=True,
        text=True,
    )


def test_real_signed_response(binary):
    result = run_verify(binary, ARTIFACT)
    assert result.returncode == 0, result.stderr
    verified = json.loads(result.stdout)
    assert verified["url"] == "https://news.ycombinator.com/"
    assert verified["status"] == 200
    assert verified["response_complete"] is True
    assert verified["connection_time_unix"] == 1789600956
    body = base64.b64decode(verified["body_base64"])
    assert len(body) == 34163
    assert b"Hacker News" in body
    assert (
        hashlib.sha256(body).hexdigest()
        == verified["body_sha256"]
        == "53b1db69859f8a91c5bcac9ae85d104069092b9286e8988e230ac72f40f6a089"
    )


@pytest.mark.parametrize(
    "artifact_name",
    ["hacker-news.tlsn", "youtube-prefix.tlsn", "youtube-gzip-prefix.tlsn"],
)
@pytest.mark.parametrize("damage", ["modified", "truncated", "trailing"])
def test_corrupted_artifact_rejected(binary, tmp_path, damage, artifact_name):
    data = (ARTIFACT.parent / artifact_name).read_bytes()
    if damage == "modified":
        data = (
            data[: len(data) // 2]
            + bytes([data[len(data) // 2] ^ 1])
            + data[len(data) // 2 + 1 :]
        )
    elif damage == "truncated":
        data = data[:-1]
    else:
        data += b"x"
    damaged = tmp_path / "damaged.tlsn"
    damaged.write_bytes(data)
    assert run_verify(binary, damaged).returncode != 0


def test_untrusted_key_rejected(binary):
    result = run_verify(binary, ARTIFACT, "02" + "00" * 32)
    assert result.returncode != 0
    assert "not trusted" in result.stderr


def test_expected_url_rejected(binary):
    result = run_verify(
        binary,
        ARTIFACT,
        KEY,
        "--expected-url",
        "https://wrong.example/",
    )
    assert result.returncode != 0
    assert "differs from expected" in result.stderr


def test_real_signed_prefix_is_not_a_complete_response(binary):
    artifact = Path(__file__).parent / "fixtures" / "youtube-prefix.tlsn"
    result = run_verify(binary, artifact)
    assert result.returncode == 0, result.stderr
    verified = json.loads(result.stdout)
    assert verified["url"] == "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    assert verified["status"] == 200
    assert verified["response_complete"] is False
    body = base64.b64decode(verified["body_base64"])
    assert len(body) == 27474
    assert (
        hashlib.sha256(body).hexdigest()
        == verified["body_sha256"]
        == "4e34695198de66340538c69aa37fbfc064adfe98400c415cee76818f488e5dcb"
    )


def test_real_gzip_prefix_decodes_authenticated_bytes(binary):
    artifact = Path(__file__).parent / "fixtures" / "youtube-gzip-prefix.tlsn"
    result = run_verify(binary, artifact)
    assert result.returncode == 0, result.stderr
    verified = json.loads(result.stdout)
    assert verified["response_complete"] is False
    body = base64.b64decode(verified["body_base64"])
    assert len(body) == 145304
    assert (
        hashlib.sha256(body).hexdigest()
        == verified["body_sha256"]
        == "d1fbd120e0d63a627af339fc1488083373f6941afc4c65845e9a402ce030a146"
    )
