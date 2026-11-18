"""Sentence embeddings with a lean runtime footprint.

The packaged embedding stack ran onnxruntime with its memory arena on and used about 570 MB RSS,
over the 512 MB free-host limit. Here the same ONNX graph is served with the arena off and one
thread, using the HF `tokenizers` library and mean pooling, which is what this sentence-transformers
model was trained with. Documents and queries
are both embedded by this class, so the vectors are always comparable.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np

from concierge import config

MODEL_FILE = "model.onnx"
SPM_FILE = "sentencepiece.bpe.model"
# XLM-R vocabulary layout: SentencePiece ids are shifted by one, with these specials in front.
BOS, PAD, EOS, UNK, OFFSET = 0, 1, 2, 3, 1


def runtime_dir(cfg: dict | None = None) -> Path:
    cfg = cfg or config.load("retrieval")["embedding"]
    return config.path(cfg["runtime_dir"])


def prepare_model(cfg: dict | None = None) -> Path:
    """Download the model, optionally quantize it, and put it with its SentencePiece model in runtime_dir.
    Idempotent."""
    cfg = cfg or config.load("retrieval")["embedding"]
    out = runtime_dir(cfg)
    if (out / MODEL_FILE).exists() and (out / SPM_FILE).exists():
        return out
    from fastembed import TextEmbedding

    cache = config.path(cfg["cache_dir"])
    TextEmbedding(cfg["model"], cache_dir=str(cache))  # downloads the snapshot
    snapshot = next(p.parent for p in cache.rglob("tokenizer.json") if "snapshots" in str(p))
    source = next(snapshot.glob("*.onnx"))
    out.mkdir(parents=True, exist_ok=True)
    if cfg.get("quantize_int8"):
        from onnxruntime.quantization import QuantType, quantize_dynamic

        fp32 = out / "model.fp32.onnx"
        _to_fp32(source, fp32)
        quantize_dynamic(str(fp32), str(out / MODEL_FILE), weight_type=QuantType.QInt8)
        fp32.unlink()
    else:
        shutil.copy(source, out / MODEL_FILE)
    _download(cfg["spm_url"], out / SPM_FILE)
    return out


def _download(url: str, dst: Path) -> None:
    import httpx

    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        with dst.open("wb") as fh:
            for chunk in r.iter_bytes():
                fh.write(chunk)


def _to_fp32(src: Path, dst: Path) -> None:
    """The published graph stores weights in fp16 and casts them at run time. onnxruntime folds those
    casts into fp32 copies at load (about 470 MB), and the int8 quantizer rejects fp16 scales. Turning
    the weights into fp32 first lets the quantizer work and leaves the casts as no-ops."""
    import onnx
    from onnx import numpy_helper

    model = onnx.load(str(src))
    for init in model.graph.initializer:
        if init.data_type == onnx.TensorProto.FLOAT16:
            init.CopyFrom(numpy_helper.from_array(numpy_helper.to_array(init).astype(np.float32), init.name))
    for node in model.graph.node:
        if node.op_type == "Cast":
            for attr in node.attribute:
                if attr.name == "to" and attr.i == onnx.TensorProto.FLOAT16:
                    attr.i = onnx.TensorProto.FLOAT
    for vi in list(model.graph.value_info) + list(model.graph.input) + list(model.graph.output):
        tt = vi.type.tensor_type
        if tt.elem_type == onnx.TensorProto.FLOAT16:
            tt.elem_type = onnx.TensorProto.FLOAT
    onnx.save(model, str(dst))


class Embedder:
    def __init__(self, model_dir: Path | None = None, max_tokens: int = 256) -> None:
        import onnxruntime as ort
        import sentencepiece as spm

        d = model_dir or runtime_dir()
        # The HF tokenizer.json for this 250k-piece vocabulary costs ~300 MB resident; the native
        # SentencePiece model gives identical ids (see tests) in ~40 MB.
        self.sp = spm.SentencePieceProcessor(model_file=str(d / SPM_FILE))
        self.max_tokens = max_tokens
        opts = ort.SessionOptions()
        opts.enable_cpu_mem_arena = False  # the arena keeps peak activations resident forever
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(d / MODEL_FILE), opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}

    def encode(self, texts: list[str]) -> tuple[np.ndarray, np.ndarray]:
        rows = []
        for pieces in self.sp.encode(texts):
            body = [p + OFFSET if p != 0 else UNK for p in pieces][: self.max_tokens - 2]
            rows.append([BOS, *body, EOS])
        width = max(len(r) for r in rows)
        ids = np.full((len(rows), width), PAD, dtype=np.int64)
        mask = np.zeros((len(rows), width), dtype=np.int64)
        for j, r in enumerate(rows):
            ids[j, : len(r)] = r
            mask[j, : len(r)] = 1
        return ids, mask

    def embed(self, texts: list[str], batch_size: int = 16) -> np.ndarray:
        out = []
        for i in range(0, len(texts), batch_size):
            ids, mask = self.encode(texts[i : i + batch_size])
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, feed)[0]
            m = mask[..., None].astype(np.float32)
            pooled = (hidden * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)
            out.append(pooled / (np.linalg.norm(pooled, axis=1, keepdims=True) + 1e-9))
        return np.vstack(out).astype(np.float32) if out else np.zeros((0, 0), dtype=np.float32)
