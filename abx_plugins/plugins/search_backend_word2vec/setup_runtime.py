"""Provision the optional local model and pinned JavaScript runtime explicitly."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from abx_plugins.plugins.search_backend_word2vec.search import (
    PLUGIN_DIR,
    RuntimeUnavailable,
    enabled,
    index_root,
    model_selection,
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


def _model_files(preset: str, model: str) -> tuple[str, dict[str, str | None]]:
    if preset == "minilm":
        return MODEL_URL, MODEL_SHA256
    with urlopen(f"https://huggingface.co/api/models/{model}", timeout=30) as response:
        metadata = json.loads(response.read(2 * 1024 * 1024 + 1))
    revision = metadata.get("sha")
    if not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise RuntimeUnavailable("Could not resolve the Hugging Face model revision")
    files = {record["rfilename"] for record in metadata["siblings"]}
    required = {
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "onnx/model_quantized.onnx",
    }
    if not required <= files:
        raise RuntimeUnavailable(
            "Custom models need their tokenizer and onnx/model_quantized.onnx"
        )
    return f"https://huggingface.co/{model}/resolve/{revision}/", {
        name: None for name in sorted(files & MODEL_SHA256.keys())
    }


def _download_model(staging: Path, preset: str, model: str, deadline: float) -> str:
    base, files = _model_files(preset, model)
    for name, expected in files.items():
        path = staging / "model" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        checksum = hashlib.sha256()
        total = 0
        with (
            urlopen(
                base + name, timeout=min(30, max(1, deadline - time.monotonic()))
            ) as response,
            path.open("wb") as dest,
        ):
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                total += len(chunk)
                limit = (
                    (32 if preset == "minilm" else 256) * 1024 * 1024
                    if name.endswith(".onnx")
                    else 16 * 1024 * 1024
                )
                if total > limit or time.monotonic() > deadline:
                    raise RuntimeUnavailable(
                        f"Model file exceeded setup budget: {name}"
                    )
                checksum.update(chunk)
                dest.write(chunk)
        if expected and checksum.hexdigest() != expected:
            raise RuntimeUnavailable(f"Model checksum mismatch: {name}")
    return base.rstrip("/").rsplit("/", 1)[-1]


def _install_packages(preset: str, deadline: float) -> Path:
    hashes_only = preset == "browservec"
    target = (
        index_root() / "packages" / ("browservec" if hashes_only else "transformers")
    )
    if target.is_dir():
        return target / "node_modules"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".install-", dir=target.parent
    ) as directory:
        staging = Path(directory)
        if hashes_only:
            lock = PLUGIN_DIR / "package-hash-lock.json"
            manifest = {**json.loads(lock.read_text())["packages"][""], "private": True}
            (staging / "package.json").write_text(json.dumps(manifest))
        else:
            lock = PLUGIN_DIR / "package-lock.json"
            shutil.copyfile(PLUGIN_DIR / "package.json", staging / "package.json")
        shutil.copyfile(lock, staging / "package-lock.json")
        subprocess.run(
            ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=staging,
            stdout=subprocess.DEVNULL,
            check=True,
            timeout=max(1, deadline - time.monotonic()),
        )
        staging.rename(target)
    return target / "node_modules"


def ensure_runtime() -> Path:
    if not enabled():
        raise RuntimeUnavailable("Enable Word2Vec Search before indexing")
    target = runtime_dir()
    if (target / "ready").is_file():
        return target
    preset, model = model_selection()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    deadline = time.monotonic() + 300
    with (index_root() / "install.lock").open("a+b") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeUnavailable("Timed out waiting for Word2Vec runtime")
                time.sleep(0.1)
        if (target / "ready").is_file():
            return target
        with tempfile.TemporaryDirectory(
            prefix=".install-", dir=target.parent
        ) as directory:
            staging = Path(directory)
            try:
                modules = _install_packages(preset, deadline)
                (staging / "node_modules").symlink_to(
                    os.path.relpath(modules, staging), target_is_directory=True
                )
                revision = (
                    ""
                    if preset == "browservec"
                    else _download_model(staging, preset, model, deadline)
                )
                (staging / "ready").write_text(
                    json.dumps({"preset": preset, "model": model, "revision": revision})
                )
                staging.rename(target)
            except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
                raise RuntimeUnavailable(
                    f"Cannot set up Word2Vec Search: {exc}"
                ) from exc
    return target
