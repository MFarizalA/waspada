#!/usr/bin/env python3
"""WA-062 — one-shot auth DB initialiser + smoke test against live RDS.

Reads DATABASE_URL from .env (or accepts it as an argument), calls
``api.db.init_db()`` to create the auth tables (users, reset_tokens),
then exercises the full auth flow: register → login → forgot-password
→ reset-password, and cleans up the test user.

Usage:
    python scripts/init-auth-db.py [DATABASE_URL]

If no argument is given the script loads ``.env`` from the repo root.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Ensure the repo root is importable regardless of CWD.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_env(env_path: Path) -> dict[str, str]:
    """Parse a .env file (KEY=value lines, no quoting gymnastics)."""
    env: dict[str, str] = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _resolve_database_url(cli_arg: str | None) -> str:
    """Return the DATABASE_URL to use, preferring the CLI argument."""
    if cli_arg:
        return cli_arg
    env = _load_env(_REPO_ROOT / ".env")
    url = env.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is empty. Pass it as an argument or set it in .env"
        )
    return url


def _print_table_schemas(db) -> None:
    """Print the created tables and their column definitions."""
    backend = getattr(db, "backend", "unknown")
    print(f"\nBackend: {backend}")

    if backend == "mysql":
        with db._cx() as cx:
            with cx.cursor() as cur:
                cur.execute("SHOW TABLES")
                tables = [row[0] for row in cur.fetchall()]
                print("Tables created:", ", ".join(tables))
                for table in ("users", "reset_tokens"):
                    if table in tables:
                        cur.execute(f"DESCRIBE {table}")
                        cols = cur.fetchall()
                        print(f"\n{table}:")
                        for col in cols:
                            # DESCRIBE returns Field, Type, Null, Key, Default, Extra
                            print(f"  {col[0]:<15} {col[1]:<20} {col[3]}")
    else:
        # SQLite fallback
        with db._cx() as cx:
            cur = cx.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = [row[0] for row in cur.fetchall()]
            print("Tables created:", ", ".join(tables))
            for table in ("users", "reset_tokens"):
                if table in tables:
                    cur = cx.execute(f"PRAGMA table_info({table})")
                    cols = cur.fetchall()
                    print(f"\n{table}:")
                    for col in cols:
                        # PRAGMA table_info returns cid, name, type, notnull, dflt_value, pk
                        pk = "PRIMARY KEY" if col[5] else ""
                        print(f"  {col[1]:<15} {col[2]:<20} {pk}")


def _smoke_test(db) -> None:
    """Run register → login → forgot → reset and clean up."""
    from api import auth as auth_mod

    test_email = "wa062-smoke@waspada.demo"
    test_password = "wa062-smoke-pass-123!"
    new_password = "wa062-smoke-pass-456!"

    print("\n--- Smoke test ---")

    # 1. Register
    print(f"1. Register {test_email} ... ", end="", flush=True)
    if db.get_user(test_email) is not None:
        # Clean up leftover from a previous run
        _delete_user(db, test_email)
    created = db.insert_user(
        test_email, auth_mod.hash_password(test_password), auth_mod._now_iso()
    )
    if not created:
        raise RuntimeError("register failed — could not insert test user")
    print("OK")

    # 2. Login
    print("2. Login ... ", end="", flush=True)
    rec = db.get_user(test_email)
    if rec is None or not auth_mod.verify_password(test_password, rec["password"]):
        raise RuntimeError("login failed — password verification failed")
    print("OK")

    # 3. Forgot-password (create reset token)
    print("3. Forgot-password ... ", end="", flush=True)
    token = auth_mod.new_token()
    expires = auth_mod._expiry_iso()
    db.create_reset_token(token, test_email, expires)
    tok_rec = db.get_reset_token(token)
    if tok_rec is None or tok_rec["used"]:
        raise RuntimeError("forgot-password failed — token not created or already used")
    print("OK")

    # 4. Reset-password
    print("4. Reset-password ... ", end="", flush=True)
    db.mark_token_used(token)
    tok_rec = db.get_reset_token(token)
    if tok_rec is None or not tok_rec["used"]:
        raise RuntimeError("reset-password failed — token not marked used")
    updated = db.set_user_password(test_email, auth_mod.hash_password(new_password))
    if not updated:
        raise RuntimeError("reset-password failed — password not updated")
    # Verify new password works
    rec = db.get_user(test_email)
    if rec is None or not auth_mod.verify_password(new_password, rec["password"]):
        raise RuntimeError("reset-password failed — new password does not verify")
    print("OK")

    # 5. Cleanup
    print("5. Cleanup ... ", end="", flush=True)
    _delete_user(db, test_email)
    if db.get_user(test_email) is not None:
        raise RuntimeError("cleanup failed — test user still present")
    print("OK")


def _delete_user(db, email: str) -> None:
    """Delete a user and their reset tokens (driver-agnostic)."""
    if db.backend == "mysql":
        with db._cx() as cx:
            with cx.cursor() as cur:
                cur.execute("DELETE FROM reset_tokens WHERE email = %s", (email,))
                cur.execute("DELETE FROM users WHERE email = %s", (email,))
    else:
        with db._cx() as cx:
            cx.execute("DELETE FROM reset_tokens WHERE email = ?", (email,))
            cx.execute("DELETE FROM users WHERE email = ?", (email,))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Initialise auth tables on the live RDS and run a smoke test."
    )
    parser.add_argument(
        "database_url",
        nargs="?",
        default=None,
        help="DATABASE_URL (default: read from .env)",
    )
    args = parser.parse_args()

    try:
        url = _resolve_database_url(args.database_url)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Mask the password for display
    safe_url = url
    if "@" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            userinfo, hostpart = rest.split("@", 1)
            if ":" in userinfo:
                user, _ = userinfo.split(":", 1)
                safe_url = f"{scheme}://{user}:***@{hostpart}"
    print(f"Target: {safe_url}")

    # Inject into the environment so api.db picks it up
    os.environ["DATABASE_URL"] = url

    # Reset any cached adapter so we build a fresh one for this URL
    from api import db as db_mod

    db_mod.reset_db()

    # Measure connection latency
    print("\nConnecting ... ", end="", flush=True)
    t0 = time.perf_counter()
    try:
        db = db_mod.init_db()
    except Exception as e:
        print(f"FAILED")
        print(f"\nERROR: could not connect / initialise: {e}", file=sys.stderr)
        return 1
    latency_ms = (time.perf_counter() - t0) * 1000
    print(f"OK ({latency_ms:.1f} ms)")

    # Show what was created
    _print_table_schemas(db)

    # Quick sanity check per acceptance criteria
    print("\nSELECT * FROM users LIMIT 1 ... ", end="", flush=True)
    try:
        if db.backend == "mysql":
            with db._cx() as cx:
                with cx.cursor() as cur:
                    cur.execute("SELECT * FROM users LIMIT 1")
                    cur.fetchall()
        else:
            with db._cx() as cx:
                cx.execute("SELECT * FROM users LIMIT 1").fetchall()
        print("OK")
    except Exception as e:
        print(f"FAILED")
        print(f"\nERROR: SELECT failed: {e}", file=sys.stderr)
        return 1

    # Full auth smoke test
    try:
        _smoke_test(db)
    except Exception as e:
        print(f"\nERROR: smoke test failed: {e}", file=sys.stderr)
        return 1

    print("\n✅ All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
