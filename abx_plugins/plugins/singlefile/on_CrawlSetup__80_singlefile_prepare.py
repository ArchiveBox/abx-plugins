#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///

"""Prepare reviewed font allocation and CSS token storage fixes for SingleFile 1.36.0.

Upstream provenance: single-file-core 79ecf4dce4acab130c0b587d8843f0328eb389f9,
core/lib/processor-helper-common.js:getPropertyValue. The selected CSS value
has already been generated to check the legacy \\9 suffix; reuse it rather
than generating the same embedded font again. Duplicate-font keys use nested
exact-string Maps instead of concatenating embedded font payloads into keys.
Repeated source parsing uses per-pass exact-string caches with fresh mutable
arrays and records for every rule. Font validation, source selection, timers and
rule-local original-URL fallback remain unchanged. CSS token offsets use exact
32-bit arithmetic and token-count-sized buffers instead of overflowing 27-bit
packed offsets and source-character-sized buffers. See patches/README.md for license and source.
Remove this hook once an upstream release includes and validates this fix.
"""

import errno
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from abx_plugins.plugins.base.utils import load_config, write_text_atomic

VERSION = "1.36.0"
ORIGINAL_SHA256 = "386dbce9c01ecc2d4edb92fe79ab36c00e10b8794149b81a4e845fb3dcb8aac2"
COMMON_SHA256 = "f48431ccfc37ed9115ab5b0d98927152f06405127c5ad75a03536ca2fe95a0a2"
PATCHED_SHA256 = "8678e171fea7873381320b91c5cb019a93dc726926d2144f38932f73aab46521"
BEFORE = r"getPropertyValue(e,n){let t;if(e.block.children&&(t=e.block.children.filter(e=>{try{return e.property==n&&!RN.generate(e.value).match(/\\9$/)}catch(t){return e.property==n}}).tail),t)try{return RN.generate(t.data.value)}catch(e){}}"
AFTER = r"getPropertyValue(e,n){let t;if(e.block.children)e.block.children.forEach(e=>{if(e.property==n)try{const n=RN.generate(e.value);n.match(/\\9$/)||(t=n)}catch(e){t=void 0}});return t}"

MAP_BEFORE = 'markRepeatedFontFaceRules(e,n,t){const a=new Map;e.fonts.forEach((o,i)=>{const r=n.get(i);r&&!t.has(r)&&t.set(r,t.size+1);const s=(r?t.get(r):0)+" "+this.getFontKey(i)+" "+this.getPropertyValue(i,"src"),l=a.get(s);l&&e.repeatedFonts.add(l),a.set(s,i)}),e.medias.forEach(e=>this.markRepeatedFontFaceRules(e,n,t)),e.supports.forEach(e=>this.markRepeatedFontFaceRules(e,n,t)),e.layers.forEach(e=>this.markRepeatedFontFaceRules(e,n,t))}'  # codespell:ignore medias
MAP_AFTER = 'markRepeatedFontFaceRules(e,n,t){const a=new Map;e.fonts.forEach((o,i)=>{const r=n.get(i);r&&!t.has(r)&&t.set(r,t.size+1);const s=(r?t.get(r):0)+" "+this.getFontKey(i);let l=a.get(s);l||(l=new Map,a.set(s,l));const c=String(this.getPropertyValue(i,"src")),d=l.get(c);d&&e.repeatedFonts.add(d),l.set(c,i)}),e.medias.forEach(e=>this.markRepeatedFontFaceRules(e,n,t)),e.supports.forEach(e=>this.markRepeatedFontFaceRules(e,n,t)),e.layers.forEach(e=>this.markRepeatedFontFaceRules(e,n,t))}'  # codespell:ignore medias


FONT_SOURCES_BEFORE = 'getFontsDetails(e,n,t,a){let o=0,i=0,r=0;n.forEach(n=>{if("Atrule"==n.type&&"media"==n.name&&n.block&&n.block.children&&n.prelude){const i=RN.generate(n.prelude),r=this.createFontsDetailsInfo();a.medias.set("media-"+t+"-"+o+"-"+i,r),o++,this.getFontsDetails(e,n.block.children,t,r)}else if("Atrule"==n.type&&"supports"==n.name&&n.block&&n.block.children&&n.prelude){const o=RN.generate(n.prelude),r=this.createFontsDetailsInfo();a.supports.set("supports-"+t+"-"+i+"-"+o,r),i++,this.getFontsDetails(e,n.block.children,t,r)}else if("Atrule"==n.type&&"layer"==n.name&&n.block&&n.block.children&&n.prelude){const o=RN.generate(n.prelude),i=this.createFontsDetailsInfo();a.layers.set("layer-"+t+"-"+r+"-"+o,i),r++,this.getFontsDetails(e,n.block.children,t,i)}else if("Atrule"==n.type&&"font-face"==n.name&&n.block&&n.block.children){const e=[];a.fonts.set(n,e);const t=this.getPropertyValue(n,"src");if(t){const n=t.match(cN);n&&n.forEach(n=>{const t=n.match(vN)[1];e.includes(t)&&e.splice(e.indexOf(t),1),e.unshift(t)})}}})}'  # codespell:ignore medias
FONT_SOURCES_AFTER = 'getFontsDetails(e,n,t,a,abxSourceLists=new Map){let o=0,i=0,r=0;n.forEach(n=>{if("Atrule"==n.type&&"media"==n.name&&n.block&&n.block.children&&n.prelude){const i=RN.generate(n.prelude),r=this.createFontsDetailsInfo();a.medias.set("media-"+t+"-"+o+"-"+i,r),o++,this.getFontsDetails(e,n.block.children,t,r,abxSourceLists)}else if("Atrule"==n.type&&"supports"==n.name&&n.block&&n.block.children&&n.prelude){const o=RN.generate(n.prelude),r=this.createFontsDetailsInfo();a.supports.set("supports-"+t+"-"+i+"-"+o,r),i++,this.getFontsDetails(e,n.block.children,t,r,abxSourceLists)}else if("Atrule"==n.type&&"layer"==n.name&&n.block&&n.block.children&&n.prelude){const o=RN.generate(n.prelude),i=this.createFontsDetailsInfo();a.layers.set("layer-"+t+"-"+r+"-"+o,i),r++,this.getFontsDetails(e,n.block.children,t,i,abxSourceLists)}else if("Atrule"==n.type&&"font-face"==n.name&&n.block&&n.block.children){const t=this.getPropertyValue(n,"src");let e=abxSourceLists.get(t);if(!e){e=[];if(t){const n=t.match(cN);n&&n.forEach(n=>{const t=n.match(vN)[1];e.includes(t)&&e.splice(e.indexOf(t),1),e.unshift(t)})}abxSourceLists.set(t,e)}a.fonts.set(n,e.slice());}})}'  # codespell:ignore medias

FONT_DETAILS_BEFORE = 'function DN(e,n){e.fonts.forEach((t,a)=>{e.fonts.set(a,t.map(e=>{const t=e.match(bN),a=e.match(yN);let o,i;const r=function(e){const n=(e=e.replace(/url\\(-sf-url-original\\\\\\(\\\\"(.*?)\\\\"\\\\\\)\\\\ /g,"")).match(dN)||e.match(uN)||e.match(mN);return n&&n[1]}(e);if(t&&t[1]){o=t[1].replace(hN,"$1").replace(pN,"$1").toLowerCase();const e=o.match(kN);e&&(o=e[1],i=e[2])}if(a&&a[1]&&(i=a[1].toLowerCase()),!o){const n=e.match(gN);if(n&&n[1])o=n[1];else{const n=e.match(fN);n&&n[1]&&(o=n[1])}}if(!o&&r){const e=r.match(wN);e&&e[1]&&(o=e[1])}if(n){const t=Array.from(n.values()).find(e=>e.name==r);return{src:e.match(vN)[1],fontUrl:r,format:o,tech:i,contentType:t&&t.contentType}}return{src:e.match(vN)[1],fontUrl:r,format:o,tech:i}}))}),n?(e.medias.forEach(e=>DN(e,n)),e.supports.forEach(e=>DN(e,n)),e.layers.forEach(e=>DN(e,n))):(e.medias.forEach(e=>DN(e)),e.supports.forEach(e=>DN(e)),e.layers.forEach(e=>DN(e)))}'  # codespell:ignore medias
FONT_DETAILS_AFTER = 'function DN(e,n,abxSourceDetails=new Map){const abxRemember=(source,info)=>{abxSourceDetails.set(source,info);return {...info}};e.fonts.forEach((t,a)=>{e.fonts.set(a,t.map(e=>{if(abxSourceDetails.has(e))return {...abxSourceDetails.get(e)};const t=e.match(bN),a=e.match(yN);let o,i;const r=function(e){const n=(e=e.replace(/url\\(-sf-url-original\\\\\\(\\\\"(.*?)\\\\"\\\\\\)\\\\ /g,"")).match(dN)||e.match(uN)||e.match(mN);return n&&n[1]}(e);if(t&&t[1]){o=t[1].replace(hN,"$1").replace(pN,"$1").toLowerCase();const e=o.match(kN);e&&(o=e[1],i=e[2])}if(a&&a[1]&&(i=a[1].toLowerCase()),!o){const n=e.match(gN);if(n&&n[1])o=n[1];else{const n=e.match(fN);n&&n[1]&&(o=n[1])}}if(!o&&r){const e=r.match(wN);e&&e[1]&&(o=e[1])}if(n){const t=Array.from(n.values()).find(e=>e.name==r);return abxRemember(e,{src:e.match(vN)[1],fontUrl:r,format:o,tech:i,contentType:t&&t.contentType})}return abxRemember(e,{src:e.match(vN)[1],fontUrl:r,format:o,tech:i})}))}),n?(e.medias.forEach(e=>DN(e,n,abxSourceDetails)),e.supports.forEach(e=>DN(e,n,abxSourceDetails)),e.layers.forEach(e=>DN(e,n,abxSourceDetails))):(e.medias.forEach(e=>DN(e,undefined,abxSourceDetails)),e.supports.forEach(e=>DN(e,undefined,abxSourceDetails)),e.layers.forEach(e=>DN(e,undefined,abxSourceDetails)))}'  # codespell:ignore medias

FONT_PASS_BEFORE = 'async removeAlternativeFonts(e,n,t,a){const o=this.createFontsDetailsInfo(),i={rules:{processed:0,discarded:0},fonts:{processed:0,discarded:0}},r=new Map;return Jp(n).forEach(({stylesheetInfo:n,index:t})=>{if(n.stylesheet){n.scope&&RN.walk(n.stylesheet,{visit:"Atrule",enter:e=>r.set(e,n.scope)});const a=n.stylesheet.children;if(a)if(i.rules.processed+=a.size,i.rules.discarded+=a.size,n.mediaText&&"all"!=n.mediaText){const i=this.createFontsDetailsInfo();o.medias.set("media-"+t+"-"+n.mediaText,i),this.getFontsDetails(e,a,t,i)}else this.getFontsDetails(e,a,t,o)}}),DN(o,t),this.markRepeatedFontFaceRules(o,r,new Map),await Promise.all([...n].map(async([,e],n)=>{if(e.stylesheet){const r=e.stylesheet.children,s=e.mediaText;r&&(s&&"all"!=s?await this.processFontFaceRules(r,n,o.medias.get("media-"+n+"-"+s),t,a,i):await this.processFontFaceRules(r,n,o,t,a,i),i.rules.discarded-=r.size)}})),i}'  # codespell:ignore medias
FONT_PASS_AFTER = 'async removeAlternativeFonts(e,n,t,a){const abxSourceLists=new Map,o=this.createFontsDetailsInfo(),i={rules:{processed:0,discarded:0},fonts:{processed:0,discarded:0}},r=new Map;return Jp(n).forEach(({stylesheetInfo:n,index:t})=>{if(n.stylesheet){n.scope&&RN.walk(n.stylesheet,{visit:"Atrule",enter:e=>r.set(e,n.scope)});const a=n.stylesheet.children;if(a)if(i.rules.processed+=a.size,i.rules.discarded+=a.size,n.mediaText&&"all"!=n.mediaText){const i=this.createFontsDetailsInfo();o.medias.set("media-"+t+"-"+n.mediaText,i),this.getFontsDetails(e,a,t,i,abxSourceLists)}else this.getFontsDetails(e,a,t,o,abxSourceLists)}}),DN(o,t),this.markRepeatedFontFaceRules(o,r,new Map),await Promise.all([...n].map(async([,e],n)=>{if(e.stylesheet){const r=e.stylesheet.children,s=e.mediaText;r&&(s&&"all"!=s?await this.processFontFaceRules(r,n,o.medias.get("media-"+n+"-"+s),t,a,i):await this.processFontFaceRules(r,n,o,t,a,i),i.rules.discarded-=r.size)}})),i}'  # codespell:ignore medias


# Exact TokenStream class from the pinned bundle; readable MIT source and
# drift-checked upstream build overlay are in patches/css-tree/.
PARSER_BEFORE = 'var qw=class{constructor(e,n){this.setSource(e,n)}reset(){this.eof=!1,this.tokenIndex=-1,this.tokenType=0,this.tokenStart=this.firstCharOffset,this.tokenEnd=this.firstCharOffset}setSource(e="",n=()=>{}){let t=(e=String(e||"")).length,a=Mw(this.offsetAndType,e.length+1),o=Mw(this.balance,e.length+1),i=0,r=-1,s=0,l=e.length;this.offsetAndType=null,this.balance=null,o.fill(0,0,t+1),n(e,(e,n,t)=>{let c=i++;if(a[c]=e<<_w|t,-1===r&&(r=n),o[c]=l,e===s){let e=o[l];o[l]=c,l=e,s=Bw[a[e]>>>_w]}else this.isBlockOpenerTokenType(e)&&(l=c,s=Bw[e])}),a[i]=0|t,o[i]=i;for(let e=0;e<i;e++){let n=o[e];if(n<=e){let t=o[n];t!==e&&(o[e]=t)}else n>i&&(o[e]=i)}this.source=e,this.firstCharOffset=-1===r?0:r,this.tokenCount=i,this.offsetAndType=a,this.balance=o,this.reset(),this.next()}lookupType(e){return(e+=this.tokenIndex)<this.tokenCount?this.offsetAndType[e]>>>_w:0}lookupTypeNonSC(e){for(let n=this.tokenIndex;n<this.tokenCount;n++){let t=this.offsetAndType[n]>>>_w;if(13!==t&&25!==t&&0===e--)return t}return 0}lookupOffset(e){return(e+=this.tokenIndex)<this.tokenCount?this.offsetAndType[e-1]&Pw:this.source.length}lookupOffsetNonSC(e){for(let n=this.tokenIndex;n<this.tokenCount;n++){let t=this.offsetAndType[n]>>>_w;if(13!==t&&25!==t&&0===e--)return n-this.tokenIndex}return 0}lookupValue(e,n){return(e+=this.tokenIndex)<this.tokenCount&&Cw(this.source,this.offsetAndType[e-1]&Pw,this.offsetAndType[e]&Pw,n)}getTokenStart(e){return e===this.tokenIndex?this.tokenStart:e>0?e<this.tokenCount?this.offsetAndType[e-1]&Pw:this.offsetAndType[this.tokenCount]&Pw:this.firstCharOffset}getTokenEnd(e){return e===this.tokenIndex?this.tokenEnd:this.offsetAndType[Fw(e,0,this.tokenCount)]&Pw}getTokenType(e){return e===this.tokenIndex?this.tokenType:this.offsetAndType[Fw(e,0,this.tokenCount)]>>>_w}substrToCursor(e){return this.source.substring(e,this.tokenStart)}isBlockOpenerTokenType(e){return 1===Ow[e]}isBlockCloserTokenType(e){return 2===Ow[e]}getBlockTokenPairIndex(e){let n=this.getTokenType(e);if(1===Ow[n]){let t=this.balance[e],a=this.getTokenType(t);return Bw[n]===a?t:-1}if(2===Ow[n]){let t=this.balance[e],a=this.getTokenType(t);return Bw[a]===n?t:-1}return-1}isBalanceEdge(e){return this.balance[this.tokenIndex]<e}isDelim(e,n){return n?9===this.lookupType(n)&&this.source.charCodeAt(this.lookupOffset(n))===e:9===this.tokenType&&this.source.charCodeAt(this.tokenStart)===e}skip(e){let n=this.tokenIndex+e;n<this.tokenCount?(this.tokenIndex=n,this.tokenStart=this.offsetAndType[n-1]&Pw,n=this.offsetAndType[n],this.tokenType=n>>>_w,this.tokenEnd=n&Pw):(this.tokenIndex=this.tokenCount,this.next())}next(){let e=this.tokenIndex+1;e<this.tokenCount?(this.tokenIndex=e,this.tokenStart=this.tokenEnd,e=this.offsetAndType[e],this.tokenType=e>>>_w,this.tokenEnd=e&Pw):(this.eof=!0,this.tokenIndex=this.tokenCount,this.tokenType=0,this.tokenStart=this.tokenEnd=this.source.length)}skipSC(){for(;13===this.tokenType||25===this.tokenType;)this.next()}skipUntilBalanced(e,n){let t=e,a=0,o=0;e:for(;t<this.tokenCount&&(a=this.balance[t],!(a<e));t++)switch(o=t>0?this.offsetAndType[t-1]&Pw:this.firstCharOffset,n(this.source.charCodeAt(o))){case 1:break e;case 2:t++;break e;default:this.isBlockOpenerTokenType(this.offsetAndType[t]>>>_w)&&(t=a)}this.skip(t-this.tokenIndex)}forEachToken(e){for(let n=0,t=this.firstCharOffset;n<this.tokenCount;n++){let a=t,o=this.offsetAndType[n],i=o&Pw;t=i,e(o>>>_w,a,i,n)}}dump(){let e=new Array(this.tokenCount);return this.forEachToken((n,t,a,o)=>{e[o]={idx:o,type:Dw[n],chunk:this.source.substring(t,a),balance:this.balance[o]}}),e}}'
PARSER_AFTER = 'var qw=class{constructor(e,n){this.setSource(e,n)}reset(){this.eof=!1,this.tokenIndex=-1,this.tokenType=0,this.tokenStart=this.firstCharOffset,this.tokenEnd=this.firstCharOffset}setSource(e="",n=()=>{}){let t=(e=String(e||"")).length,maxCapacity=t+1,a=this.offsetAndType||new Float64Array(Math.min(16384,maxCapacity)),o=this.balance||new Uint32Array(Math.min(16384,maxCapacity)),i=0,r=-1,s=0,l=e.length;this.offsetAndType=null,this.balance=null,n(e,(e,n,t)=>{let c=i++;if(i>=a.length){let next=new Float64Array(Math.min(maxCapacity,a.length*2));next.set(a);a=next;let nextBalance=new Uint32Array(Math.min(maxCapacity,o.length*2));nextBalance.set(o);o=nextBalance;}if(a[c]=e*4294967296+t,-1===r&&(r=n),o[c]=l,e===s){let e=o[l];o[l]=c,l=e,s=e===maxCapacity-1?0:Bw[Math.floor((a[e]||0)/4294967296)]}else this.isBlockOpenerTokenType(e)&&(l=c,s=Bw[e])}),a[i]=t,o[i]=i;for(let e=0;e<i;e++){let n=o[e];if(n<=e){let t=o[n];t!==e&&(o[e]=t)}else n>i&&(o[e]=i)}this.source=e,this.firstCharOffset=-1===r?0:r,this.tokenCount=i,this.offsetAndType=a,this.balance=o,this.reset(),this.next()}lookupType(e){return(e+=this.tokenIndex)<this.tokenCount?Math.floor((this.offsetAndType[e]||0)/4294967296):0}lookupTypeNonSC(e){for(let n=this.tokenIndex;n<this.tokenCount;n++){let t=Math.floor((this.offsetAndType[n]||0)/4294967296);if(13!==t&&25!==t&&0===e--)return t}return 0}lookupOffset(e){return(e+=this.tokenIndex)<this.tokenCount?((this.offsetAndType[e-1]||0)%4294967296):this.source.length}lookupOffsetNonSC(e){for(let n=this.tokenIndex;n<this.tokenCount;n++){let t=Math.floor((this.offsetAndType[n]||0)/4294967296);if(13!==t&&25!==t&&0===e--)return n-this.tokenIndex}return 0}lookupValue(e,n){return(e+=this.tokenIndex)<this.tokenCount&&Cw(this.source,((this.offsetAndType[e-1]||0)%4294967296),((this.offsetAndType[e]||0)%4294967296),n)}getTokenStart(e){return e===this.tokenIndex?this.tokenStart:e>0?e<this.tokenCount?((this.offsetAndType[e-1]||0)%4294967296):((this.offsetAndType[this.tokenCount]||0)%4294967296):this.firstCharOffset}getTokenEnd(e){return e===this.tokenIndex?this.tokenEnd:((this.offsetAndType[Fw(e,0,this.tokenCount)]||0)%4294967296)}getTokenType(e){return e===this.tokenIndex?this.tokenType:Math.floor((this.offsetAndType[Fw(e,0,this.tokenCount)]||0)/4294967296)}substrToCursor(e){return this.source.substring(e,this.tokenStart)}isBlockOpenerTokenType(e){return 1===Ow[e]}isBlockCloserTokenType(e){return 2===Ow[e]}getBlockTokenPairIndex(e){let n=this.getTokenType(e);if(1===Ow[n]){let t=this.balance[e],a=this.getTokenType(t);return Bw[n]===a?t:-1}if(2===Ow[n]){let t=this.balance[e],a=this.getTokenType(t);return Bw[a]===n?t:-1}return-1}isBalanceEdge(e){return this.balance[this.tokenIndex]<e}isDelim(e,n){return n?9===this.lookupType(n)&&this.source.charCodeAt(this.lookupOffset(n))===e:9===this.tokenType&&this.source.charCodeAt(this.tokenStart)===e}skip(e){let n=this.tokenIndex+e;n<this.tokenCount?(this.tokenIndex=n,this.tokenStart=((this.offsetAndType[n-1]||0)%4294967296),n=this.offsetAndType[n],this.tokenType=Math.floor((n||0)/4294967296),this.tokenEnd=((n||0)%4294967296)):(this.tokenIndex=this.tokenCount,this.next())}next(){let e=this.tokenIndex+1;e<this.tokenCount?(this.tokenIndex=e,this.tokenStart=this.tokenEnd,e=this.offsetAndType[e],this.tokenType=Math.floor((e||0)/4294967296),this.tokenEnd=((e||0)%4294967296)):(this.eof=!0,this.tokenIndex=this.tokenCount,this.tokenType=0,this.tokenStart=this.tokenEnd=this.source.length)}skipSC(){for(;13===this.tokenType||25===this.tokenType;)this.next()}skipUntilBalanced(e,n){let t=e,a=0,o=0;e:for(;t<this.tokenCount&&(a=this.balance[t],!(a<e));t++)switch(o=t>0?((this.offsetAndType[t-1]||0)%4294967296):this.firstCharOffset,n(this.source.charCodeAt(o))){case 1:break e;case 2:t++;break e;default:this.isBlockOpenerTokenType(Math.floor((this.offsetAndType[t]||0)/4294967296))&&(t=a)}this.skip(t-this.tokenIndex)}forEachToken(e){for(let n=0,t=this.firstCharOffset;n<this.tokenCount;n++){let a=t,o=this.offsetAndType[n],i=((o||0)%4294967296);t=i,e(Math.floor((o||0)/4294967296),a,i,n)}}dump(){let e=new Array(this.tokenCount);return this.forEachToken((n,t,a,o)=>{e[o]={idx:o,type:Dw[n],chunk:this.source.substring(t,a),balance:this.balance[o]}}),e}}'


def prepare():
    config = load_config()
    if not config.SINGLEFILE_ENABLED:
        return
    metadata_path = (
        Path(os.environ["CHROMEWEBSTORE_EXTENSIONS_DIR"]) / "singlefile.extension.json"
    )
    metadata = json.loads(metadata_path.read_text())
    source = Path(metadata["unpacked_path"])
    manifest = json.loads((source / "manifest.json").read_text())
    if metadata["version"] != VERSION or manifest["version"] != VERSION:
        raise RuntimeError(
            "SingleFile font serialization preparation requires reviewed release 1.36.0",
        )
    original = (source / "lib/single-file.js").read_bytes()
    if hashlib.sha256(original).hexdigest() != ORIGINAL_SHA256:
        raise RuntimeError("SingleFile 1.36.0 source bundle SHA256 mismatch")
    script = original.decode()
    if script.count(BEFORE) != 1:
        raise RuntimeError("SingleFile font serialization source anchor mismatch")
    if script.count(MAP_BEFORE) != 1:
        raise RuntimeError("SingleFile duplicate font-key source anchor mismatch")
    script = script.replace(BEFORE, AFTER).replace(MAP_BEFORE, MAP_AFTER)
    for before, after in (
        (FONT_SOURCES_BEFORE, FONT_SOURCES_AFTER),
        (FONT_DETAILS_BEFORE, FONT_DETAILS_AFTER),
        (FONT_PASS_BEFORE, FONT_PASS_AFTER),
    ):
        if script.count(before) != 1:
            raise RuntimeError("SingleFile font-source cache anchor mismatch")
        script = script.replace(before, after)
    if hashlib.sha256(script.encode()).hexdigest() != COMMON_SHA256:
        raise RuntimeError("SingleFile common-helper stage SHA256 mismatch")
    if script.count(PARSER_BEFORE) != 1:
        raise RuntimeError("SingleFile TokenStream source anchor mismatch")
    script = script.replace(PARSER_BEFORE, PARSER_AFTER)
    patched = script.encode()
    if hashlib.sha256(patched).hexdigest() != PATCHED_SHA256:
        raise RuntimeError("SingleFile prepared bundle SHA256 mismatch")
    destination = (
        Path(config.PERSONAS_DIR) / ".singlefile" / f"{VERSION}-{PATCHED_SHA256}"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        with tempfile.TemporaryDirectory(
            prefix=".prepare-",
            dir=destination.parent,
        ) as temporary:
            staging = Path(temporary) / "extension"
            shutil.copytree(source, staging)
            (staging / "lib/single-file.js").write_bytes(patched)
            try:
                staging.rename(destination)
            except OSError as error:
                if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                    raise
    if (destination / "lib/single-file.js").read_bytes() != patched:
        raise RuntimeError(
            "Prepared SingleFile differs from reviewed font serialization fix",
        )
    prepared_path = (
        Path(config.CRAWL_DIR) / "chrome/extensions/singlefile.extension.json"
    )
    prepared_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(
        prepared_path,
        json.dumps(
            {
                "name": "singlefile",
                "version": VERSION,
                "unpacked_path": str(destination.resolve()),
            },
        ),
    )


if __name__ == "__main__":
    prepare()
