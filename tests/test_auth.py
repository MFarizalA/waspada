"""WA-028 — auth tests.

Covers register, login (success + failure), forgot/reset-password flow, the
JWT gate on /api/run, and the on-startup default-user seed. Uses FastAPI's
TestClient (httpx-backed) so the full request lifecycle — including the
Authorization header parsing in ``current_user`` — is exercised.

All tests run offline: WASPADA_LLM_PROVIDER is forced to ``mock`` by
conftest's autouse fixture, and the /api/run happy-path below relies on the
deterministic synthetic snapshot already used by api.main.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    """Fresh app + clean auth stores for each test.

    A stable JWT secret keeps tests deterministic; clearing the stores first
    means every test starts from a known-empty state regardless of module
    import order or the startup seed.
    """
    monkeypatch.setenv("WASPADA_JWT_SECRET", "waspada-test-secret-fixed-0123456789")
    from api import main as main_mod
    from api import auth as auth_mod

    auth_mod.reset_store()
    # Re-seed so the default analyst is present in most tests; tests that
    # want an empty store can call reset_store() again themselves.
    auth_mod.seed_default_user()

    # TestClient triggers the startup event (which would also seed), but we
    # already seeded explicitly above; rebuild to pick up env cleanly.
    with TestClient(main_mod.app) as c:
        # WA-077: the lifespan OSS probe ran at startup (before any patch of
        # _probe_oss could apply), so force the probe state here or the
        # run-gate 503s on these no-OSS-cred tests.
        main_mod.app.state.oss_available = True
        yield c

    auth_mod.reset_store()


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------
class TestRegister:
    def test_register_returns_jwt_and_creates_user(self, client):
        resp = client.post("/api/auth/register", json={
            "email": "new@waspada.demo",
            "password": "supersecret1",
        })
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "token" in body
        assert body["user"]["email"] == "new@waspada.demo"
        # the issued JWT must round-trip
        from api import auth
        assert auth.decode_jwt(body["token"]) == "new@waspada.demo"

    def test_register_duplicate_email_is_409(self, client):
        payload = {"email": "dup@waspada.demo", "password": "supersecret1"}
        assert client.post("/api/auth/register", json=payload).status_code == 201
        second = client.post("/api/auth/register", json=payload)
        assert second.status_code == 409

    def test_register_short_password_rejected(self, client):
        resp = client.post("/api/auth/register", json={
            "email": "short@waspada.demo", "password": "1234567",  # 7 chars
        })
        assert resp.status_code == 422

    def test_register_bad_email_rejected(self, client):
        resp = client.post("/api/auth/register", json={
            "email": "not-an-email", "password": "supersecret1",
        })
        assert resp.status_code == 422

    def test_registered_password_is_never_plaintext(self, client):
        pw = "supersecret1"
        client.post("/api/auth/register", json={
            "email": "plain@waspada.demo", "password": pw,
        })
        from api import auth
        stored = auth._db().get_user("plain@waspada.demo")["password"]
        assert stored != pw
        assert "supersecret" not in stored
        assert stored.startswith("$2")  # bcrypt marker


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------
class TestLogin:
    def test_login_success_returns_jwt(self, client):
        client.post("/api/auth/register", json={
            "email": "login@waspada.demo", "password": "supersecret1",
        })
        resp = client.post("/api/auth/login", json={
            "email": "login@waspada.demo", "password": "supersecret1",
        })
        assert resp.status_code == 200, resp.text
        assert "token" in resp.json()

    def test_login_wrong_password_is_401(self, client):
        client.post("/api/auth/register", json={
            "email": "login@waspada.demo", "password": "supersecret1",
        })
        resp = client.post("/api/auth/login", json={
            "email": "login@waspada.demo", "password": "WRONG-password-99",
        })
        assert resp.status_code == 401

    def test_login_unknown_user_is_401(self, client):
        resp = client.post("/api/auth/login", json={
            "email": "ghost@waspada.demo", "password": "supersecret1",
        })
        assert resp.status_code == 401

    def test_login_unknown_user_and_wrong_password_same_detail(self, client):
        """Avoid user enumeration: same error detail for both failure causes."""
        client.post("/api/auth/register", json={
            "email": "known@waspada.demo", "password": "supersecret1",
        })
        r1 = client.post("/api/auth/login", json={
            "email": "ghost@waspada.demo", "password": "supersecret1",
        })
        r2 = client.post("/api/auth/login", json={
            "email": "known@waspada.demo", "password": "wrong-password-99",
        })
        assert r1.status_code == r2.status_code == 401
        assert r1.json()["detail"] == r2.json()["detail"]


# ---------------------------------------------------------------------------
# forgot-password / reset-password
# ---------------------------------------------------------------------------
class TestResetFlow:
    def test_forgot_then_reset_changes_password(self, client):
        client.post("/api/auth/register", json={
            "email": "reset@waspada.demo", "password": "supersecret1",
        })
        # request reset → server returns the dev reset token
        forgot = client.post("/api/auth/forgot-password", json={
            "email": "reset@waspada.demo",
        })
        assert forgot.status_code == 200
        token = forgot.json()["reset_token"]
        assert token, "dev mode should surface the reset token"

        # use it
        new_pw = "brand-new-password-9"
        reset = client.post("/api/auth/reset-password", json={
            "token": token, "new_password": new_pw,
        })
        assert reset.status_code == 200, reset.text

        # old password no longer works, new one does
        assert client.post("/api/auth/login", json={
            "email": "reset@waspada.demo", "password": "supersecret1",
        }).status_code == 401
        assert client.post("/api/auth/login", json={
            "email": "reset@waspada.demo", "password": new_pw,
        }).status_code == 200

    def test_reset_token_is_single_use(self, client):
        client.post("/api/auth/register", json={
            "email": "reset@waspada.demo", "password": "supersecret1",
        })
        token = client.post("/api/auth/forgot-password", json={
            "email": "reset@waspada.demo",
        }).json()["reset_token"]
        client.post("/api/auth/reset-password", json={
            "token": token, "new_password": "brand-new-password-9",
        })
        # same token can't be reused
        again = client.post("/api/auth/reset-password", json={
            "token": token, "new_password": "another-password-9",
        })
        assert again.status_code == 401

    def test_reset_with_garbage_token_is_401(self, client):
        resp = client.post("/api/auth/reset-password", json={
            "token": "not-a-real-token", "new_password": "brand-new-password-9",
        })
        assert resp.status_code == 401

    def test_forgot_unknown_email_returns_same_shape_no_token(self, client):
        """Unknown email → same message, no reset_token (no enumeration)."""
        known = client.post("/api/auth/forgot-password", json={
            "email": "analyst@waspada.demo",  # seeded
        }).json()
        unknown = client.post("/api/auth/forgot-password", json={
            "email": "ghost@waspada.demo",
        }).json()
        assert known["message"] == unknown["message"]
        assert unknown["reset_token"] is None


# ---------------------------------------------------------------------------
# JWT gate on /api/run
# ---------------------------------------------------------------------------
class TestRunGate:
    def test_run_without_token_is_401(self, client):
        resp = client.post("/api/run")
        assert resp.status_code == 401

    def test_run_with_garbage_token_is_401(self, client):
        resp = client.post("/api/run", headers={"Authorization": "Bearer garbage"})
        assert resp.status_code == 401

    def test_run_with_valid_token_passes_gate(self, client, monkeypatch):
        # login as the seeded analyst, then hit /api/run with the JWT
        login = client.post("/api/auth/login", json={
            "email": "analyst@waspada.demo",
            "password": "waspada123",
        })
        assert login.status_code == 200
        token = login.json()["token"]

        # WA-077: /api/run fetches from OSS in prod; tests have no OSS creds,
        # so stub the Data Engineer's fetch tool (per conftest's contract:
        # each test injects its own data stub).
        from api import main as main_mod
        from waspada.agents.__main__ import _sample_raw_table
        from waspada.agents.data_engineer import DataEngineerAgent

        def _test_fetch(*, lane="collections", limit=None):
            return _sample_raw_table(n=60)

        orig_build = main_mod._build_orchestrator

        def _build_with_test_fetch(brain: str = "mock", **kwargs):
            orch = orig_build(brain, **kwargs)
            orig = orch._build_agents

            def _build():
                agents = orig()
                for a in agents:
                    if isinstance(a, DataEngineerAgent):
                        a.register_tool("fetch", _test_fetch)
                return agents

            orch._build_agents = _build  # type: ignore[method-assign]
            return orch

        monkeypatch.setattr(main_mod, "_build_orchestrator", _build_with_test_fetch)

        resp = client.post("/api/run", headers={"Authorization": f"Bearer {token}"})
        # 200 means the gate passed; the pipeline itself must also succeed.
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "payload" in body and "report" in body

    def test_run_with_expired_token_is_401(self, client, monkeypatch):
        from api import auth
        # Issue a token, then rewind the clock by tampering exp via re-encode
        # is fiddly; instead verify decode_jwt rejects an explicitly expired
        # token by constructing one with exp in the past.
        import jwt as pyjwt
        import time as _t
        payload = {
            "sub": "analyst@waspada.demo",
            "iat": int(_t.time()) - 100,
            "exp": int(_t.time()) - 10,  # expired 10s ago
        }
        expired = pyjwt.encode(payload, "waspada-test-secret-fixed-0123456789", algorithm="HS256")
        resp = client.post("/api/run", headers={"Authorization": f"Bearer {expired}"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# default-user seed
# ---------------------------------------------------------------------------
class TestSeed:
    def test_seeded_analyst_exists_and_is_hashed(self, client):
        from api import auth
        rec = auth._db().get_user(auth.DEFAULT_ANALYST_EMAIL)
        assert rec is not None
        assert rec["password"].startswith("$2")
        assert rec["password"] != auth.DEFAULT_ANALYST_PASSWORD

    def test_seeded_analyst_can_login(self, client):
        resp = client.post("/api/auth/login", json={
            "email": "analyst@waspada.demo",
            "password": "waspada123",
        })
        assert resp.status_code == 200

    def test_seed_is_idempotent(self, monkeypatch):
        from api import auth
        monkeypatch.setenv("WASPADA_JWT_SECRET", "test-secret-fixed")
        auth.reset_store()
        auth.seed_default_user()
        first_hash = auth._db().get_user(auth.DEFAULT_ANALYST_EMAIL)["password"]
        auth.seed_default_user()  # second call must not re-create / re-hash
        assert auth._db().get_user(auth.DEFAULT_ANALYST_EMAIL)["password"] == first_hash
        assert auth._db().count_users() == 1
