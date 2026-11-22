"""
The pgvector retriever must survive a failed query (no LLM, no network, no DB).

Found in the live deployment: the index hadn't been seeded, so the first query
failed on a missing table — and then EVERY later query failed with "current
transaction is aborted, commands ignored until end of transaction block", long
after the real problem was fixable. The connection is one-per-process and
psycopg leaves the failed transaction open, so a single transient error takes
retrieval down until the task restarts, with every trace pointing at the wrong
cause.

Uses a fake connection rather than a database: the behaviour under test is
"does it roll back", which needs no Postgres.

Run: python -m rag.test_pgvector_recovery
"""

from rag.pgvector_retriever import PgVectorRetriever


class FakeCursor:
    def __init__(self, conn, fail):
        self._conn, self._fail = conn, fail

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a):
        if self._fail:
            raise RuntimeError('relation "policy_docs" does not exist')

    def fetchall(self):
        return [("refund_window", "30 days", 0.30)]


class FakeConn:
    closed = False

    def __init__(self, fail=False):
        self.fail, self.rollbacks = fail, 0

    def cursor(self):
        return FakeCursor(self, self.fail)

    def rollback(self):
        self.rollbacks += 1
        self.fail = False  # a rollback clears the aborted transaction


def demo() -> None:
    # Retriever embeds the query before touching the DB; stub that out.
    import rag.pgvector_retriever as mod
    mod.embed = lambda texts: [[0.0] * 1536 for _ in texts]

    r = PgVectorRetriever()
    conn = FakeConn(fail=True)
    r._conn = conn

    # 1. a failing query must raise AND roll back
    try:
        r.retrieve("How many days to request a refund?")
        raise AssertionError("expected the failing query to raise")
    except RuntimeError:
        pass
    assert conn.rollbacks == 1, f"expected 1 rollback, got {conn.rollbacks}"
    print("[ok] a failed query rolls the transaction back instead of leaving it open")

    # 2. the NEXT query must work — this is the regression that bit in production
    hits = r.retrieve("How many days to request a refund?")
    assert hits and hits[0][0] == "refund_window", f"connection stayed poisoned: {hits}"
    print("[ok] the next query succeeds — one error no longer disables retrieval")

    # 3. if rollback itself fails, drop the connection so the next call reconnects
    class Unrecoverable(FakeConn):
        def rollback(self):
            raise RuntimeError("connection already dead")

    r._conn = Unrecoverable(fail=True)
    try:
        r.retrieve("anything")
    except RuntimeError:
        pass
    assert r._conn is None, "an unrecoverable connection must be discarded, not reused"
    print("[ok] an unrecoverable connection is discarded so the next call reconnects")

    print("\nAll pgvector recovery checks passed.")


if __name__ == "__main__":
    demo()
