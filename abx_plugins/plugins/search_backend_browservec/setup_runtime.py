"""Provision the optional local model and pinned JavaScript runtime explicitly."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.request import urlopen

from abx_plugins.plugins.search_backend_browservec.search import (
    PLUGIN_DIR,
    enabled as search_backend_enabled,
    runtime_dir,
)

MODEL_URL = (
    "https://huggingface.co/Xenova/all-MiniLM-L6-v2/resolve/"
    "751bff37182d3f1213fa05d7196b954e230abad9/"
)
MODEL_SHA256 = {
    "config.json": "7135149f7cffa1a573466c6e4d8423ed73b62fd2332c575bf738a0d033f70df7",
    "tokenizer.json": "da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0",
    "tokenizer_config.json": "9261e7d79b44c8195c1cada2b453e55b00aeb81e907a6664974b4d7776172ab3",
    "special_tokens_map.json": "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
    "onnx/model_quantized.onnx": "afdb6f1a0e45b715d0bb9b11772f032c399babd23bfc31fed1c170afc848bdb1",
}


def install() -> Path:
    target = runtime_dir()
    if target.exists():
        raise FileExistsError(f"Local runtime already exists: {target}")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".install-", dir=target.parent
    ) as directory:
        staging = Path(directory) / "runtime"
        staging.mkdir(mode=0o700)
        for manifest in ("package.json", "package-lock.json"):
            shutil.copyfile(PLUGIN_DIR / manifest, staging / manifest)
        subprocess.run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=staging,
            check=True,
            timeout=300,
        )
        for name, expected in MODEL_SHA256.items():
            path = staging / "model" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            checksum = hashlib.sha256()
            total = 0
            with (
                urlopen(MODEL_URL + name, timeout=30) as response,
                path.open("wb") as dest,
            ):
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > 32 * 1024 * 1024:
                        raise ValueError(f"Model file exceeded download budget: {name}")
                    checksum.update(chunk)
                    dest.write(chunk)
            if checksum.hexdigest() != expected:
                raise ValueError(f"Model checksum mismatch: {name}")
        staging.rename(target)
    return target


def install_binary() -> Path:
    if not search_backend_enabled():
        raise RuntimeError("Enable BrowserVec before installing its optional model")
    directory = runtime_dir()
    if not directory.exists():
        directory = install()
    if not all((directory / "model" / name).is_file() for name in MODEL_SHA256):
        raise RuntimeError("Incomplete model installation")
    for relpath in (
        "browservec/dist/browservec.js",
        "@huggingface/transformers/dist/transformers.node.mjs",
    ):
        if not (directory / "node_modules" / relpath).is_file():
            raise RuntimeError("Incomplete JavaScript runtime installation")
    bin_dir = Path(os.environ["BIN_DIR"])
    bin_dir.mkdir(parents=True, exist_ok=True)
    marker = bin_dir / "browservec-runtime"
    with tempfile.NamedTemporaryFile(
        "w", dir=bin_dir, delete=False, prefix=".browservec-"
    ) as temp:
        path = Path(temp.name)
        temp.write(
            "#!/bin/sh\n"
            'if [ "${1:-}" = "--version" ]; then printf "%s\\n" "BrowserVec 1.0.0"; exit; fi\n'
            f'exec "${{NODE_BINARY:-node}}" {shlex.quote(str(PLUGIN_DIR / "runner.mjs"))} '
            f"{shlex.quote(str(directory))}\n"
        )
    path.chmod(0o700)
    path.replace(marker)
    return marker


if __name__ == "__main__":
    print(install_binary())
