"""Discover saved content by format, independently of the producing plugin."""

import csv
import os
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv"}
OFFICE_EXTENSIONS = {
    ".docx",
    ".docm",
    ".xlsx",
    ".xlsm",
    ".pptx",
    ".pptm",
    ".odt",
    ".ods",
    ".odp",
}
SKIP_DIRS = {"chrome", "__pycache__", "node_modules", "site-packages"}
MAX_XML_BYTES = 64 * 1024 * 1024
MAX_DOCUMENT_XML_BYTES = 256 * 1024 * 1024
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
T = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
XLINK = "{http://www.w3.org/1999/xlink}href"
HYPERLINK = re.compile(r'\bHYPERLINK\s*\(\s*"((?:[^"]|"")*)"', re.IGNORECASE)
WORD_HYPERLINK = re.compile(r'\bHYPERLINK\s+"([^"]+)"', re.IGNORECASE)


def skip_directory(name: str) -> bool:
    return (
        name.startswith((".", "parse_", "search_backend_"))
        or name in SKIP_DIRS
        or name.endswith(("_env", ".dist-info", ".egg-info"))
    )


def iter_document_sources(snap_dir: Path):
    """Discover all saved text/document files, independently of export manifests."""
    seen = set()
    for plugin_dir in sorted(snap_dir.iterdir()):
        if (
            not plugin_dir.is_dir()
            or plugin_dir.is_symlink()
            or skip_directory(plugin_dir.name)
        ):
            continue
        candidates = []
        for directory, dirs, files in os.walk(plugin_dir, followlinks=False):
            dirs[:] = sorted(name for name in dirs if not skip_directory(name))
            candidates.extend(
                Path(directory) / name
                for name in sorted(files)
                if not name.startswith(".")
            )
        for path in candidates:
            if path.suffix.lower() not in TEXT_EXTENSIONS | OFFICE_EXTENSIONS:
                continue
            resolved = path.resolve()
            if resolved in seen or not resolved.is_relative_to(plugin_dir.resolve()):
                continue
            if not resolved.is_file():
                continue
            seen.add(resolved)
            yield resolved


def iter_document_content(path: Path):
    """Yield (kind, value) for text or exact hyperlink targets, never XML metadata.

    Office archives are read in place. Paragraphs/cells join formatting runs so
    URLs split across runs survive. Only hyperlink relationships are followed;
    namespace, template, media and other package URLs are not crawl links.
    """
    extension = path.suffix.lower()
    if extension in {".csv", ".tsv"}:
        with path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
            for row in csv.reader(
                stream, delimiter="\t" if extension == ".tsv" else ","
            ):
                for cell in row:
                    yield "text", cell
        return
    with zipfile.ZipFile(path) as archive:
        total = 0
        for entry in archive.infolist():
            name = entry.filename
            if not (
                name == "content.xml"
                or name.startswith(("word/", "xl/", "ppt/"))
                and name.endswith((".xml", ".rels"))
            ):
                continue
            total += entry.file_size
            if entry.file_size > MAX_XML_BYTES or total > MAX_DOCUMENT_XML_BYTES:
                raise ValueError("Document XML exceeds parsing size limit")
            with archive.open(entry) as stream:
                root = ET.parse(stream).getroot()
            if name.endswith(".rels"):
                for relation in root:
                    if (
                        relation.get("Type", "").endswith("/hyperlink")
                        and relation.get("TargetMode") == "External"
                    ):
                        yield "url", relation.get("Target", "")
                continue
            for element in root.iter():
                if element.tag in {W + "p", A + "p", S + "si", S + "is"}:
                    yield (
                        "text",
                        "".join(
                            "\n"
                            if child.tag in {W + "tab", W + "br", A + "br"}
                            else child.text or ""
                            for child in element.iter()
                            if child.tag
                            in {
                                W + "t",
                                A + "t",
                                S + "t",
                                W + "tab",
                                W + "br",
                                A + "br",
                            }
                        ),
                    )
                elif element.tag in {T + "p", T + "h"}:
                    yield "text", "".join(element.itertext())
                elif element.tag == S + "c" and element.get("t") == "str":
                    yield "text", element.findtext(S + "v", "")
                elif element.tag == S + "f":
                    for match in HYPERLINK.finditer(element.text or ""):
                        yield "url", match.group(1).replace('""', '"')
                if element.tag == W + "p":
                    instruction = "".join(
                        child.text or "" for child in element.iter(W + "instrText")
                    )
                    for match in WORD_HYPERLINK.finditer(instruction):
                        yield "url", match.group(1)
                elif element.tag == W + "fldSimple":
                    for match in WORD_HYPERLINK.finditer(element.get(W + "instr", "")):
                        yield "url", match.group(1)
                if element.tag.startswith(T) and element.get(XLINK):
                    yield "url", element.attrib[XLINK]
                # OpenDocument spreadsheet HYPERLINK formulas are attributes.
                for key, value in element.attrib.items():
                    if (
                        key
                        == "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}formula"
                    ):
                        for match in HYPERLINK.finditer(value):
                            yield "url", match.group(1).replace('""', '"')
