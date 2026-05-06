import sqlite3
import json
import os
from app.core.config import config

DB_PATH = config.DATABASE_URL

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Cameras table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cameras (
        id TEXT PRIMARY KEY,
        name TEXT,
        host TEXT,
        port INTEGER,
        rtsp_port INTEGER,
        username TEXT,
        password TEXT,
        device_type TEXT,
        brand TEXT,
        manufacturer TEXT,
        model TEXT,
        firmware TEXT,
        tier TEXT,
        profiles TEXT,
        has_recording INTEGER,
        has_replay INTEGER
    )
    """)
    
    # Users table for Phase 4
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        username TEXT PRIMARY KEY,
        password_hash TEXT,
        role TEXT
    )
    """)
    
    # Audit logs
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        username TEXT,
        action TEXT,
        details TEXT
    )
    """)
    
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# Initial migration if cameras.json exists
def migrate_if_needed():
    if os.path.exists("cameras.json"):
        with open("cameras.json", "r") as f:
            cameras = json.load(f)
            conn = get_db()
            cursor = conn.cursor()
            for cid, c in cameras.items():
                cursor.execute("""
                INSERT OR REPLACE INTO cameras VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    cid, c["name"], c["host"], c["port"], c["rtsp_port"],
                    c["username"], c["password"], c["device_type"], c["brand"],
                    c["manufacturer"], c["model"], c["firmware"], c["tier"],
                    json.dumps(c["profiles"]), 1 if c["has_recording"] else 0,
                    1 if c["has_replay"] else 0
                ))
            conn.commit()
            conn.close()
        # os.rename("cameras.json", "cameras.json.bak")
