"""Native FontFace regressions against the exact installed SingleFile methods.

Only OFL fixtures are served. No public capture, font decoder mock, or replacement
CSS parser is used. Generated upstream modules remain in pytest's temporary dir.
"""

import ast
import base64
import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Response

from abx_plugins.plugins.chrome.tests.chrome_test_helpers import chrome_session

PLUGIN = Path(__file__).parents[1]
FIXTURES = PLUGIN.parent / "responses/tests/fixtures"


def _modules(bundle: Path) -> dict[str, str]:
    """Extract pinned vendor and methods, checking every production patch stage."""
    constants = {}
    for node in ast.parse(
        (PLUGIN / "on_CrawlSetup__80_singlefile_prepare.py").read_text(),
    ).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                constants[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
    original = bundle.read_text()
    assert hashlib.sha256(original.encode()).hexdigest() == constants["ORIGINAL_SHA256"]
    current = original
    stages = ("TIMER", "PRIMARY_CACHE", "PRIORITY")
    assert current.count(constants["PARSER_BEFORE"]) == 1
    prepared = current.replace(constants["PARSER_BEFORE"], constants["PARSER_AFTER"])
    assert hashlib.sha256(prepared.encode()).hexdigest() == constants["PATCHED_SHA256"]
    # Experimental native validation changes remain test-only. Keep their
    # correctness assertions without putting them in the shipped optimization.
    constants.update(
        json.loads(
            (PLUGIN / "tests/fixtures/font_validation_candidates.json").read_text(),
        ),
    )
    assert constants["TIMER_BEFORE"] in current
    assert constants["TIMER_BEFORE"] in prepared
    for stage in stages:
        before, after = (constants[stage + suffix] for suffix in ("_BEFORE", "_AFTER"))
        assert current.count(before) == 1, stage
        current = current.replace(before, after)
        assert (
            hashlib.sha256(current.encode()).hexdigest() == constants[stage + "_SHA256"]
        )
    assert hashlib.sha256(current.encode()).hexdigest() == constants["PRIORITY_SHA256"]

    def extract(start, end):
        assert original.count(start) == 1, start
        offset = original.index(start)
        return original[offset : original.index(end, offset + len(start))]

    # Preserve all original notices with each temporary extracted module.
    import re

    notices = "\n".join(re.findall(r"/\*.*?\*/", original, re.S)) + "\n"
    modules = {
        "vendor": notices
        + "var "
        + extract("pf=Object.create", ";const MT=")
        + ";export default DT;",
    }
    regexes = "const " + extract("cN=/(url|local|-sf-url-original)", ",SN=") + ";"
    metadata = (
        'import RN from "./vendor.mjs";'
        + regexes
        + "export default class Metadata {"
        + extract("getFontsDetails(e,n,t,a,o=new Map){", "createFontsDetailsInfo(){")
        + extract("createFontsDetailsInfo(){", "getFontFacesGlyphRanges(")
        + extract("getPropertyValue(e,n){", "}function DN(")
        + "}"
        + extract("function DN(", "function MN(")
        + "export {DN as processFontDetails};"
    )
    modules["metadata"] = notices + metadata
    # These are the exact bound objects used by the installed inline helper.
    bindings = (
        'import DT from "./vendor.mjs";const LT=DT.findAll,ST=DT.parse;'
        + "const TU={"
        + extract("findLast(e,n){", ",ON_BEFORE_CAPTURE_EVENT_NAME:")
        + "};"
    )
    bindings += "const sU=globalThis.FontFace," + extract("SU=/^url", ",AU=") + ";"
    for name, key in (
        ("original", "TIMER_BEFORE"),
        ("timer", "TIMER_AFTER"),
        ("eager", "PRIMARY_CACHE_AFTER"),
        ("lazy", "PRIORITY_AFTER"),
    ):
        modules[name] = (
            notices + bindings + "export default class Helper {" + constants[key] + "}"
        )
    return modules


@pytest.mark.usefixtures("ensure_chrome_test_prereqs")
def test_native_font_validation(tmp_path, singlefile_install_state):
    modules = _modules(singlefile_install_state["unpacked_path"] / "lib/single-file.js")
    fonts = [
        (FIXTURES / name).read_bytes()
        for name in ("source-sans-pro.woff", "forkawesome.woff2")
    ]
    with HTTPServer(threaded=True) as server:
        server.expect_request("/").respond_with_data(
            "<!doctype html><title>Font validation</title>",
            content_type="text/html",
        )
        server.expect_request("/favicon.ico").respond_with_data(b"", status=204)
        for name, source in modules.items():
            server.expect_request(f"/{name}.mjs").respond_with_data(
                source,
                content_type="text/javascript",
            )
        server.expect_request("/font_validation.mjs").respond_with_data(
            (Path(__file__).parent / "fixtures/font_validation.mjs").read_text(),
            content_type="text/javascript",
        )
        server.expect_request("/fonts.json").respond_with_json(
            [
                "data:font/" + kind + ";base64," + base64.b64encode(data).decode()
                for kind, data in zip(("woff", "woff2"), fonts, strict=True)
            ],
        )
        server.expect_request("/fallback-good").respond_with_data(
            fonts[0],
            content_type="font/woff",
        )
        server.expect_request("/fallback-bad").respond_with_data(
            b"\0\0\0",
            content_type="font/woff",
        )

        def delayed_font(request):
            time.sleep(5.2)
            return Response(
                fonts[0],
                content_type="font/woff",
                headers={"Cache-Control": "no-store"},
            )

        def delayed_invalid(request):
            time.sleep(0.25)
            return Response(
                b"\0\0\0",
                content_type="font/woff",
                headers={"Cache-Control": "no-store"},
            )

        server.expect_request("/slow-font").respond_with_handler(delayed_font)
        server.expect_request("/fallback-invalid").respond_with_handler(delayed_invalid)
        with chrome_session(
            tmp_path,
            test_url=server.url_for("/"),
            timeout=30,
            env_overrides={"CHROME_HEADLESS": "true"},
        ) as (_, _, chrome, env):
            script = """
const utils=require(process.argv[1]);
(async()=>{
 const {browser,page}=await utils.connectToPage({chromeSessionDir:process.argv[2],requireTargetId:true,waitForNavigationComplete:true});
 try { console.log(JSON.stringify(await page.evaluate(async()=> (await import('./font_validation.mjs')).run()))); }
 finally { await browser.disconnect(); }
})().catch(error=>{console.error(error);process.exitCode=1});
"""
            result = subprocess.run(
                [
                    env["NODE_BINARY"],
                    "-e",
                    script,
                    str(PLUGIN.parent / "chrome/chrome_utils.js"),
                    str(chrome),
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
            )
            assert result.returncode == 0, result.stderr
            report = json.loads(result.stdout)
            assert report == {
                "matrix": 82,
                "expanded": 23,
                "timerRepair": True,
                "contextRepair": True,
                "timeoutPolicy": True,
            }
        server.check_assertions()
