"""Live exports of public Google documents; no OAuth setup.

Sources: developers.google.com/workspace/{docs,sheets,slides}/api/quickstart
Drawing: github.com/evbacher/gd2md-html/wiki/Embedding-Google-drawings-by-reference
"""

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from urllib.parse import urlsplit

from abx_plugins.plugins.base.testing import (
    parse_jsonl_output,
    start_process_and_wait_for_file,
    wait_for_file,
)
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_session,
    fetch_devtools_targets,
)


HOOK = Path(__file__).resolve().parents[1] / "on_Snapshot__53_googledocs.js"


@pytest.mark.parametrize(
    ("kind", "document_id", "formats", "office_file", "office_member"),
    [
        (
            "document",
            "195j9eDD3ccgjQRttHhJPymLJUCOUjs-jmwTrekvdjFE",
            {"docx", "pdf", "odt", "rtf", "txt", "md", "zip", "epub"},
            "docx",
            "word/document.xml",
        ),
        (
            "spreadsheets",
            "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
            {"xlsx", "csv", "pdf", "ods", "tsv", "zip"},
            "xlsx",
            "xl/workbook.xml",
        ),
        (
            "presentation",
            "1EAYk18WDjIG-zp_0vLm3CsfQh_i8eXc67Jo2O9C6Vuc",
            {"pptx", "pdf", "odp", "txt"},
            "pptx",
            "ppt/presentation.xml",
        ),
        (
            "drawings",
            "1mUK8f8Hhlp_o06GPL_QxpPUL5s_vxv942a5NOn3aqE0",
            {"svg", "pdf", "png", "jpg"},
            None,
            None,
        ),
    ],
)
def test_live_google_exports(
    tmp_path,
    ensure_chrome_test_prereqs,
    kind,
    document_id,
    formats,
    office_file,
    office_member,
):
    from abx_plugins.plugins.googledocs.tests.test_googledocs import node, CHROME_UTILS

    # Google can add a selected-slide fragment with history.replaceState after
    # DOMContentLoaded. Assert document lifetime as well as target identity.
    inspect_document = """
        const {connectToPage} = require(process.argv[1]);
        (async () => {
            const {browser, page} = await connectToPage({chromeSessionDir: process.argv[2], waitForNavigationComplete: true});
            try { console.log(JSON.stringify(await page.evaluate(() => performance.timeOrigin))); }
            finally { await browser.disconnect(); }
        })().catch(e => { console.error(e); process.exitCode = 1; });
    """
    url = f"https://docs.google.com/{kind}/d/{document_id}/edit"
    with chrome_session(tmp_path, test_url=url, timeout=60) as (_, _, chrome_dir, env):
        endpoint = (chrome_dir / "cdp_url.txt").read_text().strip()
        before = fetch_devtools_targets(endpoint)
        time_origin = node(inspect_document, env, CHROME_UTILS, chrome_dir)
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
            cwd=chrome_dir.parent,
            env={
                **env,
                "GOOGLEDOCS_TIMEOUT": "120",
                "GOOGLEDOCS_FORMATS": json.dumps(sorted(formats)),
            },
            capture_output=True,
            text=True,
            timeout=150,
        )
        assert result.returncode == 0, result.stderr
        record = parse_jsonl_output(result.stdout)
        assert record is not None
        assert record["status"] == "succeeded"
        after = fetch_devtools_targets(endpoint)
        before_pages = {
            item["id"]: item["url"] for item in before if item["type"] == "page"
        }
        after_pages = {
            item["id"]: item["url"] for item in after if item["type"] == "page"
        }
        assert after_pages.keys() == before_pages.keys()
        assert {key: urlsplit(value).path for key, value in after_pages.items()} == {
            key: urlsplit(value).path for key, value in before_pages.items()
        }
        assert node(inspect_document, env, CHROME_UTILS, chrome_dir) == time_origin
        output = chrome_dir.parent / "googledocs"
        manifest = json.loads((output / "exports.json").read_text())
        if kind == "spreadsheets":
            previous = (output / "sheet-0.csv").read_bytes()
            denied = subprocess.run(
                [str(HOOK), f"--url={url}#gid=999999999999"],
                cwd=chrome_dir.parent,
                env={**env, "GOOGLEDOCS_FORMATS": '["csv"]'},
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert denied.returncode == 1, denied.stderr
            failed = parse_jsonl_output(denied.stdout)
            assert failed is not None and failed["status"] == "failed"
            assert (output / "sheet-0.csv").read_bytes() == previous
            failure_manifest = json.loads((output / "exports.json").read_text())
            assert failure_manifest["exports"] == []
            assert failure_manifest["errors"][0]["format"] == "csv"
            assert list(output.glob(".*.tmp")) == []
    assert manifest["errors"] == []
    assert {item["format"] for item in manifest["exports"]} == formats
    for item in manifest["exports"]:
        data = (output / item["path"]).read_bytes()
        assert len(data) == item["size"] > 0
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
    if office_file:
        with zipfile.ZipFile(output / f"document.{office_file}") as archive:
            assert archive.testzip() is None
            assert office_member in archive.namelist()
    if "zip" in formats:
        with zipfile.ZipFile(output / "document.zip") as archive:
            assert archive.testzip() is None
            assert any(name.endswith(".html") for name in archive.namelist())
    if kind == "drawings":
        import xml.etree.ElementTree as ET

        assert (
            ET.parse(output / "document.svg").getroot().tag
            == "{http://www.w3.org/2000/svg}svg"
        )
    assert (output / "document.pdf").read_bytes().startswith(b"%PDF-")
    if kind == "spreadsheets":
        assert "Student Name,Gender,Class Level" in (output / "sheet-0.csv").read_text()


def test_reuses_real_captured_export(tmp_path, ensure_chrome_test_prereqs):
    from abx_plugins.plugins.googledocs.tests.test_googledocs import node, CHROME_UTILS

    url = "https://docs.google.com/document/d/195j9eDD3ccgjQRttHhJPymLJUCOUjs-jmwTrekvdjFE/edit"
    export_url = url.removesuffix("edit") + "export?format=txt"
    with chrome_session(tmp_path, test_url=url, timeout=60) as (_, _, chrome_dir, env):
        snap_dir = chrome_dir.parent
        index = snap_dir / "responses" / "index.jsonl"
        responses_hook = next(
            (HOOK.parent.parent / "responses").glob("on_Snapshot__*.js"),
        )
        capture = start_process_and_wait_for_file(
            [str(responses_hook), f"--url={url}"],
            index,
            cwd=snap_dir,
            env=env,
        )
        try:
            response = node(
                """
                const {connectToPage} = require(process.argv[1]);
                (async () => {
                    const {browser, page, cdpSession} = await connectToPage({chromeSessionDir: process.argv[2], waitForNavigationComplete: true});
                    try {
                        const result = await page.evaluate(async url => { const r = await fetch(url); return {status: r.status, bytes: Array.from(new Uint8Array(await r.arrayBuffer()))}; }, process.argv[3]);
                        console.log(JSON.stringify(result));
                    } finally { await browser.disconnect(); }
                })().catch(e => { console.error(e); process.exitCode = 1; });
                """,
                env,
                CHROME_UTILS,
                chrome_dir,
                export_url,
            )
            assert response["status"] == 200
            wait_for_file(
                index,
                process=capture,
                ready=lambda file: export_url in file.read_text(),
            )
        finally:
            capture.terminate()
            capture.communicate(timeout=30)
        # An interrupted optional capture must not break valid preceding records.
        with index.open("a") as stream:
            stream.write('{"incomplete":')

        captured = next(
            json.loads(line)
            for line in index.read_text().splitlines()
            if export_url in line
        )
        captured_bytes = (index.parent / captured["path"]).read_bytes()
        network_bytes = bytes(response["bytes"])
        # CDP's text response API decodes the optional UTF-8 BOM; streaming
        # downloads retain it. Reuse must preserve the actual captured bytes.
        assert captured_bytes.decode("utf-8-sig") == network_bytes.decode("utf-8-sig")

        def export(expected_bytes):
            result = subprocess.run(
                [str(HOOK), f"--url={url}"],
                cwd=snap_dir,
                env={**env, "GOOGLEDOCS_FORMATS": '["txt"]'},
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert result.returncode == 0, result.stderr
            record = parse_jsonl_output(result.stdout)
            assert record is not None
            assert record["status"] == "succeeded"
            manifest = json.loads((snap_dir / "googledocs/exports.json").read_text())
            assert len(manifest["exports"]) == 1
            assert manifest["errors"] == []
            assert (snap_dir / "googledocs/document.txt").read_bytes() == expected_bytes
            assert (
                manifest["exports"][0]["sha256"]
                == hashlib.sha256(expected_bytes).hexdigest()
            )
            return manifest["exports"][0]

        assert export(captured_bytes)["reused_response"] is True
        # A real captured response with modified bytes cannot be trusted, even
        # when the signature and recorded request URL still match.
        (index.parent / captured["path"]).write_text("corrupted capture")
        assert export(network_bytes)["reused_response"] is False
        index.unlink()
        assert export(network_bytes)["reused_response"] is False
        assert list((snap_dir / "googledocs").glob(".*.tmp")) == []


def test_live_multiple_sheets(tmp_path, ensure_chrome_test_prereqs):
    """Public workbook linked by github.com/benborgers/opensheet."""
    import csv
    import xml.etree.ElementTree as ET

    url = "https://docs.google.com/spreadsheets/d/1o5t26He2DzTweYeleXOGiDjlU4Jkx896f95VUHVgS8U/edit#gid=211973040"
    with chrome_session(tmp_path, test_url=url, timeout=60) as (_, _, chrome_dir, env):
        result = subprocess.run(
            [str(HOOK), f"--url={url}"],
            cwd=chrome_dir.parent,
            env={**env, "GOOGLEDOCS_FORMATS": '["xlsx","csv","tsv","pdf"]'},
            capture_output=True,
            text=True,
            timeout=150,
        )
        assert result.returncode == 0, result.stderr
        output = chrome_dir.parent / "googledocs"
        manifest = json.loads((output / "exports.json").read_text())
        assert manifest["sheets"] == [
            {"id": "0", "name": "Test Sheet"},
            {"id": "211973040", "name": "this/that"},
        ]
        assert manifest["selected_sheet"] == "211973040"
        assert len(manifest["exports"]) == 6
        assert manifest["errors"] == []
        with zipfile.ZipFile(output / "document.xlsx") as workbook:
            names = [
                e.attrib["name"]
                for e in ET.fromstring(workbook.read("xl/workbook.xml")).iter(
                    "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet",
                )
            ]
        # Google removes characters Excel disallows in worksheet names.
        assert names == ["Test Sheet", "thisthat"]
        tables = []
        for sheet in manifest["sheets"]:
            csv_file = next(
                e
                for e in manifest["exports"]
                if e["format"] == "csv" and e["sheet_id"] == sheet["id"]
            )
            tsv_file = next(
                e
                for e in manifest["exports"]
                if e["format"] == "tsv" and e["sheet_id"] == sheet["id"]
            )
            assert csv_file["sheet_name"] == tsv_file["sheet_name"] == sheet["name"]
            assert csv_file["path"] == f"sheet-{sheet['id']}.csv"
            with (output / csv_file["path"]).open(
                encoding="utf-8-sig",
                newline="",
            ) as stream:
                rows = list(csv.reader(stream))
            with (output / tsv_file["path"]).open(
                encoding="utf-8-sig",
                newline="",
            ) as stream:
                assert list(csv.reader(stream, delimiter="\t")) == rows
            assert rows
            tables.append(rows)
        assert tables[0] != tables[1]
