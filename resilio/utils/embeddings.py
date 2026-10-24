"""Zero-dependency text embeddings for incident similarity search.

A feature-hashing embedder: deterministic, offline, and good enough to match
"OOMKilled in checkout after deploy" with last month's identical incident.
Swap in a model-backed embedder by implementing :class:`Embedder`.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from typing import Protocol

_TOKEN = re.compile(r"[a-z][a-z0-9_]+")
DEFAULT_DIM = 256


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Signed feature hashing over unigrams and bigrams, L2-normalized."""

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        tokens = _TOKEN.findall(text.lower())
        features = tokens + [f"{a}_{b}" for a, b in itertools.pairwise(tokens)]
        vector = [0.0] * self.dim
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (0.0 if either is zero)."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0
