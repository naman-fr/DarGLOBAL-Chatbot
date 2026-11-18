"""Spider for the Saudi marketplace brand. Ladder: sitemaps and WordPress API, then __NEXT_DATA__, then generic text.

Personal data is dropped on purpose: agent names, phone numbers and WhatsApp links are never stored
(the site's llms.txt also asks for this). Only listing facts and the public REGA ad licence remain.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

from concierge.scrape.extract import (
    clean,
    detect_lang,
    html_to_text,
    next_data,
    page_text,
    title_of,
    to_number,
    walk_strings,
)
from concierge.scrape.http import FetchResult, PoliteClient

BRAND = "wasalt"
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_CMS_SKIP = {
    "url",
    "hash",
    "ext",
    "mime",
    "name",
    "path",
    "vuid",
    "locale",
    "createdAt",
    "updatedAt",
    "publishedAt",
    "versionComment",
    "alternativeText",
    "caption",
    "ButtonText",
    "ButtonLink",
    "alt",
    "Link",
    "link",
    "href",
    "icon",
    "Icon",
    "image",
    "Image",
    "videoUrl",
    "slug",
}


def prov(res: FetchResult, lang: str, source_name: str | None = None) -> dict[str, Any]:
    return {
        "source_url": res.url,
        "canonical_url": res.url,
        "fetched_at": res.fetched_at,
        "http_status": res.status or 200,
        "content_hash": res.content_hash,
        "language": lang,
        "robots_allowed": True,
        "source_name": source_name or urlparse(res.url).netloc,
    }


def sitemap_urls(client: PoliteClient, url: str) -> list[str]:
    res = client.get(url, max_age_hours=24, decode_gzip=True)
    return _LOC.findall(res.text) if res.outcome in ("ok", "not_modified") else []


# ---- listings -------------------------------------------------------------------------------


def _attr(item: dict, key: str) -> Any:
    for a in item.get("attributes") or []:
        if a.get("key") == key:
            return a.get("value")
    return None


def parse_listing(item: dict, res: FetchResult, lang: str, site: str, max_desc: int) -> dict | None:
    info = item.get("propertyInfo") or {}
    slug = info.get("slug")
    purpose_raw = (info.get("propertyFor") or "").lower()
    if not slug or purpose_raw not in ("sale", "rent"):
        return None
    lang_prefix = "/en" if lang == "en" else ""
    url = f"{site}{lang_prefix}/property/{purpose_raw}/{slug}"
    price = to_number(info.get("salePrice")) if purpose_raw == "sale" else to_number(info.get("expectedRent"))
    if price is None:
        price = to_number(info.get("conversionPrice"))
    area = (
        to_number(_attr(item, "builtUpArea")) or to_number(_attr(item, "plotArea")) or to_number(item.get("floorSize"))
    )
    beds = to_number(_attr(item, "noOfBedrooms"))
    baths = to_number(_attr(item, "noOfBathrooms"))
    owner = item.get("propertyOwner") or {}
    desc = html_to_text(info.get("description") or "")[:max_desc]
    return {
        "kind": "listing",
        "id": f"wasalt-listing-{item['id']}-{lang}",
        "brand": BRAND,
        "title": clean(info.get("title") or info.get("propertyName") or ""),
        "listing_id": int(item["id"]),
        "purpose": purpose_raw,
        "property_type": info.get("propertySubType") or info.get("propertySubTypeSlug") or "",
        "main_type": info.get("propertyMainType") or "",
        "price": price,
        "currency": info.get("currencyType") or info.get("conversionUnit"),
        "rent_frequency": info.get("rentFreq") or None,
        "city": info.get("city") or "",
        "district": info.get("district") or info.get("zone") or "",
        "region_zone": info.get("territory") or "",
        "bedrooms": int(beds) if beds is not None else None,
        "bathrooms": int(baths) if baths is not None else None,
        "area_sqm": area,
        "rega_ad_license": owner.get("regaAdvLicNo") or None,
        "is_rega_verified": bool(item.get("isRegaProp")),
        "published_at": item.get("publishedAt"),
        "updated_at": item.get("updatedAt"),
        "description": desc,
        "url": url,
        "prov": prov(res, lang),
    }


def parse_srp(html: str, res: FetchResult, lang: str, site: str, max_desc: int) -> Iterator[dict]:
    data = next_data(html)
    if not data:
        return
    pp = data.get("props", {}).get("pageProps", {})
    meta = pp.get("metaData") or {}
    page_title = clean(meta.get("title_tag") or meta.get("meta_title") or title_of(html))
    for item in (pp.get("searchResult") or {}).get("properties") or []:
        rec = parse_listing(item, res, lang, site, max_desc)
        if rec:
            yield rec
    faq = pp.get("FAQSchema") or {}
    for i, q in enumerate(faq.get("mainEntity") or []):
        ans = (q.get("acceptedAnswer") or {}).get("text") or ""
        yield {
            "kind": "faq",
            "id": f"wasalt-faq-{urlparse(res.url).path.strip('/').replace('/', '-')}-{i}",
            "brand": BRAND,
            "title": clean(q.get("name") or ""),
            "question": clean(q.get("name") or ""),
            "answer": clean(html_to_text(ans) or ans),
            "topic": page_title,
            "prov": prov(res, lang),
        }
    para = pp.get("para")
    if isinstance(para, str) and len(para) > 200:
        yield {
            "kind": "article",
            "id": f"wasalt-guide-{urlparse(res.url).path.strip('/').replace('/', '-')}",
            "brand": BRAND,
            "title": page_title or "Area guide",
            "text": html_to_text(para)[:6000],
            "section": "area guide",
            "prov": prov(res, lang),
        }


# ---- corporate and static pages --------------------------------------------------------------


def parse_cms_page(html: str, res: FetchResult) -> Iterator[dict]:
    data = next_data(html)
    lang = "en" if "/en/" in res.url or res.url.endswith("/en") else "ar"
    title = title_of(html)
    texts: list[str] = []
    if data:
        pp = data.get("props", {}).get("pageProps", {})
        body = pp.get("SPageBody") or pp.get("staticPageData") or pp
        seen: set[str] = set()
        for _, s in walk_strings(body, _CMS_SKIP):
            s = s.strip()
            if len(s) < 3 or s.startswith(("http", "/", "#")) or s in seen or re.fullmatch(r"[\w\-.]+", s):
                continue
            seen.add(s)
            texts.append(html_to_text(s) if "<" in s else clean(s))
    text = "\n".join(t for t in texts if t)
    if len(text) < 200:
        text = page_text(html, res.url)
    if not text:
        return
    lang = detect_lang(text) if lang == "ar" else lang
    slug = urlparse(res.url).path.strip("/").replace("/", "-") or "home"
    # Split long pages into sections so retrieval can cite a focused piece.
    for i, part in enumerate(_split(text, 1800)):
        yield {
            "kind": "corporate",
            "id": f"wasalt-page-{slug}-{i}",
            "brand": BRAND,
            "title": title or slug,
            "text": part,
            "section": slug,
            "prov": prov(res, lang),
        }


def _split(text: str, size: int) -> list[str]:
    parts, cur = [], ""
    for para in text.split("\n"):
        if len(cur) + len(para) > size and cur:
            parts.append(cur.strip())
            cur = ""
        cur += para + "\n"
    if cur.strip():
        parts.append(cur.strip())
    return parts


def parse_llms_txt(text: str, res: FetchResult, brand_name: str = "") -> Iterator[dict]:
    keep = []
    for line in text.splitlines():
        if line.startswith(("Owner:", "Contact:", "Website:")) or (
            line and not line.startswith("#") and ":" not in line[:25]
        ):
            keep.append(line.strip())
    body = "\n".join(k for k in keep if k)
    if body:
        yield {
            "kind": "corporate",
            "id": "wasalt-llms-txt",
            "brand": BRAND,
            "title": f"{brand_name} platform summary (llms.txt)".strip(),
            "text": body,
            "section": "llms.txt",
            "prov": prov(res, "en"),
        }


# ---- blog ---------------------------------------------------------------------------------------


def parse_blog_posts(posts: list[dict], res: FetchResult, max_chars: int) -> Iterator[dict]:
    for p in posts:
        title = clean(html_to_text((p.get("title") or {}).get("rendered", "")))
        body = html_to_text((p.get("content") or {}).get("rendered", ""))[:max_chars]
        link = p.get("link") or res.url
        lang = detect_lang(title + " " + body[:500])
        r = FetchResult(link, 200, fetched_at=res.fetched_at, content_hash=res.content_hash)
        yield {
            "kind": "article",
            "id": f"wasalt-blog-{p.get('id')}",
            "brand": BRAND,
            "title": title or "Blog post",
            "text": body,
            "section": "blog",
            "published_at": p.get("date_gmt") or p.get("date"),
            "prov": prov(r, lang),
        }


# ---- orchestration --------------------------------------------------------------------------


def crawl(client: PoliteClient, cfg: dict, log) -> Iterator[dict]:
    import json

    site = cfg["official_site"].rstrip("/")
    budget = cfg["listing_budget"]
    max_desc = budget["description_max_chars"]

    res = client.get(cfg["llms_txt"])
    if res.outcome in ("ok", "not_modified"):
        yield from parse_llms_txt(res.text, res, cfg["name"])

    for url in cfg["pages"]:
        res = client.get(url)
        if res.outcome in ("ok", "not_modified"):
            yield from parse_cms_page(res.text, res)

    static = sitemap_urls(client, cfg["sitemaps"]["static_en"]) + sitemap_urls(client, cfg["sitemaps"]["static_ar"])
    for url in static:
        if any(url.rstrip("/").endswith(k) for k in cfg["static_page_keep"]):
            res = client.get(url)
            if res.outcome in ("ok", "not_modified"):
                yield from parse_cms_page(res.text, res)

    # Stratified listing sample: walk city x category, only URLs the sitemap advertises.
    for lang, key, cap in (
        ("en", "category_en", budget["max_srp_pages_en"]),
        ("ar", "category_ar", budget["max_srp_pages_ar"]),
    ):
        advertised = set(sitemap_urls(client, cfg["sitemaps"][key]))
        log(f"wasalt {lang}: {len(advertised)} category urls in sitemap")
        targets = _stratified_targets(advertised, budget, lang, site)
        for url in targets[:cap]:
            res = client.get(url, max_age_hours=12)
            if res.outcome in ("ok", "not_modified"):
                yield from parse_srp(res.text, res, lang, site, max_desc)

    rental = sitemap_urls(client, cfg["sitemaps"]["rental_srp_en"])
    picked = [u for u in rental if any(f"-in-{c}" in u for c in budget["cities"])][: budget["daily_rental_pages"]]
    for url in picked:
        res = client.get(url, max_age_hours=12)
        if res.outcome in ("ok", "not_modified"):
            for rec in parse_srp(res.text, res, "en", site, max_desc):
                if rec["kind"] == "listing":
                    rec["purpose"] = "daily_rent"
                yield rec

    blog = cfg["blog"]
    for page in range(1, blog["max_pages"] + 1):
        res = client.get(
            f"{blog['api']}?per_page={blog['per_page']}&page={page}&_fields=id,date_gmt,link,title,content"
        )
        if res.outcome not in ("ok", "not_modified"):
            break
        try:
            posts = json.loads(res.text)
        except json.JSONDecodeError:
            break
        if not posts:
            break
        yield from parse_blog_posts(posts, res, blog["max_chars"])


def _stratified_targets(advertised: set[str], budget: dict, lang: str, site: str) -> list[str]:
    """Round-robin over cities so every city gets coverage before any city gets depth."""
    by_city: dict[str, list[str]] = {c: [] for c in budget["cities"]}
    if lang == "en":
        for city in budget["cities"]:
            for cat in budget["categories"]:
                url = f"{site}/en/{cat}-in-{city}"
                if url in advertised:
                    by_city[city].append(url)
    else:
        # Arabic slugs are Arabic words; keep city-level hub pages that the sitemap lists.
        ar_cities = budget.get("cities_ar", {})
        from urllib.parse import unquote

        for url in sorted(advertised, key=len):
            path = unquote(urlparse(url).path)
            for city, ar in ar_cities.items():
                if city in by_city and path.endswith(ar.replace(" ", "-")) and path.count("-") <= 5:
                    by_city[city].append(url)
    out: list[str] = []
    depth = max((len(v) for v in by_city.values()), default=0)
    for i in range(depth):
        for city in budget["cities"]:
            if i < len(by_city[city]):
                out.append(by_city[city][i])
    return out
