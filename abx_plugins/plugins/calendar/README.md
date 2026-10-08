# Calendar

Saves complete iCalendar feeds and ICS invitations through the snapshot's Chrome
session, then displays them in an offline month or agenda viewer. The original
ICS files remain available to download or import into another calendar app.

```bash
abx-dl dl --plugins=calendar 'https://www.gov.uk/bank-holidays/england-and-wales.ics'
```

Supported sources include direct `.ics`, `.ical`, and `.ifb` URLs, `/ical` feed
endpoints, calendar MIME
responses, `webcal://` and `webcals://` subscriptions, public Google Calendar
links with a `src` or `cid`, and pages linking to identifiable ICS downloads.
ArchiveBox normalizes submitted subscription schemes to HTTPS; when invoking
`abx-dl` directly, use the HTTPS equivalent or the page containing the subscription
link. Links discovered on a page are fetched over HTTPS. A page with several calendar feeds
saves each feed separately. The plugin saves everything the server exports; it
does not restrict downloads to the month shown in the viewer. An authenticated
calendar application without an exposed feed/export URL is not supported.

The hook reads the existing page and uses Chrome's session credentials and HTTP
cache for downloads. It never navigates or modifies the capture tab. Unrelated
pages return `noresults` with a short stderr explanation and no download requests.
An inaccessible feed returning HTTP 401/403 reports that an authorized persona is
required. Other download or parsing errors remain failures.

## Viewer and saved files

The viewer supports all-day dates, time zones supplied by the calendar, recurring
events, recurrence exceptions, cancelled events, and invitation details. Month
navigation, an agenda view, and an event detail panel work without the source
website. RSVP actions are not sent. Timed events occupy dates in the browser's
local time zone; labels preserve the source time zone. All-day end dates are
exclusive. Unresolved time zones are explicitly labelled and retain wall time.

`downloads.json` contains the existing download manifest with file sizes and
SHA-256 hashes, plus an event summary for the card. `files/` contains the complete,
unmodified ICS downloads. The output includes the bundled ICAL.js parser and its
license so replay makes no requests to a CDN or calendar provider. See
[vendor/README.md](vendor/README.md) for its source and license.

Only display expansion is bounded: at most 2,000 occurrences or 50,000 recurrence
steps per view, with a visible notice if reached. This never truncates saved ICS
files. The viewer displays VEVENT entries; other iCalendar components remain in
the original file.

## Configuration

- `CALENDAR_ENABLED=true`: enable calendar capture.
- `CALENDAR_TIMEOUT=60`: total capture deadline in seconds, with the shared
  `TIMEOUT` fallback.

Chrome is the only plugin dependency. No calendar account, API key, extra binary,
or package installation is needed for public feeds. Private feeds use the chosen
Chrome persona when the feed supports browser-session authentication.
