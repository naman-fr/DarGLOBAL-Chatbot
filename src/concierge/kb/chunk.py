"""Structure-aware chunking: one chunk per FAQ, paragraph-packed windows for long text."""

from __future__ import annotations


def _windows(text: str, size: int, overlap: int) -> list[str]:
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    out: list[str] = []
    cur = ""
    for p in paras:
        while len(p) > size:  # a single huge paragraph still has to fit
            head, p = p[:size], p[size - overlap :]
            if cur:
                out.append(cur)
                cur = ""
            out.append(head)
        if cur and len(cur) + len(p) + 1 > size:
            out.append(cur)
            cur = cur[-overlap:].split(" ", 1)[-1] if overlap else ""
        cur = f"{cur}\n{p}".strip()
    if cur:
        out.append(cur)
    return out


def chunk_record(r: dict, size: int, overlap: int) -> list[dict]:
    p = r["prov"]
    base = {
        "record_id": r["id"],
        "kind": r["kind"],
        "brand": r["brand"],
        "lang": p["language"],
        "title": r["title"],
        "url": p["source_url"],
        "fetched_at": p["fetched_at"],
        "source_name": p.get("source_name", ""),
    }
    if r["kind"] == "faq":
        texts = [f"Q: {r['question']}\nA: {r['answer']}"]
    elif r["kind"] == "project":
        facts = [
            f"{k.replace('_', ' ')}: {r[k]}"
            for k in ("country", "city", "partner_brand", "project_type", "status")
            if r.get(k)
        ]
        texts = _windows("\n".join([*facts, r.get("description", "")]), size, overlap)
    else:
        texts = _windows(r.get("text", ""), size, overlap)
    return [{**base, "id": f"{r['id']}#{i}", "text": t} for i, t in enumerate(texts) if len(t) > 30]
