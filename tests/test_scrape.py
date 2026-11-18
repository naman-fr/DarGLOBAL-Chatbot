from concierge.scrape import darglobal, wasalt
from concierge.scrape.extract import detect_lang, html_to_text, to_number
from concierge.scrape.http import FetchResult
from concierge.scrape.run import validate


def res(url="https://example.org/en/apartments-for-sale-in-riyadh"):
    return FetchResult(url, 200, fetched_at="2026-11-18T10:00:00+00:00", content_hash="abc")


def test_srp_parser_extracts_listings_faq_and_guide(fixture_text):
    recs = list(wasalt.parse_srp(fixture_text("srp.html"), res(), "en", "https://example.org", 600))
    kinds = [r["kind"] for r in recs]
    assert kinds.count("listing") == 1  # the listing without a slug is skipped, not half-stored
    assert kinds.count("faq") == 1 and kinds.count("article") == 1
    listing = next(r for r in recs if r["kind"] == "listing")
    assert listing["price"] == 704000 and listing["currency"] == "SAR"
    assert listing["bedrooms"] == 2 and listing["bathrooms"] == 2  # Arabic-Indic digit normalised
    assert listing["area_sqm"] == 159
    assert listing["url"] == "https://example.org/en/property/sale/apartment-159-sqm-with-2-bedrooms-5893390"
    assert listing["rega_ad_license"] == "7201095070"


def test_listing_never_stores_personal_names(fixture_text):
    recs = list(wasalt.parse_srp(fixture_text("srp.html"), res(), "en", "https://example.org", 600))
    assert "PRIVATE PERSON" not in str(recs)


def test_parsed_records_pass_schema(fixture_text):
    for raw in wasalt.parse_srp(fixture_text("srp.html"), res(), "en", "https://example.org", 600):
        rec, err = validate(raw)
        assert err is None, err
        assert rec["prov"]["source_name"] == "example.org"


def test_invalid_record_is_quarantined_with_reason():
    rec, err = validate(
        {
            "kind": "faq",
            "id": "x",
            "brand": "wasalt",
            "title": "Q?",
            "question": "Q?",
            "answer": "",
            "prov": {"source_url": "https://example.org", "fetched_at": "2026-11-18"},
        }
    )
    assert rec is None and "answer" in err


def test_unknown_brand_is_rejected():
    rec, err = validate(
        {
            "kind": "corporate",
            "id": "x",
            "brand": "someone-else",
            "title": "About",
            "text": "x" * 40,
            "prov": {"source_url": "https://example.org", "fetched_at": "2026-11-18"},
        }
    )
    assert rec is None and "brand" in err


def test_wiki_parser_sections_infobox_and_skips_references(fixture_text):
    url = "https://en.example.org/wiki/DarGlobal"
    recs = list(darglobal.parse_wiki_html(fixture_text("wiki.html"), url, res(url), "en"))
    titles = [r["title"] for r in recs]
    assert titles[0].endswith("key facts")
    assert any(t.endswith("History") for t in titles)
    assert not any("References" in t for t in titles)
    history = next(r for r in recs if r["title"].endswith("History"))
    assert "[2]" not in history["text"] and "better source needed" not in history["text"]
    facts = recs[0]["text"]
    assert "Headquarters: Riyadh, Saudi Arabia" in facts


def test_brand_attribution_for_parent_pages():
    mentions = ["DarGlobal", "Dar Global", "Trump"]
    assert darglobal.attribute_brand("Trump Plaza Jeddah anchors the masterplan", "daralarkan", mentions) == "darglobal"
    assert darglobal.attribute_brand("A residential community in north Riyadh", "daralarkan", mentions) == "daralarkan"


def test_rns_body_drops_mirror_summary_and_footer():
    text = "Machine summary of the announcement.\nDar Global PLC\n24 September 2026\nHighlights\n- Revenue up.\nThis information is provided by RNS\nSite chrome"
    body, published = darglobal.rns_body(text, ["This information is provided by RNS"])
    assert body.startswith("Dar Global PLC") and "Machine summary" not in body
    assert "Site chrome" not in body and published == "24 September 2026"


def test_extract_helpers():
    assert to_number("١٢٣٬٤٥٦") == 123456
    assert to_number("850,000 SAR") == 850000
    assert detect_lang("شقة للبيع في الرياض") == "ar"
    assert detect_lang("Apartment for sale") == "en"
    assert html_to_text("<h2>Title</h2><p>Body</p><ul><li>One</li></ul>") == "## Title\nBody\n- One"
