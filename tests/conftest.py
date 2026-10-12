import re

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import hash_secret

PASS_A = "jingle bells batman smells"
PASS_B = "silent night holy night"


def make_settings(tmp_path):
    return Settings(
        secret_key="x" * 48,
        db_path=str(tmp_path / "test.db"),
        cookie_secure=False,
        turnstile_site_key="",
        turnstile_secret="",
        trust_cf_header=True,
        backup_dir=str(tmp_path / "backups"),
        backup_keep=3,
        session_days=30,
    )


class Browser:
    """A TestClient with its own cookie jar and client IP."""

    def __init__(self, app, ip):
        self.c = TestClient(app, headers={"cf-connecting-ip": ip})

    def csrf(self, path="/gate"):
        html = self.c.get(path).text
        m = re.search(r'name="csrf" value="([^"]+)"', html)
        assert m, f"no csrf token on {path}"
        return m.group(1)

    def get(self, path, **kw):
        return self.c.get(path, **kw)

    def post(self, path, data=None, csrf_from="/", **kw):
        data = dict(data or {})
        data.setdefault("csrf", self.csrf(csrf_from))
        return self.c.post(path, data=data, **kw)

    def enter(self, passphrase):
        return self.post("/gate", {"passphrase": passphrase}, csrf_from="/gate")

    def join(self, name, pin="1234"):
        return self.post("/join", {"name": name, "pin": pin, "pin2": pin})

    def login(self, person_id, pin):
        return self.post(f"/login/{person_id}", {"pin": pin})

    def logout(self):
        return self.post("/logout")


@pytest.fixture
def app(tmp_path):
    app = create_app(make_settings(tmp_path))
    db = app.state.db
    app.state.group_a = db.create_group("Smith Family", hash_secret(PASS_A))
    app.state.group_b = db.create_group("Work Exchange", hash_secret(PASS_B))
    return app


@pytest.fixture
def db(app):
    return app.state.db


_ip = [0]


@pytest.fixture
def browser(app):
    def make(ip=None):
        _ip[0] += 1
        return Browser(app, ip or f"10.0.0.{_ip[0]}")
    return make


def pid(db, group_id, name):
    return db.person_by_name(group_id, name)["id"]
