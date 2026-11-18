# 0004 A lean embedding runtime

Date: 2026-11-18. Status: accepted.

**Context.** With the packaged embedding library the container idled at 570 MB RSS, over the 512 MB
free tier. Profiling inside the container showed the HF `tokenizers` JSON for this 250k-piece
vocabulary cost about 290 MB, and onnxruntime expanded the fp16 graph into fp32 copies at load.

**Decision.**
1. Convert the published fp16 graph to fp32 and quantize it dynamically to int8 at image build time.
2. Tokenize with native SentencePiece (5 MB model) using the XLM-R id layout (ids + 1, specials 0–3).
   Checked equal to the HF tokenizer on English, Arabic, numeric, whitespace and mixed-script strings.
3. onnxruntime with the memory arena off and one thread.
4. The Docker build re-embeds the KB with the same in-image model, so document and query vectors
   always come from identical weights.

**Result.** 275 MB steady RSS, 358 MB peak under load (limit 512 MB, target 400 MB). Retrieval MRR
moved from 0.876 (fp32) to 0.835 (int8); all gates still pass.
