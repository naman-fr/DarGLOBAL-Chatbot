"""Versioned record schemas. Anything that fails validation goes to quarantine with the reason."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

SCHEMA_VERSION = "1.0"
PARSER_VERSION = "1.0"

Lang = Literal["en", "ar"]


class Provenance(BaseModel):
    source_url: HttpUrl
    canonical_url: HttpUrl | None = None
    fetched_at: str
    http_status: int = 200
    content_hash: str = ""
    parser_version: str = PARSER_VERSION
    language: Lang = "en"
    robots_allowed: bool = True
    source_name: str = ""  # host or channel the record came from


class Record(BaseModel):
    schema_version: str = SCHEMA_VERSION
    kind: str
    id: str
    brand: str

    @field_validator("brand")
    @classmethod
    def known_brand(cls, v: str) -> str:
        from concierge import config

        if v not in config.load("sources")["brands"]:
            raise ValueError(f"unknown brand {v!r}")
        return v

    title: str = Field(min_length=2)
    prov: Provenance


class Listing(Record):
    kind: Literal["listing"] = "listing"
    listing_id: int
    purpose: Literal["sale", "rent", "daily_rent"]
    property_type: str
    main_type: str = ""
    price: float | None = None
    currency: str | None = None
    rent_frequency: str | None = None
    city: str = ""
    district: str = ""
    region_zone: str = ""
    bedrooms: int | None = None
    bathrooms: int | None = None
    area_sqm: float | None = None
    rega_ad_license: str | None = None
    is_rega_verified: bool = False
    published_at: str | None = None
    updated_at: str | None = None
    description: str = ""
    url: HttpUrl

    @field_validator("price")
    @classmethod
    def positive_price(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            return None
        return v


class Article(Record):
    """Blog post, news item or long-form page section."""

    kind: Literal["article"] = "article"
    text: str = Field(min_length=40)
    section: str = ""
    published_at: str | None = None
    tags: list[str] = []


class FAQ(Record):
    kind: Literal["faq"] = "faq"
    question: str = Field(min_length=5)
    answer: str = Field(min_length=5)
    topic: str = ""


class CorporateFact(Record):
    kind: Literal["corporate"] = "corporate"
    text: str = Field(min_length=20)
    section: str = ""


class Project(Record):
    kind: Literal["project"] = "project"
    name: str
    country: str = ""
    city: str = ""
    partner_brand: str = ""
    project_type: str = ""
    status: str = ""
    description: str = ""


MODELS: dict[str, type[Record]] = {
    "listing": Listing,
    "article": Article,
    "faq": FAQ,
    "corporate": CorporateFact,
    "project": Project,
}
