#!/usr/bin/env -S uv run --script
"""Black-box checks of real captures; usage: test_artifacts_cli.py BINARY KEY CAPTURE_DIR..."""

import base64
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    binary, key, *directories = sys.argv[1:]
    assert directories, "Pass at least one directory containing a real capture.tlsn"
    for directory in directories:
        directory = Path(directory)
        proof = directory / "capture.tlsn"

        def verify(path, *args):
            return subprocess.run(
                [binary, "verify", str(path), "--trusted-key", key, *args],
                capture_output=True,
                text=True,
            )

        result = verify(proof)
        assert result.returncode == 0, result.stderr
        verified = json.loads(result.stdout)
        body = base64.b64decode(verified["body_base64"])
        assert body == (directory / "response.body").read_bytes()
        assert hashlib.sha256(body).hexdigest() == verified["body_sha256"]
        assert verified == json.loads((directory / "verified.json").read_text())
        assert verify(proof, "--expected-url", verified["url"]).returncode == 0
        assert verify(proof, "--expected-url", "https://wrong.example/").returncode != 0
        wrong_key = subprocess.run(
            [binary, "verify", str(proof), "--trusted-key", "02" + "00" * 32],
            capture_output=True,
        )
        assert wrong_key.returncode != 0
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.tlsn"
            original = proof.read_bytes()
            for label, data in [
                ("truncated", original[:-1]),
                ("trailing", original + b"x"),
                (
                    "modified",
                    original[: len(original) // 2]
                    + bytes([original[len(original) // 2] ^ 1])
                    + original[len(original) // 2 + 1 :],
                ),
            ]:
                bad.write_bytes(data)
                assert verify(bad).returncode != 0, label
        print(
            json.dumps(
                {
                    "verified": verified["url"],
                    "bytes": len(body),
                    "sha256": verified["body_sha256"],
                    "negative_checks": 5,
                },
            ),
        )


if __name__ == "__main__":
    main()
