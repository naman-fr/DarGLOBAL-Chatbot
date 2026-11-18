# 0001 Sources when the brand sites block crawlers

Date: 2026-11-18. Status: accepted.

**Context.** The DarGlobal site returns an Imperva CAPTCHA to every automated request, robots.txt
included. Wasalt allows crawling in robots.txt and llms.txt, but its CDN began returning a Cloudflare
challenge to our crawler after the first few requests.

**Decision.** Do not work around either wall: no browser impersonation, no UA rotation, no web archives
as a side door. Use sources that publish the same facts openly: DarGlobal's own regulatory
announcements (RNS) on a mirror whose robots.txt allows them, Wikipedia article pages (robots.txt
disallows the API, so HTML pages only), and the parent company's site. Keep the Wasalt spider, parser
and listing search fully built and tested on fixtures, and ship with no Wasalt rows rather than with
data obtained against the site's controls.

**Consequences.** DarGlobal answers are strong on results, strategy and launches, weak on brochure
details. Wasalt listing search answers "not available in this snapshot" until the operator gets access
(an allow-listed UA, an API key or a data export) and runs `make ingest`.
