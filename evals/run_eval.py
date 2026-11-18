"""Offline evaluation gates (router, retrieval, refusal) plus an optional live run against a URL.

python evals/run_eval.py                 # deterministic gates + retrieval experiments -> docs/evals.{json,md}
python evals/run_eval.py --live URL      # end-to-end answers from a running deployment
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from concierge import config  # noqa: E402
from concierge.chat.router import Router  # noqa: E402
from concierge.kb.store import KB  # noqa: E402

EVAL_DIR = ROOT / "evals"


def generate_candidates(kb: KB) -> list[dict]:
    """KB-derived questions: one per distinct source document, in both languages where it makes sense.
    Provenance is recorded so a reviewer can tell generated items from curated ones."""
    con = kb._con()
    brands = config.load("sources")["brands"]
    out = []
    rows = con.execute(
        "SELECT brand, MIN(title) AS title, url, json_extract(body,'$.section') AS section FROM records "
        "WHERE kind IN ('article','corporate') GROUP BY url"
    ).fetchall()
    for r in rows:
        title = r["title"].split(" — ")[0].split(" | ")[0]
        name = brands[r["brand"]]["name"].split(" (")[0]
        tail = r["url"].rstrip("/").split("/")[-2 if "announcement" in r["url"] else -1]
        if r["section"] == "regulatory announcement":
            qs = [
                (f"What are the highlights of {name}'s announcement '{title}'?", "en"),
                (f"ما أبرز ما جاء في إعلان {brands[r['brand']].get('name_ar', name)} '{title}'؟", "ar"),
            ]
        else:
            qs = [(f"Tell me about {title}", "en")]
        for q, lang in qs:
            out.append(
                {
                    "q": q,
                    "lang": lang,
                    "intent": "rag",
                    "brand": r["brand"],
                    "expect_url": tail,
                    "type": "generated",
                    "provenance": f"generated from {r['url']}",
                }
            )
    return out


def load_golden(kb: KB) -> list[dict]:
    curated = [json.loads(x) for x in (EVAL_DIR / "golden_curated.jsonl").read_text(encoding="utf-8").splitlines() if x]
    for c in curated:
        c.setdefault("provenance", "curated")
    generated = generate_candidates(kb)
    (EVAL_DIR / "golden_generated.jsonl").write_text(
        "\n".join(json.dumps(g, ensure_ascii=False) for g in generated) + "\n", encoding="utf-8"
    )
    return curated + generated


def rank_of(hits, expect: str) -> int | None:
    for i, h in enumerate(hits):
        if expect.lower() in h.url.lower():
            return i
    return None


def retrieval_metrics(kb: KB, router: Router, items: list[dict], k: int) -> dict:
    per_lang: dict[str, list[int | None]] = defaultdict(list)
    for it in items:
        route = router.route(it["q"])
        brands = [b for b in route.brands if b in config.load("sources")["brands"]]
        hits = kb.search(
            it["q"], k=k, brand=brands[0] if len(brands) == 1 else None, graph=kb.graph_context(it["q"])
        )
        per_lang[it["lang"]].append(rank_of(hits, it["expect_url"]))
    out = {}
    for lang, ranks in list(per_lang.items()) + [("all", [r for v in per_lang.values() for r in v])]:
        n = len(ranks) or 1
        out[lang] = {
            "n": len(ranks),
            "hit@1": round(sum(r is not None and r < 1 for r in ranks) / n, 3),
            "hit@3": round(sum(r is not None and r < 3 for r in ranks) / n, 3),
            f"hit@{k}": round(sum(r is not None for r in ranks) / n, 3),
            "mrr": round(sum(1 / (r + 1) for r in ranks if r is not None) / n, 3),
            f"ndcg@{k}": round(sum(1 / math.log2(r + 2) for r in ranks if r is not None) / n, 3),
        }
    return out


def guardrail_suite() -> dict:
    """Input-rail cases (EN/AR): expected one of block:unsafe, block:encoded, injection, advice, pass."""
    from concierge.chat import guardrails

    router = Router()
    rows = [json.loads(line) for line in (ROOT / "evals" / "guardrail_cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    ok = 0
    misses = []
    for r in rows:
        text = guardrails.normalise(r["q"])
        v = guardrails.check_input(text)
        if v.block:
            got = f"block:{v.block}"
        elif router.route(text).intent == "injection":
            got = "injection"
        else:
            got = "advice" if v.advice else "pass"
        ok += got == r["expect"]
        if got != r["expect"]:
            misses.append({"q": r["q"], "expect": r["expect"], "got": got})
    return {"n": len(rows), "accuracy": round(ok / len(rows), 3), "misses": misses}


def offline(write: bool = True) -> dict:
    kb, router = KB(), Router()
    golden = load_golden(kb)
    started = time.monotonic()

    intent_ok = [router.route(g["q"]).intent == g["intent"] for g in golden]
    lang_ok = [router.route(g["q"]).lang == g["lang"] for g in golden]
    retr_items = [g for g in golden if g.get("expect_url") and g["intent"] == "rag"]

    # Refusal correctness: in-scope questions must clear the confidence gate, out-of-scope must not.
    refuse_rows = []
    for g in golden:
        if g["intent"] != "rag":
            continue
        route = router.route(g["q"])
        graph = kb.graph_context(g["q"])
        hits = kb.search(g["q"], graph=graph)
        on_topic = (
            bool(route.brands)
            or any(w in g["q"].lower() for w in router.cfg["domain_words"])
            or kb.mentions_known_entity(g["q"])
            or bool(graph.seeds)
        )
        answered = kb.is_confident(hits, on_topic)
        refuse_rows.append((bool(g.get("refuse")), not answered))
    refusal_acc = sum(a == b for a, b in refuse_rows) / len(refuse_rows)
    false_refusals = sum(1 for should, did in refuse_rows if did and not should)
    missed_refusals = sum(1 for should, did in refuse_rows if should and not did)

    # Retrieval experiments: each retriever alone, hybrid without the graph, and hybrid + graph (GraphRAG).
    cfg = kb.cfg
    base_w = dict(cfg["weights"])
    experiments = {}
    for name, w in {
        "bm25_only": {"bm25": 1.0, "dense": 0.0, "graph": 0.0},
        "dense_only": {"bm25": 0.0, "dense": 1.0, "graph": 0.0},
        "graph_only": {"bm25": 0.0, "dense": 0.0, "graph": 1.0},
        "hybrid_no_graph": {"bm25": 1.0, "dense": 1.2, "graph": 0.0},
        "hybrid+graph_0.2": {"bm25": 1.0, "dense": 1.2, "graph": 0.2},
        "hybrid+graph_0.4": {"bm25": 1.0, "dense": 1.2, "graph": 0.4},
        "hybrid+graph_1.0": {"bm25": 1.0, "dense": 1.2, "graph": 1.0},
    }.items():
        cfg["weights"] = w
        experiments[name] = retrieval_metrics(kb, router, retr_items, cfg["top_k"])["all"]
    cfg["weights"] = base_w
    current = retrieval_metrics(kb, router, retr_items, cfg["top_k"])

    guard_rows = guardrail_suite()
    gates = config.load("evals")
    report = {
        "kb_version": kb.version,
        "golden_items": len(golden),
        "curated": sum(g["provenance"] == "curated" for g in golden),
        "generated": sum(g["provenance"] != "curated" for g in golden),
        "router_accuracy": round(sum(intent_ok) / len(intent_ok), 3),
        "language_accuracy": round(sum(lang_ok) / len(lang_ok), 3),
        "refusal_accuracy": round(refusal_acc, 3),
        "false_refusals": false_refusals,
        "missed_refusals": missed_refusals,
        "retrieval": current,
        "experiments": experiments,
        "guardrails": guard_rows,
        "graph": kb.manifest.get("graph"),
        "router_misses": [g["q"] for g, ok in zip(golden, intent_ok, strict=True) if not ok][:20],
        "seconds": round(time.monotonic() - started, 1),
    }
    report["gates"] = {
        "router_accuracy": report["router_accuracy"] >= gates["router_accuracy_min"],
        "hit@k": current["all"][f"hit@{cfg['top_k']}"] >= gates["retrieval_hit_at_k_min"],
        "mrr": current["all"]["mrr"] >= gates["retrieval_mrr_min"],
        "refusal_accuracy": report["refusal_accuracy"] >= gates["refusal_accuracy_min"],
        "guardrail_accuracy": guard_rows["accuracy"] >= gates.get("guardrail_accuracy_min", 0.9),
    }
    if write:
        (ROOT / "docs" / "evals.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        write_markdown(report, cfg["top_k"])
    return report


def write_markdown(r: dict, k: int) -> None:
    lines = [
        "# Evaluation report",
        "",
        f"KB `{r['kb_version']}`. Golden set: {r['golden_items']} questions "
        f"({r['curated']} curated, {r['generated']} generated from the KB; see `evals/`).",
        "",
        "## Deterministic gates (run in CI)",
        "",
        "| Metric | Value | Gate |",
        "|---|---|---|",
        f"| Router intent accuracy | {r['router_accuracy']} | {'pass' if r['gates']['router_accuracy'] else 'FAIL'} |",
        f"| Language detection accuracy | {r['language_accuracy']} | — |",
        f"| Refusal correctness (answer vs refuse) | {r['refusal_accuracy']} | {'pass' if r['gates']['refusal_accuracy'] else 'FAIL'} |",
        f"| False refusals / missed refusals | {r['false_refusals']} / {r['missed_refusals']} | — |",
        f"| Retrieval hit@{k} (all) | {r['retrieval']['all'][f'hit@{k}']} | {'pass' if r['gates']['hit@k'] else 'FAIL'} |",
        f"| Retrieval MRR (all) | {r['retrieval']['all']['mrr']} | {'pass' if r['gates']['mrr'] else 'FAIL'} |",
        f"| Input guardrail accuracy ({r['guardrails']['n']} EN/AR cases) | {r['guardrails']['accuracy']} | "
        f"{'pass' if r['gates']['guardrail_accuracy'] else 'FAIL'} |",
        "",
        "## Knowledge graph",
        "",
        f"{r['graph']['entities']} entities and {r['graph']['edges']} evidence-backed edges "
        f"({', '.join(f'{v} {t}' for t, v in r['graph']['entity_types'].items())}). Retrieval runs BM25, dense and "
        "graph expansion and fuses them with reciprocal-rank fusion; the experiments below isolate the graph's effect.",
        "",
        "## Retrieval by language",
        "",
        f"| Lang | n | hit@1 | hit@3 | hit@{k} | MRR | nDCG@{k} |",
        "|---|---|---|---|---|---|---|",
    ]
    for lang, m in r["retrieval"].items():
        lines.append(
            f"| {lang} | {m['n']} | {m['hit@1']} | {m['hit@3']} | {m[f'hit@{k}']} | {m['mrr']} | {m[f'ndcg@{k}']} |"
        )
    lines += [
        "",
        "## Retrieval experiments (all languages)",
        "",
        f"| Setting | hit@1 | hit@3 | hit@{k} | MRR |",
        "|---|---|---|---|---|",
    ]
    for name, m in r["experiments"].items():
        lines.append(f"| {name} | {m['hit@1']} | {m['hit@3']} | {m[f'hit@{k}']} | {m['mrr']} |")
    if r.get("live"):
        lv = r["live"]
        lines += [
            "",
            "## Live run",
            "",
            f"Target: `{lv['url']}`, {lv['n']} questions.",
            "",
            "| Metric | Value |",
            "|---|---|",
        ]
        for key in (
            "answered_with_citation",
            "refusal_correct",
            "unverified_number_rate",
            "llm_served_rate",
            "latency_p50_s",
            "latency_p95_s",
        ):
            lines.append(f"| {key} | {lv[key]} |")
        lines.append(f"| models served | {', '.join(lv['models'])} |")
    if r["router_misses"]:
        lines += ["", "## Router misses", ""] + [f"- {q}" for q in r["router_misses"]]
    (ROOT / "docs" / "evals.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def ask(base: str, q: str) -> tuple[dict, float]:
    req = urllib.request.Request(
        f"{base}/api/chat",
        data=json.dumps({"message": q, "session_id": "eval-run-0001"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    ev, out = None, {"text": "", "citations": [], "notices": [], "model": None, "intent": None}
    with urllib.request.urlopen(req, timeout=180) as r:  # noqa: S310 - URL is the operator's own deployment
        for raw in r:
            line = raw.decode("utf-8").rstrip("\n")
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                d = json.loads(line[6:])
                if ev == "token":
                    out["text"] += d["text"]
                elif ev == "citations":
                    out["citations"] = d
                elif ev == "notice":
                    out["notices"].append(d["kind"])
                elif ev == "model":
                    out["model"] = d["model"]
                elif ev == "meta":
                    out["intent"] = d["intent"]
    return out, time.monotonic() - started


def live(base: str, limit: int, pause: float) -> dict:
    curated = [json.loads(x) for x in (EVAL_DIR / "golden_curated.jsonl").read_text(encoding="utf-8").splitlines() if x]
    items = [c for c in curated if c["intent"] == "rag"][:limit]
    rows, lat = [], []
    for it in items:
        try:
            res, secs = ask(base.rstrip("/"), it["q"])
        except Exception as exc:  # one failed request should not end the run
            res, secs = {"text": "", "citations": [], "notices": [], "model": None, "error": repr(exc)}, 0
        lat.append(secs)
        refused = not res["citations"]
        rows.append(
            {
                "q": it["q"],
                "should_refuse": bool(it.get("refuse")),
                "refused": refused,
                "unverified": "unverified_numbers" in res["notices"],
                "model": res["model"],
            }
        )
        time.sleep(pause)  # free models are rate limited per minute
    answered = [r for r in rows if not r["should_refuse"]]
    return {
        "url": base,
        "n": len(rows),
        "answered_with_citation": round(sum(not r["refused"] for r in answered) / max(len(answered), 1), 3),
        "refusal_correct": round(sum(r["refused"] == r["should_refuse"] for r in rows) / max(len(rows), 1), 3),
        "unverified_number_rate": round(sum(r["unverified"] for r in rows) / max(len(rows), 1), 3),
        "llm_served_rate": round(
            sum(bool(r["model"]) and r["model"] != "extractive-fallback" for r in rows) / max(len(rows), 1), 3
        ),
        "latency_p50_s": round(statistics.median(lat), 2) if lat else None,
        "latency_p95_s": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 2) if lat else None,
        "models": sorted({r["model"] for r in rows if r["model"]}),
        "rows": rows,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", help="base URL of a running deployment")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--pause", type=float, default=3.5)
    args = ap.parse_args()
    report = offline()
    if args.live:
        report["live"] = live(args.live, args.limit, args.pause)
        (ROOT / "docs" / "evals.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        write_markdown(report, KB().cfg["top_k"])
    summary = {k: v for k, v in report.items() if k not in ("experiments", "live")}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if report.get("live"):
        print(json.dumps({k: v for k, v in report["live"].items() if k != "rows"}, indent=2))
    sys.exit(0 if all(report["gates"].values()) else 1)
