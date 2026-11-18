"""Extractor ladder helpers: embedded state, JSON-LD, then generic text as the fallback."""

from __future__ import annotations

import html as htmllib
import json
import re
from collections.abc import Iterator
from typing import Any

from selectolax.lexbor import LexborHTMLParser as HTMLParser

ARABIC_INDIC = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_WS = re.compile(r"[ \t ]+")
_NL = re.compile(r"\n{3,}")
_ARABIC = re.compile(r"[؀-ۿ]")


def next_data(html: str) -> dict[str, Any] | None:
    m = _NEXT_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def json_ld(html: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    tree = HTMLParser(html)
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        out.extend(i for i in items if isinstance(i, dict))
    return out


def html_to_text(fragment: str) -> str:
    if not fragment:
        return ""
    tree = HTMLParser(f"<div>{fragment}</div>")
    for bad in tree.css("script, style, noscript, iframe, svg"):
        bad.decompose()
    for br in tree.css("br"):
        br.replace_with("\n")
    blocks = []
    for node in tree.css("h1, h2, h3, h4, p, li, td, th, blockquote"):
        t = node.text(separator=" ", strip=True)
        if t:
            prefix = "## " if node.tag in ("h1", "h2", "h3", "h4") else ("- " if node.tag == "li" else "")
            blocks.append(prefix + t)
    text = "\n".join(blocks) if blocks else tree.text(separator=" ", strip=True)
    return clean(text)


def clean(text: str) -> str:
    text = htmllib.unescape(text or "")
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _NL.sub("\n\n", text).strip()


def page_text(html: str, url: str = "") -> str:
    """Generic fallback for pages without embedded data."""
    try:
        import trafilatura

        out = trafilatura.extract(html, url=url, include_tables=True, include_comments=False, favor_recall=True)
        if out:
            return clean(out)
    except Exception as exc:  # trafilatura raises a zoo of parser errors on odd markup
        print(f"[extract] trafilatura failed on {url or 'page'}: {exc!r}")
    tree = HTMLParser(html)
    for bad in tree.css("script, style, noscript, nav, footer, header, svg"):
        bad.decompose()
    body = tree.body
    return clean(body.text(separator="\n", strip=True)) if body else ""


def title_of(html: str) -> str:
    tree = HTMLParser(html)
    for sel in ('meta[property="og:title"]', "title", "h1"):
        node = tree.css_first(sel)
        if node is None:
            continue
        val = node.attributes.get("content") if node.tag == "meta" else node.text(strip=True)
        if val:
            return clean(val)
    return ""


def walk_strings(obj: Any, skip_keys: set[str] | None = None) -> Iterator[tuple[str, str]]:
    """Yield (key, string) pairs from nested CMS JSON, skipping media and technical keys."""
    skip = skip_keys or set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in skip:
                continue
            if isinstance(v, str):
                yield k, v
            else:
                yield from walk_strings(v, skip)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_strings(v, skip)


def detect_lang(text: str) -> str:
    if not text:
        return "en"
    arabic = len(_ARABIC.findall(text))
    letters = sum(ch.isalpha() for ch in text) or 1
    return "ar" if arabic / letters > 0.3 else "en"


def to_number(val: Any) -> float | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).translate(ARABIC_INDIC).replace(",", "").replace("٬", "")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None
