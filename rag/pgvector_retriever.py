"""
pgvector retrieval over RDS PostgreSQL (§2.6.2).

Replaces Assignment 1's in-process BM25 with dense-vector search in Postgres.
This is a genuinely different retrieval implementation, not a config-flag swap:
BM25 scores lexical overlap in Python over a dict of documents; this embeds the
query with OpenAI `text-embedding-3-small` and asks Postgres for the nearest
stored vectors by cosine distance.

**The contract is the seam.** `retrieve(query, k) -> [(doc_id, score, text)]` is
identical to `rag.retriever.Retriever.retrieve`, and `doc_id` stays the `.md`
filename stem, so traces and citations read the same and the harness is
untouched.

**Threshold polarity flips, and this is the trap.** BM25 scores are unbounded and
higher-is-better, so it drops hits *below* `min_score=1.0`. Cosine distance is
0-2 and lower-is-better, so this drops hits *above* `max_distance`. Carrying the
BM25 default across would silently invert the honest-gap logic — every irrelevant
doc would pass and the uncovered-question path would never fire. The returned
`score` is converted to similarity (1 - distance) so that higher still reads as
better everywhere downstream, matching BM25's polarity in the tuple the harness
prints.

`max_distance` is calibrated in rag/test_retriever.py against the same 6
questions plus the customs gap that guarded the BM25 threshold.
"""

from __future__ import annotations

import os

# Cosine distance above which a doc is considered irrelevant.
#
# Calibrated empirically against the same 6 questions + customs gap that guarded
# the BM25 threshold (rag/test_retriever.py). Measured on this corpus:
#
#   worst correct hit  (damaged_items)   d = 0.4302
#   nearest wrong hit  (customs gap ->
#                       shipping_delays) d = 0.5313
#
# So any cutoff in (0.4302, 0.5313) satisfies both requirements; 0.48 is the
# midpoint, giving ~0.05 of margin on each side. Note this is much tighter than
# an intuitive guess would suggest — 0.62 "feels" strict for cosine distance but
# would let the customs question match shipping_delays and silently destroy the
# honest-gap path, which is the whole behaviour the threshold exists to protect.
#
# Re-run rag/test_retriever.py after any change to the doc set or embedding
# model; it fails if this separation stops holding.
DEFAULT_MAX_DISTANCE = 0.48

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIM = 1536


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of strings. One API call for the whole batch."""
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    resp = client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def connect():
    """Connection to DATABASE_URL, with pgvector's type adapters registered.

    CREATE EXTENSION runs before register_vector because the adapter looks up the
    `vector` type OID at registration time and fails outright if the extension
    isn't installed yet — which is the state of a freshly-created RDS instance on
    its very first connection.
    """
    import psycopg
    from pgvector.psycopg import register_vector

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is unset. Start a local database with:\n"
            "  docker run -d --name pgvector -p 5432:5432 "
            "-e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg17\n"
            "then set DATABASE_URL in .env (see .env.example)."
        )
    conn = psycopg.connect(url)
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.commit()
    register_vector(conn)
    return conn


class PgVectorRetriever:
    """Dense retrieval over the policy docs, same contract as the BM25 Retriever."""

    def __init__(self, max_distance: float = DEFAULT_MAX_DISTANCE):
        self.max_distance = max_distance
        # Connection is opened lazily and reused: the harness constructs a
        # Retriever once per process, and reconnecting per query would dominate
        # latency in the deployed agent.
        self._conn = None

    @property
    def conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = connect()
        return self._conn

    def retrieve(self, query: str, k: int = 2, max_distance: float | None = None):
        """Top-k (doc_id, similarity, text), nearest first, filtered by distance.

        Empty list == coverage gap, exactly as in the BM25 implementation: nothing
        stored is close enough to the question, so the harness tells the model to
        say so rather than grounding an answer in an unrelated doc.
        """
        if not query.strip():
            return []
        cutoff = self.max_distance if max_distance is None else max_distance
        vec = embed([query])[0]

        with self.conn.cursor() as cur:
            # <=> is pgvector's cosine distance operator. Filtering in SQL rather
            # than in Python keeps the work in the database, which is the point of
            # storing the vectors there.
            cur.execute(
                """
                SELECT doc_id, text, embedding <=> %s::vector AS distance
                FROM policy_docs
                WHERE embedding <=> %s::vector <= %s
                ORDER BY distance
                LIMIT %s
                """,
                (vec, vec, cutoff, k),
            )
            rows = cur.fetchall()

        # Return similarity, not raw distance, so higher stays better for every
        # caller — the harness prints this as `score=`.
        return [(doc_id, 1.0 - float(distance), text) for doc_id, text, distance in rows]
