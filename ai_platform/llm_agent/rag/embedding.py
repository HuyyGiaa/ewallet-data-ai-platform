"""In-process sentence embeddings for the pinned manifest model (CPU, ONNX).

The manifest pins ``sentence-transformers/all-MiniLM-L6-v2`` at an immutable
Hugging Face revision that ships an ONNX export and ``tokenizer.json``. The
sentence-transformers pipeline for that model is Transformer → mean pooling →
L2 normalisation (``modules.json``); this module reproduces it with
onnxruntime + tokenizers, so neither torch nor an embedding service is needed.
``modules.json`` and the pooling config are checked at load time and any other
pipeline is rejected rather than silently embedded differently.

onnxruntime, tokenizers and huggingface_hub are imported lazily: they belong to
the RAG ingestion runtime (``infra/docker/rag-ingestion``), not every importer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

MODEL_FILE = "onnx/model.onnx"
TOKENIZER_FILE = "tokenizer.json"
MODULES_FILE = "modules.json"
POOLING_FILE = "1_Pooling/config.json"
EXPECTED_MODULES = ("sentence_transformers.models.Transformer",
                    "sentence_transformers.models.Pooling",
                    "sentence_transformers.models.Normalize")
# all-MiniLM-L6-v2 truncates at 256 word pieces (sentence_bert_config.json).
MAX_SEQUENCE_LENGTH = 256


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_sentence_transformer_layout(modules: Sequence[dict[str, Any]], pooling: dict[str, Any]) -> None:
    """Fail unless the model is Transformer → mean pooling → Normalize."""
    types = tuple(str(item.get("type")) for item in modules)
    if types != EXPECTED_MODULES:
        raise ValueError(f"unsupported sentence-transformers pipeline: {types}")
    if not pooling.get("pooling_mode_mean_tokens") or any(
        pooling.get(name) for name in ("pooling_mode_cls_token", "pooling_mode_max_tokens")
    ):
        raise ValueError("embedding model must use mean pooling only")


def mean_pool_normalize(token_embeddings: Any, attention_mask: Any) -> list[list[float]]:
    """Masked mean over tokens, then L2 normalisation (numpy arrays in, lists out)."""
    import numpy as np

    mask = attention_mask[..., None].astype(np.float32)
    summed = (token_embeddings * mask).sum(axis=1)
    counts = np.clip(mask.sum(axis=1), 1e-9, None)
    pooled = summed / counts
    norms = np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)
    return (pooled / norms).astype(np.float32).tolist()


@dataclass
class OnnxSentenceEmbedder:
    """Callable ``texts -> vectors`` for ``pipeline.embed_chunks``."""

    model_id: str
    model_revision: str
    dimensions: int
    cache_dir: Path | None = None
    batch_size: int = 32
    threads: int = 2
    model_sha256: str = field(init=False, default="")
    _session: Any = field(init=False, default=None, repr=False)
    _tokenizer: Any = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        if len(self.model_revision) != 40 or any(c not in "0123456789abcdef" for c in self.model_revision):
            raise ValueError("model_revision must be an immutable 40-character commit SHA")
        if self.dimensions < 1 or self.batch_size < 1 or self.threads < 1:
            raise ValueError("dimensions, batch_size and threads must be positive")

    def _download(self, filename: str) -> Path:
        from huggingface_hub import hf_hub_download

        return Path(hf_hub_download(
            repo_id=self.model_id, filename=filename, revision=self.model_revision,
            cache_dir=str(self.cache_dir) if self.cache_dir else None,
        ))

    def load(self) -> "OnnxSentenceEmbedder":
        if self._session is not None:
            return self
        import onnxruntime
        from tokenizers import Tokenizer

        check_sentence_transformer_layout(
            json.loads(self._download(MODULES_FILE).read_text(encoding="utf-8")),
            json.loads(self._download(POOLING_FILE).read_text(encoding="utf-8")),
        )
        tokenizer = Tokenizer.from_file(str(self._download(TOKENIZER_FILE)))
        tokenizer.enable_truncation(max_length=MAX_SEQUENCE_LENGTH)
        tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        model_path = self._download(MODEL_FILE)
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = self.threads
        options.inter_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"])
        self._tokenizer = tokenizer
        self.model_sha256 = _sha256(model_path)
        return self

    def _batch(self, texts: Sequence[str]) -> list[list[float]]:
        import numpy as np

        encodings = self._tokenizer.encode_batch(list(texts))
        ids = np.asarray([item.ids for item in encodings], dtype=np.int64)
        mask = np.asarray([item.attention_mask for item in encodings], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in {item.name for item in self._session.get_inputs()}:
            feeds["token_type_ids"] = np.zeros_like(ids)
        token_embeddings = self._session.run(None, feeds)[0]
        return mean_pool_normalize(token_embeddings, mask)

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("every text to embed must be a non-empty string")
        self.load()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self._batch(texts[start:start + self.batch_size]))
        for vector in vectors:
            if len(vector) != self.dimensions or not all(map(math.isfinite, vector)):
                raise ValueError(f"embedding model returned an invalid {len(vector)}-d vector")
        return vectors


@dataclass
class LocalQueryEmbedder:
    """``QueryEmbedder`` (online_retriever) over the in-process ONNX model."""

    embedder: OnnxSentenceEmbedder

    @property
    def model_revision(self) -> str:
        return self.embedder.model_revision

    @property
    def dimensions(self) -> int:
        return self.embedder.dimensions

    async def embed(self, text: str) -> Sequence[float]:
        from ai_platform.llm_agent.tools.runtime import ToolUnavailableError

        try:
            return (await asyncio.to_thread(self.embedder, [text]))[0]
        except Exception as exc:
            raise ToolUnavailableError("embedding_unavailable", "Query embedding model is unavailable") from exc
