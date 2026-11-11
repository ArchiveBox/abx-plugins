# RSS and Atom importer

Connect a feed URL in ArchiveBox's Importers UI. Optional Persona cookies support
private feeds. Preview checks titles/URLs; Import all drains the available feed
in batches and ordinary ArchiveBox crawls capture the discovered links.

The standalone `importer.py` command uses the version 1 stdin/JSONL contract in
`base.importers`. It parses RSS/Atom with feedparser and tracks stable entry IDs.
Only entries exposed by the feed are available; an RSS feed is not necessarily
the publisher's complete historical archive. Invalid/HTML responses fail without
advancing progress. Dependencies are declared in `config.json`.

Enable **Import feed categories as tags** to carry each RSS item’s categories or
Atom entry’s category terms into its captured snapshot. This is opt-in
(`IMPORTER_RSS_IMPORT_TAGS`, default `false`). Categories stay attached to their
individual article; manually configured source tags still apply to every article.
Standalone consumers receive categories in each item’s `metadata.tags` array.
