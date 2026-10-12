"""Manage gift exchanges (groups) from the command line on the NAS.

    docker compose exec app python -m app.manage list
    docker compose exec app python -m app.manage add-group "Smith Family"
    docker compose exec app python -m app.manage set-passphrase "Smith Family"
    docker compose exec app python -m app.manage rename-group "Smith Family" "Smiths"
    docker compose exec app python -m app.manage make-admin "Smith Family" "Jamie"
    docker compose exec app python -m app.manage reset-pin "Smith Family" "Jamie"
    docker compose exec app python -m app.manage delete-group "Smith Family"
"""
import argparse
import getpass
import os
import sys

from .db import Database
from .security import hash_secret, verify_secret

MIN_PASSPHRASE = 12


def _db() -> Database:
    data_dir = os.environ.get("DATA_DIR", "/data")
    return Database(os.environ.get("DB_PATH", os.path.join(data_dir, "christmas.db")))


def _group(db: Database, name: str):
    g = db.group_by_name(name)
    if not g:
        sys.exit(f"No group called {name!r}. Run 'list' to see them.")
    return g


def _ask_passphrase(db: Database, exclude_id=None) -> str:
    p1 = getpass.getpass("Passphrase for this exchange: ")
    p2 = getpass.getpass("Again: ")
    if p1 != p2:
        sys.exit("Passphrases don't match.")
    if not MIN_PASSPHRASE <= len(p1) <= 200:
        sys.exit(f"Use {MIN_PASSPHRASE}-200 characters (a few random words works well).")
    for g in db.list_groups():
        if g["id"] != exclude_id and verify_secret(g["passphrase_hash"], p1):
            sys.exit(f"That passphrase is already used by {g['name']!r}. Each exchange needs its own.")
    return p1


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m app.manage", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list gift exchanges")
    a = sub.add_parser("add-group", help="create a gift exchange")
    a.add_argument("name")
    a = sub.add_parser("set-passphrase", help="change an exchange's passphrase (logs everyone out)")
    a.add_argument("name")
    a = sub.add_parser("rename-group")
    a.add_argument("name")
    a.add_argument("new_name")
    a = sub.add_parser("make-admin", help="make someone an admin of their exchange")
    a.add_argument("group")
    a.add_argument("person")
    a = sub.add_parser("reset-pin", help="set a new PIN for someone")
    a.add_argument("group")
    a.add_argument("person")
    a = sub.add_parser("delete-group", help="delete an exchange and ALL its lists")
    a.add_argument("name")
    args = ap.parse_args(argv)
    db = _db()

    if args.cmd == "list":
        groups = db.list_groups()
        if not groups:
            print("No gift exchanges yet. Create one with: add-group \"Name\"")
        for g in groups:
            people = ", ".join(
                p["name"] + (" (admin)" if p["is_admin"] else "") for p in db.list_people(g["id"])
            )
            print(f"{g['name']}: {g['members']} people{': ' + people if people else ''}")

    elif args.cmd == "add-group":
        name = " ".join(args.name.split())
        if not 1 <= len(name) <= 60:
            sys.exit("Group names are 1-60 characters.")
        if db.group_by_name(name):
            sys.exit(f"{name!r} already exists.")
        db.create_group(name, hash_secret(_ask_passphrase(db)))
        print(f"Created {name!r}. Share the passphrase with that family; the first person to join becomes its admin.")

    elif args.cmd == "set-passphrase":
        g = _group(db, args.name)
        db.set_group_passphrase(g["id"], hash_secret(_ask_passphrase(db, exclude_id=g["id"])))
        print(f"Passphrase changed for {g['name']!r}. Everyone in it will need the new one.")

    elif args.cmd == "rename-group":
        g = _group(db, args.name)
        new = " ".join(args.new_name.split())
        if not 1 <= len(new) <= 60 or (db.group_by_name(new) and db.group_by_name(new)["id"] != g["id"]):
            sys.exit("That name is invalid or taken.")
        db.rename_group(g["id"], new)
        print(f"Renamed to {new!r}.")

    elif args.cmd in ("make-admin", "reset-pin"):
        g = _group(db, args.group)
        p = db.person_by_name(g["id"], args.person)
        if not p:
            sys.exit(f"No one called {args.person!r} in {g['name']!r}.")
        if args.cmd == "make-admin":
            db.set_admin(p["id"], True)
            print(f"{p['name']} is now an admin of {g['name']!r}.")
        else:
            pin = getpass.getpass("New PIN (4-6 digits): ")
            if not (pin.isdigit() and 4 <= len(pin) <= 6):
                sys.exit("PIN must be 4-6 digits.")
            db.set_pin(p["id"], hash_secret(pin))
            with db.connect() as c:
                c.execute("DELETE FROM login_attempts WHERE key=?", (f"pin:{p['id']}",))
            print(f"PIN reset for {p['name']}.")

    elif args.cmd == "delete-group":
        g = _group(db, args.name)
        confirm = input(f"Type the group name to delete {g['name']!r} and every list in it: ")
        if confirm.strip().casefold() != g["name"].casefold():
            sys.exit("Not deleted.")
        db.delete_group(g["id"])
        print("Deleted.")


if __name__ == "__main__":
    main()
