"""Hashing, signed sessions, CSRF, lockouts and Turnstile verification."""
import hmac
import secrets
import time
from typing import Optional

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .db import Database

_hasher = PasswordHasher()
# Used so a check against a missing hash still costs the same time.
_DUMMY_HASH = _hasher.hash("not-a-real-secret")

SESSION_COOKIE = "xmas_session"


def hash_secret(secret: str) -> str:
    return _hasher.hash(secret)


def verify_secret(stored_hash: str, secret: str) -> bool:
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, secret) and bool(stored_hash)
    except (VerificationError, InvalidHashError):
        return False


# ---- sessions -------------------------------------------------------------
class Sessions:
    """Session data lives in a signed (not encrypted) cookie; it holds no secrets."""

    def __init__(self, secret_key: str, max_age_days: int):
        self.serializer = URLSafeTimedSerializer(secret_key, salt="xmas-session")
        self.max_age = max_age_days * 86400

    def load(self, raw: Optional[str]) -> dict:
        if not raw:
            return {}
        try:
            data = self.serializer.loads(raw, max_age=self.max_age)
        except BadSignature:
            return {}
        return data if isinstance(data, dict) else {}

    def dump(self, data: dict) -> str:
        return self.serializer.dumps(data)


def new_csrf() -> str:
    return secrets.token_urlsafe(24)


def csrf_ok(session: dict, submitted: Optional[str]) -> bool:
    expected = session.get("csrf")
    return bool(expected and submitted) and hmac.compare_digest(expected, submitted)


# ---- lockouts -------------------------------------------------------------
class Limiter:
    """Counts failures per key inside a window and locks the key out when exceeded.

    Each new lockout for the same key doubles in length (capped)."""

    def __init__(self, db: Database):
        self.db = db

    def locked_for(self, key: str) -> int:
        with self.db.connect() as c:
            row = c.execute("SELECT locked_until FROM login_attempts WHERE key=?", (key,)).fetchone()
        if not row:
            return 0
        return max(0, int(row["locked_until"] - time.time()))

    def fail(self, key: str, max_fails: int, window: int, base_lock: int, max_lock: int) -> None:
        now = time.time()
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM login_attempts WHERE key=?", (key,)).fetchone()
            fails, start, lock_count = (0, now, 0)
            if row:
                lock_count = row["lock_count"]
                if now - row["window_start"] < window:
                    fails, start = row["fails"], row["window_start"]
            fails += 1
            locked_until = row["locked_until"] if row else 0
            if fails >= max_fails:
                lock_count += 1
                locked_until = now + min(max_lock, base_lock * 2 ** (lock_count - 1))
                fails, start = 0, now
            c.execute(
                "INSERT INTO login_attempts(key, fails, window_start, locked_until, lock_count) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET fails=excluded.fails, "
                "window_start=excluded.window_start, locked_until=excluded.locked_until, "
                "lock_count=excluded.lock_count",
                (key, fails, start, locked_until, lock_count),
            )

    def hit(self, key: str, max_hits: int, window: int) -> bool:
        """Plain rate limit for non-login actions. Returns False when over the limit."""
        if self.locked_for(key):
            return False
        self.fail(key, max_hits + 1, window, window, window)
        return not self.locked_for(key)

    def reset(self, key: str) -> None:
        with self.db.connect() as c:
            c.execute("DELETE FROM login_attempts WHERE key=?", (key,))


# ---- Cloudflare Turnstile -------------------------------------------------
TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


async def turnstile_ok(secret: str, token: Optional[str], ip: str) -> bool:
    if not secret:
        return True  # Turnstile not configured
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(
                TURNSTILE_URL, data={"secret": secret, "response": token, "remoteip": ip}
            )
            return bool(r.json().get("success"))
    except Exception:
        return False  # fail closed
