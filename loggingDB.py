import sqlite3

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

def log_entry(name, au_id, role, people_count=1):
    ensure_table()
    conn = sqlite3.connect('entry_logs.db')
    c = conn.cursor()
    c.execute(
        "INSERT INTO entries (name, au_id, role, people_count) VALUES (?, ?, ?, ?)",
        (name, au_id, role, people_count)
    )
    conn.commit()
    conn.close()
    print(f"✅ Logged: {name} ({role}) | people: {people_count}")

# Test
if __name__ == "__main__":
    log_entry("John Doe", "AU123", "student", people_count=2)
