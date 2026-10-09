# Spreadsheet importer

Paste a Google Sheets URL or readable CSV URL into ArchiveBox's Importers UI.
The default column is `URL`; `Title` is optional. Change the column or select a
Persona for authenticated export under Advanced options. The chosen account must
be allowed to export the sheet as CSV.

Import all drains every new URL row in batches. Row identities use content rather
than row position, so reordering does not reimport them. Changed rows are new
discoveries; ordinary ArchiveBox URL deduplication avoids recapturing existing URLs.
HTML/login responses and missing columns fail without advancing progress.

`importer.py` is also a standalone command using the version 1 stdin/JSONL contract
in `base.importers`; dependencies, UI fields, and setup guidance live in `config.json`.
