from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3

from .search import identifier_matches, metadata_matches


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
 id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'new',
 protocol TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL,
 last_checked TEXT, last_success TEXT, error TEXT NOT NULL DEFAULT '',
 records INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scans(
 id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 status TEXT NOT NULL DEFAULT 'queued', checkpoint TEXT NOT NULL DEFAULT '{}',
 processed INTEGER NOT NULL DEFAULT 0, total INTEGER,
 message TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS scan_active ON scans(source_id) WHERE status IN ('queued','running','paused');
CREATE INDEX IF NOT EXISTS scan_status ON scans(status,id);
CREATE INDEX IF NOT EXISTS scan_source ON scans(source_id,id);
CREATE TABLE IF NOT EXISTS books(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, authors TEXT NOT NULL, authors_json TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '', language TEXT NOT NULL DEFAULT 'und',
 year INTEGER, publisher TEXT NOT NULL DEFAULT '',
 tags TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL DEFAULT '[]',
 series TEXT NOT NULL DEFAULT '', series_position REAL,
 identifiers_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS book_title ON books(title COLLATE NOCASE,id);
CREATE INDEX IF NOT EXISTS book_author ON books(authors COLLATE NOCASE,id);
CREATE INDEX IF NOT EXISTS book_language ON books(language);
CREATE INDEX IF NOT EXISTS book_series ON books(series COLLATE NOCASE,series_position);
CREATE TABLE IF NOT EXISTS book_tags(book_id TEXT NOT NULL REFERENCES books(id),tag TEXT NOT NULL,PRIMARY KEY(book_id,tag));
CREATE INDEX IF NOT EXISTS tag_lookup ON book_tags(tag COLLATE NOCASE,book_id);
CREATE TABLE IF NOT EXISTS copies(
 id INTEGER PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id),
 source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 library TEXT NOT NULL, remote_id TEXT NOT NULL, title TEXT NOT NULL,
 cover_url TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL,
 observed_scan INTEGER NOT NULL, last_seen TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 UNIQUE(source_id,library,remote_id)
);
CREATE INDEX IF NOT EXISTS copies_book ON copies(book_id,active,source_id);
CREATE INDEX IF NOT EXISTS copies_source ON copies(source_id,active,observed_scan);
CREATE TABLE IF NOT EXISTS files(
 id INTEGER PRIMARY KEY, copy_id INTEGER NOT NULL REFERENCES copies(id) ON DELETE CASCADE,
 format TEXT NOT NULL, url TEXT NOT NULL, size INTEGER,
 UNIQUE(copy_id,format,url)
);
CREATE INDEX IF NOT EXISTS file_copy ON files(copy_id,format);
CREATE INDEX IF NOT EXISTS file_format ON files(format,copy_id);
CREATE TABLE IF NOT EXISTS saved(book_id TEXT PRIMARY KEY REFERENCES books(id),added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS downloads(
 id INTEGER PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id),
 preference TEXT NOT NULL DEFAULT 'auto', status TEXT NOT NULL DEFAULT 'queued',
 format TEXT NOT NULL DEFAULT '', source_id INTEGER, url TEXT NOT NULL DEFAULT '',
 bytes INTEGER NOT NULL DEFAULT 0, total INTEGER, validator TEXT NOT NULL DEFAULT '',
 path TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS download_active ON downloads(book_id) WHERE status IN ('queued','running','paused');
CREATE INDEX IF NOT EXISTS download_status ON downloads(status,id);
CREATE INDEX IF NOT EXISTS download_book ON downloads(book_id,status);
CREATE VIRTUAL TABLE IF NOT EXISTS book_search USING fts5(title,authors,series,tags,description,content='books',content_rowid='rowid',tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS books_insert AFTER INSERT ON books BEGIN
 INSERT INTO book_search(rowid,title,authors,series,tags,description) VALUES(new.rowid,new.title,new.authors,new.series,new.tags,new.description);
END;
CREATE TRIGGER IF NOT EXISTS books_delete AFTER DELETE ON books BEGIN
 INSERT INTO book_search(book_search,rowid,title,authors,series,tags,description) VALUES('delete',old.rowid,old.title,old.authors,old.series,old.tags,old.description);
END;
CREATE TRIGGER IF NOT EXISTS books_update AFTER UPDATE OF title,authors,series,tags,description ON books BEGIN
 INSERT INTO book_search(book_search,rowid,title,authors,series,tags,description) VALUES('delete',old.rowid,old.title,old.authors,old.series,old.tags,old.description);
 INSERT INTO book_search(rowid,title,authors,series,tags,description) VALUES(new.rowid,new.title,new.authors,new.series,new.tags,new.description);
END;
"""


class Database:
    def __init__(self, config):
        self.config = config
        config.prepare()
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            defaults = {
                "download_dir": str(config.download_dir), "preferred_format": "EPUB",
                "downloads_paused": False, "indexing_paused": False,
                "reserve_gb": 1, "max_file_mb": 1024,
            }
            for key, value in defaults.items():
                db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (key, json.dumps(value)))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.config.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.create_function("metadata_matches", 3, metadata_matches, deterministic=True)
        db.create_function("identifier_matches", 4, identifier_matches, deterministic=True)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def settings(self):
        with self.connection() as db:
            return {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM settings")}

    def set_settings(self, values):
        with self.connection() as db:
            db.executemany("INSERT OR REPLACE INTO settings VALUES(?,?)", [(k, json.dumps(v)) for k, v in values.items()])

    def recover(self):
        with self.connection() as db:
            for table in ("scans", "downloads"):
                db.execute(f"UPDATE {table} SET status='queued',updated_at=? WHERE status='running'", (timestamp(),))
            db.execute("UPDATE sources SET status='queued' WHERE status='indexing'")

    def claim(self, table, excluded=()):
        if table not in ("scans", "downloads"):
            raise ValueError("Unknown queue")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            clause = " AND id NOT IN (" + ",".join("?" for _ in excluded) + ")" if excluded else ""
            row = db.execute(f"SELECT * FROM {table} WHERE status='queued'{clause} ORDER BY id LIMIT 1", list(excluded)).fetchone()
            if row is None:
                return None
            db.execute(f"UPDATE {table} SET status='running',updated_at=? WHERE id=?", (timestamp(), row["id"]))
            return dict(row)
