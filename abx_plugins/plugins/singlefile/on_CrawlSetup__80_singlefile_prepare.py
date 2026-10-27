#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries python3
# /// script
# requires-python = ">=3.12"
# ///

import os
import errno
import json
import shutil
import sys
import tempfile
from pathlib import Path

from abx_plugins.plugins.base.utils import load_config, write_text_atomic

EXTENSION_NAME = "singlefile"

extensions_dir = Path(os.environ["CHROMEWEBSTORE_EXTENSIONS_DIR"])
metadata_path = extensions_dir / f"{EXTENSION_NAME}.extension.json"
if not metadata_path.is_file():
    raise RuntimeError(f"Chrome extension metadata was not prepared: {metadata_path}")
metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
manifest_path = Path(metadata["unpacked_path"]) / "manifest.json"
if not manifest_path.is_file():
    raise RuntimeError(f"Chrome extension manifest was not prepared: {manifest_path}")

config = load_config()
source = manifest_path.parent
version = json.loads(manifest_path.read_text())["version"]
script = (source / "lib/single-file.js").read_text()
old = r"""function pE(e){let n="",t=!1;for(let a=0;a<e.length;a++){let o=e.charCodeAt(a);0!==o?o<=31||127===o?(n+="\\"+o.toString(16),t=!0):o===sE||o===lE||o===cE||o===dE||o===mE||o===uE?(n+="\\"+e.charAt(a),t=!1):(t&&$w(o)&&(n+=" "),n+=e.charAt(a),t=!1):n+="�"}return"url("+n+")"}"""  # codespell:ignore ue
old_decode = r"""function hE(e){let n=e.length,t=4,a=e.charCodeAt(n-1)===uE?n-2:n-1,o="";for(;t<a&&of(e.charCodeAt(t));)t++;for(;t<a&&of(e.charCodeAt(a));){let n=0;for(;a-n>t&&e.charCodeAt(a-n-1)===lE;)n++;if(n%2==1)break;a--}for(let i=t;i<=a;i++){let t=e.charCodeAt(i);if(t===lE){if(i===a){i!==n-1&&(o+=e.substr(i+1));break}if(t=e.charCodeAt(++i),rf(lE,t)){let n=i-1,t=yf(e,n);i=t-1,o+=xf(e.substring(n+1,t))}else 13===t&&10===e.charCodeAt(i+1)&&i++}else o+=e[i]}return o}"""  # codespell:ignore ue
if script.count(old) != 1 or script.count(old_decode) != 1:
    # Chrome Web Store can update independently. This is an optimization, not
    # a new requirement for running an otherwise valid upstream extension.
    print(
        "SingleFile CSS URL optimization: unrecognized upstream encoder",
        file=sys.stderr,
    )
    sys.exit(0)
# css-tree appends one character at a time when decoding/escaping a URL. Embedded media
# can contain megabytes; a rope node per character uses tens of times their
# size, repeatedly during font processing and final CSS serialization. Handle
# entire safe runs at once, including when an escaped prefix precedes a large
# payload. Preserve the original control/NUL/hex-separator behavior exactly.
script = script.replace(
    old,
    r"""function pE(e){let pendingHex=false;
return "url("+e.replace(/[^\x00-\x20\x7f"'()\\]+|[\s\S]/g,part=>{
const code=part.charCodeAt(0);
if(code===0)return "�";
if(code<=31||code===127){pendingHex=true;return "\\"+code.toString(16)}
if(code===32||code===92||code===34||code===39||code===40||code===41){pendingHex=false;return "\\"+part}
const prefix=pendingHex&&$w(code)?" ":"";pendingHex=false;return prefix+part;
})+")"}""",
)
# Decoded URLs remain in the CSS AST throughout capture, so fixing only output
# serialization still retains a rope node per byte for each embedded resource.
# Skip whole unescaped runs; leave css-tree's escape parser and trimming intact.
script = script.replace(
    old_decode,
    old_decode.replace(
        "else o+=e[i]",
        r"""else{const n=e.indexOf("\\",i),t=n<0?a+1:Math.min(n,a+1);o+=e.slice(i,t),i=t-1}""",
    ),
)
destination = Path(config.PERSONAS_DIR) / ".singlefile" / f"{version}-css-urls3"
destination.parent.mkdir(parents=True, exist_ok=True)
if not destination.exists():
    # Use the same atomic prepared-extension publication as ArchiveWeb.page:
    # concurrent crawls must never load a partially copied extension.
    with tempfile.TemporaryDirectory(
        prefix=".prepare-",
        dir=destination.parent,
    ) as temporary:
        staging = Path(temporary) / "extension"
        shutil.copytree(source, staging)
        (staging / "lib/single-file.js").write_text(script)
        try:
            staging.rename(destination)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
if (destination / "lib/single-file.js").read_text() != script:
    raise RuntimeError("Prepared SingleFile differs from pinned CSS URL serializer")
prepared_path = (
    Path(config.CRAWL_DIR) / "chrome" / "extensions" / "singlefile.extension.json"
)
prepared_path.parent.mkdir(parents=True, exist_ok=True)
write_text_atomic(
    prepared_path,
    json.dumps(
        {
            "name": EXTENSION_NAME,
            "version": version,
            "unpacked_path": str(destination.resolve()),
        },
    ),
)
