# Search importers

Choose **Search** in Importers, select Kagi, Brave, DuckDuckGo, or Exa, enter a
query, then use **Dry Run** before importing or scheduling it. Each discovered
HTTP(S) result URL enters the normal importer/snapshot pipeline, including its
folder, tags, schedule, and deduplication. Result fragments are removed; query
parameters are retained. No search query or credentials are logged by this plugin.

Configure only the provider you use in global Search plugin settings:

- `KAGI_SEARCH_API_KEY`: [Kagi API dashboard](https://kagi.com/api/keys).
- `BRAVE_SEARCH_API_KEY`: [Brave API dashboard](https://api-dashboard.search.brave.com/).
- `EXA_SEARCH_API_KEY`: [Exa API dashboard](https://dashboard.exa.ai/api-keys).
- DuckDuckGo requires no API key. It reads the ordinary public HTML results page,
  not the Instant Answer API. Consent, challenges, or unrecognized markup fail
  visibly; the importer does not bypass them.

API calls incur your provider's normal charges, including checks and Dry Runs.
This is a finite first-result-window monitor, not exhaustive search pagination:
Brave and Exa request 20 results; Kagi and DuckDuckGo use their returned first
window. Each scheduled run queries again. Native checkpoints batch and suppress
already observed item IDs; previously created snapshots remain deduplicated by
the host even when rankings change. Dry Run ignores the import checkpoint and
never creates snapshots. No provider content-fetch/AI-answer API is requested.

Requests always use the official provider endpoint; redirects are not followed.

## Verified API contracts

- [Kagi v1 Search](https://kagi.com/api/docs): POST `/api/v1/search`, Bearer key,
  `query`/`workflow=search`, results in `data.search`.
- [Brave web search](https://api-dashboard.search.brave.com/api-reference/web/search/get):
  GET `/res/v1/web/search`, `X-Subscription-Token`, results in `web.results`.
- [Exa Search](https://exa.ai/docs/reference/search): POST `/search`, `x-api-key`,
  `query`/`type=auto`/`numResults`, results in `results`.

Tests exercise the real network function against local HTTP protocol fixtures,
public DDG markup, native result emission, and the credential-free CLI failure path. They do not establish paid live account access.

Google is intentionally not advertised yet: ordinary public HTTP returned a
JavaScript interstitial during verification. Google's [legacy Custom Search JSON
API](https://developers.google.com/custom-search/v1/overview) is closed to new
customers, requires an engine ID as well as a key, and ends January 1, 2027.
No unsupported substitute, extra search service, or false empty success is used.
