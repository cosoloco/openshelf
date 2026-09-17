import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import time
import zipfile

from .catalogs import FORMATS
from .db import timestamp

log = logging.getLogger(__name__)


class Interrupted(Exception):
    pass


def safe_filename(title, author, book_id, fmt):
    stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]', " ", f"{author} - {title}")
    stem = re.sub(r"\s+", " ", stem).strip(" .")[:150].rstrip(" .") or "Book"
    while len(stem.encode("utf-8")) > 190:
        stem = stem[:-1]
    return f"{stem} [{book_id[:8]}].{fmt.lower()}"


def validate_file(path, fmt):
    size = path.stat().st_size
    if size < 16:
        raise ValueError("The downloaded file is empty or incomplete.")
    with path.open("rb") as file:
        header = file.read(4096)
    if re.match(br"\s*(<!doctype\s+html|<html|<head|<body)", header, re.I):
        raise ValueError("Server returned an HTML page instead of a book.")
    if fmt in ("EPUB", "DOCX", "ODT", "CBZ", "HTMLZ"):
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 30000 or sum(e.file_size for e in entries) > 4 * 1024**3:
                raise ValueError("Archive exceeds the validation limit.")
            if any(e.flag_bits & 1 for e in entries):
                raise ValueError("Encrypted archives are not supported.")
            names = set(archive.namelist())
            if fmt == "EPUB" and not {"mimetype", "META-INF/container.xml"}.issubset(names):
                raise ValueError("File is not a complete EPUB.")
            if fmt == "EPUB" and archive.read("mimetype").strip() != b"application/epub+zip":
                raise ValueError("Invalid EPUB media type.")
            if fmt == "DOCX" and "[Content_Types].xml" not in names:
                raise ValueError("Invalid DOCX container.")
            if archive.testzip() is not None:
                raise ValueError("Archive checksum validation failed.")
    elif fmt == "PDF" and b"%PDF-" not in header[:1024]:
        raise ValueError("File is not a PDF.")
    elif fmt in ("MOBI", "AZW3") and header[60:68] != b"BOOKMOBI":
        raise ValueError("File is not a supported Kindle book.")
    elif fmt == "DJVU" and not header.startswith(b"AT&TFORM"):
        raise ValueError("File is not a DjVu document.")
    elif fmt == "RTF" and not header.startswith(b"{\\rtf"):
        raise ValueError("File is not an RTF document.")
    elif fmt == "FB2" and b"FictionBook" not in header:
        raise ValueError("File is not an FB2 document.")
    elif fmt == "TXT" and b"\x00" in header and not header.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("File is not a text document.")
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def queue_books(database, book_ids, preference="auto"):
    if preference != "auto" and preference not in FORMATS:
        raise ValueError("Unsupported download format.")
    queued, skipped = 0, 0
    with database.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        for book_id in book_ids:
            book = db.execute("SELECT id FROM books WHERE id=?", (book_id,)).fetchone()
            if not book:
                continue
            existing = db.execute("SELECT id,status,format FROM downloads WHERE book_id=? AND status IN ('queued','running','paused','complete')", (book_id,)).fetchall()
            if any(r["status"] != "complete" or preference == "auto" or r["format"] == preference for r in existing):
                skipped += 1
                continue
            usable = db.execute("""SELECT 1 FROM files f JOIN copies c ON c.id=f.copy_id JOIN sources s ON s.id=c.source_id
                                 WHERE c.book_id=? AND c.active=1 AND s.enabled=1 AND (?='auto' OR f.format=?) LIMIT 1""",
                                (book_id, preference, preference)).fetchone()
            if not usable:
                skipped += 1
                continue
            db.execute("INSERT INTO downloads(book_id,preference,created_at,updated_at) VALUES(?,?,?,?)", (book_id, preference, timestamp(), timestamp()))
            queued += 1
    return {"queued": queued, "skipped": skipped}


def run_download(database, network, job, stop):
    job_id = job["id"]
    with database.connection() as db:
        book = dict(db.execute("SELECT * FROM books WHERE id=?", (job["book_id"],)).fetchone())
        candidates = [dict(r) for r in db.execute("""SELECT f.*,c.source_id,s.url AS base,s.status AS source_status
                         FROM files f JOIN copies c ON c.id=f.copy_id JOIN sources s ON s.id=c.source_id
                         WHERE c.book_id=? AND c.active=1 AND s.enabled=1
                         AND (?='auto' OR f.format=?) ORDER BY s.last_success DESC""", (job["book_id"], job["preference"], job["preference"]))]
    preferences = database.settings()
    preferred = preferences["preferred_format"]
    order = [preferred] + [f for f in FORMATS if f != preferred]
    candidates.sort(key=lambda c: (order.index(c["format"]) if c["format"] in order else 99, c["source_status"] == "error"))
    seen, unique = set(), []
    for candidate in candidates:
        if candidate["url"] not in seen:
            seen.add(candidate["url"])
            unique.append(candidate)
    partial = database.config.data_dir / "partial" / f"{job_id}.part"
    last_error = "No indexed download source matches this request."

    def active():
        if stop.is_set():
            raise Interrupted()
        with database.connection() as db:
            row = db.execute("SELECT status FROM downloads WHERE id=?", (job_id,)).fetchone()
            if not row or row[0] != "running":
                raise Interrupted()
        if database.settings()["downloads_paused"]:
            with database.connection() as db:
                db.execute("UPDATE downloads SET status='queued',updated_at=? WHERE id=? AND status='running'", (timestamp(), job_id))
            raise Interrupted()

    try:
        for candidate in unique[:30]:
            active()
            try:
                preferences = database.settings()
                destination = Path(preferences["download_dir"]).expanduser().resolve()
                destination.mkdir(parents=True, exist_ok=True)
                reserve = int(float(preferences["reserve_gb"]) * 1024**3)
                maximum = int(float(preferences["max_file_mb"]) * 1024**2)
                if shutil.disk_usage(destination).free <= reserve:
                    raise RuntimeError("Download folder has reached its free-space reserve.")
                with database.connection() as db:
                    current = dict(db.execute("SELECT * FROM downloads WHERE id=?", (job_id,)).fetchone())
                resume = partial.stat().st_size if partial.exists() and current["url"] == candidate["url"] and current["validator"] else 0
                if not resume:
                    partial.unlink(missing_ok=True)
                headers = {"Accept-Encoding": "identity"}
                if resume:
                    headers.update({"Range": f"bytes={resume}-", "If-Range": current["validator"]})
                with network.request(candidate["url"], base=candidate["base"], headers=headers) as response:
                    if response.status_code == 206:
                        if not resume or not response.headers.get("Content-Range", "").startswith(f"bytes {resume}-"):
                            raise ValueError("Invalid partial response from download server.")
                    else:
                        resume = 0
                    length = response.headers.get("Content-Length", "")
                    total = int(length) + resume if length.isdigit() else None
                    if total and total > maximum:
                        raise ValueError("File exceeds the configured size limit.")
                    if total and total > shutil.disk_usage(destination).free - reserve:
                        raise RuntimeError("Not enough free space for this file and the configured reserve.")
                    etag = response.headers.get("ETag", "")
                    validator = etag if etag and not etag.startswith("W/") else response.headers.get("Last-Modified", "")
                    if resume and validator and validator != current["validator"]:
                        raise ValueError("The remote file changed while resuming. Retry to download a fresh copy.")
                    with database.connection() as db:
                        db.execute("UPDATE downloads SET format=?,source_id=?,url=?,bytes=?,total=?,validator=?,error='',updated_at=? WHERE id=?",
                                   (candidate["format"], candidate["source_id"], candidate["url"], resume, total, validator, timestamp(), job_id))
                    count, last_report, started = resume, 0, time.monotonic()
                    with partial.open("ab" if resume else "wb") as file:
                        for block in response.iter_content(128 * 1024):
                            if not block:
                                continue
                            if time.monotonic() - last_report >= 0.5:
                                active()
                                if shutil.disk_usage(destination).free <= reserve or shutil.disk_usage(partial.parent).free < 64 * 1024**2:
                                    raise RuntimeError("The download paused at the free-space reserve.")
                                with database.connection() as db:
                                    db.execute("UPDATE downloads SET bytes=?,updated_at=? WHERE id=?", (count, timestamp(), job_id))
                                last_report = time.monotonic()
                            if time.monotonic() - started > 1800:
                                raise ValueError("Transfer exceeded the 30-minute limit.")
                            count += len(block)
                            if count > maximum:
                                raise ValueError("File exceeds the configured size limit.")
                            file.write(block)
                        file.flush()
                        os.fsync(file.fileno())
                    if total is not None and count != total:
                        raise ValueError("Server ended the download before the file was complete.")
                active()
                digest = validate_file(partial, candidate["format"])
                target = destination / safe_filename(book["title"], book["authors"], book["id"], candidate["format"])
                if target.exists() or target.is_symlink():
                    # Keep existing files, including files edited outside Open Shelf.
                    target = target.with_name(f"{target.stem} (download {job_id}){target.suffix}")
                # Publish on the destination filesystem so the final rename is atomic.
                staging = destination / f".openshelf-{job_id}.part"
                try:
                    shutil.copyfile(partial, staging)
                    with staging.open("rb") as file:
                        os.fsync(file.fileno())
                    with database.connection() as db:
                        db.execute("BEGIN IMMEDIATE")
                        status = db.execute("SELECT status FROM downloads WHERE id=?", (job_id,)).fetchone()
                        if status[0] != "running" or stop.is_set():
                            raise Interrupted()
                        if target.exists() or target.is_symlink():
                            raise ValueError("A file already exists at the download destination.")
                        os.replace(staging, target)
                        db.execute("UPDATE downloads SET status='complete',bytes=?,total=?,path=?,sha256=?,error='',updated_at=?,finished_at=? WHERE id=?",
                                   (count, count, str(target), digest, timestamp(), timestamp(), job_id))
                    partial.unlink(missing_ok=True)
                    return
                finally:
                    staging.unlink(missing_ok=True)
            except Interrupted:
                raise
            except Exception as exc:
                last_error = str(exc)[:400]
                log.info("Download %s: source failed: %s", job_id, last_error)
                partial.unlink(missing_ok=True)
                with database.connection() as db:
                    db.execute("UPDATE downloads SET bytes=0,validator='',error=?,updated_at=? WHERE id=?", (last_error, timestamp(), job_id))
                if isinstance(exc, RuntimeError):
                    break
        with database.connection() as db:
            db.execute("UPDATE downloads SET status='failed',error=?,updated_at=?,finished_at=? WHERE id=? AND status='running'", (last_error, timestamp(), timestamp(), job_id))
    except Interrupted:
        with database.connection() as db:
            row = db.execute("SELECT status FROM downloads WHERE id=?", (job_id,)).fetchone()
        if row and row[0] == "cancelled":
            partial.unlink(missing_ok=True)
