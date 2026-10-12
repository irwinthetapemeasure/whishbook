import datetime as dt

from app.main import current_year
from tests.conftest import PASS_A, PASS_B, pid


def item_ids(db, person_id, year=None):
    return [r["id"] for r in db.items_for_owner(person_id, year or current_year())]


def setup_family(app, db, browser):
    """Alice and Bob in group A, each with one item; returns their browsers."""
    alice, bob = browser(), browser()
    alice.enter(PASS_A)
    alice.join("Alice", "1111")
    bob.enter(PASS_A)
    bob.join("Bob", "2222")
    alice.post("/items", {"title": "Wool socks", "link": "example.com/socks"})
    bob.post("/items", {"title": "Board game"})
    return alice, bob


def _token(b):
    from app.security import Sessions
    return Sessions("x" * 48, 30).load(b.c.cookies.get("xmas_session"))["csrf"]


# ---- gate -------------------------------------------------------------------
def test_everything_requires_passphrase(browser):
    b = browser()
    for path in ["/", "/join", "/p/1", "/shopping", "/admin", "/account", "/login/1"]:
        r = b.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/gate", path


def test_healthz_is_public(browser):
    assert browser().get("/healthz").text == "ok"


def test_wrong_passphrase_locks_ip_out(browser):
    b = browser()
    for _ in range(10):
        b.enter("nope nope nope nope")
    assert "Too many wrong guesses" in b.get("/gate").text  # form is hidden
    r = b.c.post("/gate", data={"passphrase": PASS_A, "csrf": _token(b)})  # right one still refused
    assert r.status_code == 429
    assert "Too many wrong guesses" in r.text
    other = browser()  # another IP is unaffected
    assert "Who's here?" in other.enter(PASS_A).text


def test_passphrase_picks_the_group(app, db, browser):
    a, b = browser(), browser()
    assert "Smith Family" in a.enter(PASS_A).text
    assert "Work Exchange" in b.enter(PASS_B).text


# ---- groups are isolated ----------------------------------------------------
def test_groups_cannot_see_each_other(app, db, browser):
    alice, bob = setup_family(app, db, browser)
    carol = browser()
    carol.enter(PASS_B)
    carol.join("Carol", "3333")
    alice_id = pid(db, app.state.group_a, "Alice")
    sock = item_ids(db, alice_id)[0]

    assert "Alice" not in carol.get("/").text
    assert carol.get(f"/p/{alice_id}").status_code == 404
    assert carol.get(f"/login/{alice_id}").status_code == 404
    assert carol.post(f"/items/{sock}/claim").status_code == 404
    assert db.get_claim(sock) is None


def test_same_name_allowed_in_different_groups(app, db, browser):
    a, b = browser(), browser()
    a.enter(PASS_A)
    a.join("Sam")
    b.enter(PASS_B)
    b.join("Sam")
    assert db.person_by_name(app.state.group_a, "Sam")["id"] != db.person_by_name(app.state.group_b, "Sam")["id"]


def test_first_joiner_of_each_group_is_admin(app, db, browser):
    alice, bob = setup_family(app, db, browser)
    carol = browser()
    carol.enter(PASS_B)
    carol.join("Carol")
    assert db.person_by_name(app.state.group_a, "Alice")["is_admin"] == 1
    assert db.person_by_name(app.state.group_a, "Bob")["is_admin"] == 0
    assert db.person_by_name(app.state.group_b, "Carol")["is_admin"] == 1
    assert bob.get("/admin").status_code == 403


# ---- people / PINs ----------------------------------------------------------
def test_duplicate_name_rejected(app, db, browser):
    setup_family(app, db, browser)
    c = browser()
    c.enter(PASS_A)
    r = c.join("alice")
    assert "already someone called" in r.text
    assert db.count_people(app.state.group_a) == 2


def test_bad_names_and_pins_rejected(app, db, browser):
    c = browser()
    c.enter(PASS_A)
    assert "Names can be" in c.join("<script>").text
    assert "4 to 6 digits" in c.join("Dan", "12").text
    assert db.count_people(app.state.group_a) == 0


def test_pin_lockout(app, db, browser):
    setup_family(app, db, browser)
    alice_id = pid(db, app.state.group_a, "Alice")
    m = browser()
    m.enter(PASS_A)
    for _ in range(5):
        m.login(alice_id, "0000")
    r = m.login(alice_id, "1111")
    assert r.status_code == 429
    assert m.get("/shopping", follow_redirects=False).status_code == 303  # not logged in


def test_cannot_act_without_pin(app, db, browser):
    setup_family(app, db, browser)
    m = browser()
    m.enter(PASS_A)
    r = m.post("/items", {"title": "free stuff"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_pin_change_logs_out_other_sessions(app, db, browser):
    alice, _ = setup_family(app, db, browser)
    alice_id = pid(db, app.state.group_a, "Alice")
    phone = browser()
    phone.enter(PASS_A)
    phone.login(alice_id, "1111")
    assert phone.get("/shopping").status_code == 200
    alice.post("/account/pin", {"current": "1111", "pin": "9999", "pin2": "9999"}, csrf_from="/account")
    assert alice.get("/shopping").status_code == 200  # this browser stays in
    assert phone.get("/shopping", follow_redirects=False).status_code == 303


# ---- lists & claims ---------------------------------------------------------
def test_owner_never_sees_claims(app, db, browser):
    alice, bob = setup_family(app, db, browser)
    alice_id = pid(db, app.state.group_a, "Alice")
    sock = item_ids(db, alice_id)[0]
    bob.post(f"/items/{sock}/claim")
    assert db.get_claim(sock)["claimer_id"] == pid(db, app.state.group_a, "Bob")

    mine = alice.get(f"/p/{alice_id}").text
    assert "Wool socks" in mine
    assert "getting this" not in mine and "claimed" not in mine and "Bob" not in mine

    carol = browser()
    carol.enter(PASS_A)
    carol.join("Carol")
    assert "Bob is getting this" in carol.get(f"/p/{alice_id}").text
    assert "Wool socks" in bob.get("/shopping").text


def test_cannot_claim_own_or_double_claim_or_unclaim_others(app, db, browser):
    alice, bob = setup_family(app, db, browser)
    alice_id = pid(db, app.state.group_a, "Alice")
    sock = item_ids(db, alice_id)[0]
    assert alice.post(f"/items/{sock}/claim").status_code == 403
    bob.post(f"/items/{sock}/claim")
    carol = browser()
    carol.enter(PASS_A)
    carol.join("Carol")
    assert "already got that one" in carol.post(f"/items/{sock}/claim").text
    carol.post(f"/items/{sock}/unclaim")
    assert db.get_claim(sock)["claimer_id"] == pid(db, app.state.group_a, "Bob")


def test_cannot_edit_someone_elses_items(app, db, browser):
    alice, bob = setup_family(app, db, browser)
    sock = item_ids(db, pid(db, app.state.group_a, "Alice"))[0]
    assert bob.post(f"/items/{sock}/delete").status_code == 404
    assert bob.post(f"/items/{sock}/edit", {"title": "hacked"}).status_code == 404
    assert bob.get(f"/items/{sock}/edit").status_code == 404
    assert db.get_item(sock)["title"] == "Wool socks"


def test_past_years_are_read_only_and_viewable(app, db, browser):
    alice, bob = setup_family(app, db, browser)
    alice_id = pid(db, app.state.group_a, "Alice")
    last = current_year() - 1
    old = db.add_item(alice_id, last, "Old scarf", "", "", "", 2)
    assert "Old scarf" in bob.get(f"/p/{alice_id}?year={last}").text
    assert str(last) in bob.get(f"/p/{alice_id}").text  # year tab shown
    assert alice.post(f"/items/{old}/delete").status_code == 403
    assert bob.post(f"/items/{old}/claim").status_code == 403
    assert db.get_item(old) is not None


def test_copy_last_year(app, db, browser):
    alice, _ = setup_family(app, db, browser)
    alice_id = pid(db, app.state.group_a, "Alice")
    for i in item_ids(db, alice_id):
        db.delete_item(i)
    db.add_item(alice_id, current_year() - 1, "Old scarf", "", "", "", 2)
    assert "Start from last year" in alice.get(f"/p/{alice_id}").text
    alice.post("/items/copy-last-year")
    assert [r["title"] for r in db.items_for_owner(alice_id, current_year())] == ["Old scarf"]


def test_links_must_be_http(app, db, browser):
    alice, _ = setup_family(app, db, browser)
    alice_id = pid(db, app.state.group_a, "Alice")
    r = alice.post("/items", {"title": "x", "link": "javascript:alert(1)"})
    assert "look like a web address" in r.text
    items = db.items_for_owner(alice_id, current_year())
    assert [i["link"] for i in items] == ["https://example.com/socks"]


def test_html_is_escaped(app, db, browser):
    alice, bob = setup_family(app, db, browser)
    alice.post("/items", {"title": "<img src=x onerror=alert(1)>"})
    html = bob.get(f"/p/{pid(db, app.state.group_a, 'Alice')}").text
    assert "<img src=x" not in html and "&lt;img" in html


def test_item_limit(app, db, browser):
    alice, _ = setup_family(app, db, browser)
    alice_id = pid(db, app.state.group_a, "Alice")
    for i in range(99):
        db.add_item(alice_id, current_year(), f"thing {i}", "", "", "", 2)
    r = alice.post("/items", {"title": "one too many"})
    assert "limited to 100" in r.text


# ---- admin ------------------------------------------------------------------
def test_admin_passphrase_change_only_affects_own_group(app, db, browser):
    alice, bob = setup_family(app, db, browser)
    carol = browser()
    carol.enter(PASS_B)
    carol.join("Carol")
    alice.post("/admin/passphrase", {"passphrase": "new family words here", "passphrase2": "new family words here"},
               csrf_from="/admin")
    assert alice.get("/admin").status_code == 200
    assert bob.get("/", follow_redirects=False).headers["location"] == "/gate"
    assert carol.get("/").status_code == 200
    d = browser()
    assert "Smith Family" in d.enter("new family words here").text


def test_admin_cannot_reuse_other_groups_passphrase(app, db, browser):
    alice, _ = setup_family(app, db, browser)
    r = alice.post("/admin/passphrase", {"passphrase": PASS_B, "passphrase2": PASS_B}, csrf_from="/admin")
    assert "Pick a different passphrase" in r.text


def test_admin_cannot_touch_other_groups(app, db, browser):
    alice, _ = setup_family(app, db, browser)
    carol = browser()
    carol.enter(PASS_B)
    carol.join("Carol")
    carol_id = pid(db, app.state.group_b, "Carol")
    assert alice.post(f"/admin/people/{carol_id}/pin", {"pin": "0000"}, csrf_from="/admin").status_code == 404
    assert alice.post(f"/admin/people/{carol_id}/delete", {"confirm_name": "Carol"}, csrf_from="/admin").status_code == 404


def test_admin_reset_pin(app, db, browser):
    alice, bob = setup_family(app, db, browser)
    bob_id = pid(db, app.state.group_a, "Bob")
    alice.post(f"/admin/people/{bob_id}/pin", {"pin": "5555"}, csrf_from="/admin")
    assert bob.get("/shopping", follow_redirects=False).status_code == 303
    assert bob.login(bob_id, "5555").status_code == 200
    assert bob.get("/shopping").status_code == 200


# ---- web hardening ----------------------------------------------------------
def test_csrf_required(app, db, browser):
    alice, _ = setup_family(app, db, browser)
    r = alice.c.post("/items", data={"title": "no token"})
    assert r.status_code == 400
    r = alice.c.post("/items", data={"title": "bad token", "csrf": "forged"})
    assert r.status_code == 400


def test_security_headers_and_cookie_flags(browser):
    r = browser().get("/gate")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_large_body_rejected(browser):
    b = browser()
    r = b.c.post("/gate", data={"passphrase": "x" * 40000})
    assert r.status_code == 413


def test_tampered_session_cookie_ignored(app, db, browser):
    alice, _ = setup_family(app, db, browser)
    alice.c.cookies.set("xmas_session", "garbage")
    assert alice.get("/", follow_redirects=False).headers["location"] == "/gate"


def test_backup(app, db, tmp_path):
    p = db.backup(str(tmp_path / "bk"), keep=2)
    assert p.endswith(".db")
