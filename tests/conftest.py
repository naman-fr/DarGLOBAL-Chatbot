import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("CONCIERGE_SKIP_KB_LOAD", "1")
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_text():
    return lambda name: (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def fixture_records():
    with (FIXTURES / "records.jsonl").open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.fixture
def lexical_kb(tmp_path, fixture_records, monkeypatch):
    """A KB built from fixture records with embeddings off: fast, offline, deterministic."""
    from concierge import config
    from concierge.kb import build as kb_build

    records = tmp_path / "records.jsonl"
    records.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in fixture_records), encoding="utf-8")
    cfg = dict(config.load("retrieval"))
    cfg.update(records_path=str(records), kb_dir=str(tmp_path / "kb"), embedding={**cfg["embedding"], "enabled": False})
    real_load = config.load

    def fake_load(name):
        return cfg if name == "retrieval" else real_load(name)

    monkeypatch.setattr(config, "load", fake_load)
    kb_build.build(str(tmp_path / "kb"))
    from concierge.kb.store import KB

    return KB(tmp_path / "kb")
