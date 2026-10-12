from app.questionnaire import TEMPLATES
from tests.conftest import PASS_A, PASS_B, pid


def where(r):
    assert r.status_code == 303, r.status_code
    return r.headers["location"]


def admin_and_member(app, browser):
    """Alice (admin, first to join) and Bob in group A."""
    alice, bob = browser(), browser()
    alice.enter(PASS_A)
    alice.join("Alice", "1111")
    bob.enter(PASS_A)
    bob.join("Bob", "2222")
    return alice, bob


def turn_on(b):
    return b.post("/admin/questionnaire/enabled", {"enabled": "1"})


def answer_form(db, group_id, **by_text):
    return {f"q{q['id']}": by_text.get(q["text"], "") for q in db.quiz_questions(group_id)}


def test_off_by_default(app, db, browser):
    alice, _ = admin_and_member(app, browser)
    b = browser()
    b.enter(PASS_A)
    assert where(b.post("/join", {"name": "Cara", "pin": "3333", "pin2": "3333"},
                        follow_redirects=False)).startswith("/p/")
    assert alice.get("/questionnaire").status_code == 404
    assert "About Bob" not in alice.get(f"/p/{pid(db, app.state.group_a, 'Bob')}").text


def test_turning_on_loads_family_template(app, db, browser):
    alice, _ = admin_and_member(app, browser)
    turn_on(alice)
    qs = db.quiz_questions(app.state.group_a)
    assert [q["text"] for q in qs] == [t for t, _ in TEMPLATES["family"]["questions"]]
    assert db.quiz_enabled(app.state.group_a) and not db.quiz_enabled(app.state.group_b)


def test_new_joiner_is_asked_and_can_skip_until_they_save(app, db, browser):
    alice, _ = admin_and_member(app, browser)
    turn_on(alice)
    cara = browser()
    cara.enter(PASS_A)
    r = cara.post("/join", {"name": "Cara", "pin": "3333", "pin2": "3333"}, follow_redirects=False)
    assert where(r) == "/questionnaire"
    page = cara.get("/questionnaire").text
    assert "Favorite Candy" in page and "Skip for now" in page
    cid = pid(db, app.state.group_a, "Cara")

    # Skipping and logging in again asks again.
    cara.logout()
    assert where(cara.post(f"/login/{cid}", {"pin": "3333"}, follow_redirects=False)) == "/questionnaire"

    # Saving (even mostly blank) counts as done.
    cara.post("/questionnaire", answer_form(db, app.state.group_a, **{"Favorite Candy": "Smarties"}))
    cara.logout()
    assert where(cara.post(f"/login/{cid}", {"pin": "3333"}, follow_redirects=False)) == f"/p/{cid}"
    assert "Skip for now" not in cara.get("/questionnaire").text


def test_answers_shown_to_the_group_only_while_on(app, db, browser):
    alice, bob = admin_and_member(app, browser)
    turn_on(alice)
    bob.post("/questionnaire", answer_form(db, app.state.group_a, **{
        "Favorite Cereal": "Cheerios", "2 Gifts I want the most": "Lego\nA telescope"}))
    bob_id = pid(db, app.state.group_a, "Bob")

    page = alice.get(f"/p/{bob_id}").text
    assert "About Bob" in page and "Cheerios" in page and "A telescope" in page
    assert "Favorite Movie" not in page  # blank answers aren't listed
    assert "Cheerios" in bob.get(f"/p/{bob_id}").text  # owner sees their own

    alice.post("/admin/questionnaire/enabled", {"enabled": "0"})
    assert "Cheerios" not in alice.get(f"/p/{bob_id}").text
    assert db.quiz_answers(bob_id)  # hidden, not deleted


def test_new_question_asks_everyone_again(app, db, browser):
    alice, bob = admin_and_member(app, browser)
    turn_on(alice)
    bob.post("/questionnaire", answer_form(db, app.state.group_a))
    bob_id = pid(db, app.state.group_a, "Bob")
    assert not db.quiz_pending(bob_id, app.state.group_a)
    alice.post("/admin/questionnaire/questions", {"text": "Favourite board game", "long": ""})
    assert db.quiz_pending(bob_id, app.state.group_a)


def test_admin_can_edit_reorder_remove_and_load_templates(app, db, browser):
    alice, _ = admin_and_member(app, browser)
    g = app.state.group_a
    turn_on(alice)
    first, second = db.quiz_questions(g)[:2]
    alice.post(f"/admin/questionnaire/questions/{first['id']}/edit", {"text": "  Favourite   colour ", "long": "1"})
    q = db.get_quiz_question(first["id"])
    assert q["text"] == "Favourite colour" and q["long"] == 1
    alice.post(f"/admin/questionnaire/questions/{first['id']}/move", {"direction": "down"})
    assert [r["id"] for r in db.quiz_questions(g)][:2] == [second["id"], first["id"]]
    alice.post(f"/admin/questionnaire/questions/{first['id']}/delete")
    assert first["id"] not in [r["id"] for r in db.quiz_questions(g)]
    alice.post("/admin/questionnaire/template", {"template": "grownups"})
    assert len(db.quiz_questions(g)) == len(TEMPLATES["grownups"]["questions"])
    assert "Grown-up favourites" in alice.get("/admin/questionnaire").text


def test_only_admins_manage_it(app, db, browser):
    alice, bob = admin_and_member(app, browser)
    turn_on(alice)
    qid = db.quiz_questions(app.state.group_a)[0]["id"]
    assert bob.get("/admin/questionnaire").status_code == 403
    for path, data in [("/admin/questionnaire/enabled", {"enabled": "0"}),
                       ("/admin/questionnaire/template", {"template": "grownups"}),
                       ("/admin/questionnaire/questions", {"text": "Sneaky?"}),
                       (f"/admin/questionnaire/questions/{qid}/edit", {"text": "Hacked"}),
                       (f"/admin/questionnaire/questions/{qid}/delete", {})]:
        assert bob.post(path, data).status_code == 403, path
    assert db.quiz_enabled(app.state.group_a)
    assert db.get_quiz_question(qid)["text"] == TEMPLATES["family"]["questions"][0][0]


def test_groups_cannot_touch_each_others_questions(app, db, browser):
    alice, bob = admin_and_member(app, browser)
    turn_on(alice)
    a_qid = db.quiz_questions(app.state.group_a)[0]["id"]

    wendy = browser()
    wendy.enter(PASS_B)
    wendy.join("Wendy", "4444")  # admin of group B
    turn_on(wendy)
    assert wendy.post(f"/admin/questionnaire/questions/{a_qid}/edit", {"text": "Mine now"}).status_code == 404
    assert wendy.post(f"/admin/questionnaire/questions/{a_qid}/delete").status_code == 404
    assert db.get_quiz_question(a_qid) is not None

    # Answers for another group's question ids are ignored.
    wendy.post("/questionnaire", {**answer_form(db, app.state.group_b), f"q{a_qid}": "planted"})
    assert a_qid not in db.quiz_answers(pid(db, app.state.group_b, "Wendy"))
    bob_id = pid(db, app.state.group_a, "Bob")
    assert "Wendy" not in alice.get(f"/p/{bob_id}").text


def test_answers_are_length_limited(app, db, browser):
    alice, _ = admin_and_member(app, browser)
    turn_on(alice)
    qid = db.quiz_questions(app.state.group_a)[0]["id"]
    alice.post("/questionnaire", {f"q{qid}": "x" * 2000})
    assert len(db.quiz_answers(pid(db, app.state.group_a, "Alice"))[qid]) == 500


def test_deleting_a_person_or_group_cleans_up(app, db, browser):
    alice, bob = admin_and_member(app, browser)
    turn_on(alice)
    bob.post("/questionnaire", answer_form(db, app.state.group_a, **{"Favorite Snack": "Chips"}))
    bob_id = pid(db, app.state.group_a, "Bob")
    db.delete_person(bob_id)
    assert db.quiz_answers(bob_id) == {}
    db.delete_group(app.state.group_a)
    assert db.quiz_questions(app.state.group_a) == []
