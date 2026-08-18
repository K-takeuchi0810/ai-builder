from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from builder import api
from builder.auth import AuthError, AuthStore


def test_invite_redeem_creates_user_session_and_enforces_use_limit(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    token, invite = store.create_invite(expires_hours=2, max_uses=1, now=1000)
    session_token, user = store.redeem(token, "参加者A", now=1001)
    assert user["role"] == "user" and user["display_name"] == "参加者A"
    assert user["expires_at"] == invite["expires_at"]
    assert store.get_session(session_token, now=1002)["user_id"] == user["user_id"]
    assert store.list_invites(now=1002)[0]["uses"] == 1
    with pytest.raises(AuthError, match="利用可能人数"):
        store.redeem(token, "参加者B", now=1003)
    assert invite["max_uses"] == 1


def test_user_access_ends_at_invite_expiry_but_admin_session_does_not(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    invite_token, invite = store.create_invite(expires_hours=1, now=1000)
    user_token, user = store.redeem(invite_token, "参加者", now=1001)
    admin_token, admin = store.create_admin_session(now=1001)

    assert store.get_session(user_token, now=invite["expires_at"] - 1)
    assert store.get_session(user_token, now=invite["expires_at"]) is None
    assert store.get_session(admin_token, now=invite["expires_at"])["role"] == "admin"
    listed = store.list_users(now=invite["expires_at"])
    assert listed[0]["effective_status"] == "expired"
    assert listed[0]["access_expired"] is True
    assert user["expires_at"] == invite["expires_at"]


def test_revoking_invite_immediately_ends_redeemed_user_access(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    invite_token, invite = store.create_invite(expires_hours=24, now=1000)
    user_token, _ = store.redeem(invite_token, "参加者", now=1001)

    assert store.get_session(user_token, now=1002)
    assert store.revoke_invite(invite["id"], now=1003)
    assert store.get_session(user_token, now=1004) is None
    listed = store.list_users(now=1004)
    assert listed[0]["effective_status"] == "revoked"


def test_legacy_users_without_invite_link_require_new_invite(tmp_path):
    path = tmp_path / "auth.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE users (id TEXT PRIMARY KEY, display_name TEXT NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'active', created_at INTEGER NOT NULL, "
            "last_seen INTEGER)"
        )
        conn.execute("INSERT INTO users VALUES ('old','旧利用者','active',1000,1001)")

    store = AuthStore(path)
    listed = store.list_users(now=2000)
    assert listed[0]["effective_status"] == "expired"
    assert listed[0]["access_expires_at"] is None


def test_disabled_user_loses_all_sessions(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    token, _ = store.create_invite(now=1000)
    session_token, user = store.redeem(token, "参加者", now=1001)
    assert store.set_user_status(user["user_id"], "disabled")
    assert store.get_session(session_token, now=1002) is None


def test_expired_and_revoked_invites_are_rejected(tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    expired, _ = store.create_invite(expires_hours=1, now=1000)
    with pytest.raises(AuthError, match="有効期限"):
        store.redeem(expired, "参加者", now=5000)
    revoked, invite = store.create_invite(now=1000)
    assert store.revoke_invite(invite["id"], now=1001)
    with pytest.raises(AuthError, match="無効化"):
        store.redeem(revoked, "参加者", now=1002)


def _request(url, path, *, body=None, cookie=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url + path, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, json.loads(res.read()), res.headers
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read()), err.headers


def test_shared_http_flow_issues_admin_and_invite_qr(tmp_path):
    api.configure_auth(enabled=True, db_path=tmp_path / "auth.db",
                       admin_password="a-long-admin-password",
                       public_url="https://mai.example.test")
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status, body, _ = _request(base, "/api/features")
        assert status == 401 and body["error"] == "authentication_required"
        status, body, headers = _request(
            base, "/api/auth/admin-login", body={"password": "a-long-admin-password"})
        assert status == 200 and body["user"]["role"] == "admin"
        assert isinstance(body["server_now"], int)
        assert headers["Cache-Control"] == "no-store"
        assert "Secure" in headers["Set-Cookie"]
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, invite, _ = _request(
            base, "/api/admin/invites",
            body={"expires_hours": 24, "max_uses": 2}, cookie=cookie)
        assert status == 201
        assert invite["url"].startswith("https://mai.example.test/?invite=")
        assert invite["qr_png"].startswith("data:image/png;base64,")
        token = invite["url"].split("invite=", 1)[1]
        status, user, user_headers = _request(
            base, "/api/auth/redeem", body={"token": token, "display_name": "参加者A"})
        assert status == 200 and user["user"]["role"] == "user"
        assert user["user"]["expires_at"] == invite["expires_at"]
        assert isinstance(user["server_now"], int)
        assert "HttpOnly" in user_headers["Set-Cookie"]
    finally:
        server.shutdown()
        server.server_close()
        api.configure_auth(enabled=False)
