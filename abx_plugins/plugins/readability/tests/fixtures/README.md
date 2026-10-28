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

# Multiple article headings and JSON-LD graph regression

`multiple-article-headings.html.gz` preserves the eight complete article elements
and original JSON-LD graph from snapshot `06ae1408338c7e2e80007fac726528b0`,
captured on 2026-10-28 for
https://divemagazine.com/scuba-diving-news/ss-united-states-to-be-sunk-as-worlds-largest-artificial-reef-in-november .
Navigation, executable scripts and styles were omitted. Each appended story has
its own H1; only the original story matches the graph's HTML-encoded headline.
Native Readability selected the longer unrelated ancient-shipwreck story.
Tests require the correct article's beginning and ending, reject the other story,
and retain native selection when metadata is unmatched or ambiguous.

Article text and images remain copyright their respective DIVE authors/owners;
this saved excerpt is retained only as a regression fixture, not demo content.
