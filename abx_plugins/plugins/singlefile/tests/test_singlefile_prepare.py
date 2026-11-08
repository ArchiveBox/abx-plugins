"""Real installed-extension preparation and CSS serialization regression."""

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

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
        ({"version": "1.36.0", "unpacked_path": str(destination)}, True, ""),
        (
            {"version": "1.36.0", "unpacked_path": str(source)},
            False,
            "selected bundle SHA256 mismatch",
        ),
        (
            {"version": "999.0.0", "unpacked_path": str(destination)},
            False,
            "requires reviewed extension version 1.36.0",
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

    # Execute the actual reviewed methods with the real upstream CSS-tree
    # implementation, not a substitute serializer or browser simulation.
    url = "https://raw.githubusercontent.com/gildas-lormeau/single-file-core/79ecf4dce4acab130c0b587d8843f0328eb389f9/vendor/css-tree.js"
    with urlopen(url, timeout=30) as response:
        css_tree = response.read()
    assert (
        hashlib.sha256(css_tree).hexdigest()
        == "da97c5839e02559d9cfec7ca9182124e388d2d85b73e316bb16323289383089c"
    )
    (tmp_path / "css-tree.mjs").write_bytes(css_tree)
    for name, method in (("original", PREPARE.BEFORE), ("candidate", PREPARE.AFTER)):
        (tmp_path / f"{name}.mjs").write_text(
            'import * as RN from "./css-tree.mjs"; export default class Helper {'
            + method
            + "}",
        )
    script = tmp_path / "regression.mjs"
    script.write_text(r"""
import assert from 'node:assert/strict';
import * as css from './css-tree.mjs';
import Original from './original.mjs';
import Candidate from './candidate.mjs';
import inspector from 'node:inspector/promises';
const original=new Original(), candidate=new Candidate();
const cases=['','src:url(font.woff2)','src:url(first);src:url(last)','src:url(first);src:url(last)\\9','src:local("Responsive Family"),url(data:font/woff2;base64,QUJD);font-weight:100 900','font-family:"Wide Family";src:url(abc\\)def);font-style:italic'];
for(const source of cases){const rule=css.parse('@font-face{'+source+'}').children.head.data;for(const property of ['src','font-family','font-weight','font-style','missing'])assert.equal(candidate.getPropertyValue(rule,property),original.getPropertyValue(rule,property));}
const rule=css.parse('@font-face{src:url(data:font/woff2;base64,'+'QUJD'.repeat(4*1024*1024)+')}').children.head.data;
assert.equal(candidate.getPropertyValue(rule,'src'),original.getPropertyValue(rule,'src'));
const session=new inspector.Session();session.connect();await session.post('HeapProfiler.startSampling',{samplingInterval:131072,includeObjectsCollectedByMajorGC:true,includeObjectsCollectedByMinorGC:true});
const helper=process.argv[2]==='candidate'?candidate:original;
for(let i=0;i<20;i++)assert.equal(helper.getPropertyValue(rule,'src').length,16777244);
const {profile}=await session.post('HeapProfiler.stopSampling');let total=0;function visit(n){total+=n.selfSize;for(const c of n.children)visit(c)}visit(profile.head);session.disconnect();
console.log(JSON.stringify({allocated:total}));
assert.ok(total<400*1024*1024,'font property serialization duplicates payload');
""")
    red = subprocess.run(
        [env["NODE_BINARY"], str(script), "original"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    green = subprocess.run(
        [env["NODE_BINARY"], str(script), "candidate"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        red.returncode != 0
        and "font property serialization duplicates payload" in red.stderr
    ), red.stderr
    assert green.returncode == 0, green.stderr
    assert (
        json.loads(red.stdout)["allocated"]
        > json.loads(green.stdout)["allocated"] * 1.8
    )

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
    assert "requires reviewed release 1.36.0" in rejected.stderr

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


def test_duplicate_font_key_retained_allocations(tmp_path):
    import re

    env, _ = chrome_extension_install_env(tmp_path)
    sources = {}
    with urlopen(
        "https://raw.githubusercontent.com/gildas-lormeau/single-file-core/79ecf4dce4acab130c0b587d8843f0328eb389f9/core/lib/processor-helper-common.js",
        timeout=30,
    ) as response:
        content = response.read()
    assert (
        hashlib.sha256(content).hexdigest()
        == "e17165922a5a9e0327f6a0d7fcd10c2630453562538e55f92b3a8edb28327f0c"
    )
    sources["common"] = content.decode()
    with urlopen(
        "https://raw.githubusercontent.com/gildas-lormeau/single-file-core/79ecf4dce4acab130c0b587d8843f0328eb389f9/core/helper.js",
        timeout=30,
    ) as response:
        content = response.read()
    assert (
        hashlib.sha256(content).hexdigest()
        == "3d5dc14fd4696568016f16c2d3c13d368992d16415d4e4abc3c80a79ed7d89c2"
    )
    sources["helper"] = content.decode()
    with urlopen(
        "https://raw.githubusercontent.com/gildas-lormeau/single-file-core/79ecf4dce4acab130c0b587d8843f0328eb389f9/vendor/css-unescape.js",
        timeout=30,
    ) as response:
        content = response.read()
    assert (
        hashlib.sha256(content).hexdigest()
        == "6ec9a93f16fbcdf00a113cff7dd7160930dc7145cee757d6c8f04fb7ff77011e"
    )
    sources["unescape"] = content.decode()
    with urlopen(
        "https://raw.githubusercontent.com/gildas-lormeau/single-file-core/79ecf4dce4acab130c0b587d8843f0328eb389f9/vendor/css-tree.js",
        timeout=30,
    ) as response:
        content = response.read()
    assert (
        hashlib.sha256(content).hexdigest()
        == "da97c5839e02559d9cfec7ca9182124e388d2d85b73e316bb16323289383089c"
    )
    sources["tree"] = content.decode()
    (tmp_path / "sf-css-tree.mjs").write_text(sources["tree"])
    (tmp_path / "css-unescape.mjs").write_text(sources["unescape"])

    def method(name):
        source = sources["common"]
        start = source.index("\t" + name + "(")
        next_method = re.search(r"\n\t(?:async )?[A-Za-z]\w*\(", source[start + 1 :])
        end = (
            start + 1 + next_method.start()
            if next_method
            else source.index("\n}", start)
        )
        return source[start:end].rstrip()

    helper = sources["helper"]
    functions = []
    for name in (
        "normalizeFontFamily",
        "removeQuotes",
        "getFontWeight",
        "getFontStretch",
    ):
        start = helper.index("function " + name + "(")
        functions.append(helper[start : helper.index("\n}\n", start) + 2])
    constants = helper[
        helper.index("const REGEXP_SIMPLE_QUOTES_STRING") : helper.index(
            "const COMMENT_HEADER_LEGACY",
        )
    ]
    prefix = 'import * as cssTree from "./sf-css-tree.mjs";\nimport * as cssUnescape from "./css-unescape.mjs";\nconst RN=cssTree;\n'
    prefix += constants + "\n".join(functions)
    prefix += r"const REGEXP_URL_FUNCTION = /(url|local|-sf-url-original)\(.*?\)\s*(,|$)/g;const REGEXP_FONT_SRC = /(.*?)\s*,?$/;"
    for variant, source in (
        ("original", PREPARE.MAP_BEFORE),
        ("candidate", PREPARE.MAP_AFTER),
    ):
        # Observe the actual completed Map while retaining identical fixture
        # roots, then await native GC. No serializer or hook is substituted.
        assert source.count("}),e.medias.forEach") == 1  # codespell:ignore medias
        source = source.replace(
            "}),e.medias.forEach",  # codespell:ignore medias
            "});globalThis.observe?.(a);e.medias.forEach",  # codespell:ignore medias
        )
        body = (
            source
            + method("getFontKey")
            + PREPARE.AFTER
            + method("getFontsDetails")
            + method("createFontsDetailsInfo")
        )
        (tmp_path / f"map-{variant}.mjs").write_text(
            prefix + "export default class Helper {" + body + "}",
        )
    script = tmp_path / "map-regression.mjs"
    script.write_text(r"""import assert from 'node:assert/strict';
import * as css from './sf-css-tree.mjs';
import Original from './map-original.mjs';
import Candidate from './map-candidate.mjs';
import inspector from 'node:inspector/promises';
const rule=(family,src,extra='')=>`@font-face{font-family:"${family}";${src===null?'':`src:${src};`}${extra}}`;
const body=rule('Family','url(a)')+rule('Family','url(a)')+rule('Family','url(b)')+rule('Family','url(a)','font-weight:bold')+rule('Family','url(a)','font-weight:700')+rule('Réseau []','local("A"),url(c)')+rule('Réseau []','local("A"),url(c)')+rule('Missing',null)+rule('Missing','undefined');
const text=body+`@media(max-width:500px){${body}}@supports(font-tech:variations){${body}}@layer narrow{${body}}`;
function evaluate(Helper){const helper=new Helper(),sheet=css.parse(text),details=helper.createFontsDetailsInfo();helper.getFontsDetails(null,sheet.children,0,details);const faces=css.findAll(sheet,n=>n.type==='Atrule'&&n.name==='font-face');const scopes=new Map([[faces[0],sheet],[faces[1],sheet]]);helper.markRepeatedFontFaceRules(details,scopes,new Map());const repeated=new Set();function visit(d){for(const r of d.repeatedFonts)repeated.add(r);for(const m of [d.medias,d.supports,d.layers])for(const child of m.values())visit(child)}visit(details);return {indices:faces.flatMap((r,i)=>repeated.has(r)?[i]:[]),kept:faces.filter(r=>!repeated.has(r)).map(r=>css.generate(r))};} // codespell:ignore medias
assert.deepEqual(evaluate(Original),evaluate(Candidate));
const helper=process.argv[2]==='candidate'?new Candidate():new Original();
const payload='QUJD'.repeat(2*1024*1024),large=Array.from({length:16},(_,i)=>rule('Face'+i,'url(data:font/woff2;base64,'+payload+')')).join('');
const sheet=css.parse(large),details=helper.createFontsDetailsInfo();helper.getFontsDetails(null,sheet.children,0,details);
const session=new inspector.Session();session.connect();
globalThis.fixtureRoots={sheet,details,large,payload};
await session.post('HeapProfiler.collectGarbage');const baseline=process.memoryUsage().heapUsed;
const maps=[];globalThis.observe=map=>maps.push(map);
await session.post('HeapProfiler.startSampling',{samplingInterval:131072,includeObjectsCollectedByMajorGC:true,includeObjectsCollectedByMinorGC:true});
helper.markRepeatedFontFaceRules(details,new Map(),new Map());
await session.post('HeapProfiler.collectGarbage');const peakLive=process.memoryUsage().heapUsed;
assert.equal(maps.length,1);assert.equal(maps[0].size,16);assert.equal(globalThis.fixtureRoots.details.fonts.size,16);
const {profile}=await session.post('HeapProfiler.stopSampling');let cumulative=0;function visit(n){cumulative+=n.selfSize;for(const c of n.children)visit(c)}visit(profile.head);session.disconnect();
console.log(JSON.stringify({variant:process.argv[2],equivalence_faces:36,baseline,peakLive,liveIncrement:peakLive-baseline,cumulative}));
""")
    red = subprocess.run(
        [env["NODE_BINARY"], str(script), "original"],
        capture_output=True,
        text=True,
        timeout=90,
    )
    green = subprocess.run(
        [env["NODE_BINARY"], str(script), "candidate"],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert red.returncode == 0, red.stderr
    assert green.returncode == 0, green.stderr
    original, candidate = json.loads(red.stdout), json.loads(green.stdout)
    assert candidate["liveIncrement"] <= original["liveIncrement"] * 1.01
    assert original["cumulative"] > candidate["cumulative"] * 1.2


def _assert_font_source_cache_regression(bundle, output, env):
    s = bundle.read_text()
    constants = {name: value for name, value in vars(PREPARE).items() if name.isupper()}
    assert (
        hashlib.sha256(bundle.read_bytes()).hexdigest() == constants["ORIGINAL_SHA256"]
    )
    patched = s
    for key, before in constants.items():
        if key.endswith("BEFORE") and isinstance(before, str):
            after = constants[key[:-6] + "AFTER"]
            assert patched.count(before) == 1, key
            patched = patched.replace(before, after)
    assert hashlib.sha256(patched.encode()).hexdigest() == constants["PATCHED_SHA256"]
    output.mkdir(parents=True, exist_ok=True)

    def extract(start, end):
        assert s.count(start) == 1, start
        a = s.index(start)
        return s[a : s.index(end, a + len(start))]

    def pair(signature):
        found = [
            (v, constants[k[:-6] + "AFTER"])
            for k, v in constants.items()
            if k.endswith("BEFORE") and isinstance(v, str) and v.startswith(signature)
        ]
        assert len(found) == 1, signature
        before, after = found[0]
        assert s.count(before) == 1
        return before, after

    sources = pair("getFontsDetails(")
    metadata = pair("function DN(")
    # These boundaries identify the bundled CSS-tree module in exactly pinned1.36.0.
    vendor = (
        "var " + extract("pf=Object.create", ";const MT=") + ";\nexport default DT;\n"
    )
    notice = '// @eslint/css-tree 4.1.1 (https://github.com/eslint/csstree), bundled by css-tree-build/build.js\n// with the token offset field widened from 24 to 27 bits so stylesheets larger\n// than 16MB do not corrupt the token stream (parsing never terminated on them),\n// with the tokenizer clearing only the part of its balance buffer it uses (the\n// buffer never shrinks, so every later parse was paying for the largest source),\n// and with url() decoding keeping an escaped whitespace at the end of the value\n// (the decoder trimmed it and lost everything before the escape), and with\n// URL encoding/decoding preserving whole runs to avoid per-character ropes\n\n/*\n * Copyright (C) 2016-2026 by Roman Dvornov\n * Copyright OpenJS Foundation and other contributors, <www.openjsf.org>\n *\n * Permission is hereby granted, free of charge, to any person obtaining a copy\n * of this software and associated documentation files (the "Software"), to deal\n * in the Software without restriction, including without limitation the rights\n * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell\n * copies of the Software, and to permit persons to whom the Software is\n * furnished to do so, subject to the following conditions:\n *\n * The above copyright notice and this permission notice shall be included in\n * all copies or substantial portions of the Software.\n *\n * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\n * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\n * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\n * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\n * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\n * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN\n * THE SOFTWARE.\n */\n\n// Exact vendor segment from installed SingleFile 1.36.0 core SHA386dbce9c01ecc2d4edb92fe79ab36c00e10b8794149b81a4e845fb3dcb8aac2.\n'
    (output / "vendor.mjs").write_text(notice + vendor)
    regexes = "const " + extract("cN=/(url|local|-sf-url-original)", ",SN=") + ";\n"
    create = extract("createFontsDetailsInfo(){", "getFontFacesGlyphRanges(")
    property_method = constants["AFTER"]
    assert property_method.startswith("getPropertyValue(")
    for name, index in [("original", 0), ("candidate", 1)]:
        # Each module has its own lexical regexes, exactly like the real core module.
        content = (
            "// Copyright 2010-2022 Gildas Lormeau, AGPL-3.0-or-later.\n"
            "// Extracted from installed SingleFile 1.36.0; source: https://github.com/gildas-lormeau/single-file-core\n"
            'import RN from "./vendor.mjs";\n'
            + regexes
            + "export default class Helper {"
            + sources[index]
            + create
            + property_method
            + "}\n"
            + metadata[index]
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
 const a=collect(Original),b=collect(Cached);assert.deepEqual(snapshot(b.details),snapshot(a.details));
 const arrays=nodes(b.details);assert.equal(new Set(arrays).size,arrays.length);const before=arrays[18].slice();arrays[0].push('MUTATION');assert.deepEqual(arrays[18],before);arrays[0].pop();
 originalProcess(a.details,resources);cachedProcess(b.details,resources);assert.deepEqual(snapshot(b.details),snapshot(a.details));
 const records=nodes(b.details).flat();assert.equal(new Set(records).size,records.length);for(const record of records)assert.ok(!Object.hasOwn(record,'valid'));const same=records.filter(r=>r.src===records[0].src);assert.ok(same.length>1);same[0].valid=false;assert.ok(!Object.hasOwn(same[1],'valid'));delete same[0].valid;
 // A second independent pass with different resource metadata must not reuse the first pass cache.
 const freshA=collect(Original),freshB=collect(Cached),nextResources=new Map([[1,{name:'font.woff2',contentType:'application/font-woff'}]]);
 originalProcess(freshA.details,nextResources);cachedProcess(freshB.details,nextResources);assert.deepEqual(snapshot(freshB.details),snapshot(freshA.details));
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
const started=performance.now();helper.getFontsDetails(null,sheet.children,0,details,sourceLists);processFontDetails(details);const elapsed=performance.now()-started;
await session.post('HeapProfiler.collectGarbage');const retained=process.memoryUsage().heapUsed-baseline;
const {profile}=await session.post('HeapProfiler.stopSampling');let allocated=0;function visit(n){allocated+=n.selfSize;for(const child of n.children)visit(child)}visit(profile.head);
assert.equal(details.fonts.size,24);const records=[...details.fonts.values()].flat();assert.equal(records.length,24);assert.equal(new Set(records).size,24);assert.equal(new Set(records.map(r=>r.src)).size,1);
const hash=crypto.createHash('sha256');for(const record of records)hash.update(JSON.stringify(record));
console.log(JSON.stringify({variant:process.argv[2],retained,allocated,elapsedMs:elapsed,metadataSha256:hash.digest('hex')}));session.disconnect();
assert.ok(retained<16*1024*1024,'Repeated identical font sources must not retain independent large regex result strings');
""")
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
        assert result.returncode == (1 if variant == "original" else 0), result.stderr
        if variant == "original":
            assert (
                "must not retain independent large regex result strings"
                in result.stderr
            )
        results.append(json.loads(result.stdout))
    original, candidate = results
    assert original["metadataSha256"] == candidate["metadataSha256"]
    assert original["retained"] > candidate["retained"] * 4


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
