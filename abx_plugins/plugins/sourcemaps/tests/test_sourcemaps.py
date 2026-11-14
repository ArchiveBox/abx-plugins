"""Exercise source map capture through the actual Chrome and abx-dl pipeline."""
import base64
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]


def test_captures_external_and_inline_source_maps(tmp_path, httpserver):
    external = json.dumps({"version": 3, "sources": ["src/hello.ts"], "names": [], "mappings": "AAAA"}).encode()
    inline = json.dumps({"version": 3, "sources": ["src/inline.ts"], "names": [], "mappings": "AAAA"}).encode()
    data = base64.b64encode(inline).decode()
    httpserver.expect_request("/").respond_with_data(
        '<!doctype html><title>Source maps</title><script src="/bundle.js"></script><script src="/inline.js"></script><script src="/header.js"></script><script src="/legacy.js"></script>',
        content_type="text/html",
    )
    httpserver.expect_request("/bundle.js").respond_with_data(
        'console.log("external");\n//# sourceMappingURL=bundle.js.map', content_type="application/javascript"
    )
    httpserver.expect_request("/inline.js").respond_with_data(
        f'console.log("inline");\n//# sourceMappingURL=data:application/json;base64,{data}', content_type="application/javascript"
    )
    httpserver.expect_request("/header.js").respond_with_data(
        'console.log("header");', content_type="application/javascript", headers={"SourceMap": "/header.map"}
    )
    httpserver.expect_request("/header.map").respond_with_data(external, content_type="application/json")
    httpserver.expect_request("/legacy.js").respond_with_data(
        'console.log("legacy");', content_type="application/javascript", headers={"X-SourceMap": "/legacy.map"}
    )
    httpserver.expect_request("/legacy.map").respond_with_data(inline, content_type="application/json")
    httpserver.expect_request("/bundle.js.map").respond_with_data(external, content_type="application/json")
    destination = tmp_path / "snapshot"
    result = subprocess.run(
        ["uv", "run", "--project", str(ROOT), "--no-sync", "--no-sources", "abx-dl", "dl", "--plugins=sourcemaps", "--no-install", "--dir", str(destination), httpserver.url_for("/")],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    records = json.loads((destination / "sourcemaps" / "index.json").read_text())
    assert len(records) == 4
    maps = {record["script_url"]: (destination / "sourcemaps" / record["path"]).read_bytes() for record in records}
    assert maps[httpserver.url_for("/bundle.js")] == external
    assert maps[httpserver.url_for("/inline.js")] == inline
    assert maps[httpserver.url_for("/header.js")] == external
    assert maps[httpserver.url_for("/legacy.js")] == inline
    assert any('"plugin": "sourcemaps"' in line and '"status": "succeeded"' in line for line in result.stdout.splitlines())
