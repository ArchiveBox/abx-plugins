#!/usr/bin/env -S abxpkg run --script --deps-from=./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///
"""
Extract text from PDFs, Office documents, and images using LiteParse
(the ``lit`` CLI by LlamaIndex, v2+).

Scans the snapshot directory for downloaded documents produced by other plugins
(``responses``, ``staticfile``, ``wget``, ``papersdl``, ``googledocs``,
``googledrive``, ``dropbox``) and runs ``lit batch-parse``
on each supported file. Each source produces one ``<source-stem>.txt`` and
``<source-stem>.json`` directly in the plugin output dir — no merged
``content.txt`` or manifest. Search backends (ripgrep / sqlite FTS / sonic)
auto-discover every ``.txt`` here, and the flat layout means each source's
text is indexed exactly once.

ArchiveBox's ``pdf/`` extractor output is intentionally not treated as input:
those files are browser-printed captures of already-archived webpages, not
source documents. Feeding them back into LiteParse duplicates existing HTML
text extraction and makes all-plugin crawls spend minutes parsing generated
PDFs for ordinary webpages.

Disposable batch outputs, OCR rasters, and embedded-image staging use local
``TMP_DIR`` (or the host temporary directory). Only completed extracted outputs
are written to the snapshot archive. Each invocation and batch is isolated.

Tiny images (favicons, sprite thumbnails, etc.) are filtered out by
``LITEPARSE_MIN_IMAGE_DIMENSION`` so we don't waste OCR time on them.

Usage: on_Snapshot__68_liteparse.py --url=<url> > events.jsonl

Environment variables: see config.json (LITEPARSE_* settings).
"""

import sys
import atexit
import concurrent.futures
import hashlib
import json
import fnmatch
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
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
CONFIG_PATH = Path(__file__).resolve().with_name("config.json")
CONFIG = load_config(CONFIG_PATH, hydrate_binaries=False)
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
    ".avif",
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
    include_images: bool = True,
    archive_dir: Path | None = None,
) -> list[tuple[Path, str]]:
    """Find documents produced by upstream plugins that LiteParse can parse.

    Looks for LiteParse-supported file types under responses/, staticfile/, wget/, and
    papersdl/ trees (where downloaded documents land regardless of MIME). Filters:
      - resolved-path dedup (drops symlink duplicates)
      - content-hash dedup (drops same-bytes-different-paths duplicates)
      - image dimension filter (drops favicons/sprites/tracking pixels)

    Images are only useful when OCR is enabled; when OCR is disabled we skip
    them during source discovery instead of handing image-only inputs to
    LiteParse just to produce empty output slowly.

    Returns ``(path, content_digest)`` so callers can re-use the digest for
    batch symlink naming without paying a second hash cost.
    """
    document_roots = (
        "responses",
        "*_responses",
        "staticfile",
        "*_staticfile",
        "wget",
        "*_wget",
        "papersdl",
        "googledocs",
        "googledrive",
        "dropbox",
        "liteparse_input",
    )

    archive_extensions = {
        ".zip",
        ".docx",
        ".docm",
        ".xlsx",
        ".xlsm",
        ".pptx",
        ".pptm",
        ".odt",
        ".ods",
        ".odp",
        ".epub",
    }
    found: list[tuple[Path, str]] = []
    seen_paths: set[str] = set()
    seen_digests: dict[str, Path] = {}
    seen_archives: set[Path] = set()
    archive_bytes = 0
    warned_limit = False

    def consider(
        match: Path,
        source_stat: os.stat_result | None = None,
        resolved_path: Path | None = None,
    ) -> None:
        # Discovered sources pass their checked target and metadata so hashing
        # does not repeat archive I/O. The default is only for images we just
        # extracted ourselves into scratch storage, which may be outside SNAP_DIR.
        # is_file() suppresses filesystem errors on Python 3.14. A failed
        # source read is a failed capture, not evidence that no source exists.
        source_stat = source_stat or match.stat()
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size == 0:
            return
        resolved_path = resolved_path or match
        resolved = str(resolved_path)
        if resolved in seen_paths:
            return
        suffix = match.suffix.lower()
        if suffix in IMAGE_EXTENSIONS and not include_images:
            return
        if suffix in IMAGE_EXTENSIONS and _image_is_too_small(
            resolved_path,
            min_image_dim,
        ):
            return
        digest = _content_digest(resolved_path)
        if digest in seen_digests:
            return
        seen_digests[digest] = match
        seen_paths.add(resolved)
        found.append((match, digest))

    # Only sibling producer directories are inputs. Enumerate the snapshot
    # once instead of probing absent roots inside this parser's own output.
    with os.scandir(SNAP_DIR) as entries:
        root_entries = list(entries)
    for root in document_roots:
        for entry in root_entries:
            if not fnmatch.fnmatchcase(entry.name, root) or not entry.is_dir():
                continue
            root_dir = Path(entry.path)
            # Producer directories may themselves alias another plugin's
            # outputs. Permit that within the snapshot, but never traverse an
            # escaping alias or private browser state to discover documents.
            resolved_root = root_dir.resolve() if entry.is_symlink() else root_dir
            if not resolved_root.is_relative_to(SNAP_DIR) or any(
                part.casefold() == ".persona"
                for part in resolved_root.relative_to(SNAP_DIR).parts
            ):
                continue
            for match in root_dir.rglob("*"):
                suffix = match.suffix.lower()
                embedded_images = (
                    include_images
                    and archive_dir is not None
                    and suffix in archive_extensions
                )
                if suffix not in SUPPORTED_EXTENSIONS and not embedded_images:
                    continue
                # Validate both ordinary documents and archives before any
                # hashing, parsing, or embedded-image extraction.
                # Otherwise an escaping archive can copy private image bytes
                # into ordinary OCR outputs that no longer look like symlinks.
                # rglob does not recurse into directory symlinks. Reuse one
                # lstat for normal files, resolving only actual file links.
                # Enabling recursive symlink traversal would invalidate this
                # fast path; full resolution repeats costly ancestor lookups.
                resolved = resolved_root / match.relative_to(root_dir)
                if any(
                    part.casefold() == ".persona"
                    for part in resolved.relative_to(SNAP_DIR).parts
                ):
                    continue
                source_stat = resolved.lstat()
                if stat.S_ISLNK(source_stat.st_mode):
                    resolved = resolved.resolve()
                    if not resolved.is_relative_to(SNAP_DIR) or any(
                        part.casefold() == ".persona"
                        for part in resolved.relative_to(SNAP_DIR).parts
                    ):
                        continue
                    source_stat = resolved.stat()
                if suffix in SUPPORTED_EXTENSIONS:
                    consider(match, source_stat, resolved)
                # Office/HTML exports may contain images even when the
                # document also has native text. OCR those images once,
                # deduplicated against the other saved representations.
                if (
                    not embedded_images
                    or archive_dir is None
                    or not stat.S_ISREG(source_stat.st_mode)
                ):
                    continue
                if resolved in seen_archives:
                    continue
                seen_archives.add(resolved)
                try:
                    with zipfile.ZipFile(resolved) as archive:
                        for entry in archive.infolist():
                            extension = Path(entry.filename).suffix.lower()
                            if entry.is_dir() or extension not in IMAGE_EXTENSIONS:
                                continue
                            # Bound temporary OCR input expansion; read one
                            # member at a time without loading its archive.
                            if (
                                entry.file_size > 256 * 1024 * 1024
                                or archive_bytes + entry.file_size > 1024 * 1024 * 1024
                            ):
                                if not warned_limit:
                                    print(
                                        "[liteparse] Skipping embedded images exceeding OCR expansion limits",
                                        file=sys.stderr,
                                    )
                                    warned_limit = True
                                continue
                            identity = hashlib.sha256(
                                f"{resolved}\0{entry.header_offset}\0{entry.filename}".encode(),
                            ).hexdigest()[:16]
                            image = (
                                archive_dir / f"{match.stem[:60]}-{identity}{extension}"
                            )
                            try:
                                with (
                                    image.open("wb") as target,
                                    archive.open(entry) as source,
                                ):
                                    shutil.copyfileobj(
                                        source,
                                        target,
                                        length=1024 * 1024,
                                    )
                            except (
                                zipfile.BadZipFile,
                                RuntimeError,
                                ValueError,
                            ) as error:
                                image.unlink(missing_ok=True)
                                print(
                                    f"[liteparse] Cannot read embedded image {match.name}/{entry.filename}: {error}",
                                    file=sys.stderr,
                                )
                                continue
                            archive_bytes += entry.file_size
                            consider(image)
                except (
                    zipfile.BadZipFile,
                    RuntimeError,
                    ValueError,
                ) as error:
                    print(
                        f"[liteparse] Cannot inspect embedded images in {match.name}: {error}",
                        file=sys.stderr,
                    )

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
    scratch_dir: Path,
    imagemagick_binary: str,
    hook_deadline: float,
) -> tuple[list[tuple[int, Path, str, str]], list[str]]:
    """Run ``lit batch-parse`` once for an isolated source.

    Symlinks the batch's sources into a temp input dir, runs lit, then reads
    each per-source output by stem. Returns ``(idx, src, text, json_text)``
    per source, plus parser failures. Read outputs before deleting scratch even
    when parsing fails or times out, so completed work is retained.
    """
    results: list[tuple[int, Path, str, str]] = []
    errors: list[str] = []
    deadline = min(hook_deadline, time.monotonic() + timeout)
    if time.monotonic() >= deadline:
        return [], ["Hook deadline reached before parsing queued document"]
    # Disposable parser output and OCR rasterization belong on local runtime
    # storage, not the snapshot's potentially remote archive filesystem.
    # Each batch has its own TMPDIR because lit uses fixed page_<n>_ocr names.
    with tempfile.TemporaryDirectory(dir=scratch_dir, prefix=".batch_") as tmpdir:
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
            # Servers commonly return AVIF bytes behind a .jpg URL. LiteParse's
            # native decoder rejects those bytes even with the correct suffix.
            # Normalize only the disposable OCR input, preserving the archive.
            with src.open("rb") as source:
                header = source.read(32)
            if header[4:8] == b"ftyp" and b"avif" in header[8:]:
                link_name = str(Path(link_name).with_suffix(".png"))
                converted_dir = tmp / f"convert_{idx}"
                converted_dir.mkdir()
                converted = converted_dir / link_name
                try:
                    conversion = subprocess.run(
                        [
                            imagemagick_binary,
                            str(src.resolve()),
                            # These lossless PNGs are disposable OCR inputs.
                            # Avoid spending the parsing deadline compressing them.
                            "-define",
                            "png:compression-level=1",
                            str(converted),
                        ],
                        capture_output=True,
                        text=True,
                        env=batch_env,
                        timeout=max(0.001, deadline - time.monotonic()),
                    )
                    # Multi-frame conversions produce numbered files. Never
                    # silently OCR only one frame and claim complete extraction.
                    if conversion.returncode or not converted.is_file():
                        errors.append(
                            f"{src.name}: AVIF conversion failed or produced multiple frames: "
                            f"{conversion.stderr.strip()}",
                        )
                    else:
                        converted.rename(input_dir / link_name)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    errors.append(f"{src.name}: AVIF conversion failed: {exc}")
            else:
                (input_dir / link_name).symlink_to(src.resolve())
            link_names.append(link_name)

        for fmt, out_dir in (("text", text_out), ("json", json_out)):
            if fmt not in formats or (fmt == "text" and "json" in formats):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append(f"batch timed out after {timeout}s")
                break
            cmd = [
                binary,
                "batch-parse",
                str(input_dir),
                str(out_dir),
                "--format",
                fmt,
                *args,
            ]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=remaining,
                    text=True,
                    env=batch_env,
                )
                if result.stderr.strip():
                    print(result.stderr, file=sys.stderr, end="")
                if result.returncode:
                    errors.append(f"{fmt} parser exited with code {result.returncode}")
            except subprocess.TimeoutExpired as exc:
                if exc.stderr:
                    stderr = (
                        exc.stderr.decode(errors="replace")
                        if isinstance(exc.stderr, bytes)
                        else exc.stderr
                    )
                    print(stderr, file=sys.stderr, end="")
                errors.append(
                    f"{fmt} parser timed out (document limit {timeout}s; hook deadline enforced)",
                )
                break

        for (idx, src, _digest), link_name in zip(batch, link_names, strict=True):
            stem = Path(link_name).stem
            text = (
                _read_if_present(text_out / f"{stem}.txt") if "text" in formats else ""
            )
            json_text = (
                _read_if_present(json_out / f"{stem}.json") if "json" in formats else ""
            )
            if "text" in formats and "json" in formats and json_text:
                try:
                    pages = json.loads(json_text)["pages"]
                    if not isinstance(pages, list) or any(
                        not isinstance(page["text"], str)
                        or type(page["page"]) is not int
                        or page["page"] < 1
                        for page in pages
                    ):
                        raise ValueError("Invalid LiteParse page number or text")
                    # Exact upstream cli-text.ts serializer: JSON retains the
                    # same page text, so producing both formats need not OCR twice.
                    text = "\n\n".join(
                        f"\n--- Page {page['page']} ---\n{page['text']}"
                        for page in pages
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    errors.append(f"{src.name}: Invalid LiteParse JSON: {exc}")
            results.append((idx, src, text, json_text))

    return results, errors


def extract_liteparse(url: str, scratch_dir: Path) -> tuple[str, str]:
    """Run lit on every document found in the snapshot dir.

    Returns: (status, output_str)
    """
    started_at = time.monotonic()
    config = load_config(CONFIG_PATH, hydrate_binaries=False)
    timeout = config.LITEPARSE_TIMEOUT
    formats = [
        f.lower() for f in config.LITEPARSE_FORMATS if f.lower() in ("text", "json")
    ]
    if not formats:
        return "failed", "LITEPARSE_FORMATS must contain at least one of: text, json"

    archive_dir = scratch_dir / "images"
    archive_dir.mkdir()
    min_image_dim = int(config.LITEPARSE_MIN_IMAGE_DIMENSION or 0)
    sources = find_document_sources(
        min_image_dim=min_image_dim,
        include_images=config.LITEPARSE_OCR_ENABLED,
        archive_dir=archive_dir,
    )
    if not sources:
        return "noresults", "No document sources found"

    binary = str(config.LITEPARSE_BINARY)
    base_args = build_lit_args(config)

    env = os.environ.copy()
    language = config.LITEPARSE_OCR_LANGUAGE or "eng"
    if config.LITEPARSE_OCR_ENABLED:
        tesseract_binary = str(config.LITEPARSE_TESSERACT_BINARY)
        tessdata = resolve_tessdata_dir(
            config.LITEPARSE_TESSDATA_DIR,
            tesseract_binary,
            language,
        )
    else:
        tessdata = None
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
        """Remove only this invocation's local batch scratch directories.

        Called on SIGTERM/SIGINT (cleans the in-flight batch dirs before the
        orchestrator escalates to SIGKILL), and via ``atexit`` (covers
        normal exit + uncaught exceptions). Each lit batch creates its
        own ``TemporaryDirectory`` whose ``__exit__`` *also* cleans, so
        these belt-and-suspenders runs are usually no-ops.
        """
        for stale in scratch_dir.glob(".batch_*"):
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)

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
    batch_count = (len(indexed) + batch_size - 1) // batch_size
    # Native batch-parse is sequential: one stalled document prevents every
    # later input in that process from starting. Isolate documents while
    # retaining the configured budget groups and source indexes.
    batches = [[item] for item in indexed]

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
    batch_errors: list[str] = []

    workers = max(1, int(config.LITEPARSE_PARALLEL_WORKERS or 2))
    batch_waves = max(1, (batch_count + workers - 1) // workers)
    hook_cleanup_budget = max(1, int(timeout * 0.1))
    batch_timeout = max(1, int((timeout - hook_cleanup_budget) / batch_waves))
    hook_deadline = started_at + timeout - hook_cleanup_budget

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
                batch_timeout,
                base_args,
                env,
                scratch_dir,
                str(config.LITEPARSE_IMAGEMAGICK_BINARY),
                hook_deadline,
            ): batch
            for batch in batches
        }
        for future in concurrent.futures.as_completed(future_to_batch):
            batch = future_to_batch[future]
            first_name = batch[0][1].name if batch else "<empty>"
            try:
                results, errors = future.result()
            except (FileNotFoundError, PermissionError, OSError) as e:
                print(
                    f"[liteparse] Binary execution failed: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                batch_errors.append(f"Binary execution failed: {type(e).__name__}: {e}")
                continue
            except Exception as e:
                batch_errors.append(f"{type(e).__name__}: {e}")
                print(
                    f"[liteparse] Batch starting at {first_name}: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                continue
            _absorb_batch_results(results)
            for error in errors:
                batch_errors.append(error)
                print(
                    f"[liteparse] Batch starting at {first_name}: {error}",
                    file=sys.stderr,
                )

    if batch_errors:
        return "failed", (
            f"{len(batch_errors)} batch error(s), {len(sources_completed)} sources retained: "
            f"{batch_errors[0]}"
        )

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
        config = load_config(CONFIG_PATH, hydrate_binaries=False)

        if not config.LITEPARSE_ENABLED:
            print("Skipping liteparse (LITEPARSE_ENABLED=False)", file=sys.stderr)
            emit_archive_result_record("skipped", "LITEPARSE_ENABLED=False")
            sys.exit(0)

        print("LiteParse extraction started", flush=True)
        runtime_tmp = Path(config.TMP_DIR).expanduser() if config.TMP_DIR else None
        if runtime_tmp is not None:
            runtime_tmp.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="abx-liteparse-",
            dir=runtime_tmp,
        ) as scratch_dir:
            status, output = extract_liteparse(url, Path(scratch_dir))
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
