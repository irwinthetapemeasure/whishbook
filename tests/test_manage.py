import pytest

from app import manage
from app.security import verify_secret


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "m.db"))
    return tmp_path


def answers(monkeypatch, *values):
    it = iter(values)
    monkeypatch.setattr(manage.getpass, "getpass", lambda prompt="": next(it))


def test_add_and_list_groups(env, monkeypatch, capsys):
    answers(monkeypatch, "correct horse battery", "correct horse battery")
    manage.main(["add-group", "Smith Family"])
    manage.main(["list"])
    out = capsys.readouterr().out
    assert "Smith Family: 0 people" in out
    g = manage._db().group_by_name("smith family")
    assert verify_secret(g["passphrase_hash"], "correct horse battery")


def test_passphrase_must_be_unique_and_long(env, monkeypatch):
    answers(monkeypatch, "correct horse battery", "correct horse battery")
    manage.main(["add-group", "A"])
    answers(monkeypatch, "correct horse battery", "correct horse battery")
    with pytest.raises(SystemExit, match="already used"):
        manage.main(["add-group", "B"])
    answers(monkeypatch, "short", "short")
    with pytest.raises(SystemExit, match="characters"):
        manage.main(["add-group", "B"])


def test_set_passphrase_bumps_version(env, monkeypatch):
    answers(monkeypatch, "correct horse battery", "correct horse battery")
    manage.main(["add-group", "A"])
    answers(monkeypatch, "another long phrase", "another long phrase")
    manage.main(["set-passphrase", "A"])
    g = manage._db().group_by_name("A")
    assert g["gate_version"] == 2
    assert verify_secret(g["passphrase_hash"], "another long phrase")
