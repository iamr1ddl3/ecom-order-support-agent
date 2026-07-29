"""
BM25 retriever over the policy-doc set, behind a small Retriever interface.

Pure stdlib — no embedding model, no network, no torch. BM25 is the lexical
ranker real search systems use (Elasticsearch, Lucene); it beats plain keyword
overlap by weighting rare terms higher and saturating repeated ones. It won't
match true synonyms with zero shared words — that's the ceiling an embedding
model would lift, and the Retriever interface below is exactly the seam to swap
in Chroma later without touching the harness.

Two things the assignment demands live here:
  - Traceable: retrieve() returns (doc_id, score, text) so the caller can print
    exactly which chunk grounded each answer.
  - Honest gap: scores below `min_score` are dropped. An empty result means
    "nothing in the corpus covers this", which the harness surfaces instead of
    letting the model guess.

`min_score` note: BM25 scores are unbounded (not 0-1 like cosine), so the default
threshold is calibrated to THIS corpus. It's the one knob to tune if you swap the
doc set — see test_retriever.py for the sanity check that guards it.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

_DOCS_DIR = Path(__file__).parent / "policy_docs"

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "will", "i", "my", "me",
    "you", "your", "to", "for", "of", "in", "on", "and", "or", "if", "do",
    "does", "can", "could", "would", "have", "has", "had", "it", "its",
    "this", "that", "with", "be", "get", "got", "im", "as", "still", "am",
    "up", "now", "what", "how", "many", "so", "at", "from", "by", "after",
    "before", "days", "day",  # 'days' is in nearly every doc -> no signal
    # e-commerce boilerplate: appears across the domain, carries no disambiguating
    # signal, and on a small corpus BM25's IDF under-penalizes it. Dropping these
    # is what separates a real topical match from a query that only shares generic
    # nouns (e.g. the customs-fee gap question, which matches only 'order'/'fee').
    "order", "orders", "fee", "item", "items", "customer", "customers", "will",
}


def _tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if w not in _STOPWORDS]


def load_policy_docs() -> dict[str, str]:
    """{doc_id: text} from the policy_docs/ markdown files. doc_id is the filename
    stem (e.g. 'refund_window'), which is what gets cited in traces and answers."""
    return {p.stem: p.read_text(encoding="utf-8").strip() for p in sorted(_DOCS_DIR.glob("*.md"))}


class Retriever:
    """BM25 over a fixed doc set. Build once, query many times."""

    def __init__(self, docs: dict[str, str] | None = None, k1: float = 1.5, b: float = 0.75):
        self.docs = docs if docs is not None else load_policy_docs()
        self._k1 = k1
        self._b = b
        self._tokens = {doc_id: _tokenize(text) for doc_id, text in self.docs.items()}
        self._lengths = {doc_id: len(toks) for doc_id, toks in self._tokens.items()}
        self._avg_len = sum(self._lengths.values()) / max(1, len(self.docs))
        self._tf = {doc_id: Counter(toks) for doc_id, toks in self._tokens.items()}
        self._n_docs = len(self.docs)

    def _idf(self, term: str) -> float:
        n = sum(1 for toks in self._tokens.values() if term in toks)
        # +1 inside the log keeps common terms non-negative on a small corpus.
        return math.log((self._n_docs - n + 0.5) / (n + 0.5) + 1)

    def _bm25(self, q_terms: list[str], doc_id: str) -> float:
        tf, doc_len = self._tf[doc_id], self._lengths[doc_id]
        score = 0.0
        for term in q_terms:
            f = tf.get(term, 0)
            if f == 0:
                continue
            denom = f + self._k1 * (1 - self._b + self._b * (doc_len / self._avg_len))
            score += self._idf(term) * (f * (self._k1 + 1)) / denom
        return score

    def retrieve(self, query: str, k: int = 2, min_score: float = 1.0) -> list[tuple[str, float, str]]:
        """Top-k (doc_id, score, text) with score >= min_score, best first.
        Empty list == coverage gap: nothing in the corpus is relevant enough."""
        q_terms = _tokenize(query)
        if not q_terms:
            return []
        scored = [(doc_id, self._bm25(q_terms, doc_id), self.docs[doc_id]) for doc_id in self.docs]
        scored.sort(key=lambda s: s[1], reverse=True)
        return [s for s in scored[:k] if s[1] >= min_score]
