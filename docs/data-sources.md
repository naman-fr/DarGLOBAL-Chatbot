# Data sources and collection stance

Collected 2026-11-18 by `make ingest` (`src/concierge/scrape/`). Every record keeps `source_url`,
`fetched_at`, HTTP status, content hash, parser version, language and `robots_allowed`.

## How we crawl

- Identifiable User-Agent (`config/crawler.yaml`), optional contact via `CRAWLER_CONTACT`.
- robots.txt is read per host (Protego) and honoured, including crawl-delay. A robots.txt we cannot
  read because it sits behind a bot wall is treated as "disallow everything".
- Per-host pacing (1–2.5 s between requests), retries with jitter on 5xx, a circuit breaker after three
  403/429/challenge responses, conditional requests (ETag / If-Modified-Since) and a local cache.
- No login, CAPTCHA, paywall or bot-protection bypass. A challenge page is recorded as `blocked` and
  the host is left alone. We did not rotate user agents, spoof a browser, or use archives as a way
  round a block.
- Minimum personal data: listing agents' names, phone numbers and WhatsApp links are never stored,
  only listing facts and the public REGA ad-licence number.

## Sources used

| Source | What | robots / terms | Status on 2026-11-18 |
|---|---|---|---|
| DarGlobal official site (`darglobal.co.uk`) | Brand site | Serves an Imperva CAPTCHA to every automated client, including robots.txt | **Blocked, not crawled** |
| DarGlobal regulatory announcements (RNS), via `investegate.co.uk` | 22 first-party announcements: half-year and full-year results 2023–2026, project launches (Trump Jeddah, Rayana), acquisitions, board changes, capital raises | robots.txt allows `/announcement/` and `/company/` | 23 pages OK |
| Wikipedia `/wiki/` article pages (EN, AR) | DarGlobal and Dar Al Arkan articles, infobox facts, project sections | robots.txt disallows `/w/` (the API), so only article HTML is used; CC BY-SA, attributed per citation | 3 pages OK |
| `daralarkan.com` (parent company) | About, investor relations, portfolio project pages | robots.txt allows these paths | 30 pages OK, 1 error |
| Wasalt sitemaps (`cdn.wasalt.sa`) | 60k listing URLs, 8.5k category pages, 2.7k daily-rental pages | Advertised in robots.txt | OK (used for discovery only) |
| Wasalt site and blog (`wasalt.sa`, `blog.wasalt.sa`) | Search-result pages with structured listings and FAQs, corporate page, blog | robots.txt allows `/`; `llms.txt` allows summarisation and listing extraction with attribution, asks for moderate rate and no contact-detail extraction | **Blocked**: Cloudflare challenge to the crawler UA after the first requests; breaker opened |

## Brand attribution

The parent company's pages and the Dar Al Arkan Wikipedia article describe both group projects and the
international arm's projects. Text is attributed to DarGlobal only when it names DarGlobal or one of its
partner brands as a whole word (`brand_mentions` in `config/sources.yaml`); everything else is
labelled "Dar Al Arkan (parent company)" in answers. Facts are never merged across labels.

## What this means for the demo

- DarGlobal questions are answered from first-party disclosures (RNS) plus encyclopaedia and parent
  sources, with citations. Coverage of marketing-only details (brochure prices, payment plans) is thin
  because those live on the blocked site; the bot says so rather than guessing.
- Wasalt listing search is fully built (parser, schema, SQL filters, cards, tests on fixtures) but the
  live snapshot has no Wasalt rows. The bot says listing data is unavailable and offers a handoff.
  See `docs/runbook.md` → "Scraper blocked" for how to refresh once access is allowed.

## Legal and ethical stance

Public pages only, attributed, short excerpts in answers, links back to the source, no images
hotlinked or copied, no republishing of full pages. The KB stores capped text (6 000 characters per
blog post, 600 per listing description). This is a technical demo, not affiliated with any company named.
