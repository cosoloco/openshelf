import json
import logging
import sqlite3

from .catalogs import CatalogClient
from .db import timestamp
from .metadata import upsert_record

log = logging.getLogger(__name__)


def queue_scan(database, source_id, *, restart=False):
    with database.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        source = db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not source:
            raise ValueError("Server not found.")
        active = db.execute("SELECT id,status FROM scans WHERE source_id=? AND status IN ('queued','running','paused')", (source_id,)).fetchone()
        if active:
            if active["status"] == "paused":
                db.execute("UPDATE scans SET status='queued',updated_at=? WHERE id=?", (timestamp(), active["id"]))
                db.execute("UPDATE sources SET enabled=1,status='queued' WHERE id=?", (source_id,))
            return active["id"]
        latest = None if restart else db.execute("SELECT * FROM scans WHERE source_id=? ORDER BY id DESC LIMIT 1", (source_id,)).fetchone()
        failed = latest if latest and latest["status"] == "failed" else None
        if failed:
            scan_id = failed["id"]
            db.execute("UPDATE scans SET status='queued',error='',updated_at=?,finished_at=NULL WHERE id=?", (timestamp(), scan_id))
        else:
            scan_id = db.execute("INSERT INTO scans(source_id,created_at,updated_at) VALUES(?,?,?)", (source_id, timestamp(), timestamp())).lastrowid
        db.execute("UPDATE sources SET enabled=1,status='queued',error='' WHERE id=?", (source_id,))
        return scan_id


def run_scan(database, network, job, stop):
    source_id, scan_id = job["source_id"], job["id"]
    with database.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        source = dict(db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())
        status = db.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
        if not source["enabled"] or not status or status[0] != "running":
            return
        db.execute("UPDATE sources SET status='indexing',last_checked=?,error='' WHERE id=?", (timestamp(), source_id))
    client = CatalogClient(network, database.config.page_size)
    try:
        state = json.loads(job["checkpoint"])
        if not state:
            state = client.discover(source)
        with database.connection() as db:
            db.execute("UPDATE sources SET protocol=? WHERE id=?", (state["kind"], source_id))
        while not stop.is_set():
            with database.connection() as db:
                status = db.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
            if not status or status[0] != "running":
                return
            if database.settings()["indexing_paused"]:
                with database.connection() as db:
                    db.execute("UPDATE scans SET status='queued' WHERE id=? AND status='running'", (scan_id,))
                    db.execute("UPDATE sources SET status='queued' WHERE id=?", (source_id,))
                return
            records, next_state, done, total, message = client.advance(state)
            with database.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute("SELECT status FROM scans WHERE id=?", (scan_id,)).fetchone()
                if not current or current[0] != "running":
                    return
                for record in records:
                    upsert_record(db, source_id, scan_id, record)
                db.execute("UPDATE scans SET checkpoint=?,processed=?,total=?,message=?,updated_at=? WHERE id=?",
                           (json.dumps(next_state), next_state["processed"], total, message, timestamp(), scan_id))
                db.execute("UPDATE sources SET records=(SELECT COUNT(*) FROM copies WHERE source_id=? AND active=1) WHERE id=?", (source_id, source_id))
                if done:
                    db.execute("UPDATE copies SET active=0 WHERE source_id=? AND observed_scan<>?", (source_id, scan_id))
                    db.execute("UPDATE scans SET status='complete',finished_at=? WHERE id=?", (timestamp(), scan_id))
                    db.execute("UPDATE sources SET status='ready',last_success=?,error='',records=(SELECT COUNT(*) FROM copies WHERE source_id=? AND active=1) WHERE id=?", (timestamp(), source_id, source_id))
                    return
            state = next_state
    except Exception as exc:
        log.info("Scan %s failed: %s", scan_id, exc)
        error = str(exc)[:500]
        with database.connection() as db:
            db.execute("UPDATE scans SET status='failed',error=?,updated_at=?,finished_at=? WHERE id=? AND status='running'", (error, timestamp(), timestamp(), scan_id))
            db.execute("UPDATE sources SET status='error',error=? WHERE id=? AND status='indexing'", (error, source_id))
