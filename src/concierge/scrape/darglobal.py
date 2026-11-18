"""Sources for the international developer brand.

The brand's own site answers automated clients with a CAPTCHA, so it is recorded as blocked and
left alone. Facts come from public third-party and parent-company sources: the Wikipedia API
(CC BY-SA, attributed) and the parent company's public pages that describe the international arm.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from urllib.parse import quote, urlparse

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from concierge.scrape.extract import clean, detect_lang, page_text, title_of
from concierge.scrape.http import FetchResult, PoliteClient

BRAND = "darglobal"
_HEADING = re.compile(r"^(={2,4})\s*(.+?)\s*\1\s*$", re.M)
_SKIP_SECTIONS = {
    "references",
    "external links",
    "see also",
    "notes",
    "further reading",
    "المراجع",
    "مراجع",
    "وصلات خارجية",
    "انظر أيضا",
}


def _prov(url: str, res: FetchResult, lang: str, source: str) -> dict:
    return {
        "source_url": url,
        "canonical_url": url,
        "fetched_at": res.fetched_at,
        "http_status": res.status or 200,
        "content_hash": res.content_hash,
        "language": lang,
        "robots_allowed": True,
        "source_name": source,
    }


def parse_wiki_html(html: str, url: str, res: FetchResult, lang: str) -> Iterator[dict]:
    """Article HTML -> one record per section, plus the infobox as a key-facts record."""
    tree = HTMLParser(html)
    h1 = tree.css_first("h1")
    page_title = clean(h1.text()) if h1 else url.rsplit("/", 1)[-1].replace("_", " ")
    root = tree.css_first("div.mw-parser-output")
    if root is None:
        return
    slug = re.sub(r"\W+", "-", url.rsplit("/", 1)[-1]).strip("-").lower()[:60]

    infobox = root.css_first("table.infobox")
    if infobox is not None:
        facts = []
        for tr in infobox.css("tr"):
            th, td = tr.css_first("th"), tr.css_first("td")
            if th is not None and td is not None:
                facts.append(f"{clean(th.text(separator=' '))}: {_tidy(td.text(separator=' '))}")
        if facts:
            yield {
                "kind": "corporate",
                "id": f"darglobal-wiki-{lang}-{slug}-facts",
                "brand": BRAND,
                "title": f"{page_title} — key facts",
                "text": _strip_refs("\n".join(facts)),
                "section": "infobox",
                "prov": _prov(url, res, lang, urlparse(url).netloc),
            }

    section, buf, idx = "Overview", [], 0

    def flush() -> str:
        text = _strip_refs("\n".join(buf)).strip()
        return text if len(text) >= 60 and section.lower() not in _SKIP_SECTIONS else ""

    for node in root.css("h2, h3, p, ul, ol, table.wikitable"):
        if _inside_boilerplate(node, root):
            continue
        tag = node.tag
        if tag in ("h2", "h3"):
            text = flush()
            if text:
                yield _wiki_rec(page_title, section, text, idx, slug, url, res, lang)
                idx += 1
            section, buf = clean(node.text()), []
        elif tag == "p":
            buf.append(clean(node.text(separator=" ")))
        elif tag in ("ul", "ol"):
            buf.extend("- " + clean(li.text(separator=" ")) for li in node.css("li"))
        else:
            for tr in node.css("tr"):
                cells = [clean(c.text(separator=" ")) for c in tr.css("th, td")]
                if any(cells):
                    buf.append(" | ".join(cells))
    text = flush()
    if text:
        yield _wiki_rec(page_title, section, text, idx, slug, url, res, lang)


_BOILER = (
    "infobox",
    "navbox",
    "reflist",
    "references",
    "mw-references-wrap",
    "metadata",
    "sidebar",
    "hatnote",
    "toc",
    "wikitable",
    "thumb",
    "gallery",
)


def _inside_boilerplate(node, root) -> bool:
    cur = node.parent
    while cur is not None and cur is not root and cur.tag != "html":
        cls = cur.attributes.get("class") or ""
        if any(b in cls for b in _BOILER) or cur.tag in ("li", "td", "th"):
            return True
        cur = cur.parent
    return False


def _strip_refs(text: str) -> str:
    text = re.sub(r"\s*\[\s*[^\[\]]{1,30}?\s*\]", "", text)  # [12], [a], [citation needed], [better source needed]
    return re.sub(r" +([,.;:)])", r"\1", text)


def _tidy(text: str) -> str:
    text = clean(text)
    text = re.sub(r"\s+([,.;:)])", r"\1", text)
    return re.sub(r"\(\s+", "(", text)


def _wiki_rec(page_title, section, text, idx, slug, url, res, lang) -> dict:
    return {
        "kind": "article",
        "id": f"darglobal-wiki-{lang}-{slug}-{idx}",
        "brand": BRAND,
        "title": page_title if section == "Overview" else f"{page_title} — {section}",
        "text": text[:5000],
        "section": section,
        "prov": _prov(url, res, lang, urlparse(url).netloc),
    }


def crawl_wikipedia(client: PoliteClient, cfg: dict, log) -> Iterator[dict]:
    """Article pages under /wiki/ only: robots.txt disallows the /w/ API for crawlers."""
    queue = [cfg["page_url_en"] + quote(t.replace(" ", "_")) for t in cfg["titles_en"]]
    seen: set[str] = set()
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        res = client.get(url, max_age_hours=24)
        if res.outcome not in ("ok", "not_modified"):
            continue
        lang = "ar" if url.startswith(cfg["page_url_ar"]) else "en"
        n = 0
        for rec in parse_wiki_html(res.text, url, res, lang):
            n += 1
            yield rec
        log(f"wikipedia {lang} {url.rsplit('/', 1)[-1]}: {n} records")
        if lang == "en":
            link = HTMLParser(res.text).css_first('a.interlanguage-link-target[lang="ar"]')
            href = link.attributes.get("href") if link is not None else None
            if href and href.startswith(cfg["page_url_ar"]):
                queue.append(href)


def crawl_parent_site(client: PoliteClient, cfg: dict, log) -> Iterator[dict]:
    res = client.get(cfg["sitemap"], max_age_hours=24)
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", res.text) if res.text else []
    patterns = [re.compile(p) for p in cfg["include_patterns"]]
    picked = [u for u in urls if any(p.search(u.replace(cfg["base"], "")) for p in patterns)][: cfg["max_pages"]]
    log(f"darglobal parent site: {len(picked)} of {len(urls)} sitemap urls selected")
    for url in picked:
        page = client.get(url, max_age_hours=24)
        if page.outcome not in ("ok", "not_modified"):
            continue
        text = page_text(page.text, url)
        if len(text) < 120:
            continue
        lang = detect_lang(text)
        title = title_of(page.text)
        if not title or re.fullmatch(r"[\w]+\.[\w.]+", title):  # untranslated i18n keys leak into some titles
            title = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
        slug = url.replace(cfg["base"], "").strip("/").replace("/", "-")
        for i, part in enumerate(_chunks(text, 1800)):
            yield {
                "kind": "corporate",
                "id": f"parent-{slug}-{i}",
                "brand": attribute_brand(part, cfg["brand"], cfg["brand_mentions"]),
                "title": title,
                "text": part,
                "section": slug,
                "prov": _prov(url, page, lang, cfg["base"].split("//", 1)[-1]),
            }


def attribute_brand(text: str, default: str, mentions: list[str]) -> str:
    """Parent-company pages describe both group and international-arm projects. Only text that
    names the international arm (or one of its partner brands) as a whole word is attributed to it:
    a plain substring test matched "aida" inside "Buraidah"."""
    low = text.lower()
    for m in mentions:
        if re.search(rf"(?<![\w]){re.escape(m.lower())}(?![\w])", low):
            return BRAND
    return default


def crawl_rns(client: PoliteClient, cfg: dict, log) -> Iterator[dict]:
    """Regulatory announcements (RNS) are the brand's own first-party disclosures."""
    res = client.get(cfg["index_url"], max_age_hours=12)
    if res.outcome not in ("ok", "not_modified"):
        log(f"rns index: {res.outcome}")
        return
    links = list(dict.fromkeys(m for m in re.findall(cfg["link_pattern"], res.text)))
    skip = [s.lower() for s in cfg["skip_title_words"]]
    picked = [u for u in links if not any(s in u.lower() for s in skip)][: cfg["max_announcements"]]
    log(f"rns: {len(picked)} of {len(links)} announcements selected")
    for url in picked:
        page = client.get(url, max_age_hours=24 * 7)
        if page.outcome not in ("ok", "not_modified"):
            continue
        text, published = rns_body(page_text(page.text, url), cfg["end_markers"])
        if len(text) < 200:
            continue
        title = title_of(page.text).split("|")[0].strip() or url.rsplit("/", 2)[-2]
        ann_id = url.rstrip("/").rsplit("/", 1)[-1]
        for i, part in enumerate(_chunks(text[: cfg["max_chars"]], 2500)):
            yield {
                "kind": "article",
                "id": f"darglobal-rns-{ann_id}-{i}",
                "brand": BRAND,
                "title": title,
                "text": part,
                "section": "regulatory announcement",
                "published_at": published,
                "prov": _prov(url, page, "en", cfg["source_name"]),
            }


_DATE_LINE = re.compile(r"^\d{1,2} [A-Z][a-z]+ \d{4}$")


def rns_body(text: str, end_markers: list[str]) -> tuple[str, str | None]:
    """Keep the issuer's announcement only: drop the mirror site's machine summary above the dated
    header and the RNS footer and site chrome below it."""
    lines = [clean(x) for x in text.splitlines()]
    lines = [x for x in lines if x and not re.fullmatch(r"[\W_]+", x)]
    start, published = 0, None
    for i, line in enumerate(lines[:40]):
        if _DATE_LINE.match(line):
            start, published = max(i - 1, 0), line
            break
    out = []
    for line in lines[start:]:
        if any(line.startswith(m) for m in end_markers):
            break
        out.append(line)
    return "\n".join(out), published


def _chunks(text: str, size: int) -> list[str]:
    out, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) > size and cur:
            out.append(cur.strip())
            cur = ""
        cur += para + "\n"
    if cur.strip():
        out.append(cur.strip())
    return out


def crawl(client: PoliteClient, cfg: dict, log) -> Iterator[dict]:
    official = client.get(cfg["official_site"])
    log(f"darglobal official site: {official.outcome} (status {official.status})")
    yield from crawl_rns(client, cfg["sources"]["rns"], log)
    wiki = cfg["sources"]["wikipedia"]
    for rec in crawl_wikipedia(client, wiki, log):
        if rec["title"].split(" — ")[0] not in wiki["brand_pages"]:
            rec["brand"] = attribute_brand(
                rec["text"] + " " + rec["title"], wiki["parent_brand"], wiki["brand_mentions"]
            )
        yield rec
    yield from crawl_parent_site(client, cfg["sources"]["parent_site"], log)
