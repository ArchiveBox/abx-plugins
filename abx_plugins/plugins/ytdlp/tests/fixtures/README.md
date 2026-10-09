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
