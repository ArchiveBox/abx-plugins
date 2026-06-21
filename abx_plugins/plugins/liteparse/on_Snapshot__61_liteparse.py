#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12,<3.14"
# ///
"""
Extract text from PDFs, Office documents, and images using LiteParse
(the ``lit`` CLI by LlamaIndex, v2+).

Scans the snapshot directory for documents produced by other plugins
(``pdf``, ``responses``, ``staticfile``, ``wget``) and runs ``lit batch-parse``
on each supported file. Each source produces one ``<source-stem>.txt`` and
``<source-stem>.json`` directly in the plugin output dir — no merged
``content.txt`` or manifest. Search backends (ripgrep / sqlite FTS / sonic)
auto-discover every ``.txt`` here, and the flat layout means each source's
text is indexed exactly once.

Tiny images (favicons, sprite thumbnails, etc.) are filtered out by
``LITEPARSE_MIN_IMAGE_DIMENSION`` so we don't waste OCR time on them.

Usage: on_Snapshot__61_liteparse.py --url=<url> > events.jsonl

Environment variables: see config.json (LITEPARSE_* settings).
"""

import atexit
import concurrent.futures
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import rich_click as click

from abx_plugins.plugins.base.utils import (
    emit_archive_result_record,
    load_config,
    write_text_atomic,
)


PLUGIN_NAME = "liteparse"
BIN_NAME = "lit"
BIN_PROVIDERS = "env,pnpm"
PLUGIN_DIR = Path(__file__).resolve().parent.name
CONFIG = load_config()
SNAP_DIR = Path(CONFIG.SNAP_DIR or ".").resolve()
OUTPUT_DIR = SNAP_DIR / PLUGIN_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
os.chdir(OUTPUT_DIR)

# File types LiteParse v2 can parse. Kept as a single canonical set so source
# discovery, plugin docs, and tests stay in sync.
PDF_EXTENSIONS = {".pdf"}
OFFICE_EXTENSIONS = {
    ".doc",
    ".docx",
    ".docm",
    ".odt",
    ".rtf",
    ".ppt",
    ".pptx",
    ".pptm",
    ".odp",
    ".xls",
    ".xlsx",
    ".xlsm",
    ".ods",
    ".csv",
    ".tsv",
}
IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".tiff",
    ".tif",
    ".webp",
}
SUPPORTED_EXTENSIONS = PDF_EXTENSIONS | OFFICE_EXTENSIONS | IMAGE_EXTENSIONS

# Common host paths where Tesseract trained data lives. Probed in order if
# LITEPARSE_TESSDATA_DIR/TESSDATA_PREFIX are unset, so OCR works without
# requiring per-environment setup.
TESSDATA_CANDIDATES = (
    "/opt/homebrew/share/tessdata",
    "/usr/local/share/tessdata",
    "/usr/share/tessdata",
    "/usr/share/tesseract-ocr/4.00/tessdata",
    "/usr/share/tesseract-ocr/5/tessdata",
)


def _image_is_too_small(path: Path, min_dim: int) -> bool:
    """Return True for image files smaller than min_dim in BOTH dimensions.

    Reads dimensions from the file header via ``imagesize`` (no full decode).
    Returns False on parse failure so we still attempt OCR rather than silently
    drop unfamiliar formats.
    """
    if min_dim <= 0:
        return False
    try:
        import imagesize

        width, height = imagesize.get(str(path))
    except (ValueError, OSError):
        return False
    if width <= 0 or height <= 0:
        return False
    return width < min_dim and height < min_dim


def _content_digest(path: Path) -> str:
    """Cheap content fingerprint: full MD5 of the file bytes.

    Used to dedupe identical-content files saved by multiple plugins (wget,
    responses, staticfile all routinely store the same image at different
    paths). MD5 on the typical 50KB–2MB blog asset is sub-millisecond and
    not security-sensitive here.
    """
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def find_document_sources(
    min_image_dim: int = 0,
) -> list[tuple[Path, str]]:
    """Find documents produced by upstream plugins that LiteParse can parse.

    Looks for PDFs in any pdf/ output directory, and any LiteParse-supported
    file type under responses/, staticfile/, and wget/ trees (where downloaded
    documents land regardless of MIME). Filters:
      - resolved-path dedup (drops symlink duplicates)
      - content-hash dedup (drops same-bytes-different-paths duplicates)
      - image dimension filter (drops favicons/sprites/tracking pixels)

    Returns ``(path, content_digest)`` so callers can re-use the digest for
    batch symlink naming without paying a second hash cost.
    """
    pdf_globs = ("pdf/**/*.pdf", "*_pdf/**/*.pdf")
    document_roots = (
        "responses",
        "*_responses",
        "staticfile",
        "*_staticfile",
        "wget",
        "*_wget",
        "liteparse_input",
    )

    found: list[tuple[Path, str]] = []
    seen_paths: set[str] = set()
    seen_digests: dict[str, Path] = {}

    def consider(match: Path) -> None:
        if not match.is_file() or match.stat().st_size == 0:
            return
        resolved = str(match.resolve())
        if resolved in seen_paths:
            return
        suffix = match.suffix.lower()
        if suffix in IMAGE_EXTENSIONS and _image_is_too_small(match, min_image_dim):
            return
        try:
            digest = _content_digest(match)
        except OSError:
            return
        if digest in seen_digests:
            return
        seen_digests[digest] = match
        seen_paths.add(resolved)
        found.append((match, digest))

    for base in (Path.cwd(), Path.cwd().parent):
        for pattern in pdf_globs:
            for match in base.glob(pattern):
                consider(match)

        for root in document_roots:
            for root_dir in base.glob(root):
                if not root_dir.is_dir():
                    continue
                for match in root_dir.rglob("*"):
                    if match.suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue
                    consider(match)

    return found


def _tessdata_dir_from_tesseract_binary(
    tesseract_binary: str,
    language: str,
) -> str | None:
    """Ask the host's tesseract for the path of its compiled-in tessdata dir.

    Runs ``tesseract --list-langs`` and parses the ``List of available
    languages in "<path>"`` preamble. Returns the path only if ``language``
    is among the listed langs, so the caller can trust that
    ``<path>/<language>.traineddata`` exists.
    """
    try:
        proc = subprocess.run(
            [tesseract_binary, "--list-langs"],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "TESSDATA_PREFIX": ""},
        )
    except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired):
        return None
    output = (proc.stdout or "") + (proc.stderr or "")
    match = re.search(r'available languages in "([^"]+)"', output)
    if not match:
        return None
    candidate = Path(match.group(1).rstrip("/"))
    listed_langs = {line.strip() for line in output.splitlines() if line.strip()}
    if language not in listed_langs:
        return None
    if not (candidate / f"{language}.traineddata").is_file():
        return None
    return str(candidate)


def resolve_tessdata_dir(
    configured: str,
    tesseract_binary: str,
    language: str,
) -> str | None:
    """Return a tessdata directory that contains ``<language>.traineddata``.

    Priority:
      1. Explicit ``LITEPARSE_TESSDATA_DIR`` (returned as-is if non-empty).
      2. ``TESSDATA_PREFIX`` env var, if it points at a dir with the lang file.
      3. The path the host's tesseract binary reports via ``--list-langs``.
      4. Hardcoded common system paths (brew/apt defaults).
    """
    if configured:
        candidate = Path(configured).expanduser()
        if (candidate / f"{language}.traineddata").is_file():
            return str(candidate)
        # Explicit value that doesn't contain the requested language is a
        # configuration error; fall through to discovery so we hard-fail
        # later with a useful message instead of silently passing a bad path
        # to lit.

    env_value = os.environ.get("TESSDATA_PREFIX", "")
    if env_value and (Path(env_value) / f"{language}.traineddata").is_file():
        return env_value

    from_binary = _tessdata_dir_from_tesseract_binary(tesseract_binary, language)
    if from_binary:
        return from_binary

    for path in TESSDATA_CANDIDATES:
        if (Path(path) / f"{language}.traineddata").is_file():
            return path

    return None


def build_lit_args(config) -> list[str]:
    """Translate LITEPARSE_* config values into ``lit batch-parse`` flags.

    Pins ``--num-workers=1``: lit's default and higher values measurably
    hurt local OCR (tesseract-rs already uses NEON SIMD inside the single
    worker, and the multi-worker contention costs more than it saves).
    """
    args: list[str] = ["-q", "--num-workers", "1"]

    if not config.LITEPARSE_OCR_ENABLED:
        args.append("--no-ocr")
    else:
        if config.LITEPARSE_OCR_LANGUAGE and config.LITEPARSE_OCR_LANGUAGE != "eng":
            args.extend(["--ocr-language", config.LITEPARSE_OCR_LANGUAGE])
        if config.LITEPARSE_OCR_SERVER_URL:
            args.extend(["--ocr-server-url", config.LITEPARSE_OCR_SERVER_URL])

    if config.LITEPARSE_MAX_PAGES and config.LITEPARSE_MAX_PAGES != 1000:
        args.extend(["--max-pages", str(config.LITEPARSE_MAX_PAGES)])
    if config.LITEPARSE_DPI and config.LITEPARSE_DPI != 150:
        args.extend(["--dpi", str(config.LITEPARSE_DPI)])
    if config.LITEPARSE_PASSWORD:
        args.extend(["--password", config.LITEPARSE_PASSWORD])

    args.extend(list(config.LITEPARSE_ARGS))
    args.extend(list(config.LITEPARSE_ARGS_EXTRA))
    return args


def _safe_link_name(idx: int, src: Path, content_digest: str) -> str:
    """Stable, collision-free symlink name for batch input dirs.

    ``idx`` keeps the original size-desc ordering visible in batch output
    filenames; ``content_digest`` (already computed during source discovery)
    disambiguates same-named files without paying a second hash cost.
    """
    return f"{idx:05d}_{content_digest[:8]}{src.suffix.lower()}"


def _safe_output_basename(src: Path) -> str:
    """Sanitised filesystem-safe form of the full source basename.

    Preserves the source file's name *including its extension* so the
    per-source outputs are unambiguous (``sample.pdf`` → ``sample.pdf.txt``,
    ``eurotext.png`` → ``eurotext.png.txt``). Strips path-hostile chars and
    caps length so we don't blow the FS name limit when a source has a
    huge URL-encoded basename (responses/ plugin filenames routinely run
    200+ chars).
    """
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", src.name).strip("-")[:96]
    return safe or "source"


def _assign_output_basenames(
    sources: list[tuple[Path, str]],
) -> dict[int, str]:
    """Map source idx → unique per-source output basename (incl. extension).

    Output basenames mirror input basenames (e.g. ``sample.pdf`` →
    ``sample.pdf`` so writes become ``sample.pdf.txt`` / ``sample.pdf.json``).
    On collision (two sources sharing a sanitised basename), the runner-up
    gets a ``__<shorthash>`` suffix inserted before the extension, derived
    from the content digest so disambiguation is stable across runs.
    """
    assigned: dict[int, str] = {}
    used: set[str] = set()
    for idx, (src, digest) in enumerate(sources):
        base = _safe_output_basename(src)
        candidate = base
        if candidate in used:
            stem, dot, ext = base.rpartition(".")
            if dot:
                candidate = f"{stem}__{digest[:8]}.{ext}"
            else:
                candidate = f"{base}__{digest[:8]}"
            # Pathological case: even the hash-suffixed name collides
            # (basically only when two sources have identical content AND
            # identical sanitised basename — already deduped upstream, but
            # be defensive).
            suffix = 2
            while candidate in used:
                stem, dot, ext = base.rpartition(".")
                if dot:
                    candidate = f"{stem}__{digest[:8]}_{suffix}.{ext}"
                else:
                    candidate = f"{base}__{digest[:8]}_{suffix}"
                suffix += 1
        assigned[idx] = candidate
        used.add(candidate)
    return assigned


def _read_if_present(path: Path) -> str:
    """Return file contents, or empty string if missing/zero-size."""
    if not path.is_file() or path.stat().st_size == 0:
        return ""
    return path.read_text(errors="ignore")


def _process_batch(
    binary: str,
    batch: list[tuple[int, Path, str]],
    formats: list[str],
    timeout: int,
    args: list[str],
    env: dict[str, str],
) -> list[tuple[int, Path, str, str]]:
    """Run ``lit batch-parse`` once per requested format over one batch.

    Symlinks the batch's sources into a temp input dir, runs lit, then reads
    each per-source output by stem. Returns ``(idx, src, text, json_text)``
    per source.
    """
    results: list[tuple[int, Path, str, str]] = []
    # Scratch dir lives under the plugin output dir so all working state is
    # visible in the snapshot tree; ``TemporaryDirectory.__exit__`` deletes
    # it once the batch returns (success or exception). We also point
    # ``TMPDIR`` at this dir so lit's tesseract-rs internal scratch files
    # (it writes fixed-name ``page_<n>_ocr.png`` to $TMPDIR) land here too —
    # otherwise two concurrent batches collide on the system $TMPDIR.
    with tempfile.TemporaryDirectory(dir=str(OUTPUT_DIR), prefix=".batch_") as tmpdir:
        tmp = Path(tmpdir)
        input_dir = tmp / "in"
        text_out = tmp / "text"
        json_out = tmp / "json"
        for d in (input_dir, text_out, json_out):
            d.mkdir()
        batch_env = {**env, "TMPDIR": str(tmp)}

        link_names: list[str] = []
        for idx, src, digest in batch:
            link_name = _safe_link_name(idx, src, digest)
            (input_dir / link_name).symlink_to(src.resolve())
            link_names.append(link_name)

        for fmt, out_dir in (("text", text_out), ("json", json_out)):
            if fmt not in formats:
                continue
            cmd = [
                binary,
                "batch-parse",
                str(input_dir),
                str(out_dir),
                "--format",
                fmt,
                *args,
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                text=True,
                env=batch_env,
            )
            if result.stderr.strip():
                print(result.stderr, file=sys.stderr, end="")

        for (idx, src, _digest), link_name in zip(batch, link_names, strict=True):
            stem = Path(link_name).stem
            text = (
                _read_if_present(text_out / f"{stem}.txt") if "text" in formats else ""
            )
            json_text = (
                _read_if_present(json_out / f"{stem}.json") if "json" in formats else ""
            )
            results.append((idx, src, text, json_text))

    return results


def extract_liteparse(url: str, binary: str) -> tuple[str, str]:
    """Run lit on every document found in the snapshot dir.

    Returns: (status, output_str)
    """
    config = load_config()
    timeout = config.LITEPARSE_TIMEOUT
    formats = [
        f.lower() for f in config.LITEPARSE_FORMATS if f.lower() in ("text", "json")
    ]
    if not formats:
        return "failed", "LITEPARSE_FORMATS must contain at least one of: text, json"

    base_args = build_lit_args(config)

    env = os.environ.copy()
    language = config.LITEPARSE_OCR_LANGUAGE or "eng"
    tessdata = resolve_tessdata_dir(
        config.LITEPARSE_TESSDATA_DIR,
        config.LITEPARSE_TESSERACT_BINARY,
        language,
    )
    if tessdata:
        env["TESSDATA_PREFIX"] = tessdata
    elif config.LITEPARSE_OCR_ENABLED and not config.LITEPARSE_OCR_SERVER_URL:
        # OCR is configured but no usable tessdata is reachable. Don't bail —
        # native-text PDFs still extract cleanly via PDFium, and the user gets
        # a clear warning rather than a failed snapshot. Image-only or scanned
        # inputs will produce empty results, which propagates naturally.
        print(
            f"[liteparse] WARN: OCR is enabled but no tessdata directory "
            f"containing '{language}.traineddata' was found. Install tesseract "
            f"(brew/apt) or set LITEPARSE_TESSDATA_DIR. Native-text extraction "
            f"will still run.",
            file=sys.stderr,
        )

    min_image_dim = int(config.LITEPARSE_MIN_IMAGE_DIMENSION or 0)
    sources = find_document_sources(min_image_dim=min_image_dim)
    if not sources:
        return "noresults", "No document sources found"

    # Largest-first ordering: most content-rich files get OCR'd before any
    # wall-clock pressure kicks in, and the cumulative content.txt always
    # shows the most important extractions at the top.
    sources.sort(key=lambda entry: -entry[0].stat().st_size)

    max_sources = int(config.LITEPARSE_MAX_SOURCES or 0)
    total_found = len(sources)
    if max_sources > 0 and len(sources) > max_sources:
        print(
            f"[liteparse] Capping at {max_sources} sources "
            f"(found {len(sources)}, set LITEPARSE_MAX_SOURCES=0 for no cap)",
            file=sys.stderr,
        )
        sources = sources[:max_sources]

    print(
        f"[liteparse] Processing {len(sources)} document(s) (of {total_found} found)",
        file=sys.stderr,
    )

    output_dir = Path(OUTPUT_DIR)

    def _wipe_batch_scratch() -> None:
        """Remove every ``.batch_*`` scratch dir under OUTPUT_DIR.

        Called at hook entry (cleans leftovers from a prior killed run),
        on SIGTERM/SIGINT (cleans the in-flight batch dirs before the
        orchestrator escalates to SIGKILL), and via ``atexit`` (covers
        normal exit + uncaught exceptions). Each lit batch creates its
        own ``TemporaryDirectory`` whose ``__exit__`` *also* cleans, so
        these belt-and-suspenders runs are usually no-ops.
        """
        for stale in output_dir.glob(".batch_*"):
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)

    _wipe_batch_scratch()
    atexit.register(_wipe_batch_scratch)

    def _on_signal(signum, _frame):
        _wipe_batch_scratch()
        # Default action for the signal (exit), preserving the conventional
        # 128+N exit code so callers can distinguish kill cause.
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    batch_size = max(1, int(config.LITEPARSE_BATCH_SIZE or 8))
    indexed: list[tuple[int, Path, str]] = [
        (idx, path, digest) for idx, (path, digest) in enumerate(sources)
    ]
    batches: list[list[tuple[int, Path, str]]] = [
        indexed[i : i + batch_size] for i in range(0, len(indexed), batch_size)
    ]

    # Pre-compute the per-source output basename for every source. The
    # basename keeps the source's full input filename including extension
    # (sample.pdf → ``sample.pdf.txt`` / ``sample.pdf.json``) so the
    # original format is always visible in the output filename. Collisions
    # get a content-hash suffix for stable disambiguation.
    output_basenames: dict[int, str] = _assign_output_basenames(sources)

    # Per-source completion bookkeeping (no on-disk merged manifest —
    # search backends auto-discover every .txt in the plugin dir, so
    # we only emit one file per source and nothing else).
    largest_text_output: dict[int, str] = {}
    sources_completed: set[int] = set()
    write_lock = threading.Lock()
    binary_failed = False

    workers = max(1, int(config.LITEPARSE_PARALLEL_WORKERS or 2))

    def _absorb_batch_results(results: list[tuple[int, Path, str, str]]) -> None:
        """Write per-source ``<name>.txt`` / ``<name>.json`` for one batch.

        No cumulative ``content.txt`` / ``content.json`` / ``metadata.json``
        — search backends (ripgrep / sqlite FTS / sonic) auto-discover every
        ``.txt`` file in the plugin dir, so a merged file would just
        duplicate each source's text in the index.
        """
        with write_lock:
            for idx, src, text, json_text in results:
                out_base = output_basenames[idx]
                src_text_path = output_dir / f"{out_base}.txt"
                src_json_path = output_dir / f"{out_base}.json"

                if text:
                    write_text_atomic(
                        src_text_path,
                        f"<!-- source: {src.name} -->\n{text}",
                    )
                    print(f"{PLUGIN_DIR}/{src_text_path.name}", flush=True)
                    # Track the largest-source successful text output so the
                    # hook's final ArchiveResult ``output_str`` can point at
                    # something concrete (lowest idx = largest source).
                    if idx not in largest_text_output or idx < min(largest_text_output):
                        largest_text_output[idx] = src_text_path.name

                if json_text:
                    write_text_atomic(src_json_path, json_text)
                    print(f"{PLUGIN_DIR}/{src_json_path.name}", flush=True)

                if text or json_text:
                    sources_completed.add(idx)
                else:
                    print(
                        f"[liteparse] No content extracted from {src.name}",
                        file=sys.stderr,
                    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_batch = {
            executor.submit(
                _process_batch,
                binary,
                batch,
                formats,
                timeout,
                base_args,
                env,
            ): batch
            for batch in batches
        }
        for future in concurrent.futures.as_completed(future_to_batch):
            batch = future_to_batch[future]
            first_name = batch[0][1].name if batch else "<empty>"
            try:
                results = future.result()
            except subprocess.TimeoutExpired:
                print(
                    f"[liteparse] Batch starting at {first_name} timed out after {timeout}s",
                    file=sys.stderr,
                )
                continue
            except (FileNotFoundError, PermissionError, OSError) as e:
                print(
                    f"[liteparse] Binary execution failed: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                binary_failed = True
                continue
            except Exception as e:
                print(
                    f"[liteparse] Batch starting at {first_name}: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                continue
            _absorb_batch_results(results)

    if binary_failed and not sources_completed:
        return "failed", f"Binary '{binary}' could not be executed"

    if not sources_completed:
        return "noresults", "No content extracted from sources"

    # Point the orchestrator's "output" preview at the largest source's
    # per-source text file (lowest idx = largest after the size-desc sort).
    if largest_text_output:
        primary_idx = min(largest_text_output)
        return "succeeded", f"{PLUGIN_DIR}/{largest_text_output[primary_idx]}"
    return "succeeded", f"{len(sources_completed)} sources extracted"


@click.command(
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--url", required=True, help="URL being archived")
def main(url: str):
    """Extract text from documents using LiteParse."""

    try:
        config = load_config()

        if not config.LITEPARSE_ENABLED:
            print("Skipping liteparse (LITEPARSE_ENABLED=False)", file=sys.stderr)
            emit_archive_result_record("skipped", "LITEPARSE_ENABLED=False")
            sys.exit(0)

        binary = config.LITEPARSE_BINARY

        status, output = extract_liteparse(url, binary)
        if status == "failed":
            print(f"ERROR: {output}", file=sys.stderr)
        emit_archive_result_record(status, output)
        sys.exit(0 if status != "failed" else 1)

    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"ERROR: {error}", file=sys.stderr)
        emit_archive_result_record("failed", error)
        sys.exit(1)


if __name__ == "__main__":
    main()
