"""Settings read from environment variables."""
import os
from dataclasses import dataclass


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    secret_key: str
    db_path: str
    cookie_secure: bool
    turnstile_site_key: str
    turnstile_secret: str
    trust_cf_header: bool
    backup_dir: str
    backup_keep: int
    session_days: int


def load_settings() -> Settings:
    secret = os.environ.get("SECRET_KEY", "")
    if len(secret) < 32:
        raise RuntimeError(
            "SECRET_KEY must be set to a random string of at least 32 characters "
            "(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
        )
    data_dir = os.environ.get("DATA_DIR", "/data")
    return Settings(
        secret_key=secret,
        db_path=os.environ.get("DB_PATH", os.path.join(data_dir, "christmas.db")),
        cookie_secure=_bool("COOKIE_SECURE", True),
        turnstile_site_key=os.environ.get("TURNSTILE_SITE_KEY", ""),
        turnstile_secret=os.environ.get("TURNSTILE_SECRET_KEY", ""),
        trust_cf_header=_bool("TRUST_CF_CONNECTING_IP", True),
        backup_dir=os.environ.get("BACKUP_DIR", os.path.join(data_dir, "backups")),
        backup_keep=int(os.environ.get("BACKUP_KEEP", "14")),
        session_days=int(os.environ.get("SESSION_DAYS", "30")),
    )
