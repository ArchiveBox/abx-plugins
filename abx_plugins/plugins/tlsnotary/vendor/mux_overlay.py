"""Apply the removable, checksum-pinned mux fix to an official extension copy."""

import bz2
import hashlib
import json
from pathlib import Path

OVERLAY_DIR = Path(__file__).resolve().parent / "mux-close-drain"


def _integer(value: bytes) -> int:
    """BSDIFF40 uses little-endian sign-magnitude integers, not two's complement."""
    number = int.from_bytes(value, "little")
    return -(number & ((1 << 63) - 1)) if number >> 63 else number


def _decompress(value: bytes, limit: int) -> bytes:
    decoder = bz2.BZ2Decompressor()
    decoded = decoder.decompress(value, max_length=limit + 1)
    if len(decoded) > limit or not decoder.eof or decoder.unused_data:
        raise ValueError("Invalid TLSNotary mux delta compressed block")
    return decoded


def _apply_delta(original: bytes, patch: bytes, expected_size: int) -> bytes:
    """Decode the standard BSDIFF40 delta using only Python's standard library."""
    if len(patch) < 32 or patch[:8] != b"BSDIFF40":
        raise ValueError("Invalid TLSNotary mux delta header")
    controls_size, differences_size, output_size = (
        _integer(patch[offset : offset + 8]) for offset in (8, 16, 24)
    )
    if (
        controls_size < 0
        or differences_size < 0
        or output_size != expected_size
        or not 0 < output_size <= 16 * 1024 * 1024
        or 32 + controls_size + differences_size > len(patch)
    ):
        raise ValueError("Invalid TLSNotary mux delta lengths")
    boundary = 32 + controls_size
    controls = _decompress(patch[32:boundary], 4 * 1024 * 1024)
    differences = _decompress(
        patch[boundary : boundary + differences_size],
        output_size,
    )
    extra = _decompress(patch[boundary + differences_size :], output_size)
    output = bytearray(output_size)
    old_position = new_position = diff_position = extra_position = control_position = 0
    while new_position < output_size:
        if control_position + 24 > len(controls):
            raise ValueError("Truncated TLSNotary mux delta controls")
        size, extra_size, seek = (
            _integer(controls[offset : offset + 8])
            for offset in range(control_position, control_position + 24, 8)
        )
        control_position += 24
        if (
            size < 0
            or extra_size < 0
            or new_position + size + extra_size > output_size
            or old_position < 0
            or old_position + size > len(original)
            or diff_position + size > len(differences)
            or extra_position + extra_size > len(extra)
        ):
            raise ValueError("Invalid TLSNotary mux delta range")
        output[new_position : new_position + size] = bytes(
            (old + difference) & 255
            for old, difference in zip(
                original[old_position : old_position + size],
                differences[diff_position : diff_position + size],
                strict=True,
            )
        )
        new_position += size
        diff_position += size
        output[new_position : new_position + extra_size] = extra[
            extra_position : extra_position + extra_size
        ]
        new_position += extra_size
        extra_position += extra_size
        old_position += size + seek
    if (
        control_position != len(controls)
        or diff_position != len(differences)
        or extra_position != len(extra)
    ):
        raise ValueError("Unexpected trailing TLSNotary mux delta data")
    return bytes(output)


def apply_mux_overlay(extension: Path) -> tuple[str, ...]:
    """Fail closed on a different release, damaged delta, or changed output bytes."""
    manifest = json.loads((OVERLAY_DIR / "manifest.json").read_text())
    patch = (OVERLAY_DIR / "tlsn_wasm_bg.wasm.bsdiff").read_bytes()
    if hashlib.sha256(patch).hexdigest() != manifest["patch_sha256"]:
        raise ValueError("TLSNotary mux delta checksum mismatch")
    wasm = None
    for name, specification in manifest["files"].items():
        path = extension / name
        original = path.read_bytes()
        if hashlib.sha256(original).hexdigest() != specification["original_sha256"]:
            raise ValueError(f"TLSNotary mux overlay input checksum mismatch: {name}")
        if name.endswith(".wasm"):
            if wasm is None:
                wasm = _apply_delta(original, patch, specification["output_size"])
            output = wasm
        else:
            # The matching compiler preserves the original WASM ABI and glue.
            output = original
        if hashlib.sha256(output).hexdigest() != specification["output_sha256"]:
            raise ValueError(f"TLSNotary mux overlay output checksum mismatch: {name}")
        path.write_bytes(output)
    return tuple(manifest["files"])
