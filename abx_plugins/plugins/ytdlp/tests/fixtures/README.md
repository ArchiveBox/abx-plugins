# yt-dlp recorded public input

`substack-newsletter.html.gz` contains the byte-exact anonymous public response
saved on 2026-10-09 with the installed yt-dlp 2026.06.09 command:

```sh
yt-dlp --ignore-config --write-pages --skip-download https://engineercodex.substack.com/p/how-apple-built-icloud-to-store-billions
```

The request redirects to https://read.engineerscodex.com/p/how-apple-built-icloud-to-store-billions.
Uncompressed size: 267646 bytes.
SHA256: `e2fe1f2435ddfca8713cf651a7b64f05d5e66039a5f35ba81df34c24aa3c326c`.
No cookies or authentication were supplied. The embedded user ID is null;
publication analytics IDs and public payment/captcha keys are public page data.

The test uses yt-dlp's native `--load-pages` option and its expected request-dump
filename to exercise the actual Substack extractor and hook classification.
This isolates classification from hosted-runner HTTP403 responses. It does not
assert live Substack accessibility; real HTTP403/404 hook tests remain separate.

`reddit-text-post.json.gz` contains the byte-exact anonymous Reddit API response
recorded on 2026-10-09 with yt-dlp 2026.06.09:

```sh
yt-dlp --ignore-config --write-pages --skip-download https://www.reddit.com/comments/1k11yw5/
```

API URL: https://www.reddit.com/comments/1k11yw5/.json
Uncompressed size: 481981 bytes.
SHA256: `7a1ce8abd0549a72be7c469a1e6333e366eff6ba1b71ff04c082ea3eef118db2`.
No account cookies or authentication were supplied. The public response includes
the text post and public comments. Its post metadata has `is_self=true`,
`is_video=false`, and no `media_metadata`; the real downloader reported
`ERROR: [Reddit] 1k11yw5: No media found`.
Native `--load-pages` replays this response through the actual Reddit extractor.

`reddit-deleted-text-post.json.gz` contains the byte-exact anonymous Reddit API
response recorded on 2026-10-13 with yt-dlp 2026.06.09:

```sh
yt-dlp --ignore-config --write-pages --skip-download https://www.reddit.com/comments/5z4wer/
```

API URL: https://www.reddit.com/comments/5z4wer/.json
Uncompressed size: 220241 bytes.
SHA256: `020583c030e83e1d78ce1d47efe5f77b4c0dcbabe625a88b4c8569ef5ac69bcc`.
No account cookies or authentication were supplied. The post has `is_self=true`,
`is_video=false`, `removed_by_category="deleted"`, an empty `url`, and no media.
The upstream extractor passes the empty URL to its generic extractor, which
fails with `'' is not a valid URL`. Native `--load-pages` exercises the actual
Reddit extractor against this unchanged response.

`twitter-summary-card.json.gz` contains the complete byte-exact anonymous public
GraphQL response recorded on 2026-10-23 with yt-dlp 2026.10.21:

```sh
yt-dlp --ignore-config --write-pages --skip-download https://x.com/ArchiveBoxApp/status/1877264551613190437
```

API: `https://x.com/i/api/graphql/2ICDjqPd81tulZcYrtpTuQ/TweetResultByRestId`.
Uncompressed size: 10478 bytes.
SHA256: `c3c391c2db81c02ff1f971289858f19f2c87d5bc05d230dcbd209c46b44f5ead`.
No account cookies or authentication were supplied. This response contains the
public post, public author metadata and the complete `summary_large_image` card
linking to `https://gist.github.com/pirate/c89b7d42be148e9180d8c7cf81e734c8`.
It contains no guest token, account credentials, cookies, email or phone number;
the separate guest-token activation response is deliberately not recorded here.

The test uses native `--load-pages` for this GraphQL response while guest-token
setup and the linked public Gist request remain real network requests. It
verifies the existing summary-card override and hook classification, independently
of X omitting card/link data from a hosted-runner response. The failed hosted
response was not retained, so its exact shape remains unverified. Separate live
image-only and downloadable-video tests remain unchanged.

`amazon-product-video-data.js.gz` contains the unmodified public product-data
script excerpt recorded anonymously on 2026-10-23 from
https://www.amazon.com/dp/B0F7QP994T using yt-dlp2026.10.21:

```sh
yt-dlp --ignore-config --write-pages --skip-download --dump-single-json https://www.amazon.com/dp/B0F7QP994T
```

The complete response was2878414bytes, SHA256
`619eb562bf976894921e8541c6a5e9df4aa316f59cad514f31c233ef8204641b`.
The excerpt begins at the literal `var obj = jQuery.parseJSON('` and ends at
its first following `');`, inclusive. It is an exact13478byte substring of that
response, SHA256
`81683cf2cbdfafb03fea716653f60994e85cf66a0e515e834a77fe18d499f352`.
No fields are rewritten. All five original video entries, titles, identifiers,
HLS master URLs, thumbnails, dimensions and durations remain present. Unrelated
page scripts with guest-session/CSRF values are excluded; the recorded excerpt
contains no session, CSRF, token or account credentials.

Native `--load-pages` exercises the actual AmazonStore extractor using this
recorded script. HLS masters, variants, media segments and thumbnails are fetched
from the actual public media CDN. The regression requires all five MP4 outputs,
original metadata, meaningful durations, audio/video streams, and complete real
FFmpeg decoding. Without HLS format extraction, the downloader concatenates four
variant playlists as if they were media fragments and FFmpeg rejects the result.

`substack-json-script-newsletter.html.gz` and
`substack-json-script-video.html.gz` are complete byte-exact anonymous public
HTML responses fetched with `curl -L` on2026-10-23 from:

- https://www.thefocalpoints.com/p/new-study-frog-derived-gut-bacterium
  (187343bytes, SHA256`ce82cfe52afa7217ee2e5c9ab8161b428a16107b5c8759bbe92ef692bfb092f4`)
- https://andrewzimmern.substack.com/p/mussels-with-black-bean-sauce-recipe
  (206323bytes, SHA256`85b64ba067f2defc06ec7d5b71aeb1c34fb5424e6eb2242878cefd95e77f205c`)

No authentication or cookies were supplied. Account user/publication-user
settings are null; tracking identifiers are public publication analytics IDs.
Both responses use the new `preloads-json` application/json script. Native
`--load-pages` parses these unchanged bytes through the actual Substack
extractor. The video test downloads its original public media and requires
complete FFmpeg decoding, audio and video streams, and its actual60second
length. Deliberately truncated/corrupted copies separately verify that missing
or invalid metadata stays failed. The existing legacy newsletter fixture remains
unchanged and verifies the original quoted JSON.parse format.
