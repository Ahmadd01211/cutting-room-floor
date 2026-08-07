"""Embeddings, with a real path and an offline path.

`VertexEmbedder` is what runs in the demo and in production: Google's
`gemini-embedding-001` via the GenAI SDK. It is the only embedding model the
competition rules permit — no OpenAI, no sentence-transformers pulling a
non-Google model.

`HashingEmbedder` is a deterministic offline fallback for tests and for anyone
who clones the repo without cloud credentials. It is feature hashing over
character n-grams: a real, if weak, text vectoriser. It is NOT a semantic model
and is never used when credentials are present — `for_environment()` picks the
real one whenever it can.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol, Sequence

EMBED_DIM = 768  # gemini-embedding-001's default output dimensionality


class Embedder(Protocol):
    model_name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _l2(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


class HashingEmbedder:
    """Deterministic, offline, no network. Character 3-grams hashed into a
    fixed-width vector with signed accumulation, then L2-normalised.

    Good enough that similar strings land near each other, which is all the
    tests need. Not a semantic model — do not ship a demo on it.
    """

    model_name = "hashing-ngram-v1"

    def __init__(self, dim: int = EMBED_DIM, ngram: int = 3):
        self.dim = dim
        self.ngram = ngram

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            norm_text = re.sub(r"\s+", " ", text.lower().strip())
            tokens = norm_text.split(" ")
            grams = tokens + [
                norm_text[i : i + self.ngram]
                for i in range(max(0, len(norm_text) - self.ngram + 1))
            ]
            for gram in grams:
                digest = hashlib.blake2b(gram.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(digest[:4], "big") % self.dim
                sign = 1.0 if digest[4] & 1 else -1.0
                vec[idx] += sign
            out.append(_l2(vec))
        return out


class VertexEmbedder:
    """gemini-embedding-001 via google-genai. Batched, with the task type set
    to CLUSTERING because that is what these vectors are for — grouping
    comments into themes, not retrieval ranking."""

    model_name = "gemini-embedding-001"

    def __init__(self, dim: int = EMBED_DIM, batch: int = 64,
                 task_type: str = "CLUSTERING"):
        from google import genai

        self.client = genai.Client()
        self.dim = dim
        self.batch = batch
        self.task_type = task_type

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        from google.genai import types

        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            chunk = list(texts[i : i + self.batch])
            resp = self.client.models.embed_content(
                model=self.model_name,
                contents=chunk,
                config=types.EmbedContentConfig(
                    task_type=self.task_type,
                    output_dimensionality=self.dim,
                ),
            )
            out.extend(_l2(list(e.values)) for e in resp.embeddings)
        return out


def for_environment(prefer_real: bool = True) -> Embedder:
    """Real embedder when credentials are configured, offline fallback when not.

    Never silently degrades in a way you cannot see: the chosen model name is
    written to `crf.comment.embed_model`, so a query can always tell which
    vectors it is looking at.
    """
    if prefer_real and (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
    ):
        try:
            return VertexEmbedder()
        except Exception as exc:  # noqa: BLE001
            print(f"  ! Vertex embedder unavailable ({exc}); using offline fallback")
    return HashingEmbedder()
