# QZ infinite-scroll article regression

`qz-infinite-articles.html` is an excerpt of the real DOM saved by ArchiveBox
snapshot `06ac8d8ce94b73bc8000ecd0bbdcd552` on 2026-10-18 for
https://qz.com/1307380/yoga-and-meditation-boost-your-ego-say-psychology-researchers/ .

It retains the original headline, author, full article body and Article JSON-LD,
plus the actual longer data-center article appended during infinite scrolling.
Navigation, unrelated appended articles, executable scripts and capture metadata
were omitted. The stale document title and retained semantic element boundaries
are deliberate: native Readability selected the unrelated story while reporting
the original headline. Tests also wrap the primary body in an article element
and exercise ambiguous metadata and legitimate nested/continuing article blocks.

Article text and images remain copyright their respective QZ authors/owners;
this source excerpt is retained only as a regression fixture, not demo content.
