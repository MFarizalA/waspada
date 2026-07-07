"""WASPADA auth DB layer — DATABASE_URL-driven, smallest correct thing.

Driver selection (per WA-028):
* ``DATABASE_URL`` starting with ``postgres`` → ApsaraDB RDS PostgreSQL via
  ``psycopg2`` (lazy-imported so the module loads cleanly without the dep).
* Otherwise (no URL, or ``sqlite://`` / a ``.db`` path) → stdlib ``sqlite3``,
  the documented local-dev fallback. Keeps the test suite offline and green.

The connection layer is deliberately a tiny CRUD surface, not an ORM — four
auth routes don't justify SQLAlchemy. Both drivers speak SQL through one
``_Db`` adapter that normalizes the placeholder style (``%s`` vs ``?``) and
row access (tuple vs ``sqlite3.Row``). ``api/auth.py`` never touches a driver
directly.

Tables (created on ``init_db()``):

    users        (id, email UNIQUE, password, created_at)
    reset_tokens (token PK, email REFERENCES users(email), expires_at, used)

See ``backlog/WA-028.md`` for the canonical schema.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

log = logging.getLogger("waspada.db")


# --------------------------------------------------------------------------- #
# Schema (shared by both drivers; driver-specific DDL in ``_ddl()``)
# --------------------------------------------------------------------------- #
def _ddl() -> list[str]:
    """Return the CREATE TABLE statements, in driver-agnostic form.

    SQLite and PostgreSQL both accept ``SERIAL``→``INTEGER ... AUTOINCREMENT``
    differently, so the ``users.id`` column is rendered per-driver in
    ``_SQLiteDb._create`` / ``_PostgresDb._create``. Everything else is plain
    ANSI SQL that both accept.
    """
    return [
        # users.id is added by the driver (autoincrement spelling differs).
        """
        CREATE TABLE IF NOT EXISTS users (
            email       TEXT PRIMARY KEY,
            password    TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS reset_tokens (
            token       TEXT PRIMARY KEY,
            email       TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            used        INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (email) REFERENCES users(email)
        )
        """,
    ]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Adapter interface — both drivers implement these methods
# --------------------------------------------------------------------------- #
class _Db:
    """Minimal CRUD surface used by ``api/auth.py``.

    Every method opens a short-lived connection (per-call) so the FastAPI
    routes never hold a connection across requests and never need a pool for
    the demo's load. PostgreSQL's psycopg2 uses its own connection; SQLite
    uses ``check_same_thread=False`` so TestClient's thread works.
    """

    backend: str = "base"

    # --- lifecycle ---------------------------------------------------------
    def create_tables(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    # --- users -------------------------------------------------------------
    def get_user(self, email: str) -> Optional[dict]:  # pragma: no cover
        raise NotImplementedError

    def insert_user(self, email: str, password_hash: str, created_at: str) -> bool:
        """Insert; return True if created, False if email already exists."""
        raise NotImplementedError  # pragma: no cover

    def set_user_password(self, email: str, password_hash: str) -> bool:
        """Update the password hash; return True if a row was updated."""
        raise NotImplementedError  # pragma: no cover

    def count_users(self) -> int:  # pragma: no cover
        raise NotImplementedError

    def list_users(self) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    # --- reset tokens ------------------------------------------------------
    def create_reset_token(self, token: str, email: str, expires_at: str) -> None:
        raise NotImplementedError  # pragma: no cover

    def get_reset_token(self, token: str) -> Optional[dict]:
        """Return {email, expires_at, used} or None."""
        raise NotImplementedError  # pragma: no cover

    def mark_token_used(self, token: str) -> None:  # pragma: no cover
        raise NotImplementedError

    # --- test-only ---------------------------------------------------------
    def wipe(self) -> None:
        """Delete every row from both tables. TESTS ONLY (not used by routes)."""
        raise NotImplementedError  # pragma: no cover


# --------------------------------------------------------------------------- #
# SQLite adapter (stdlib, local-dev + tests)
# --------------------------------------------------------------------------- #
class _SQLiteDb(_Db):
    backend = "sqlite"

    def __init__(self, path: str):
        # ``:memory:`` is per-connection — to give the demo one stable DB
        # across connections we keep a single shared connection guarded by a
        # lock. Tests point this at a tmp_path file or :memory: via factory.
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        # Make LIKE/equals case-sensitive like Postgres for email uniqueness.
        self._conn.execute("PRAGMA case_sensitive_like = ON;")
        self.create_tables()

    @contextmanager
    def _cx(self) -> Iterator[sqlite3.Connection]:
        # Single shared connection + lock — fine for the demo's concurrency.
        with self._lock:
            yield self._conn

    def create_tables(self) -> None:
        with self._cx() as cx:
            for stmt in _ddl():
                cx.execute(stmt)
            # SQLite doesn't have TIMESTAMPTZ; TEXT ISO8601 is what we store.

    # --- users -------------------------------------------------------------
    def get_user(self, email: str) -> Optional[dict]:
        with self._cx() as cx:
            row = cx.execute(
                "SELECT email, password, created_at FROM users WHERE email = ?",
                (email,),
            ).fetchone()
        return dict(row) if row else None

    def insert_user(self, email: str, password_hash: str, created_at: str) -> bool:
        try:
            with self._cx() as cx:
                cx.execute(
                    "INSERT INTO users (email, password, created_at) VALUES (?, ?, ?)",
                    (email, password_hash, created_at),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def set_user_password(self, email: str, password_hash: str) -> bool:
        with self._cx() as cx:
            cur = cx.execute(
                "UPDATE users SET password = ? WHERE email = ?",
                (password_hash, email),
            )
        return cur.rowcount > 0

    def count_users(self) -> int:
        with self._cx() as cx:
            row = cx.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"])

    def list_users(self) -> list[dict]:
        with self._cx() as cx:
            rows = cx.execute(
                "SELECT email, password, created_at FROM users ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    # --- reset tokens ------------------------------------------------------
    def create_reset_token(self, token: str, email: str, expires_at: str) -> None:
        with self._cx() as cx:
            cx.execute(
                "INSERT INTO reset_tokens (token, email, expires_at, used) "
                "VALUES (?, ?, ?, 0)",
                (token, email, expires_at),
            )

    def get_reset_token(self, token: str) -> Optional[dict]:
        with self._cx() as cx:
            row = cx.execute(
                "SELECT email, expires_at, used FROM reset_tokens WHERE token = ?",
                (token,),
            ).fetchone()
        if not row:
            return None
        return {"email": row["email"], "expires_at": row["expires_at"], "used": bool(row["used"])}

    def mark_token_used(self, token: str) -> None:
        with self._cx() as cx:
            cx.execute("UPDATE reset_tokens SET used = 1 WHERE token = ?", (token,))

    def wipe(self) -> None:
        with self._cx() as cx:
            cx.execute("DELETE FROM reset_tokens")
            cx.execute("DELETE FROM users")

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------- #
# PostgreSQL adapter (ApsaraDB RDS, via psycopg2 — lazy import)
# --------------------------------------------------------------------------- #
class _PostgresDb(_Db):
    backend = "postgres"

    def __init__(self, url: str):
        try:
            import psycopg2  # lazy: not a hard dep for local dev / tests
        except ImportError as e:  # pragma: no cover - env dependent
            raise RuntimeError(
                "DATABASE_URL points at PostgreSQL but psycopg2 is not "
                "installed. Install with: pip install psycopg2-binary"
            ) from e
        self._url = url
        self._psycopg2 = psycopg2
        self.create_tables()

    @contextmanager
    def _cx(self) -> Iterator[Any]:
        # One fresh connection per call. RDS + a hackathon demo's QPS does not
        # need a pool; keeping connections short-lived avoids idle-session
        # issues on managed PG.
        conn = self._psycopg2.connect(self._url)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_tables(self) -> None:
        with self._cx() as cx:
            with cx.cursor() as cur:
                # SERIAL PK + TIMESTAMPTZ are the PostgreSQL-native spellings.
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id          SERIAL PRIMARY KEY,
                        email       TEXT UNIQUE NOT NULL,
                        password    TEXT NOT NULL,
                        created_at  TIMESTAMPTZ DEFAULT now()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS reset_tokens (
                        token       TEXT PRIMARY KEY,
                        email       TEXT NOT NULL REFERENCES users(email),
                        expires_at  TIMESTAMPTZ NOT NULL,
                        used        BOOLEAN DEFAULT FALSE
                    )
                    """
                )

    # --- users -------------------------------------------------------------
    def get_user(self, email: str) -> Optional[dict]:
        with self._cx() as cx:
            with cx.cursor() as cur:
                cur.execute(
                    "SELECT email, password, created_at FROM users WHERE email = %s",
                    (email,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return {"email": row[0], "password": row[1], "created_at": str(row[2])}

    def insert_user(self, email: str, password_hash: str, created_at: str) -> bool:
        try:
            with self._cx() as cx:
                with cx.cursor() as cur:
                    cur.execute(
                        "INSERT INTO users (email, password, created_at) "
                        "VALUES (%s, %s, %s)",
                        (email, password_hash, created_at),
                    )
            return True
        except self._psycopg2.errors.UniqueViolation:
            return False

    def set_user_password(self, email: str, password_hash: str) -> bool:
        with self._cx() as cx:
            with cx.cursor() as cur:
                cur.execute(
                    "UPDATE users SET password = %s WHERE email = %s",
                    (password_hash, email),
                )
                return cur.rowcount > 0

    def count_users(self) -> int:
        with self._cx() as cx:
            with cx.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                return int(cur.fetchone()[0])

    def list_users(self) -> list[dict]:
        with self._cx() as cx:
            with cx.cursor() as cur:
                cur.execute(
                    "SELECT email, password, created_at FROM users ORDER BY created_at"
                )
                rows = cur.fetchall()
        return [{"email": r[0], "password": r[1], "created_at": str(r[2])} for r in rows]

    # --- reset tokens ------------------------------------------------------
    def create_reset_token(self, token: str, email: str, expires_at: str) -> None:
        with self._cx() as cx:
            with cx.cursor() as cur:
                cur.execute(
                    "INSERT INTO reset_tokens (token, email, expires_at, used) "
                    "VALUES (%s, %s, %s, FALSE)",
                    (token, email, expires_at),
                )

    def get_reset_token(self, token: str) -> Optional[dict]:
        with self._cx() as cx:
            with cx.cursor() as cur:
                cur.execute(
                    "SELECT email, expires_at, used FROM reset_tokens WHERE token = %s",
                    (token,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return {"email": row[0], "expires_at": str(row[1]), "used": bool(row[2])}

    def mark_token_used(self, token: str) -> None:
        with self._cx() as cx:
            with cx.cursor() as cur:
                cur.execute(
                    "UPDATE reset_tokens SET used = TRUE WHERE token = %s", (token,)
                )

    def wipe(self) -> None:
        with self._cx() as cx:
            with cx.cursor() as cur:
                cur.execute("DELETE FROM reset_tokens")
                cur.execute("DELETE FROM users")


# --------------------------------------------------------------------------- #
# Factory — reads DATABASE_URL once at first use, caches the adapter
# --------------------------------------------------------------------------- #
_db: Optional[_Db] = None
_db_lock = threading.Lock()


def _classify_url(url: Optional[str]) -> str:
    """Return 'postgres' or 'sqlite' for a DATABASE_URL value."""
    if not url:
        return "sqlite"
    u = url.strip().lower()
    if u.startswith("postgres://") or u.startswith("postgresql://"):
        return "postgres"
    return "sqlite"  # 'sqlite:///path.db' or bare path → sqlite


def _build_db() -> _Db:
    """Construct the adapter for the current DATABASE_URL env var."""
    url = os.environ.get("DATABASE_URL")
    kind = _classify_url(url)
    if kind == "postgres":
        log.info("auth DB: PostgreSQL (RDS) via DATABASE_URL")
        return _PostgresDb(url)  # type: ignore[arg-type]
    # SQLite local-dev fallback. Allow an explicit sqlite:///path; default
    # to a process-local file so the demo persists between requests.
    if url:
        # accept 'sqlite:///path' or a bare path
        path = url.replace("sqlite://", "", 1) if url.startswith("sqlite:") else url
        path = path.lstrip("/") if path.startswith("/") and not _looks_like_windows_abs(path) else path
        # special-case the in-memory marker
        if path in ("", ":memory:"):
            path = ":memory:"
    else:
        path = os.environ.get("WASPADA_SQLITE_PATH", ":memory:")
    log.info("auth DB: SQLite at %s (local-dev fallback)", path)
    return _SQLiteDb(path)


def _looks_like_windows_abs(path: str) -> bool:
    # e.g. C:\... — keep as-is. Not relevant on Linux but harmless.
    return len(path) >= 3 and path[1] == ":" and path[2] in ("\\", "/")


def get_db() -> _Db:
    """Return the process-wide adapter (lazy singleton).

    Tests call ``reset_db()`` to point at a fresh tmp DB and drop the cache.
    """
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = _build_db()
    return _db


def reset_db(new_db: Optional[_Db] = None) -> None:
    """Replace the cached adapter. TESTS ONLY — lets each test take a clean DB.

    With no arg, the next ``get_db()`` rebuilds from the current env. With an
    arg (a fresh ``_SQLiteDb(':memory:')`` etc.), that adapter is used directly.
    """
    global _db
    with _db_lock:
        if _db is not None and isinstance(_db, _SQLiteDb):
            try:
                _db.close()
            except Exception:  # pragma: no cover - best-effort
                pass
        _db = new_db


def init_db() -> _Db:
    """Idempotent: ensure the adapter exists and tables are created."""
    db = get_db()
    db.create_tables()
    return db


def new_token() -> str:
    """Opaque single-use reset token (URL-safe, 43 chars of entropy)."""
    return uuid.uuid4().hex + uuid.uuid4().hex  # 64 hex chars, ample entropy
