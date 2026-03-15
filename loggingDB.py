#!/usr/bin/env python3
import argparse
import sqlite3
from datetime import datetime

def ensure_table():
    conn = sqlite3.connect('entry_logs.db')
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            au_id TEXT NOT NULL,
            role TEXT CHECK(role IN ('student', 'faculty', 'admin')) NOT NULL,
            people_count INTEGER DEFAULT 1,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_au_time ON entries(au_id, timestamp)")
    conn.commit()
    conn.close()

def log_entry(name, au_id, role, people_count):
    ensure_table()
    conn = sqlite3.connect('entry_logs.db')
    c = conn.cursor()
    c.execute(
        "INSERT INTO entries (name, au_id, role, people_count) VALUES (?, ?, ?, ?)",
        (name, au_id, role, people_count)
    )
    conn.commit()
    entry_id = c.lastrowid
    conn.close()
    print(f"Logged ID {entry_id}: {name} ({role}) | people: {people_count}")

def view_recent(n=10):
    ensure_table()
    conn = sqlite3.connect('entry_logs.db')
    c = conn.cursor()
    c.execute("SELECT id, name, au_id, role, people_count, timestamp FROM entries ORDER BY timestamp DESC LIMIT ?", (n,))
    rows = c.fetchall()
    conn.close()
    if rows:
        print("\nRecent entries:")
        for row in rows:
            print(f"  ID{row[0]}: {row[1]} ({row[2]}, {row[3]}) | {row[4]} people | {row[5]}")
    else:
        print("No entries yet.")

def main():
    parser = argparse.ArgumentParser(description="Tinkerer's Lab Entry Logger CLI")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Log command
    log_parser = subparsers.add_parser("log", help="Log an entry")
    log_parser.add_argument("name", help="User full name")
    log_parser.add_argument("au_id", help="AU ID number")
    log_parser.add_argument("role", choices=["student", "faculty", "admin"], help="User role")
    log_parser.add_argument("-p", "--people", type=int, default=1, help="People count (default: 1)")

    # View command
    view_parser = subparsers.add_parser("view", help="View recent entries")
    view_parser.add_argument("-n", "--num", type=int, default=10, help="Number to show (default: 10)")

    args = parser.parse_args()

    if args.command == "log":
        log_entry(args.name, args.au_id, args.role, args.people)
    elif args.command == "view":
        view_recent(args.num)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
