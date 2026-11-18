"""Brand names, site URLs and model IDs belong in config/, prompts/ and locales/, never in src/."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _forbidden() -> list[str]:
    sources = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    llm = yaml.safe_load((ROOT / "config" / "llm.yaml").read_text(encoding="utf-8"))
    words: set[str] = set()
    for brand in sources["brands"].values():
        for key in ("name", "name_ar"):
            if brand.get(key):
                words.add(brand[key].split(" (")[0])
        words.add(re.sub(r"^https?://(www\.)?", "", brand["official_site"]).rstrip("/"))
    text = (ROOT / "config" / "sources.yaml").read_text(encoding="utf-8")
    words.update(h for h in re.findall(r"https?://([\w.-]+)", text))
    words.update(llm["static_fallback"])
    words.add(llm["last_resort"])
    words.add(llm["base_url"].split("//", 1)[1].split("/")[0])
    return sorted(w for w in words if len(w) > 3)


def test_no_brand_names_urls_or_model_ids_in_src():
    forbidden = _forbidden()
    hits = []
    for path in (ROOT / "src").rglob("*.py"):
        content = path.read_text(encoding="utf-8")
        for word in forbidden:
            if word in content:
                hits.append(f"{path.relative_to(ROOT)}: {word}")
    assert not hits, "hardcoded literals found:\n" + "\n".join(hits)


def test_ui_has_no_brand_literals():
    forbidden = _forbidden()
    for path in (p for p in (ROOT / "web").rglob("*") if p.is_file() and p.suffix in (".html", ".js", ".css")):
        content = path.read_text(encoding="utf-8")
        assert not [w for w in forbidden if w in content], path.name
