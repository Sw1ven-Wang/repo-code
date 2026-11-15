"""Isolated JSON-lines worker: no Host state mount, credentials or network."""
import json
import sys

import torch
from sentence_transformers import SentenceTransformer, CrossEncoder

torch.set_num_threads(2)
embedding = SentenceTransformer("/models/embedding", local_files_only=True, trust_remote_code=False, device="cpu")
ranker = CrossEncoder("/models/reranker", local_files_only=True, trust_remote_code=False, device="cpu", max_length=512)
print(json.dumps({"ready": True, "dimension": embedding.get_sentence_embedding_dimension()}), flush=True)
for line in sys.stdin:
    try:
        if len(line) > 256000: raise ValueError("request too large")
        request = json.loads(line); texts = request["texts"]
        if len(texts) > 64 or any(not isinstance(t, str) or len(t) > 8000 for t in texts): raise ValueError("invalid texts")
        if request["operation"] == "embed":
            result = embedding.encode(texts, normalize_embeddings=True, batch_size=8, show_progress_bar=False).tolist()
        elif request["operation"] == "rerank":
            if len(request["query"]) > 8000: raise ValueError("query too large")
            result = ranker.predict([(request["query"], text) for text in texts], batch_size=8, show_progress_bar=False).tolist()
        else: raise ValueError("unknown operation")
        print(json.dumps({"result": result}, allow_nan=False), flush=True)
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}), flush=True)
