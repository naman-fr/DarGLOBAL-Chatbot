"""YAML config, prompts and locale loading. Everything tunable comes through here."""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(os.environ.get("CONCIERGE_ROOT", Path(__file__).resolve().parents[2]))


def path(*parts: str) -> Path:
    return ROOT.joinpath(*parts)


@cache
def load(name: str) -> dict[str, Any]:
    with path("config", f"{name}.yaml").open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@cache
def prompt(name: str) -> str:
    return path("prompts", f"{name}.md").read_text(encoding="utf-8")


@cache
def locale(lang: str) -> dict[str, Any]:
    p = path("locales", f"{lang}.json")
    if not p.exists():
        p = path("locales", "en.json")
    return json.loads(p.read_text(encoding="utf-8"))


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is not None:
        # docker --env-file and some dashboards keep quotes literally: KEY="abc" arrives as '"abc"'
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
    return value if value not in (None, "") else default
