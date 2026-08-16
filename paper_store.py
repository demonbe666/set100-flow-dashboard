import json
import os
import sqlite3
import threading
from contextlib import contextmanager


class PaperStateConflict(Exception):
    def __init__(self, state, revision):
        super().__init__("paper portfolio was updated by another client")
        self.state = state
        self.revision = revision


def state_has_activity(state):
    if not isinstance(state, dict):
        return False
    return bool(
        state.get("orders")
        or state.get("positions")
        or any(state.get("dailyEntries", {}).values())
    )


def state_activity_score(state):
    if not isinstance(state, dict):
        return 0
    daily_entries = sum(
        len(items) for items in state.get("dailyEntries", {}).values()
        if isinstance(items, list)
    )
    return (
        len(state.get("orders", [])) * 1_000_000
        + len(state.get("positions", {})) * 10_000
        + daily_entries * 100
        + max(0, len(state.get("equityHistory", [])) - 1)
    )


class PaperStateStore:
    def __init__(self, database_url=None, sqlite_path=None):
        self.database_url = database_url or os.environ.get("DATABASE_URL", "")
        self.sqlite_path = sqlite_path or os.environ.get(
            "PAPER_STATE_DB_PATH",
            os.path.join(os.path.dirname(__file__), "paper_state.db"),
        )
        self.backend = "postgres" if self.database_url.startswith("postgres") else "sqlite"
        self.persistent = self.backend == "postgres"
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self._sqlite_lock = threading.Lock()

    @contextmanager
    def _connect(self):
        if self.backend == "postgres":
            import psycopg

            with psycopg.connect(self.database_url) as connection:
                yield connection
            return

        connection = sqlite3.connect(self.sqlite_path, timeout=15)
        try:
            yield connection
        finally:
            connection.close()

    def _ensure_schema(self):
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS paper_portfolios (
                        portfolio_id TEXT PRIMARY KEY,
                        state_json TEXT NOT NULL,
                        revision BIGINT NOT NULL,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.commit()
            self._schema_ready = True

    def get(self, portfolio_id):
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json, revision, updated_at FROM paper_portfolios WHERE portfolio_id = %s"
                if self.backend == "postgres"
                else "SELECT state_json, revision, updated_at FROM paper_portfolios WHERE portfolio_id = ?",
                (portfolio_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "state": json.loads(row[0]),
            "revision": int(row[1]),
            "updated_at": str(row[2]),
        }

    def put(self, portfolio_id, state, expected_revision=None, allow_richer_migration=False):
        self._ensure_schema()
        lock = self._sqlite_lock if self.backend == "sqlite" else _NullLock()
        with lock, self._connect() as connection:
            if self.backend == "sqlite":
                connection.execute("BEGIN IMMEDIATE")
                select_sql = (
                    "SELECT state_json, revision FROM paper_portfolios WHERE portfolio_id = ?"
                )
                write_placeholder = "?"
            else:
                select_sql = (
                    "SELECT state_json, revision FROM paper_portfolios "
                    "WHERE portfolio_id = %s FOR UPDATE"
                )
                write_placeholder = "%s"

            row = connection.execute(select_sql, (portfolio_id,)).fetchone()
            payload = json.dumps(state, separators=(",", ":"), allow_nan=False)
            if row is None:
                revision = 1
                connection.execute(
                    "INSERT INTO paper_portfolios "
                    f"(portfolio_id, state_json, revision) VALUES ({write_placeholder}, {write_placeholder}, {write_placeholder})",
                    (portfolio_id, payload, revision),
                )
                connection.commit()
                return {"state": state, "revision": revision}

            current_state = json.loads(row[0])
            current_revision = int(row[1])
            revision_matches = expected_revision == current_revision
            migration_wins = (
                allow_richer_migration
                and state_has_activity(state)
                and state_activity_score(state) > state_activity_score(current_state)
            )
            if not revision_matches and not migration_wins:
                connection.rollback()
                raise PaperStateConflict(current_state, current_revision)

            revision = current_revision + 1
            connection.execute(
                "UPDATE paper_portfolios SET state_json = {0}, revision = {0}, "
                "updated_at = CURRENT_TIMESTAMP WHERE portfolio_id = {0}".format(write_placeholder),
                (payload, revision, portfolio_id),
            )
            connection.commit()
            return {"state": state, "revision": revision}

    def delete(self, portfolio_id):
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM paper_portfolios WHERE portfolio_id = %s"
                if self.backend == "postgres"
                else "DELETE FROM paper_portfolios WHERE portfolio_id = ?",
                (portfolio_id,),
            )
            connection.commit()


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False
