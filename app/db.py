"""SQLite storage. Every query is parameterised."""
import datetime as dt
import glob
import os
import sqlite3
from contextlib import contextmanager
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    passphrase_hash  TEXT NOT NULL,
    gate_version     INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS people (
    id          INTEGER PRIMARY KEY,
    group_id    INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    name        TEXT NOT NULL COLLATE NOCASE,
    pin_hash    TEXT NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(group_id, name)
);
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    year        INTEGER NOT NULL,
    title       TEXT NOT NULL,
    link        TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    price       TEXT NOT NULL DEFAULT '',
    priority    INTEGER NOT NULL DEFAULT 2,
    sort        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS items_person_year ON items(person_id, year);
CREATE TABLE IF NOT EXISTS claims (
    item_id     INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    claimer_id  INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS login_attempts (
    key           TEXT PRIMARY KEY,
    fails         INTEGER NOT NULL DEFAULT 0,
    window_start  REAL NOT NULL DEFAULT 0,
    locked_until  REAL NOT NULL DEFAULT 0,
    lock_count    INTEGER NOT NULL DEFAULT 0
);
-- Optional "get to know me" questionnaire, one per group. Answers are shown to the whole group.
CREATE TABLE IF NOT EXISTS quiz_settings (
    group_id  INTEGER PRIMARY KEY REFERENCES groups(id) ON DELETE CASCADE,
    enabled   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS quiz_questions (
    id        INTEGER PRIMARY KEY,
    group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    text      TEXT NOT NULL,
    long      INTEGER NOT NULL DEFAULT 0,
    sort      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS quiz_questions_group ON quiz_questions(group_id, sort);
-- A row (even with an empty answer) means the person has seen and saved that question.
CREATE TABLE IF NOT EXISTS quiz_answers (
    person_id    INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    question_id  INTEGER NOT NULL REFERENCES quiz_questions(id) ON DELETE CASCADE,
    answer       TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (person_id, question_id)
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- groups (one per gift exchange) ---------------------------------
    def list_groups(self):
        with self.connect() as c:
            return c.execute(
                "SELECT g.id, g.name, g.passphrase_hash, g.gate_version, "
                "(SELECT COUNT(*) FROM people p WHERE p.group_id = g.id) AS members "
                "FROM groups g ORDER BY g.name"
            ).fetchall()

    def get_group(self, group_id: int):
        with self.connect() as c:
            return c.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()

    def group_by_name(self, name: str):
        with self.connect() as c:
            return c.execute("SELECT * FROM groups WHERE name=?", (name,)).fetchone()

    def create_group(self, name: str, passphrase_hash: str) -> int:
        with self.connect() as c:
            return c.execute(
                "INSERT INTO groups(name, passphrase_hash) VALUES(?, ?)", (name, passphrase_hash)
            ).lastrowid

    def set_group_passphrase(self, group_id: int, passphrase_hash: str) -> int:
        """Change the passphrase and bump the version so existing sessions are logged out."""
        with self.connect() as c:
            c.execute(
                "UPDATE groups SET passphrase_hash=?, gate_version=gate_version+1 WHERE id=?",
                (passphrase_hash, group_id),
            )
            return c.execute("SELECT gate_version FROM groups WHERE id=?", (group_id,)).fetchone()[0]

    def rename_group(self, group_id: int, name: str) -> None:
        with self.connect() as c:
            c.execute("UPDATE groups SET name=? WHERE id=?", (name, group_id))

    def delete_group(self, group_id: int) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM groups WHERE id=?", (group_id,))

    # ---- people (always scoped to a group) ------------------------------
    def list_people(self, group_id: int):
        with self.connect() as c:
            return c.execute(
                "SELECT id, name, is_admin FROM people WHERE group_id=? ORDER BY name", (group_id,)
            ).fetchall()

    def get_person(self, person_id: int):
        with self.connect() as c:
            return c.execute("SELECT * FROM people WHERE id=?", (person_id,)).fetchone()

    def person_by_name(self, group_id: int, name: str):
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM people WHERE group_id=? AND name=?", (group_id, name)
            ).fetchone()

    def count_people(self, group_id: Optional[int] = None) -> int:
        with self.connect() as c:
            if group_id is None:
                return c.execute("SELECT COUNT(*) FROM people").fetchone()[0]
            return c.execute(
                "SELECT COUNT(*) FROM people WHERE group_id=?", (group_id,)
            ).fetchone()[0]

    def create_person(self, group_id: int, name: str, pin_hash: str, is_admin: bool) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO people(group_id, name, pin_hash, is_admin) VALUES(?, ?, ?, ?)",
                (group_id, name, pin_hash, int(is_admin)),
            )
            return cur.lastrowid

    def set_pin(self, person_id: int, pin_hash: str) -> None:
        with self.connect() as c:
            c.execute("UPDATE people SET pin_hash=? WHERE id=?", (pin_hash, person_id))

    def rename_person(self, person_id: int, name: str) -> None:
        with self.connect() as c:
            c.execute("UPDATE people SET name=? WHERE id=?", (name, person_id))

    def set_admin(self, person_id: int, is_admin: bool) -> None:
        with self.connect() as c:
            c.execute("UPDATE people SET is_admin=? WHERE id=?", (int(is_admin), person_id))

    def delete_person(self, person_id: int) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM people WHERE id=?", (person_id,))

    # ---- items ----------------------------------------------------------
    def years_for(self, person_id: int):
        with self.connect() as c:
            rows = c.execute(
                "SELECT DISTINCT year FROM items WHERE person_id=? ORDER BY year DESC",
                (person_id,),
            ).fetchall()
        return [r["year"] for r in rows]

    def items_for_owner(self, person_id: int, year: int):
        """The owner's own view. Deliberately never touches the claims table."""
        with self.connect() as c:
            return c.execute(
                "SELECT id, person_id, year, title, link, notes, price, priority, sort "
                "FROM items WHERE person_id=? AND year=? ORDER BY sort, id",
                (person_id, year),
            ).fetchall()

    def items_for_viewer(self, person_id: int, year: int):
        """Someone else's view: includes who (if anyone) claimed each item."""
        with self.connect() as c:
            return c.execute(
                "SELECT i.id, i.person_id, i.year, i.title, i.link, i.notes, i.price, "
                "i.priority, i.sort, cl.claimer_id, p.name AS claimer_name "
                "FROM items i "
                "LEFT JOIN claims cl ON cl.item_id = i.id "
                "LEFT JOIN people p ON p.id = cl.claimer_id "
                "WHERE i.person_id=? AND i.year=? ORDER BY i.sort, i.id",
                (person_id, year),
            ).fetchall()

    def get_item(self, item_id: int):
        with self.connect() as c:
            return c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

    def count_items(self, person_id: int, year: int) -> int:
        with self.connect() as c:
            return c.execute(
                "SELECT COUNT(*) FROM items WHERE person_id=? AND year=?", (person_id, year)
            ).fetchone()[0]

    def add_item(self, person_id, year, title, link, notes, price, priority) -> int:
        with self.connect() as c:
            nxt = c.execute(
                "SELECT COALESCE(MAX(sort), 0) + 1 FROM items WHERE person_id=? AND year=?",
                (person_id, year),
            ).fetchone()[0]
            cur = c.execute(
                "INSERT INTO items(person_id, year, title, link, notes, price, priority, sort) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (person_id, year, title, link, notes, price, priority, nxt),
            )
            return cur.lastrowid

    def update_item(self, item_id, title, link, notes, price, priority) -> None:
        with self.connect() as c:
            c.execute(
                "UPDATE items SET title=?, link=?, notes=?, price=?, priority=? WHERE id=?",
                (title, link, notes, price, priority, item_id),
            )

    def delete_item(self, item_id: int) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM items WHERE id=?", (item_id,))

    def move_item(self, item_id: int, direction: int) -> None:
        with self.connect() as c:
            item = c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            rows = c.execute(
                "SELECT id FROM items WHERE person_id=? AND year=? ORDER BY sort, id",
                (item["person_id"], item["year"]),
            ).fetchall()
            ids = [r["id"] for r in rows]
            i = ids.index(item_id)
            j = i + direction
            if 0 <= j < len(ids):
                ids[i], ids[j] = ids[j], ids[i]
                for pos, iid in enumerate(ids, start=1):
                    c.execute("UPDATE items SET sort=? WHERE id=?", (pos, iid))

    def copy_year(self, person_id: int, from_year: int, to_year: int, limit: int) -> int:
        """Copy every item (claimed or not, so claims aren't revealed) into another year."""
        with self.connect() as c:
            have = c.execute(
                "SELECT COUNT(*) FROM items WHERE person_id=? AND year=?", (person_id, to_year)
            ).fetchone()[0]
            src = c.execute(
                "SELECT title, link, notes, price, priority FROM items "
                "WHERE person_id=? AND year=? ORDER BY sort, id",
                (person_id, from_year),
            ).fetchall()
            n = 0
            for r in src[: max(0, limit - have)]:
                n += 1
                c.execute(
                    "INSERT INTO items(person_id, year, title, link, notes, price, priority, sort) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (person_id, to_year, r["title"], r["link"], r["notes"], r["price"],
                     r["priority"], have + n),
                )
            return n

    # ---- claims ---------------------------------------------------------
    def get_claim(self, item_id: int):
        with self.connect() as c:
            return c.execute("SELECT * FROM claims WHERE item_id=?", (item_id,)).fetchone()

    def claim(self, item_id: int, claimer_id: int) -> bool:
        with self.connect() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO claims(item_id, claimer_id) VALUES(?, ?)",
                (item_id, claimer_id),
            )
            return cur.rowcount == 1

    def unclaim(self, item_id: int, claimer_id: int) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM claims WHERE item_id=? AND claimer_id=?", (item_id, claimer_id))

    def my_claims(self, claimer_id: int, year: int):
        with self.connect() as c:
            return c.execute(
                "SELECT i.id, i.title, i.link, i.price, i.notes, p.id AS owner_id, p.name AS owner_name "
                "FROM claims cl JOIN items i ON i.id = cl.item_id "
                "JOIN people p ON p.id = i.person_id "
                "WHERE cl.claimer_id=? AND i.year=? ORDER BY p.name, i.sort",
                (claimer_id, year),
            ).fetchall()

    # ---- questionnaire --------------------------------------------------
    def quiz_enabled(self, group_id: int) -> bool:
        with self.connect() as c:
            row = c.execute("SELECT enabled FROM quiz_settings WHERE group_id=?", (group_id,)).fetchone()
            return bool(row and row["enabled"])

    def set_quiz_enabled(self, group_id: int, enabled: bool) -> None:
        with self.connect() as c:
            c.execute(
                "INSERT INTO quiz_settings(group_id, enabled) VALUES(?, ?) "
                "ON CONFLICT(group_id) DO UPDATE SET enabled=excluded.enabled",
                (group_id, int(enabled)),
            )

    def quiz_questions(self, group_id: int):
        with self.connect() as c:
            return c.execute(
                "SELECT id, text, long FROM quiz_questions WHERE group_id=? ORDER BY sort, id", (group_id,)
            ).fetchall()

    def get_quiz_question(self, question_id: int):
        with self.connect() as c:
            return c.execute("SELECT * FROM quiz_questions WHERE id=?", (question_id,)).fetchone()

    def add_quiz_question(self, group_id: int, text: str, long: bool) -> int:
        with self.connect() as c:
            top = c.execute(
                "SELECT COALESCE(MAX(sort), 0) FROM quiz_questions WHERE group_id=?", (group_id,)
            ).fetchone()[0]
            return c.execute(
                "INSERT INTO quiz_questions(group_id, text, long, sort) VALUES(?, ?, ?, ?)",
                (group_id, text, int(long), top + 1),
            ).lastrowid

    def update_quiz_question(self, question_id: int, text: str, long: bool) -> None:
        with self.connect() as c:
            c.execute("UPDATE quiz_questions SET text=?, long=? WHERE id=?", (text, int(long), question_id))

    def delete_quiz_question(self, question_id: int) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM quiz_questions WHERE id=?", (question_id,))

    def move_quiz_question(self, question_id: int, direction: int) -> None:
        with self.connect() as c:
            q = c.execute("SELECT group_id FROM quiz_questions WHERE id=?", (question_id,)).fetchone()
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM quiz_questions WHERE group_id=? ORDER BY sort, id", (q["group_id"],))]
            i = ids.index(question_id)
            j = i + direction
            if 0 <= j < len(ids):
                ids[i], ids[j] = ids[j], ids[i]
                for pos, qid in enumerate(ids, start=1):
                    c.execute("UPDATE quiz_questions SET sort=? WHERE id=?", (pos, qid))

    def replace_quiz_questions(self, group_id: int, questions) -> None:
        """Swap in a whole new question set (from a template). Old answers go with the old questions."""
        with self.connect() as c:
            c.execute("DELETE FROM quiz_questions WHERE group_id=?", (group_id,))
            for pos, (text, long) in enumerate(questions, start=1):
                c.execute(
                    "INSERT INTO quiz_questions(group_id, text, long, sort) VALUES(?, ?, ?, ?)",
                    (group_id, text, int(long), pos),
                )

    def quiz_answers(self, person_id: int):
        """{question_id: answer} for every question this person has saved."""
        with self.connect() as c:
            return {r["question_id"]: r["answer"] for r in c.execute(
                "SELECT question_id, answer FROM quiz_answers WHERE person_id=?", (person_id,))}

    def quiz_answers_shown(self, person_id: int, group_id: int):
        """Non-empty answers in question order, for the "About" panel, plus when they were last saved."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT q.text, q.long, a.answer, a.updated_at FROM quiz_questions q "
                "JOIN quiz_answers a ON a.question_id = q.id AND a.person_id=? "
                "WHERE q.group_id=? AND a.answer != '' ORDER BY q.sort, q.id",
                (person_id, group_id),
            ).fetchall()
            return rows, max((r["updated_at"] for r in rows), default=None)

    def quiz_pending(self, person_id: int, group_id: int) -> bool:
        """True if the questionnaire is on and has a question this person hasn't saved yet."""
        with self.connect() as c:
            row = c.execute(
                "SELECT 1 FROM quiz_settings s JOIN quiz_questions q ON q.group_id = s.group_id "
                "LEFT JOIN quiz_answers a ON a.question_id = q.id AND a.person_id=? "
                "WHERE s.group_id=? AND s.enabled=1 AND a.question_id IS NULL LIMIT 1",
                (person_id, group_id),
            ).fetchone()
            return row is not None

    def save_quiz_answers(self, person_id: int, answers: dict) -> None:
        with self.connect() as c:
            for qid, text in answers.items():
                c.execute(
                    "INSERT INTO quiz_answers(person_id, question_id, answer) VALUES(?, ?, ?) "
                    "ON CONFLICT(person_id, question_id) DO UPDATE SET answer=excluded.answer, "
                    "updated_at=CASE WHEN answer=excluded.answer THEN updated_at ELSE CURRENT_TIMESTAMP END",
                    (person_id, qid, text),
                )

    # ---- backups --------------------------------------------------------
    def backup(self, backup_dir: str, keep: int) -> str:
        os.makedirs(backup_dir, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest_path = os.path.join(backup_dir, f"christmas-{stamp}.db")
        src = sqlite3.connect(self.path)
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        files = sorted(glob.glob(os.path.join(backup_dir, "christmas-*.db")))
        for old in files[:-keep] if keep > 0 else []:
            os.remove(old)
        return dest_path
