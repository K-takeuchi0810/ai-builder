"""共有運用向けの招待・利用者・セッション管理。

外部の認証サービスに登録人数を依存させず、期限付き招待QRから利用者を作る。
秘密値は平文保存せず SHA-256 だけをSQLiteへ保存する。
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
import uuid
from pathlib import Path


SESSION_DAYS = 30


def _now(value: int | None) -> int:
    return int(time.time() if value is None else value)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token() -> str:
    return secrets.token_urlsafe(32)


class AuthError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AuthStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at INTEGER NOT NULL,
                    last_seen INTEGER,
                    invite_id TEXT,
                    access_expires_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS invites (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    max_uses INTEGER NOT NULL,
                    uses INTEGER NOT NULL DEFAULT 0,
                    revoked_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT,
                    role TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);
            """)
            # 共有運用の初期版には利用者と招待の紐付けが無かった。既存DBを
            # 壊さず列だけ追加し、期限が不明な旧利用者は get_session で失効扱いにする。
            user_columns = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
            if "invite_id" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN invite_id TEXT")
            if "access_expires_at" not in user_columns:
                conn.execute("ALTER TABLE users ADD COLUMN access_expires_at INTEGER")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_invite ON users(invite_id)")

    def _session(self, conn: sqlite3.Connection, *, user_id: str | None,
                 role: str, display_name: str, now: int,
                 access_expires_at: int | None = None) -> tuple[str, dict]:
        token = _token()
        expires = now + SESSION_DAYS * 86400
        if access_expires_at is not None:
            expires = min(expires, int(access_expires_at))
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_hash(token), user_id, role, display_name, now, expires, now),
        )
        return token, {"user_id": user_id, "role": role,
                       "display_name": display_name, "expires_at": expires}

    def create_admin_session(self, *, now: int | None = None) -> tuple[str, dict]:
        now = _now(now)
        with self._connect() as conn:
            return self._session(conn, user_id=None, role="admin",
                                 display_name="管理者", now=now)

    def create_invite(self, *, expires_hours: int = 24, max_uses: int = 1,
                      now: int | None = None) -> tuple[str, dict]:
        if not 1 <= int(expires_hours) <= 24 * 30:
            raise AuthError("invalid_expiry", "有効期間は1時間から30日で指定してください")
        if not 1 <= int(max_uses) <= 1000:
            raise AuthError("invalid_max_uses", "利用可能人数は1人から1000人で指定してください")
        now = _now(now)
        invite_id, token = uuid.uuid4().hex, _token()
        expires = now + int(expires_hours) * 3600
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO invites(id,token_hash,created_at,expires_at,max_uses) "
                "VALUES(?,?,?,?,?)",
                (invite_id, _hash(token), now, expires, int(max_uses)),
            )
        return token, {"id": invite_id, "created_at": now, "expires_at": expires,
                       "max_uses": int(max_uses), "uses": 0, "revoked": False}

    def redeem(self, token: str, display_name: str,
               *, now: int | None = None) -> tuple[str, dict]:
        name = " ".join(str(display_name or "").strip().split())
        if not 1 <= len(name) <= 40:
            raise AuthError("invalid_name", "表示名を1文字から40文字で入力してください")
        now = _now(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM invites WHERE token_hash=?", (_hash(str(token)),)
            ).fetchone()
            if not row:
                raise AuthError("invite_not_found", "この招待QRは使用できません")
            if row["revoked_at"] is not None:
                raise AuthError("invite_revoked", "この招待QRは無効化されています")
            if row["expires_at"] <= now:
                raise AuthError("invite_expired", "この招待QRの有効期限が切れています")
            if row["uses"] >= row["max_uses"]:
                raise AuthError("invite_used", "この招待QRは利用可能人数に達しています")
            conn.execute("UPDATE invites SET uses=uses+1 WHERE id=?", (row["id"],))
            user_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO users(id,display_name,created_at,last_seen,invite_id,"
                "access_expires_at) VALUES(?,?,?,?,?,?)",
                (user_id, name, now, now, row["id"], row["expires_at"]),
            )
            return self._session(conn, user_id=user_id, role="user",
                                 display_name=name, now=now,
                                 access_expires_at=row["expires_at"])

    def get_session(self, token: str | None, *, now: int | None = None) -> dict | None:
        if not token:
            return None
        now = _now(now)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT s.*, u.status AS user_status, u.access_expires_at, "
                "u.invite_id, i.revoked_at AS invite_revoked_at "
                "FROM sessions s LEFT JOIN users u ON u.id=s.user_id "
                "LEFT JOIN invites i ON i.id=u.invite_id "
                "WHERE s.token_hash=?", (_hash(token),)
            ).fetchone()
            invalid = not row or row["expires_at"] <= now
            if row and row["user_id"]:
                # access_expires_at が NULL の旧利用者は、どの招待期限に属するか
                # 証明できないため再招待が必要。管理者セッションはこの判定の対象外。
                invalid = invalid or row["user_status"] == "disabled"
                invalid = invalid or not row["access_expires_at"]
                invalid = invalid or int(row["access_expires_at"] or 0) <= now
                invalid = invalid or row["invite_revoked_at"] is not None
            if invalid:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash(token),))
                return None
            conn.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, _hash(token)))
            if row["user_id"]:
                conn.execute("UPDATE users SET last_seen=? WHERE id=?", (now, row["user_id"]))
            return {"user_id": row["user_id"], "role": row["role"],
                    "display_name": row["display_name"], "expires_at": row["expires_at"]}

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (_hash(token),))

    def list_invites(self, *, now: int | None = None) -> list[dict]:
        now = _now(now)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM invites ORDER BY created_at DESC").fetchall()
        return [{"id": r["id"], "created_at": r["created_at"],
                 "expires_at": r["expires_at"], "max_uses": r["max_uses"],
                 "uses": r["uses"], "revoked": r["revoked_at"] is not None,
                 "expired": r["expires_at"] <= now} for r in rows]

    def revoke_invite(self, invite_id: str, *, now: int | None = None) -> bool:
        with self._connect() as conn:
            cur = conn.execute("UPDATE invites SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                               (_now(now), invite_id))
            if cur.rowcount:
                conn.execute(
                    "DELETE FROM sessions WHERE user_id IN "
                    "(SELECT id FROM users WHERE invite_id=?)", (invite_id,)
                )
            return cur.rowcount > 0

    def list_users(self, *, now: int | None = None) -> list[dict]:
        now = _now(now)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT u.id,u.display_name,u.status,u.created_at,u.last_seen,"
                "u.invite_id,u.access_expires_at,i.revoked_at AS invite_revoked_at "
                "FROM users u LEFT JOIN invites i ON i.id=u.invite_id "
                "ORDER BY u.created_at DESC"
            ).fetchall()
        users = []
        for row in rows:
            user = dict(row)
            expired = (not user["access_expires_at"] or
                       int(user["access_expires_at"] or 0) <= now)
            revoked = user["invite_revoked_at"] is not None
            if user["status"] == "disabled":
                effective = "disabled"
            elif revoked:
                effective = "revoked"
            elif expired:
                effective = "expired"
            else:
                effective = "active"
            user.update(access_expired=expired, invite_revoked=revoked,
                        effective_status=effective)
            users.append(user)
        return users

    def set_user_status(self, user_id: str, status: str) -> bool:
        if status not in ("active", "disabled"):
            raise AuthError("invalid_status", "利用者状態が正しくありません")
        with self._connect() as conn:
            cur = conn.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
            if status == "disabled":
                conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return cur.rowcount > 0
