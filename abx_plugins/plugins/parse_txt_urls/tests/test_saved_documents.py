"""Run the real URL parser against saved provider documents and text outputs."""

import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__71_parse_txt_urls.py"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/package/2006/relationships"


def run_parser(root, source, depth=0):
    result = subprocess.run(
        [str(HOOK), f"--url={source}", f"--depth={depth}"],
        cwd=root,
        env={**os.environ, "SNAP_DIR": str(root)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    records = [
        json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
    ]
    return result, [row for row in records if row["type"] == "Snapshot"]


def test_explicit_input_file_is_parsed_alongside_saved_text(tmp_path):
    snapshot = tmp_path / "snapshot"
    (snapshot / "title").mkdir(parents=True)
    (snapshot / "title/title.txt").write_text("Saved page title")
    (snapshot / "readability").mkdir()
    (snapshot / "readability/content.txt").write_text("https://example.com/saved")
    source = tmp_path / "bookmarks.txt"
    source.write_text("https://example.com/input\nhttps://example.com/saved")
    _, records = run_parser(snapshot, source.as_uri(), depth=4)
    assert {row["url"] for row in records} == {
        "https://example.com/input",
        "https://example.com/saved",
    }
    assert len(records) == 2
    assert all(row["depth"] == 5 for row in records)


@pytest.mark.parametrize(
    "provider",
    ["googledocs", "googledrive", "dropbox", "future_provider"],
)
@pytest.mark.parametrize("extension", ["docx", "xlsx"])
def test_saved_office_hyperlinks_and_text(tmp_path, provider, extension):
    output = tmp_path / provider
    output.mkdir()
    document = output / f"document.{extension}"
    with zipfile.ZipFile(document, "w", zipfile.ZIP_DEFLATED) as archive:
        if extension == "docx":
            archive.writestr(
                "word/document.xml",
                f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>https://example.com/vis</w:t></w:r><w:r><w:t>ible</w:t></w:r></w:p></w:body></w:document>',
            )
            rels = "word/_rels/document.xml.rels"
        else:
            archive.writestr(
                "xl/sharedStrings.xml",
                f'<sst xmlns="{S}"><si><r><t>https://example.com/vis</t></r><r><t>ible</t></r></si></sst>',
            )
            archive.writestr(
                "xl/worksheets/sheet2.xml",
                f'<worksheet xmlns="{S}"><sheetData><row><c><f>HYPERLINK("https://example.com/formula", "Label")</f></c></row></sheetData></worksheet>',
            )
            rels = "xl/worksheets/_rels/sheet2.xml.rels"
        archive.writestr(
            rels,
            f'<Relationships xmlns="{R}"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.com/What\'s?a=1&amp;b=2" TargetMode="External"/><Relationship Id="r2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="https://example.com/image.png" TargetMode="External"/></Relationships>',
        )
        archive.writestr(
            "docProps/core.xml",
            "<metadata>https://example.com/not-content</metadata>",
        )
    key = "exports" if provider == "googledocs" else "files"
    name = "exports.json" if provider == "googledocs" else "downloads.json"
    (output / name).write_text(json.dumps({key: [{"path": document.name}]}))
    source = tmp_path / "source.txt"
    source.write_text("")
    _, records = run_parser(tmp_path, source.as_uri(), depth=0)
    expected = {"https://example.com/visible", "https://example.com/What's?a=1&b=2"}
    if extension == "xlsx":
        expected.add("https://example.com/formula")
    assert {row["url"] for row in records} == expected
    assert all(
        row["depth"] == 1 and row["plugin"] == "parse_txt_urls" for row in records
    )
    saved = tmp_path / "parse_txt_urls/urls.jsonl"
    assert [
        {**json.loads(line), "id": ""} for line in saved.read_text().splitlines()
    ] == records


def test_generic_text_outputs_and_csv_cells(tmp_path):
    output = tmp_path / "future_text_plugin" / "nested"
    output.mkdir(parents=True)
    (output / "notes.md").write_text("[Link](https://example.com/markdown)\n")
    (output / "book.csv").write_text(
        'URL,Label\nhttps://example.com/cell,description\n"https://example.com/quoted?a=1&b=2",label\n',
    )
    (output / "book.tsv").write_text("https://example.com/tab\tlabel\n")
    (output / "ocr.txt").write_text(
        "https://example.com/ocr\nhttps://example.com/markdown\n",
    )
    source = tmp_path / "source.txt"
    source.write_text("")
    _, records = run_parser(tmp_path, source.as_uri(), depth=3)
    assert {row["url"] for row in records} == {
        "https://example.com/markdown",
        "https://example.com/cell",
        "https://example.com/quoted?a=1&b=2",
        "https://example.com/tab",
        "https://example.com/ocr",
    }
    assert all(row["depth"] == 4 for row in records)


def test_all_saved_outputs_are_scanned_despite_manifests(tmp_path):
    output = tmp_path / "future_provider"
    output.mkdir()
    (output / "good.txt").write_text("https://example.com/good\n")
    (output / "old.txt").write_text("https://example.com/stale\n")
    (output / "broken.docx").write_bytes(b"not a ZIP")
    outside = tmp_path / "outside.txt"
    outside.write_text("https://example.com/outside\n")
    (output / "escape.txt").symlink_to(outside)
    (output / "downloads.json").write_text(
        json.dumps(
            {
                "files": [
                    {"path": "good.txt"},
                    {"path": "broken.docx"},
                    {"path": "../outside.txt"},
                    {"path": "escape.txt"},
                    {"path": str(outside)},
                    None,
                    {"path": 42},
                ],
            },
        ),
    )
    other = tmp_path / "broken_provider"
    other.mkdir()
    (other / "downloads.json").write_text("{")
    (other / "old.txt").write_text("https://example.com/stale\n")
    chrome = tmp_path / "chrome"
    chrome.mkdir()
    (chrome / "cdp_url.txt").write_text("http://localhost:9222\n")
    cache = tmp_path / "text_plugin" / "node_modules"
    cache.mkdir(parents=True)
    (cache / "README.md").write_text("https://example.com/runtime\n")
    source = tmp_path / "source.txt"
    source.write_text("")
    result, records = run_parser(tmp_path, source.as_uri())
    assert {row["url"] for row in records} == {
        "https://example.com/good",
        "https://example.com/stale",
    }
    assert "broken.docx" in result.stderr


@pytest.mark.parametrize("extension", ["odt", "ods", "odp", "pptx", "docx"])
def test_other_document_formats_without_provider_registration(tmp_path, extension):
    output = tmp_path / "new_provider" / "files"
    output.mkdir(parents=True)
    with zipfile.ZipFile(output / f"links.{extension}", "w") as archive:
        if extension in {"odt", "ods", "odp"}:
            archive.writestr(
                "content.xml",
                '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:xlink="http://www.w3.org/1999/xlink">'
                "<office:body><text:p>https://example.com/vis<text:span>ible</text:span></text:p>"
                '<text:p><text:a xlink:href="https://example.com/labeled">Label</text:a></text:p>'
                "</office:body></office:document-content>",
            )
        elif extension == "pptx":
            archive.writestr(
                "ppt/slides/slide1.xml",
                '<a:p xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                "<a:r><a:t>https://example.com/vis</a:t></a:r><a:r><a:t>ible</a:t></a:r></a:p>",
            )
            archive.writestr(
                "ppt/slides/_rels/slide1.xml.rels",
                f'<Relationships xmlns="{R}"><Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
                'Target="https://example.com/labeled" TargetMode="External"/></Relationships>',
            )
        else:
            archive.writestr(
                "word/document.xml",
                f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>https://example.com/visible</w:t><w:tab/><w:t>https://example.com/labeled</w:t></w:r></w:p>'
                "<w:p><w:fldSimple w:instr='HYPERLINK \"https://example.com/labeled\"'><w:r><w:t>Label</w:t></w:r></w:fldSimple></w:p>"
                '<w:p><w:r><w:instrText>HYPER</w:instrText></w:r><w:r><w:instrText>LINK "https://example.com/labeled"</w:instrText></w:r></w:p>'
                "</w:body></w:document>",
            )
    source = tmp_path / "source.txt"
    source.write_text("")
    _, records = run_parser(tmp_path, source.as_uri())
    assert {row["url"] for row in records} == {
        "https://example.com/visible",
        "https://example.com/labeled",
    }


def test_empty_manifest_does_not_hide_saved_files(tmp_path):
    output = tmp_path / "provider"
    output.mkdir()
    (output / "downloads.json").write_text('{"files": []}')
    (output / "old.txt").write_text("https://example.com/old\n")
    parsed = tmp_path / "parse_txt_urls"
    parsed.mkdir()
    (parsed / "urls.jsonl").write_text(
        '{"type":"Snapshot","url":"https://example.com/old"}\n',
    )
    source = tmp_path / "source.txt"
    source.write_text("")
    _, records = run_parser(tmp_path, source.as_uri())
    assert {row["url"] for row in records} == {"https://example.com/old"}
    assert (parsed / "urls.jsonl").is_file()

    (output / "old.txt").write_text("No links left in this document.\n")
    result, records = run_parser(tmp_path, source.as_uri())
    assert records == []
    assert '"status": "noresults"' in result.stdout
    assert not (parsed / "urls.jsonl").exists()
