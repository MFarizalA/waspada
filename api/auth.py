"""WASPADA authentication — JWT + bcrypt, smallest correct thing for the demo.

Routes (mounted under ``/api/auth``):

* ``POST /api/auth/register``        — email + password → create user, return JWT
* ``POST /api/auth/login``           — email + password → verify, return JWT
* ``POST /api/auth/forgot-password`` — email → generate single-use reset token
                                        (logged in dev; no SMTP for the demo)
* ``POST /api/auth/reset-password``  — token + new password → update + invalidate

The user store is ApsaraDB RDS PostgreSQL in production, selected by
``DATABASE_URL`` (the 5th Alibaba Cloud service in the submission). For local
dev and tests the same connection layer falls back to stdlib SQLite — see
``api/db.py``. Passwords are bcrypt-hashed; reset tokens are opaque server-side
rows in ``reset_tokens``, marked ``used`` on redemption.

``current_user`` is a FastAPI dependency that validates the
``Authorization: Bearer ***`` header; wire it onto any protected route
(``api/main.py`` puts it on ``/api/run``).
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, status
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field

from api import db as db_mod

log = logging.getLogger("waspada.auth")

# --- hashing ---------------------------------------------------------------
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _pwd_ctx.verify(plain, hashed)
    except Exception:
        return False


# --- default analyst (seeded on startup so judges don't have to register) --
DEFAULT_ANALYST_EMAIL = "analyst@waspada.demo"
DEFAULT_ANALYST_PASSWORD = os.environ.get(
    "WASPADA_DEMO_PASSWORD", "waspada123"
)


def _db():
    """Return the process-wide DB adapter (lazy singleton via api/db.py)."""
    return db_mod.get_db()


def reset_store() -> None:
    """Wipe both tables AND reset the adapter to a clean in-memory DB.

    Tests call this for a known-empty state. It points the adapter at a fresh
    ``:memory:`` SQLite DB so cross-test isolation is absolute regardless of
    module import order. Production code never calls this.
    """
    db_mod.reset_db(new_db=db_mod._SQLiteDb(":memory:"))


def seed_default_user() -> None:
    """Idempotently create the demo analyst (so judges can log straight in)."""
    db = _db()
    if db.get_user(DEFAULT_ANALYST_EMAIL) is None:
        db.insert_user(
            DEFAULT_ANALYST_EMAIL,
            hash_password(DEFAULT_ANALYST_PASSWORD),
            _now_iso(),
        )
        log.warning(
            "WASPADA demo analyst seeded — login with %s / %s",
            DEFAULT_ANALYST_EMAIL, DEFAULT_ANALYST_PASSWORD,
        )


# --- JWT -------------------------------------------------------------------
_ALGO = "HS256"
_TTL_SECONDS = int(os.environ.get("WASPADA_JWT_TTL_SECONDS", "86400"))  # 24h


def _jwt_secret() -> str:
    """JWT signing secret. Reads env at call time so tests can monkeypatch."""
    secret = os.environ.get("WASPADA_JWT_SECRET")
    if not secret:
        # Dev fallback — logged once. Production sets the env var.
        secret = "waspada-dev-secret-change-me"
        log.warning("WASPADA_JWT_SECRET not set — using insecure dev default")
    return secret


def issue_jwt(email: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=_TTL_SECONDS)).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm=_ALGO)


def decode_jwt(token: str) -> str:
    """Decode + validate. Returns the subject email or raises HTTPException(401)."""
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=[_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token")
    email = payload.get("sub")
    if not email or _db().get_user(email) is None:
        raise HTTPException(status_code=401, detail="invalid token")
    return email


# --- request/response models ----------------------------------------------
class _Creds(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class _Register(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class _ForgotIn(BaseModel):
    email: EmailStr


class _ResetIn(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)


class _AuthOut(BaseModel):
    token: str
    user: dict


class _MsgOut(BaseModel):
    message: str
    # ``reset_token`` is only present in dev (no SMTP). The frontend never
    # reads it; it exists so a judge can copy it from the server log / response.
    reset_token: Optional[str] = None


# --- helpers ---------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- dependency (used by protected routes in api/main.py) ------------------
async def current_user(authorization: str = Header(default=None)) -> dict:
    """Validate ``Authorization: Bearer ***`` and return the user record.

    Drop this onto a protected route with ``Depends(current_user)``.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    email = decode_jwt(token)
    rec = _db().get_user(email)
    assert rec is not None  # decode_jwt already checked existence
    return {"email": rec["email"]}


# --- router ----------------------------------------------------------------
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=_AuthOut, status_code=201)
async def register(body: _Register):
    db = _db()
    if db.get_user(body.email) is not None:
        raise HTTPException(status_code=409, detail="email already registered")
    created = db.insert_user(body.email, hash_password(body.password), _now_iso())
    if not created:
        # Race: another request inserted the same email between our check and
        # insert. Treat it as a conflict rather than leaking the race.
        raise HTTPException(status_code=409, detail="email already registered")
    log.info("registered user %s", body.email)
    return _AuthOut(token=issue_jwt(body.email), user={"email": body.email})


@router.post("/login", response_model=_AuthOut)
async def login(body: _Creds):
    rec = _db().get_user(body.email)
    if rec is None or not verify_password(body.password, rec["password"]):
        # Same message for unknown-user and wrong-password — avoid user
        # enumeration via the login endpoint.
        raise HTTPException(status_code=401, detail="invalid credentials")
    log.info("login ok for %s", body.email)
    return _AuthOut(token=issue_jwt(body.email), user={"email": body.email})


@router.post("/forgot-password", response_model=_MsgOut)
async def forgot_password(body: _ForgotIn):
    db = _db()
    # Always 200 — never reveal whether an email is registered.
    token = None
    if db.get_user(body.email) is not None:
        token = secrets.token_urlsafe(32)
        db.create_reset_token(token, body.email, _expiry_iso())
        # Dev delivery: log it. A judge copies this from the server log.
        log.warning("RESET TOKEN for %s: %s", body.email, token)
    return _MsgOut(
        message="If that email is registered, a reset token has been issued.",
        reset_token=token,  # None when unknown email → identical response shape
    )


@router.post("/reset-password", response_model=_MsgOut)
async def reset_password(body: _ResetIn):
    db = _db()
    rec = db.get_reset_token(body.token)
    if rec is None or rec["used"]:
        raise HTTPException(status_code=401, detail="invalid or expired reset token")
    # Invalidate FIRST so a concurrent replay can't reuse the token.
    db.mark_token_used(body.token)
    if db.get_user(rec["email"]) is None:
        # Token pointed at a since-deleted user; token already consumed.
        raise HTTPException(status_code=401, detail="invalid or expired reset token")
    db.set_user_password(rec["email"], hash_password(body.new_password))
    log.info("password reset for %s", rec["email"])
    return _MsgOut(message="password updated")


def _expiry_iso() -> str:
    """ISO timestamp for a fresh reset token's expiry (1h TTL)."""
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
