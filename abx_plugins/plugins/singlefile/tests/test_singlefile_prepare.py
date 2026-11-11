"""Real installed-extension preparation and CSS serialization regression."""

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from abx_plugins.plugins.base.testing import install_required_binary_from_config
from abx_plugins.plugins.chrome.tests.chrome_test_helpers import (
    chrome_extension_install_env,
)

PLUGIN = Path(__file__).parents[1]
HOOK = PLUGIN / "on_CrawlSetup__80_singlefile_prepare.py"
SPEC = importlib.util.spec_from_file_location("singlefile_prepare", HOOK)
assert SPEC is not None and SPEC.loader is not None
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


def test_real_singlefile_preparation_and_serialization(tmp_path):
    assert (
        json.loads((PLUGIN / "config.json").read_text())["required_binaries"][0][
            "min_version"
        ]
        == PREPARE.VERSION
    )
    env, extensions = chrome_extension_install_env(tmp_path)
    env["CHROMEWEBSTORE_EXTENSIONS_DIR"] = str(extensions)
    install_required_binary_from_config(PLUGIN, "singlefile", env=env)
    metadata_file = extensions / "singlefile.extension.json"
    metadata_bytes = metadata_file.read_bytes()
    source = Path(json.loads(metadata_bytes)["unpacked_path"])
    _assert_font_source_cache_regression(
        source / "lib/single-file.js",
        tmp_path / "cache",
        env,
    )
    before = {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob("*")
        if p.is_file()
    }
    # Reproduce a test-only experimental prepared copy. A changed bundle must
    # publish separately, never reuse, rewrite or delete the old immutable copy.
    earlier = (
        Path(env["PERSONAS_DIR"])
        / ".singlefile"
        / f"{PREPARE.VERSION}-experimental-font-validation"
    )
    shutil.copytree(source, earlier)
    combined = (source / "lib/single-file.js").read_text()
    candidates = json.loads(
        (PLUGIN / "tests/fixtures/font_validation_candidates.json").read_text(),
    )
    for stage in ("TIMER", "PRIMARY_CACHE", "PRIORITY"):
        combined = combined.replace(
            candidates[stage + "_BEFORE"],
            candidates[stage + "_AFTER"],
        )
    assert (
        hashlib.sha256(combined.encode()).hexdigest() == candidates["PRIORITY_SHA256"]
    )
    (earlier / "lib/single-file.js").write_text(combined)
    earlier_hashes = {
        str(p.relative_to(earlier)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in earlier.rglob("*")
        if p.is_file()
    }
    result = subprocess.run(
        [str(HOOK)],
        cwd=env["CRAWL_DIR"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    prepared = json.loads(
        (
            Path(env["CRAWL_DIR"]) / "chrome/extensions/singlefile.extension.json"
        ).read_text(),
    )
    destination = Path(prepared["unpacked_path"])
    assert destination.is_relative_to(Path(env["PERSONAS_DIR"]))
    assert destination.name == f"{PREPARE.VERSION}-{PREPARE.PATCHED_SHA256}"
    assert destination != earlier
    assert earlier_hashes == {
        str(p.relative_to(earlier)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in earlier.rglob("*")
        if p.is_file()
    }
    assert (
        hashlib.sha256((destination / "lib/single-file.js").read_bytes()).hexdigest()
        == PREPARE.PATCHED_SHA256
    )
    assert metadata_file.read_bytes() == metadata_bytes
    assert before == {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob("*")
        if p.is_file()
    }
    selected = subprocess.run(
        [
            env["NODE_BINARY"],
            "-e",
            "const c=require(process.argv[1]); console.log(JSON.stringify(c.loadInstalledExtensionsFromCache().installedExtensions));",
            str(PLUGIN.parent / "chrome/chrome_utils.js"),
        ],
        env=env | {"PLUGINS": "singlefile"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert selected.returncode == 0, selected.stderr
    assert json.loads(selected.stdout)[0]["unpacked_path"] == str(destination)

    helper = PLUGIN / "singlefile_extension_save.js"
    validate = "const {validatePreparedSinglefile}=require(process.argv[1]); validatePreparedSinglefile(JSON.parse(process.argv[2]));"
    for extension, succeeds, message in (
        ({"version": PREPARE.VERSION, "unpacked_path": str(destination)}, True, ""),
        (
            {"version": PREPARE.VERSION, "unpacked_path": str(source)},
            False,
            "selected bundle SHA256 mismatch",
        ),
        (
            {"version": "999.0.0", "unpacked_path": str(destination)},
            False,
            f"requires reviewed extension version {PREPARE.VERSION}",
        ),
    ):
        checked = subprocess.run(
            [env["NODE_BINARY"], "-e", validate, str(helper), json.dumps(extension)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert (checked.returncode == 0) == succeeds, checked.stderr
        assert message in checked.stderr

    # An explicit opt-out removes only this crawl's prepared projection, so
    # even a reused crawl selects the provider's unchanged native extension.
    native_env = env | {"SINGLEFILE_PATCH_LARGE_CSS": "false"}
    opted_out = subprocess.run(
        [str(HOOK)],
        cwd=env["CRAWL_DIR"],
        env=native_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert opted_out.returncode == 0, opted_out.stderr
    assert not (
        Path(env["CRAWL_DIR"]) / "chrome/extensions/singlefile.extension.json"
    ).exists()
    assert metadata_file.read_bytes() == metadata_bytes
    assert before == {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source.rglob("*")
        if p.is_file()
    }
    assert (
        hashlib.sha256((destination / "lib/single-file.js").read_bytes()).hexdigest()
        == PREPARE.PATCHED_SHA256
    )
    native_selected = subprocess.run(
        selected.args,
        env=native_env | {"PLUGINS": "singlefile"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert native_selected.returncode == 0, native_selected.stderr
    assert json.loads(native_selected.stdout)[0]["unpacked_path"] == str(source)
    native_extension = json.dumps(
        {"version": PREPARE.VERSION, "unpacked_path": str(source)}
    )
    for config, succeeds in (
        ({}, False),
        ({"SINGLEFILE_PATCH_LARGE_CSS": True}, False),
        ({"SINGLEFILE_PATCH_LARGE_CSS": None}, False),
        ({"SINGLEFILE_PATCH_LARGE_CSS": 0}, False),
        ({"SINGLEFILE_PATCH_LARGE_CSS": ""}, False),
        ({"SINGLEFILE_PATCH_LARGE_CSS": False}, True),
    ):
        checked = subprocess.run(
            [
                env["NODE_BINARY"],
                "-e",
                "require(process.argv[1]).validatePreparedSinglefile(JSON.parse(process.argv[2]),JSON.parse(process.argv[3]));",
                str(helper),
                native_extension,
                json.dumps(config),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert (checked.returncode == 0) == succeeds, checked.stderr
        if not succeeds:
            assert "selected bundle SHA256 mismatch" in checked.stderr
    checked = subprocess.run(
        [env["NODE_BINARY"], "-e", validate, str(helper), native_extension],
        env=native_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert checked.returncode == 0, checked.stderr

    # A changed provider release must fail closed, without replacing metadata.
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["version"] = "999.0.0"
    manifest_path.write_text(json.dumps(manifest))
    rejected = subprocess.run(
        [str(HOOK)],
        cwd=env["CRAWL_DIR"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert rejected.returncode != 0
    assert f"requires reviewed release {PREPARE.VERSION}" in rejected.stderr

    manifest["version"] = PREPARE.VERSION
    manifest_path.write_text(json.dumps(manifest))
    bundle = source / "lib/single-file.js"
    bundle.write_bytes(bundle.read_bytes() + b"\n")
    rejected = subprocess.run(
        [str(HOOK)],
        cwd=env["CRAWL_DIR"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert rejected.returncode != 0
    assert "source bundle SHA256 mismatch" in rejected.stderr


def _assert_font_source_cache_regression(bundle, output, env):
    s = bundle.read_text()
    constants = {name: value for name, value in vars(PREPARE).items() if name.isupper()}
    assert (
        hashlib.sha256(bundle.read_bytes()).hexdigest() == constants["ORIGINAL_SHA256"]
    )
    assert {
        name
        for name, value in constants.items()
        if name.endswith("BEFORE") and isinstance(value, str)
    } == {"PARSER_BEFORE"}
    assert s.count(PREPARE.PARSER_BEFORE) == 1
    patched = s.replace(PREPARE.PARSER_BEFORE, PREPARE.PARSER_AFTER)
    assert hashlib.sha256(patched.encode()).hexdigest() == constants["PATCHED_SHA256"]
    native_candidates = json.loads(
        (PLUGIN / "tests/fixtures/font_validation_candidates.json").read_text(),
    )
    native_method = native_candidates["TIMER_BEFORE"]
    assert s.count(native_method) == 1
    assert patched.count(native_method) == 1, (
        "Native font validation must remain byte-for-byte original"
    )
    output.mkdir(parents=True, exist_ok=True)

    def extract(start, end):
        assert s.count(start) == 1, start
        a = s.index(start)
        return s[a : s.index(end, a + len(start))]

    # Font allocation fixes are native in 1.37.0. Exercise those exact
    # installed methods before and after the parser-only preparation.
    sources = extract(
        "getFontsDetails(e,n,t,a,o=new Map){", "createFontsDetailsInfo(){"
    )
    metadata = extract("function DN(", "function MN(")
    notice = "\n".join(re.findall(r"/\*.*?\*/", s, re.S)) + "\n"
    for name, bundle_source in (("original", s), ("candidate", patched)):
        start = bundle_source.index("pf=Object.create")
        end = bundle_source.index(";const MT=", start)
        (output / f"vendor-{name}.mjs").write_text(
            notice + "var " + bundle_source[start:end] + ";export default DT;\n"
        )
    (output / "vendor.mjs").write_bytes((output / "vendor-original.mjs").read_bytes())
    regexes = "const " + extract("cN=/(url|local|-sf-url-original)", ",SN=") + ";\n"
    create = extract("createFontsDetailsInfo(){", "getFontFacesGlyphRanges(")
    property_method = extract("getPropertyValue(e,n){", "}function DN(")
    font_helpers = (
        "const "
        + extract("Mh=", ",_h=")
        + ";"
        + "const "
        + extract("dm=", "var hm=")
        + extract(
            "function Bp(",
            "function ",
        )
        + extract("function Kp(", "function Jp(")
    )
    duplicate_method = extract(
        "markRepeatedFontFaceRules(e,n,t){", "async processFontFaceRules("
    )
    key_method = extract("getFontKey(e){", "getPropertyValue(e,n){")
    for name in ("original", "candidate"):
        # Each module has its own lexical regexes, exactly like the real core module.
        content = (
            "// Copyright 2010-2022 Gildas Lormeau, AGPL-3.0-or-later.\n"
            "// Extracted from installed SingleFile 1.37.0; source: https://github.com/gildas-lormeau/single-file-core\n"
            f'import RN from "./vendor-{name}.mjs";\n'
            + regexes
            + font_helpers
            + "export default class Helper {"
            + sources
            + create
            + property_method
            + duplicate_method
            + key_method
            + "}\n"
            + metadata
            + "\nexport {DN as processFontDetails};\n"
        )
        (output / f"{name}.mjs").write_text(content)
    license_path = HOOK.parent / "patches" / "AGPL-3.0.txt"
    assert license_path.is_file()
    (output / "AGPL-3.0.txt").write_bytes(license_path.read_bytes())
    (output / "semantic.mjs").write_text(r"""import assert from 'node:assert/strict';
import css from './vendor.mjs';
import Original from './original.mjs';
import Cached from './candidate.mjs';
import {processFontDetails as originalProcess} from './original.mjs';
import {processFontDetails as cachedProcess} from './candidate.mjs';
const actual=['url(data:font/woff2;base64,QUJD)format("woff2")','url(data:font/woff;base64,REVG)format("woff")'];
const cases=[null,'',actual[0],actual[0]+','+actual[1],`local("Fallback Face"),${actual[0]},${actual[1]}`,`${actual[0]},${actual[1]},${actual[0]}`,'local("Missing")','url(font.woff2)','url(font.woff) format("woff supports variations")','url(font.otf) format("opentype") tech(color-COLRv1)','url(font.ttf) format(truetype)','url("escaped\\20 name.woff")','url(font.woff)\\9','url(one);src:url(two)','url("font.woff") format("woff")\n, local("A")','url(data:application/x-font-woff;base64,QUJD)','url(data:font/woff2;base64,QUJD)','url(font.svg?x=1#f)'];
const body=cases.map((src,i)=>`@font-face{font-family:"Face${i}";${src===null?'':`src:${src};`}font-weight:400}`).join('');
const text=body+`@media(max-width:500px){${body}}@supports(font-tech(variations)){${body}}@layer alternate{${body}}`;
function nodes(d){return [...d.fonts.values(),...[...d.medias.values(),...d.supports.values(),...d.layers.values()].flatMap(nodes)];} // codespell:ignore medias
function snapshot(d){return {fonts:[...d.fonts.values()],medias:[...d.medias].map(([k,v])=>[k,snapshot(v)]),supports:[...d.supports].map(([k,v])=>[k,snapshot(v)]),layers:[...d.layers].map(([k,v])=>[k,snapshot(v)])};} // codespell:ignore medias
function collect(Cls){const helper=new Cls(),sheet=css.parse(text),details=helper.createFontsDetailsInfo();helper.getFontsDetails(null,sheet.children,0,details);return {helper,sheet,details};}
for(const resources of [undefined,new Map(),new Map([[1,{name:'font.woff2',contentType:'font/woff2'}]])]){
 const a=collect(Original),b=collect(Cached);assert.deepEqual(snapshot(b.details),snapshot(a.details));assert.equal(css.generate(b.sheet),css.generate(a.sheet));
 const arrays=nodes(b.details);assert.equal(new Set(arrays).size,arrays.length);const before=arrays[18].slice();arrays[0].push('MUTATION');assert.deepEqual(arrays[18],before);arrays[0].pop();
 originalProcess(a.details,resources);cachedProcess(b.details,resources);assert.deepEqual(snapshot(b.details),snapshot(a.details));
 const records=nodes(b.details).flat();assert.equal(new Set(records).size,records.length);for(const record of records)assert.ok(!Object.hasOwn(record,'valid'));const same=records.filter(r=>r.src===records[0].src);assert.ok(same.length>1);same[0].valid=false;assert.ok(!Object.hasOwn(same[1],'valid'));delete same[0].valid;
 // A second independent pass with different resource metadata must not reuse the first pass cache.
 const freshA=collect(Original),freshB=collect(Cached),nextResources=new Map([[1,{name:'font.woff2',contentType:'application/font-woff'}]]);
 originalProcess(freshA.details,nextResources);cachedProcess(freshB.details,nextResources);assert.deepEqual(snapshot(freshB.details),snapshot(freshA.details));
}
const duplicateBody=[
 '@font-face{font-family:Family;src:url(a)}',
 '@font-face{font-family:Family;src:url(a)}',
 '@font-face{font-family:Family;src:url(b)}',
 '@font-face{font-family:Family;src:url(a);font-weight:bold}',
 '@font-face{font-family:Family;src:url(a);font-weight:700}',
 '@font-face{font-family:"Réseau []";src:local("A"),url(c)}',
 '@font-face{font-family:"Réseau []";src:local("A"),url(c)}',
 '@font-face{font-family:Missing}',
 '@font-face{font-family:Missing;src:undefined}',
].join('');
for(const Cls of [Original,Cached]){
 const helper=new Cls(),sheet=css.parse(duplicateBody+`@media(max-width:500px){${duplicateBody}}@supports(font-tech:variations){${duplicateBody}}@layer narrow{${duplicateBody}}`),details=helper.createFontsDetailsInfo();
 helper.getFontsDetails(null,sheet.children,0,details);
 const faces=css.findAll(sheet,n=>n.type==='Atrule'&&n.name==='font-face');
 helper.markRepeatedFontFaceRules(details,new Map([[faces[0],sheet],[faces[1],sheet]]),new Map());
 const repeated=new Set();function visit(d){for(const r of d.repeatedFonts)repeated.add(r);for(const m of [d.medias,d.supports,d.layers])for(const child of m.values())visit(child)}visit(details);
 assert.deepEqual(faces.flatMap((r,i)=>repeated.has(r)?[i]:[]),[0,3,5,7,9,12,14,16,18,21,23,25,27,30,32,34]);
 for(const [source,expected] of [['',undefined],['src:url(first);src:url(last)','url(last)'],['src:url(first);src:url(last)\\9','url(first)']]){
  const rule=css.parse('@font-face{'+source+'}').children.head.data;
  assert.equal(helper.getPropertyValue(rule,'src'),expected);
 }
}
console.log(JSON.stringify({fixtureRules:cases.length*4,resourceModes:3,metadataEqual:true,arraysIndependent:true,recordsIndependent:true,cacheLifetimeIndependent:true}));
""")
    (output / "retained.mjs").write_text(r"""import assert from 'node:assert/strict';
import inspector from 'node:inspector/promises';
import crypto from 'node:crypto';
import css from './vendor.mjs';
const {default:Helper,processFontDetails}=await import(process.argv[2]==='original'?'./original.mjs':'./candidate.mjs');
const sheet=css.parse('');
const payload='QUJD'.repeat(512*1024);
for(let i=0;i<24;i++)sheet.children.appendData(css.parse(`@font-face{font-family:Face${i};src:url(data:font/woff2;base64,${payload})format("woff2");font-weight:${100+i}}`).children.head.data);
const helper=new Helper(),details=helper.createFontsDetailsInfo(),sourceLists=new Map();
globalThis.fixtureRoots={sheet,helper,details,sourceLists,payload};
const session=new inspector.Session();session.connect();await session.post('HeapProfiler.collectGarbage');const baseline=process.memoryUsage().heapUsed;
await session.post('HeapProfiler.startSampling',{samplingInterval:131072,includeObjectsCollectedByMajorGC:true,includeObjectsCollectedByMinorGC:true});
const started=performance.now();helper.getFontsDetails(null,sheet.children,0,details,sourceLists);processFontDetails(details);helper.markRepeatedFontFaceRules(details,new Map(),new Map());const elapsed=performance.now()-started;
await session.post('HeapProfiler.collectGarbage');const retained=process.memoryUsage().heapUsed-baseline;
const {profile}=await session.post('HeapProfiler.stopSampling');let allocated=0;function visit(n){allocated+=n.selfSize;for(const child of n.children)visit(child)}visit(profile.head);
assert.equal(details.fonts.size,24);assert.equal(details.repeatedFonts.size,0);const records=[...details.fonts.values()].flat();assert.equal(records.length,24);assert.equal(new Set(records).size,24);assert.equal(new Set(records.map(r=>r.src)).size,1);
const hash=crypto.createHash('sha256');for(const record of records)hash.update(JSON.stringify(record));
console.log(JSON.stringify({variant:process.argv[2],retained,allocated,elapsedMs:elapsed,metadataSha256:hash.digest('hex')}));session.disconnect();
assert.ok(retained<16*1024*1024,'Repeated identical font sources must not retain independent large regex result strings');
""")
    (
        output / "serialization.mjs"
    ).write_text(r"""import assert from 'node:assert/strict';
import inspector from 'node:inspector/promises';
import css from './vendor.mjs';
const {default:Helper}=await import(`./${process.argv[2]}.mjs`);
const helper=new Helper(),rule=css.parse('@font-face{src:url(data:font/woff2;base64,'+'QUJD'.repeat(4*1024*1024)+')}').children.head.data;
const session=new inspector.Session();session.connect();
await session.post('HeapProfiler.startSampling',{samplingInterval:131072,includeObjectsCollectedByMajorGC:true,includeObjectsCollectedByMinorGC:true});
for(let i=0;i<20;i++)assert.equal(helper.getPropertyValue(rule,'src').length,16777244);
const {profile}=await session.post('HeapProfiler.stopSampling');let total=0;function visit(n){total+=n.selfSize;for(const c of n.children)visit(c)}visit(profile.head);session.disconnect();
assert.ok(total<400*1024*1024,'font property serialization duplicates payload');
console.log(JSON.stringify({allocated:total}));
""")
    for variant in ("original", "candidate"):
        serialized = subprocess.run(
            [env["NODE_BINARY"], str(output / "serialization.mjs"), variant],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert serialized.returncode == 0, serialized.stderr
        assert json.loads(serialized.stdout)["allocated"] < 400 * 1024 * 1024
    semantic = subprocess.run(
        [env["NODE_BINARY"], str(output / "semantic.mjs")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert semantic.returncode == 0, semantic.stderr
    semantics = json.loads(semantic.stdout)
    assert semantics["fixtureRules"] == 72 and semantics["resourceModes"] == 3
    results = []
    for variant in ("original", "candidate"):
        result = subprocess.run(
            [env["NODE_BINARY"], str(output / "retained.mjs"), variant],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        results.append(json.loads(result.stdout))
    original, candidate = results
    assert original["metadataSha256"] == candidate["metadataSha256"]
    assert original["retained"] < 16 * 1024 * 1024
    assert candidate["retained"] < 16 * 1024 * 1024


def test_disabled_singlefile_preparation_needs_no_extension(tmp_path):
    env, extensions = chrome_extension_install_env(tmp_path)
    env["SINGLEFILE_ENABLED"] = "false"
    env["CHROMEWEBSTORE_EXTENSIONS_DIR"] = str(extensions)
    assert not (extensions / "singlefile.extension.json").exists()
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        cwd=env["CRAWL_DIR"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert not (extensions / "singlefile.extension.json").exists()
    assert not (
        Path(env["CRAWL_DIR"]) / "chrome/extensions/singlefile.extension.json"
    ).exists()
    assert not (Path(env["PERSONAS_DIR"]) / ".singlefile").exists()


def test_unpatched_singlefile_needs_no_preparation(tmp_path):
    env, extensions = chrome_extension_install_env(tmp_path)
    env["SINGLEFILE_PATCH_LARGE_CSS"] = "false"
    env["CHROMEWEBSTORE_EXTENSIONS_DIR"] = str(extensions)
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        cwd=env["CRAWL_DIR"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert not (extensions / "singlefile.extension.json").exists()
    assert not (
        Path(env["CRAWL_DIR"]) / "chrome/extensions/singlefile.extension.json"
    ).exists()
    assert not (Path(env["PERSONAS_DIR"]) / ".singlefile").exists()
