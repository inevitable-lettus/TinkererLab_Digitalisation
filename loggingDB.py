#!/usr/bin/env python3
import argparse
import sqlite3
from datetime import datetime

DB_PATH = "entry_logs.db"

def ensure_table():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL,
            au_id        TEXT NOT NULL,
            role         TEXT CHECK(role IN ('student', 'faculty', 'admin')) NOT NULL,
            people_count INTEGER DEFAULT 1,
            status       TEXT CHECK(status IN ('AUTHORISED', 'UNAUTHORISED', 'EXPIRED_AUTH')) NOT NULL DEFAULT 'AUTHORISED',
            screenshot   BLOB,
            timestamp    DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_au_time ON entries(au_id, timestamp)")

    # ── users table: populated at startup from users.json ─────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            au_id            TEXT PRIMARY KEY,
            name             TEXT NOT NULL,
            role             TEXT CHECK(role IN ('student', 'faculty', 'admin')) NOT NULL,
            encrypted_payload TEXT
        )
    """)

    conn.commit()
    conn.close()


def upsert_user(au_id: str, name: str, role: str, encrypted_payload: str = None):
    """Insert or replace a user record (called at startup during QR generation)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO users (au_id, name, role, encrypted_payload)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(au_id) DO UPDATE SET
            name              = excluded.name,
            role              = excluded.role,
            encrypted_payload = excluded.encrypted_payload
        """,
        (au_id, name, role, encrypted_payload),
    )
    conn.commit()
    conn.close()


def lookup_user(au_id: str) -> dict | None:
    """Return user dict {au_id, name, role} or None if not found."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT au_id, name, role FROM users WHERE au_id = ?", (au_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"au_id": row[0], "name": row[1], "role": row[2]}
    return None


def log_entry(name: str, au_id: str, role: str, people_count: int,
              status: str = "AUTHORISED", screenshot: bytes = None):
    """
    Log an entry event.

    Parameters
    ----------
    status     : 'AUTHORISED' | 'UNAUTHORISED' | 'EXPIRED_AUTH'
    screenshot : raw JPEG/PNG bytes to store as BLOB (optional)
    """
    ensure_table()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO entries (name, au_id, role, people_count, status, screenshot)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (name, au_id, role, people_count, status, screenshot),
    )
    conn.commit()
    entry_id = c.lastrowid
    conn.close()
    print(f"  📝 Logged ID {entry_id}: {name} ({role}) | status: {status} | people: {people_count}")


# ── CLI helpers ───────────────────────────────────────────────────────────────

def view_recent(n=10):
    ensure_table()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT id, name, au_id, role, people_count, status, timestamp FROM entries ORDER BY timestamp DESC LIMIT ?",
        (n,),
    )
    rows = c.fetchall()
    conn.close()
    if rows:
        print("\nRecent entries:")
        for row in rows:
            print(f"  ID{row[0]}: {row[1]} ({row[2]}, {row[3]}) | {row[4]} people | {row[5]} | {row[6]}")
    else:
        print("No entries yet.")


def main():
    parser = argparse.ArgumentParser(description="Tinkerer's Lab Entry Logger CLI")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    log_parser = subparsers.add_parser("log", help="Log an entry")
    log_parser.add_argument("name")
    log_parser.add_argument("au_id")
    log_parser.add_argument("role", choices=["student", "faculty", "admin"])
    log_parser.add_argument("-p", "--people", type=int, default=1)
    log_parser.add_argument("-s", "--status", choices=["AUTHORISED", "UNAUTHORISED", "EXPIRED_AUTH"],
                            default="AUTHORISED")

    view_parser = subparsers.add_parser("view", help="View recent entries")
    view_parser.add_argument("-n", "--num", type=int, default=10)

    args = parser.parse_args()

    if args.command == "log":
        log_entry(args.name, args.au_id, args.role, args.people, args.status)
    elif args.command == "view":
        view_recent(args.num)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
