"""Knowledge graph for GraphRAG: entity extraction, typed co-occurrence edges with evidence, and
query-time expansion.

Build time: every indexed chunk is scanned for the entities declared in config/graph.yaml. Two entities
named in the same sentence get an edge labelled by their type pair ("project|place" -> "located in"),
and the edge keeps the chunk rowids that evidence it. Nothing is inferred beyond the text.

Query time: entities named in the question are the seeds. Their neighbours (optionally filtered by the
type the question asks for: "projects in Oman") pull in the chunks that evidence those edges, which
become a third ranked list in reciprocal-rank fusion next to BM25 and dense retrieval. The edges whose
evidence made it into the final context are handed to the model as relation facts with citations.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from concierge import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (id TEXT PRIMARY KEY, type TEXT, name TEXT, name_ar TEXT, brand TEXT,
                                     mentions INTEGER, chunks INTEGER);
CREATE TABLE IF NOT EXISTS edges (a TEXT, b TEXT, label TEXT, weight INTEGER, evidence TEXT, PRIMARY KEY (a, b));
CREATE TABLE IF NOT EXISTS chunk_entities (rowid INTEGER, entity_id TEXT, n INTEGER, PRIMARY KEY (rowid, entity_id));
CREATE INDEX IF NOT EXISTS chunk_entities_by_entity ON chunk_entities (entity_id);
"""

_SENTENCE = re.compile(r"(?<=[.!?؟])\s+|\n+")
_ARABIC = re.compile(r"[؀-ۿ]")


class EntityMatcher:
    """One compiled alternation over every alias, longest first, so 'London Stock Exchange' wins over
    'London' and a matched span is never counted twice."""

    def __init__(self, entities: list[dict]) -> None:
        self.by_alias: dict[str, str] = {}
        for e in entities:
            for alias in {e["name"], *(e.get("aliases") or []), *([e["name_ar"]] if e.get("name_ar") else [])}:
                self.by_alias.setdefault(alias.lower(), e["id"])
        latin = sorted((a for a in self.by_alias if not _ARABIC.search(a)), key=len, reverse=True)
        arabic = sorted((a for a in self.by_alias if _ARABIC.search(a)), key=len, reverse=True)
        parts = []
        if latin:
            # not followed by a hyphen: "London-listed" is an attribute, not a place mention
            parts.append(r"(?<![\w-])(" + "|".join(map(re.escape, latin)) + r")(?![\w-])")
        if arabic:
            # Arabic clitics (wa-, bi-, li-, fa-) attach to the following word
            parts.append(r"(?<!\w)[وبلف]?(" + "|".join(map(re.escape, arabic)) + r")(?!\w)")
        self.rx = re.compile("|".join(parts), re.I) if parts else None

    def find_spans(self, text: str) -> list[tuple[str, int]]:
        if not self.rx or not text:
            return []
        out = []
        for m in self.rx.finditer(text):
            alias = next(g for g in m.groups() if g)
            eid = self.by_alias.get(alias.lower())
            if eid:
                out.append((eid, m.start()))
        return out

    def find(self, text: str) -> list[str]:
        return [eid for eid, _ in self.find_spans(text)]


def _cfg() -> dict:
    return config.load("graph")


def _pair_label(ta: str, tb: str, relations: dict) -> str | None:
    return relations.get(f"{ta}|{tb}") or relations.get(f"{tb}|{ta}")


def _nearest(a: str, b: str, pos: dict[str, int], entities: dict) -> bool:
    """True when b is the closest entity of its type to a, or a the closest of its type to b."""

    def closest(src: str, etype: str) -> str | None:
        cands = [e for e in pos if e != src and entities[e]["type"] == etype]
        return min(cands, key=lambda e: abs(pos[e] - pos[src])) if cands else None

    return closest(a, entities[b]["type"]) == b and closest(b, entities[a]["type"]) == a


def build_graph(con: sqlite3.Connection, chunks: list[dict]) -> dict:
    """chunks: dicts with rowid, title, text. Writes entities/edges/chunk_entities; returns stats."""
    cfg = _cfg()
    con.executescript(SCHEMA)
    if not cfg.get("enabled", True):
        return {"entities": 0, "edges": 0}
    entities = {e["id"]: e for e in cfg["entities"]}
    matcher = EntityMatcher(cfg["entities"])
    relations = cfg["relations"]
    cap = cfg["max_evidence_per_edge"]
    nearest_only = set(cfg.get("nearest_only", []))

    mentions: Counter = Counter()
    chunk_count: Counter = Counter()
    edges: dict[tuple[str, str], dict] = {}
    for c in chunks:
        per_chunk: Counter = Counter()
        for sentence in _SENTENCE.split(f"{c['title']}.\n{c['text']}"):
            spans = matcher.find_spans(sentence)
            pos: dict[str, int] = {}
            for eid, at in spans:
                pos.setdefault(eid, at)
            found = list(pos)
            per_chunk.update(eid for eid, _ in spans)
            for i, a in enumerate(found):
                for b in found[i + 1 :]:
                    label = _pair_label(entities[a]["type"], entities[b]["type"], relations)
                    if not label:
                        continue
                    if label in nearest_only and not _nearest(a, b, pos, entities):
                        continue  # list sentences ("projects in Dubai, Oman and Jeddah") would cross-link
                    key = tuple(sorted((a, b)))
                    e = edges.setdefault(key, {"label": label, "weight": 0, "evidence": []})
                    e["weight"] += 1
                    if c["rowid"] not in e["evidence"] and len(e["evidence"]) < cap:
                        e["evidence"].append(c["rowid"])
        for eid, n in per_chunk.items():
            con.execute("INSERT INTO chunk_entities VALUES (?,?,?)", (c["rowid"], eid, n))
            mentions[eid] += n
            chunk_count[eid] += 1

    for eid, e in entities.items():
        if mentions[eid]:
            con.execute(
                "INSERT INTO entities VALUES (?,?,?,?,?,?,?)",
                (eid, e["type"], e["name"], e.get("name_ar"), e.get("brand"), mentions[eid], chunk_count[eid]),
            )
    kept = 0
    for (a, b), e in edges.items():
        if e["weight"] >= cfg["min_edge_weight"]:
            con.execute(
                "INSERT INTO edges VALUES (?,?,?,?,?)", (a, b, e["label"], e["weight"], json.dumps(e["evidence"]))
            )
            kept += 1
    return {
        "entities": sum(1 for v in mentions.values() if v),
        "edges": kept,
        "entity_types": dict(Counter(entities[e]["type"] for e in mentions if mentions[e])),
    }


@dataclass
class Fact:
    a: str
    a_name: str
    a_type: str
    label: str
    b: str
    b_name: str
    b_type: str
    weight: int
    cites: list[int] = field(default_factory=list)


@dataclass
class GraphContext:
    seeds: list[dict] = field(default_factory=list)  # entities named in the question
    neighbours: list[dict] = field(default_factory=list)  # one-hop expansion, ranked
    chunk_ranking: list[int] = field(default_factory=list)  # rowids, best first
    wanted_type: str | None = None


class GraphIndex:
    """Read side, loaded once per process. The graph is small (tens of nodes, hundreds of edges)."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self.cfg = _cfg()
        self.entities: dict[str, dict] = {}
        self.adj: dict[str, list[tuple[str, str, int, list[int]]]] = defaultdict(list)
        self.chunk_entities: dict[int, set[str]] = defaultdict(set)
        self.entity_chunks: dict[str, list[tuple[int, int]]] = defaultdict(list)
        try:
            for r in con.execute("SELECT id, type, name, name_ar, brand, mentions, chunks FROM entities"):
                self.entities[r[0]] = {
                    "id": r[0],
                    "type": r[1],
                    "name": r[2],
                    "name_ar": r[3],
                    "brand": r[4],
                    "mentions": r[5],
                    "chunks": r[6],
                }
            for a, b, label, w, ev in con.execute("SELECT a, b, label, weight, evidence FROM edges"):
                evidence = json.loads(ev)
                self.adj[a].append((b, label, w, evidence))
                self.adj[b].append((a, label, w, evidence))
            for rowid, eid, n in con.execute("SELECT rowid, entity_id, n FROM chunk_entities"):
                self.chunk_entities[rowid].add(eid)
                self.entity_chunks[eid].append((rowid, n))
        except sqlite3.OperationalError:  # a KB built before the graph existed
            self.entities = {}
        # Match against every configured alias, but only keep entities the corpus actually mentions.
        self.matcher = EntityMatcher([e for e in self.cfg.get("entities", []) if e["id"] in self.entities])

    @property
    def enabled(self) -> bool:
        return bool(self.entities) and self.cfg.get("enabled", True)

    def name(self, eid: str, lang: str = "en") -> str:
        e = self.entities[eid]
        return (e.get("name_ar") if lang == "ar" else None) or e["name"]

    def wanted_type(self, text: str) -> str | None:
        low = text.lower()
        for etype, cues in self.cfg.get("type_cues", {}).items():
            for cue in cues:
                if _ARABIC.search(cue):
                    if cue in low:
                        return etype
                elif re.search(rf"(?<![a-z]){re.escape(cue)}(?![a-z])", low):
                    return etype
        return None

    def context(self, text: str, brand: str | None = None) -> GraphContext:
        ctx = GraphContext()
        if not self.enabled:
            return ctx
        seeds = list(dict.fromkeys(self.matcher.find(text)))[: self.cfg["max_seed_entities"]]
        if not seeds:
            return ctx
        ctx.wanted_type = self.wanted_type(text)
        ctx.seeds = [self.entities[s] for s in seeds]
        seed_set = set(seeds)

        # One-hop neighbours, scored by edge weight; the type the question asks for goes first.
        nb: dict[str, float] = defaultdict(float)
        nb_evidence: dict[str, list[int]] = defaultdict(list)
        for s in seeds:
            for other, _label, w, ev in self.adj.get(s, []):
                if other in seed_set:
                    continue
                nb[other] += w
                nb_evidence[other].extend(ev)

        def nb_key(eid: str) -> tuple:
            return (self.entities[eid]["type"] != ctx.wanted_type, -nb[eid])

        ranked = sorted(nb, key=nb_key)[: self.cfg["max_neighbours"]]
        ctx.neighbours = [{**self.entities[e], "score": nb[e]} for e in ranked]

        # Chunk ranking: chunks that name several seeds, then chunks evidencing seed->neighbour edges
        # (weighted by the neighbour's rank), then other chunks naming a seed.
        score: dict[int, float] = defaultdict(float)
        for s in seeds:
            for rowid, n in self.entity_chunks.get(s, []):
                score[rowid] += 1.0 + 0.1 * min(n, 5)
        for i, e in enumerate(ranked):
            bonus = 2.0 / (1 + i) + (1.0 if self.entities[e]["type"] == ctx.wanted_type else 0.0)
            for rowid in nb_evidence[e]:
                score[rowid] += bonus
        for rowid in list(score):
            present = self.chunk_entities.get(rowid, set())
            score[rowid] += 1.5 * max(0, len(present & seed_set) - 1)
        ctx.chunk_ranking = [r for r, _ in sorted(score.items(), key=lambda kv: -kv[1])][: self.cfg["max_graph_chunks"]]
        return ctx

    def facts(self, ctx: GraphContext, hit_rowids: list[int], lang: str = "en") -> list[Fact]:
        """Edges touching a seed whose evidence is among the final hits, with those hits' numbers."""
        if not ctx.seeds:
            return []
        pos = {rowid: i + 1 for i, rowid in enumerate(hit_rowids)}
        out: dict[tuple[str, str], Fact] = {}
        keep = {s["id"] for s in ctx.seeds} | {n["id"] for n in ctx.neighbours}
        for s in ctx.seeds:
            for other, label, w, ev in self.adj.get(s["id"], []):
                if other not in keep:
                    continue
                cites = sorted({pos[r] for r in ev if r in pos})
                if not cites:
                    continue
                a, b = s["id"], other
                # read naturally: "AIDA located in Oman", not "Oman located in AIDA"
                order = ["company", "person", "partner", "project", "exchange", "place"]
                ta, tb = self.entities[a]["type"], self.entities[b]["type"]
                if order.index(ta) > order.index(tb):
                    a, b, ta, tb = b, a, tb, ta
                key = (a, b)
                if key not in out:
                    out[key] = Fact(a, self.name(a, lang), ta, label, b, self.name(b, lang), tb, w, cites)
        facts = sorted(out.values(), key=lambda f: (-len(f.cites), -f.weight))
        return facts[: self.cfg["max_facts_in_prompt"]]

    def subgraph(self, limit_nodes: int = 60) -> dict:
        """Nodes and edges for the explorer view: the most-mentioned entities and the edges among them."""
        top = sorted(self.entities.values(), key=lambda e: -e["mentions"])[:limit_nodes]
        ids = {e["id"] for e in top}
        edges = []
        seen = set()
        for a in ids:
            for b, label, w, _ev in self.adj.get(a, []):
                if b in ids and (b, a) not in seen:
                    seen.add((a, b))
                    edges.append({"a": a, "b": b, "label": label, "weight": w})
        return {"nodes": top, "edges": edges}
