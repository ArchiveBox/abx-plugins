#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///

"""Prepare the temporary large-CSS TokenStream fix for SingleFile 1.37.0.

WHY: upstream still packs offsets into 27 bits, corrupting CSS above 128 MiB.
Related upstream issue: https://github.com/eslint/csstree/issues/174
Font-source caching is upstream in core 1.12.0 and needs no local patch.
Remove this hook and the capture guard after upstream fixes token storage and
passes the large-CSS and full-fidelity capture regressions. See patches/README.md.
"""

import errno
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from abx_plugins.plugins.base.utils import load_config, write_text_atomic

VERSION = "1.37.0"
ORIGINAL_SHA256 = "66e04a0203fcaf633d84e7758004df490300fd13c6d074bd2c2b045ffd1371ab"
PATCHED_SHA256 = "a9379319cbcf018ceff47365d36b91f56af0140b607bcc5d7abece8e9db54050"


# Exact TokenStream class from the pinned bundle; readable MIT source and
# drift-checked upstream build overlay are in patches/css-tree/.
PARSER_BEFORE = 'var qw=class{constructor(e,n){this.setSource(e,n)}reset(){this.eof=!1,this.tokenIndex=-1,this.tokenType=0,this.tokenStart=this.firstCharOffset,this.tokenEnd=this.firstCharOffset}setSource(e="",n=()=>{}){let t=(e=String(e||"")).length,a=Mw(this.offsetAndType,e.length+1),o=Mw(this.balance,e.length+1),i=0,r=-1,s=0,l=e.length;this.offsetAndType=null,this.balance=null,o.fill(0,0,t+1),n(e,(e,n,t)=>{let c=i++;if(a[c]=e<<_w|t,-1===r&&(r=n),o[c]=l,e===s){let e=o[l];o[l]=c,l=e,s=Bw[a[e]>>>_w]}else this.isBlockOpenerTokenType(e)&&(l=c,s=Bw[e])}),a[i]=0|t,o[i]=i;for(let e=0;e<i;e++){let n=o[e];if(n<=e){let t=o[n];t!==e&&(o[e]=t)}else n>i&&(o[e]=i)}this.source=e,this.firstCharOffset=-1===r?0:r,this.tokenCount=i,this.offsetAndType=a,this.balance=o,this.reset(),this.next()}lookupType(e){return(e+=this.tokenIndex)<this.tokenCount?this.offsetAndType[e]>>>_w:0}lookupTypeNonSC(e){for(let n=this.tokenIndex;n<this.tokenCount;n++){let t=this.offsetAndType[n]>>>_w;if(13!==t&&25!==t&&0===e--)return t}return 0}lookupOffset(e){return(e+=this.tokenIndex)<this.tokenCount?this.offsetAndType[e-1]&Pw:this.source.length}lookupOffsetNonSC(e){for(let n=this.tokenIndex;n<this.tokenCount;n++){let t=this.offsetAndType[n]>>>_w;if(13!==t&&25!==t&&0===e--)return n-this.tokenIndex}return 0}lookupValue(e,n){return(e+=this.tokenIndex)<this.tokenCount&&Cw(this.source,this.offsetAndType[e-1]&Pw,this.offsetAndType[e]&Pw,n)}getTokenStart(e){return e===this.tokenIndex?this.tokenStart:e>0?e<this.tokenCount?this.offsetAndType[e-1]&Pw:this.offsetAndType[this.tokenCount]&Pw:this.firstCharOffset}getTokenEnd(e){return e===this.tokenIndex?this.tokenEnd:this.offsetAndType[Fw(e,0,this.tokenCount)]&Pw}getTokenType(e){return e===this.tokenIndex?this.tokenType:this.offsetAndType[Fw(e,0,this.tokenCount)]>>>_w}substrToCursor(e){return this.source.substring(e,this.tokenStart)}isBlockOpenerTokenType(e){return 1===Ow[e]}isBlockCloserTokenType(e){return 2===Ow[e]}getBlockTokenPairIndex(e){let n=this.getTokenType(e);if(1===Ow[n]){let t=this.balance[e],a=this.getTokenType(t);return Bw[n]===a?t:-1}if(2===Ow[n]){let t=this.balance[e],a=this.getTokenType(t);return Bw[a]===n?t:-1}return-1}isBalanceEdge(e){return this.balance[this.tokenIndex]<e}isDelim(e,n){return n?9===this.lookupType(n)&&this.source.charCodeAt(this.lookupOffset(n))===e:9===this.tokenType&&this.source.charCodeAt(this.tokenStart)===e}skip(e){let n=this.tokenIndex+e;n<this.tokenCount?(this.tokenIndex=n,this.tokenStart=this.offsetAndType[n-1]&Pw,n=this.offsetAndType[n],this.tokenType=n>>>_w,this.tokenEnd=n&Pw):(this.tokenIndex=this.tokenCount,this.next())}next(){let e=this.tokenIndex+1;e<this.tokenCount?(this.tokenIndex=e,this.tokenStart=this.tokenEnd,e=this.offsetAndType[e],this.tokenType=e>>>_w,this.tokenEnd=e&Pw):(this.eof=!0,this.tokenIndex=this.tokenCount,this.tokenType=0,this.tokenStart=this.tokenEnd=this.source.length)}skipSC(){for(;13===this.tokenType||25===this.tokenType;)this.next()}skipUntilBalanced(e,n){let t=e,a=0,o=0;e:for(;t<this.tokenCount&&(a=this.balance[t],!(a<e));t++)switch(o=t>0?this.offsetAndType[t-1]&Pw:this.firstCharOffset,n(this.source.charCodeAt(o))){case 1:break e;case 2:t++;break e;default:this.isBlockOpenerTokenType(this.offsetAndType[t]>>>_w)&&(t=a)}this.skip(t-this.tokenIndex)}forEachToken(e){for(let n=0,t=this.firstCharOffset;n<this.tokenCount;n++){let a=t,o=this.offsetAndType[n],i=o&Pw;t=i,e(o>>>_w,a,i,n)}}dump(){let e=new Array(this.tokenCount);return this.forEachToken((n,t,a,o)=>{e[o]={idx:o,type:Dw[n],chunk:this.source.substring(t,a),balance:this.balance[o]}}),e}}'
PARSER_AFTER = 'var qw=class{constructor(e,n){this.setSource(e,n)}reset(){this.eof=!1,this.tokenIndex=-1,this.tokenType=0,this.tokenStart=this.firstCharOffset,this.tokenEnd=this.firstCharOffset}setSource(e="",n=()=>{}){let t=(e=String(e||"")).length,maxCapacity=t+1,a=this.offsetAndType||new Float64Array(Math.min(16384,maxCapacity)),o=this.balance||new Uint32Array(Math.min(16384,maxCapacity)),i=0,r=-1,s=0,l=e.length;this.offsetAndType=null,this.balance=null,n(e,(e,n,t)=>{let c=i++;if(i>=a.length){let next=new Float64Array(Math.min(maxCapacity,a.length*2));next.set(a);a=next;let nextBalance=new Uint32Array(Math.min(maxCapacity,o.length*2));nextBalance.set(o);o=nextBalance;}if(a[c]=e*4294967296+t,-1===r&&(r=n),o[c]=l,e===s){let e=o[l];o[l]=c,l=e,s=e===maxCapacity-1?0:Bw[Math.floor((a[e]||0)/4294967296)]}else this.isBlockOpenerTokenType(e)&&(l=c,s=Bw[e])}),a[i]=t,o[i]=i;for(let e=0;e<i;e++){let n=o[e];if(n<=e){let t=o[n];t!==e&&(o[e]=t)}else n>i&&(o[e]=i)}this.source=e,this.firstCharOffset=-1===r?0:r,this.tokenCount=i,this.offsetAndType=a,this.balance=o,this.reset(),this.next()}lookupType(e){return(e+=this.tokenIndex)<this.tokenCount?Math.floor((this.offsetAndType[e]||0)/4294967296):0}lookupTypeNonSC(e){for(let n=this.tokenIndex;n<this.tokenCount;n++){let t=Math.floor((this.offsetAndType[n]||0)/4294967296);if(13!==t&&25!==t&&0===e--)return t}return 0}lookupOffset(e){return(e+=this.tokenIndex)<this.tokenCount?((this.offsetAndType[e-1]||0)%4294967296):this.source.length}lookupOffsetNonSC(e){for(let n=this.tokenIndex;n<this.tokenCount;n++){let t=Math.floor((this.offsetAndType[n]||0)/4294967296);if(13!==t&&25!==t&&0===e--)return n-this.tokenIndex}return 0}lookupValue(e,n){return(e+=this.tokenIndex)<this.tokenCount&&Cw(this.source,((this.offsetAndType[e-1]||0)%4294967296),((this.offsetAndType[e]||0)%4294967296),n)}getTokenStart(e){return e===this.tokenIndex?this.tokenStart:e>0?e<this.tokenCount?((this.offsetAndType[e-1]||0)%4294967296):((this.offsetAndType[this.tokenCount]||0)%4294967296):this.firstCharOffset}getTokenEnd(e){return e===this.tokenIndex?this.tokenEnd:((this.offsetAndType[Fw(e,0,this.tokenCount)]||0)%4294967296)}getTokenType(e){return e===this.tokenIndex?this.tokenType:Math.floor((this.offsetAndType[Fw(e,0,this.tokenCount)]||0)/4294967296)}substrToCursor(e){return this.source.substring(e,this.tokenStart)}isBlockOpenerTokenType(e){return 1===Ow[e]}isBlockCloserTokenType(e){return 2===Ow[e]}getBlockTokenPairIndex(e){let n=this.getTokenType(e);if(1===Ow[n]){let t=this.balance[e],a=this.getTokenType(t);return Bw[n]===a?t:-1}if(2===Ow[n]){let t=this.balance[e],a=this.getTokenType(t);return Bw[a]===n?t:-1}return-1}isBalanceEdge(e){return this.balance[this.tokenIndex]<e}isDelim(e,n){return n?9===this.lookupType(n)&&this.source.charCodeAt(this.lookupOffset(n))===e:9===this.tokenType&&this.source.charCodeAt(this.tokenStart)===e}skip(e){let n=this.tokenIndex+e;n<this.tokenCount?(this.tokenIndex=n,this.tokenStart=((this.offsetAndType[n-1]||0)%4294967296),n=this.offsetAndType[n],this.tokenType=Math.floor((n||0)/4294967296),this.tokenEnd=((n||0)%4294967296)):(this.tokenIndex=this.tokenCount,this.next())}next(){let e=this.tokenIndex+1;e<this.tokenCount?(this.tokenIndex=e,this.tokenStart=this.tokenEnd,e=this.offsetAndType[e],this.tokenType=Math.floor((e||0)/4294967296),this.tokenEnd=((e||0)%4294967296)):(this.eof=!0,this.tokenIndex=this.tokenCount,this.tokenType=0,this.tokenStart=this.tokenEnd=this.source.length)}skipSC(){for(;13===this.tokenType||25===this.tokenType;)this.next()}skipUntilBalanced(e,n){let t=e,a=0,o=0;e:for(;t<this.tokenCount&&(a=this.balance[t],!(a<e));t++)switch(o=t>0?((this.offsetAndType[t-1]||0)%4294967296):this.firstCharOffset,n(this.source.charCodeAt(o))){case 1:break e;case 2:t++;break e;default:this.isBlockOpenerTokenType(Math.floor((this.offsetAndType[t]||0)/4294967296))&&(t=a)}this.skip(t-this.tokenIndex)}forEachToken(e){for(let n=0,t=this.firstCharOffset;n<this.tokenCount;n++){let a=t,o=this.offsetAndType[n],i=((o||0)%4294967296);t=i,e(Math.floor((o||0)/4294967296),a,i,n)}}dump(){let e=new Array(this.tokenCount);return this.forEachToken((n,t,a,o)=>{e[o]={idx:o,type:Dw[n],chunk:this.source.substring(t,a),balance:this.balance[o]}}),e}}'


def prepare():
    config = load_config()
    if not config.SINGLEFILE_ENABLED:
        return
    prepared_path = (
        Path(config.CRAWL_DIR) / "chrome/extensions/singlefile.extension.json"
    )
    if not config.SINGLEFILE_PATCH_LARGE_CSS:
        # A reused crawl must not keep selecting a previously prepared copy.
        prepared_path.unlink(missing_ok=True)
        return
    metadata_path = (
        Path(os.environ["CHROMEWEBSTORE_EXTENSIONS_DIR"]) / "singlefile.extension.json"
    )
    metadata = json.loads(metadata_path.read_text())
    source = Path(metadata["unpacked_path"])
    manifest = json.loads((source / "manifest.json").read_text())
    if metadata["version"] != VERSION or manifest["version"] != VERSION:
        raise RuntimeError(
            "SingleFile large-CSS preparation requires reviewed release 1.37.0",
        )
    original = (source / "lib/single-file.js").read_bytes()
    if hashlib.sha256(original).hexdigest() != ORIGINAL_SHA256:
        raise RuntimeError("SingleFile 1.37.0 source bundle SHA256 mismatch")
    script = original.decode()
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
            "Prepared SingleFile differs from reviewed large-CSS fix",
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
