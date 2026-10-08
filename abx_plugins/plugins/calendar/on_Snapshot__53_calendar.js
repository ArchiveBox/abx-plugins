#!/usr/bin/env -S abxpkg run --script --deps-from=../chrome/config.json:required_binaries,./config.json:required_binaries node
// /// script
// ///
const fs = require("fs");
const os = require("os");
const path = require("path");
const ICAL = require("./vendor/ical.cjs");
const {
  loadConfig,
  getEnvBool,
  getEnvInt,
  parseArgs,
  emitArchiveResultRecord,
  writeFileAtomic,
} = require("../base/utils.js");
const {
  connectToPage,
  downloadBrowserResource,
} = require("../chrome/chrome_utils.js");
const { saveDownloads } = require("../base/downloads.js");

function calendarUrls(value, explicit = false) {
  let url;
  const subscription = /^webcals?:/i.test(value);
  try {
    url = new URL(
      subscription ? value.replace(/^webcals?:/i, "https:") : value,
    );
  } catch {
    return [];
  }
  if (!["https:", "http:"].includes(url.protocol)) return [];
  url.hash = "";
  if (
    url.hostname === "calendar.google.com" &&
    url.pathname.startsWith("/calendar")
  ) {
    const ids = [
      ...url.searchParams.getAll("src"),
      ...url.searchParams.getAll("cid"),
    ];
    if (ids.length)
      return ids.flatMap((id) => {
        if (!id.includes("@")) {
          try {
            id = Buffer.from(id, "base64url").toString("utf8");
          } catch {
            return [];
          }
        }
        if (!/^[^\s/@]+@[^\s/]+$/.test(id)) return [];
        return [
          `https://calendar.google.com/calendar/ical/${encodeURIComponent(id)}/public/basic.ics`,
        ];
      });
  }
  const format =
    url.searchParams.get("format") ||
    url.searchParams.get("outlook-ical") ||
    "";
  if (
    explicit ||
    subscription ||
    /\.(ics|ical|ifb)$/i.test(url.pathname) ||
    /\/(?:ical|icalendar)\/?$/i.test(url.pathname) ||
    /^(ics|ical|icalendar)$/i.test(format)
  )
    return [url.href];
  return [];
}

function result(status, message) {
  if (status !== "succeeded") console.error(message);
  return emitArchiveResultRecord(status, message);
}

async function main() {
  const config = loadConfig();
  const { url } = parseArgs();
  if (!url) throw Error("Missing --url");
  if (!getEnvBool("CALENDAR_ENABLED", true))
    return result("skipped", "CALENDAR_ENABLED=False");
  const timeoutMs = getEnvInt("CALENDAR_TIMEOUT", 60) * 1000;
  const deadline = Date.now() + timeoutMs;
  const remaining = () => {
    if (Date.now() >= deadline)
      throw Error("Calendar download deadline exceeded");
    return deadline - Date.now();
  };
  const snapshot = path.resolve(config.SNAP_DIR || ".");
  const chrome = path.join(snapshot, "chrome");
  const { browser, page, cdpSession } = await connectToPage({
    chromeSessionDir: chrome,
    timeoutMs,
  });
  let temporary;
  try {
    const navigation = JSON.parse(
      fs.readFileSync(path.join(chrome, "navigation.json"), "utf8"),
    );
    const isCalendarMime =
      /^\s*(?:text\/(?:calendar|x-vcalendar)|application\/(?:ics|icalendar))(?:;|$)/i.test(
        navigation.content_type || "",
      );
    const candidates = new Set(calendarUrls(url, isCalendarMime));
    for (const value of calendarUrls(page.url(), isCalendarMime))
      candidates.add(value);
    const discovered = await page.evaluate(() => {
      const plain = /^(text\/plain|text\/calendar)/.test(document.contentType);
      return {
        calendarText:
          plain &&
          /^\s*BEGIN:VCALENDAR\b/.test(
            document.body?.innerText.slice(0, 100) || "",
          ),
        title: document.title,
        links: [
          ...document.querySelectorAll("a[href],link[href],iframe[src]"),
        ].map((el) => ({
          url: el.href || el.src,
          explicit:
            /^text\/calendar(?:;|$)/i.test(el.getAttribute("type") || "") ||
            /\.ics$/i.test(el.getAttribute("download") || "") ||
            /\b(?:iCalendar|ICS)\b/i.test(el.textContent || ""),
        })),
      };
    });
    if (discovered.calendarText)
      for (const value of calendarUrls(page.url(), true)) candidates.add(value);
    for (const link of discovered.links)
      for (const value of calendarUrls(link.url, link.explicit))
        candidates.add(value);
    if (!candidates.size)
      return result("noresults", "No calendar feed or ICS invitation");
    temporary = await fs.promises.mkdtemp(
      path.join(os.tmpdir(), "abx-calendar-"),
    );
    const downloads = [],
      events = [],
      names = [];
    for (const [index, candidate] of [...candidates].entries()) {
      const filePath = path.join(temporary, `${index}.ics`);
      try {
        await downloadBrowserResource({
          cdpSession,
          url: candidate,
          outputPath: filePath,
          timeoutMs: remaining(),
        });
      } catch (error) {
        if (/HTTP (401|403)\b/.test(error.message) && candidates.size === 1)
          return result(
            "skipped",
            "Calendar access requires an authorized persona",
          );
        throw error;
      }
      const data = await fs.promises.readFile(filePath, "utf8");
      if (!/^\s*BEGIN:VCALENDAR\b/.test(data))
        throw Error("Calendar URL did not return an ICS calendar");
      const component = new ICAL.Component(
        ICAL.parse(data.replace(/^\uFEFF/, "")),
      );
      if (component.name !== "vcalendar")
        throw Error("Downloaded file is not a calendar");
      const components = component.getAllSubcomponents("vevent");
      const basename = new URL(candidate).pathname.split("/").at(-1);
      const feedName = /\.(?:ics|ical|ifb)$/i.test(basename || "")
        ? decodeURIComponent(basename)
            .replace(/\.(?:ics|ical|ifb)$/i, "")
            .replace(/[-_]+/g, " ")
        : "";
      const name =
        component.getFirstPropertyValue("x-wr-calname") ||
        component.getFirstPropertyValue("name") ||
        (components.length === 1 &&
          components[0].getFirstPropertyValue("summary")) ||
        feedName ||
        `Calendar ${index + 1}`;
      names.push(String(name));
      for (const event of components) {
        const start = event.getFirstPropertyValue("dtstart");
        const end = event.getFirstPropertyValue("dtend");
        events.push({
          summary: String(
            event.getFirstPropertyValue("summary") || "Untitled event",
          ),
          start: start?.toString() || "",
          end: end?.toString() || "",
          all_day: Boolean(start?.isDate),
          timezone: start?.zone?.tzid || "",
          location: String(event.getFirstPropertyValue("location") || ""),
          status: String(event.getFirstPropertyValue("status") || ""),
        });
      }
      downloads.push({
        filePath,
        suggestedFilename:
          String(name)
            .replace(/[\\/\x00-\x1f]/g, "_")
            .slice(0, 120) + ".ics",
      });
    }
    const output = path.join(snapshot, "calendar");
    const title =
      names.length === 1 ? names[0] : discovered.title || "Saved calendars";
    const manifest = await saveDownloads(output, title, downloads, {
      timeoutMs: remaining(),
    });
    manifest.event_count = events.length;
    manifest.events = events.sort((a, b) => a.start.localeCompare(b.start));
    await fs.promises.copyFile(
      path.join(__dirname, "vendor/ical.cjs"),
      path.join(output, "ical.js"),
    );
    await fs.promises.copyFile(
      path.join(__dirname, "vendor/LICENSE"),
      path.join(output, "ICAL-LICENSE.txt"),
    );
    writeFileAtomic(
      path.join(output, "downloads.json"),
      JSON.stringify(manifest, null, 2),
    );
    result("succeeded", "calendar/downloads.json");
  } finally {
    if (temporary)
      await fs.promises.rm(temporary, { recursive: true, force: true });
    await browser.disconnect();
  }
}
main().catch((error) => {
  result("failed", error.message);
  process.exitCode = 1;
});
