# Parse Text URLs

Discovers links from saved outputs of any plugin, without a provider registry or
additional dependencies. Runs after document export and text extraction hooks.

- Reads TXT, Markdown, CSV and TSV in plugin output directories, recursively.
  This includes LiteParse text/OCR output when that plugin is enabled.
- Reads DOCX/XLSX/PPTX (and their macro-enabled variants) and ODT/ODS/ODP directly.
  Finds text URLs, labeled hyperlinks, and literal `HYPERLINK` formula targets.
  XLSX discovery covers every worksheet, including links missing from CSV exports.
- Scans all supported files across plugin outputs, including files absent from
  export manifests. Future producers need no parser changes; their hooks must
  finish before this parser's `on_Snapshot__71` hook.
- Ignores parser/search outputs, Chrome runtime files, hidden/cache directories,
  paths outside the producing plugin directory, and XML package metadata. Missing
  or malformed optional documents are reported on stderr without discarding links
  from valid documents. Compressed XML has per-part and per-document size limits.

Discovered HTTP(S) links are deduplicated into `urls.jsonl` and emitted as normal
Snapshot records at the source's depth plus one. ArchiveBox applies the crawl's
depth and URL filters. Run the standalone hook from this repository's root over
a saved text file, without installing ArchiveBox or signing in to a provider:

```bash
set -euo pipefail
output_dir="$(mktemp -d)"
mkdir -p "$output_dir/readability"
printf 'Read https://example.org/article for details.\n' > "$output_dir/readability/content.txt"
(cd "$output_dir" && uv run --project "$OLDPWD" --no-sync --no-sources \
  python "$OLDPWD/abx_plugins/plugins/parse_txt_urls/on_Snapshot__71_parse_txt_urls.py" \
  --url=https://example.com/ --depth=0 > result.jsonl)
uv run --no-sync --no-sources python - "$output_dir" <<'PY'
import json
import sys
from pathlib import Path

output = Path(sys.argv[1])
records = [json.loads(line) for line in (output / "parse_txt_urls/urls.jsonl").read_text().splitlines()]
assert len(records) == 1, records
assert records[0]["url"] == "https://example.org/article", records
assert records[0]["depth"] == 1, records
assert '"status": "succeeded"' in (output / "result.jsonl").read_text()
PY
```

All enabled URL parsers continue to run over their supported inputs. Private
documents require an authorized Chrome persona. The default Google DOCX/XLSX exports retain embedded hyperlinks;
CSV-only exports retain visible cell text, not labeled hyperlink targets.

PDFs, images, legacy Office binaries and other unsupported formats require an
upstream text extractor such as LiteParse. Only links present in its saved text
can be discovered. Spreadsheet formulas are never evaluated, and arbitrary ZIP
files are not recursively unpacked by this parser.
