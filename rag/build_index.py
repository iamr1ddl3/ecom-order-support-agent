"""
Build the pgvector index over the policy docs (§2.6.2).

One command, idempotent: creates the extension and table if absent, embeds the
6 policy docs, and upserts them. Safe to re-run — the ON CONFLICT clause means a
second run refreshes embeddings rather than duplicating rows, which matters
because this runs both against local Docker and against RDS after deploy.

    python -m rag.build_index

Reads DATABASE_URL and OPENAI_API_KEY from the environment (.env locally, task
definition env vars on ECS).
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

from rag.pgvector_retriever import EMBED_DIM, EMBED_MODEL, connect, embed
from rag.retriever import load_policy_docs

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS policy_docs (
    doc_id    text PRIMARY KEY,
    text      text NOT NULL,
    embedding vector({EMBED_DIM}) NOT NULL
);
"""


def main() -> int:
    load_dotenv()
    docs = load_policy_docs()
    if not docs:
        print("No policy docs found in rag/policy_docs/.")
        return 1

    doc_ids = list(docs)
    print(f"Embedding {len(doc_ids)} policy docs with {EMBED_MODEL}...")
    vectors = embed([docs[d] for d in doc_ids])

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
            for doc_id, vector in zip(doc_ids, vectors):
                cur.execute(
                    """
                    INSERT INTO policy_docs (doc_id, text, embedding)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (doc_id)
                    DO UPDATE SET text = EXCLUDED.text, embedding = EXCLUDED.embedding
                    """,
                    (doc_id, docs[doc_id], vector),
                )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM policy_docs")
            count = cur.fetchone()[0]

    print(f"Indexed {len(doc_ids)} docs: {doc_ids}")
    print(f"policy_docs now holds {count} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
