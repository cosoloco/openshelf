from collections import Counter
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import threading
import time

from flask import Flask, abort, jsonify, render_template, request, send_file, session

from . import __version__
from .catalogs import FORMATS
from .config import Config
from .covers import Covers
from .db import Database, timestamp
from .downloads import queue_books
from .indexer import queue_scan
from .network import parse_servers
from .queries import BOOK_FIELDS, book_condition, book_json, search_books
from .workers import Workers


def create_app(config=None, *, start_workers=False):
    config = config or Config.from_env()
    database = Database(config)
    keyfile = config.data_dir / ".session-key"
    if not keyfile.exists():
        try:
            fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_hex(32))
        except FileExistsError:
            pass
    app = Flask(__name__)
    app.config.update(SECRET_KEY=keyfile.read_text().strip(), TESTING=config.testing,
        MAX_CONTENT_LENGTH=2_100_000, SESSION_COOKIE_NAME="openshelf",
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
        TRUSTED_HOSTS=os.environ.get("OPENSHELF_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(","))
    workers = Workers(database)
    covers = Covers(database, workers.network)
    app.extensions.update(database=database, workers=workers, covers=covers)
    if start_workers:
        workers.start()

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("Send a JSON object.")
        return value

    def integer(value, default=1, maximum=1_000_000):
        try:
            return min(maximum, max(1, int(value)))
        except (ValueError, TypeError):
            return default

    @app.before_request
    def protect_mutations():
        if request.method in ("POST", "PATCH", "DELETE", "PUT"):
            expected = session.get("csrf", "")
            if not request.is_json or not expected or not secrets.compare_digest(expected, request.headers.get("X-CSRF-Token", "")):
                abort(403, "Reload Open Shelf before making changes.")
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                abort(403, "Cross-site changes are not allowed.")

    @app.after_request
    def response_headers(response):
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"})
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    for code in (400, 403, 404, 409, 413, 405):
        app.register_error_handler(code, lambda exc: (jsonify(error=exc.description), exc.code))

    @app.get("/")
    def index():
        return render_template("index.html", csrf=csrf_token(), version=__version__)

    @app.get("/api/health")
    def health():
        return jsonify(status="ok", version=__version__)

    stats_cache, stats_lock = {}, threading.Lock()

    @app.get("/api/bootstrap")
    def bootstrap():
        with stats_lock:
            if time.monotonic() - stats_cache.get("at", 0) > 8:
                with database.connection() as db:
                    stats_cache.update(at=time.monotonic(), data={
                        "books": db.execute("SELECT COUNT(*) FROM books").fetchone()[0],
                        "series": db.execute("SELECT COUNT(DISTINCT series COLLATE NOCASE) FROM books WHERE series<>''").fetchone()[0],
                        "tags": [dict(r) for r in db.execute("SELECT tag,COUNT(*) AS count FROM book_tags GROUP BY tag COLLATE NOCASE ORDER BY count DESC LIMIT 100")],
                        "languages": [dict(r) for r in db.execute("SELECT language,COUNT(*) AS count FROM books GROUP BY language ORDER BY count DESC")],
                        "formats": [dict(r) for r in db.execute("SELECT format,COUNT(*) AS count FROM files GROUP BY format ORDER BY count DESC")],
                    })
            result = dict(stats_cache["data"])
        with database.connection() as db:
            result.update(sources=[dict(r) for r in db.execute("SELECT id,name,url,status,records FROM sources ORDER BY name")],
                saved=db.execute("SELECT COUNT(*) FROM saved").fetchone()[0],
                scans={r["status"]: r["n"] for r in db.execute("SELECT status,COUNT(*) n FROM scans GROUP BY status")},
                downloads={r["status"]: r["n"] for r in db.execute("SELECT status,COUNT(*) n FROM downloads GROUP BY status")})
        result.update(csrf=csrf_token(), settings=database.settings(), version=__version__)
        return jsonify(result)

    @app.get("/api/books")
    def books():
        with database.connection() as db:
            return jsonify(search_books(db, request.args, integer(request.args.get("page")), integer(request.args.get("per_page"), 48, 96)))

    @app.get("/api/books/<book_id>")
    def book(book_id):
        with database.connection() as db:
            row = db.execute(f"SELECT {BOOK_FIELDS},b.description,b.identifiers_json FROM books b WHERE b.id=?", (book_id,)).fetchone()
            if row is None:
                abort(404, "Book not found.")
            result = book_json(row)
            result["copies"] = [dict(r) for r in db.execute("""SELECT c.id,c.library,c.last_seen,c.active,c.source_id,s.name,s.url,s.status,
                 GROUP_CONCAT(DISTINCT f.format) AS formats FROM copies c JOIN sources s ON s.id=c.source_id
                 LEFT JOIN files f ON f.copy_id=c.id WHERE c.book_id=? GROUP BY c.id ORDER BY c.active DESC,s.status='error',c.last_seen DESC""", (book_id,))]
            for copy in result["copies"]:
                copy["files"] = [dict(r) for r in db.execute("SELECT format,url,size FROM files WHERE copy_id=? ORDER BY format", (copy["id"],))]
            result["downloads"] = [dict(r) for r in db.execute("SELECT id,status,format,bytes,path,error FROM downloads WHERE book_id=? ORDER BY id DESC", (book_id,))]
            return jsonify(result)

    @app.post("/api/books/<book_id>/save")
    def save_book(book_id):
        value = body().get("saved")
        with database.connection() as db:
            if not db.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
                abort(404, "Book not found.")
            if value:
                db.execute("INSERT OR IGNORE INTO saved VALUES(?,?)", (book_id, timestamp()))
            else:
                db.execute("DELETE FROM saved WHERE book_id=?", (book_id,))
        return jsonify(saved=bool(value))

    @app.get("/api/series")
    def series():
        query = str(request.args.get("q", ""))[:500]
        page = integer(request.args.get("page"))
        with database.connection() as db:
            condition = "series<>'' AND series LIKE ? ESCAPE '\\'"
            term = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            total = db.execute(f"SELECT COUNT(DISTINCT series COLLATE NOCASE) FROM books WHERE {condition}", (term,)).fetchone()[0]
            rows = [dict(r) for r in db.execute(f"SELECT series AS name,COUNT(*) AS books FROM books WHERE {condition} GROUP BY series COLLATE NOCASE ORDER BY series COLLATE NOCASE LIMIT 36 OFFSET ?", (term, (page - 1) * 36))]
            for row in rows:
                row["covers"] = [r[0] for r in db.execute("SELECT id FROM books WHERE series=? COLLATE NOCASE ORDER BY series_position IS NULL,series_position LIMIT 4", (row["name"],))]
        return jsonify(series=rows, total=total, page=page, pages=max(1, math.ceil(total / 36)))

    @app.get("/api/sources")
    def sources():
        with database.connection() as db:
            rows = [dict(r) for r in db.execute("""SELECT s.*,j.id AS scan_id,j.status AS scan_status,j.processed,j.total,j.message,
                j.updated_at AS scan_updated FROM sources s LEFT JOIN scans j ON j.id=(SELECT MAX(id) FROM scans WHERE source_id=s.id)
                ORDER BY CASE s.status WHEN 'indexing' THEN 0 WHEN 'queued' THEN 1 WHEN 'ready' THEN 2 WHEN 'new' THEN 3 ELSE 4 END,s.id""")]
        return jsonify(sources=rows, counts=dict(Counter(r["status"] for r in rows)))

    @app.post("/api/sources/preview")
    def preview_sources():
        result = parse_servers(str(body().get("text", "")))
        with database.connection() as db:
            existing = {r[0] for r in db.execute("SELECT url FROM sources")}
        result["existing"] = sum(url in existing for url in result["urls"])
        return jsonify(result)

    @app.post("/api/sources/import")
    def import_sources():
        payload = body()
        parsed = parse_servers(str(payload.get("text", "")))
        if not parsed["urls"]:
            raise ValueError("No valid HTTP or HTTPS server addresses were found.")
        added, ids, to_scan = 0, [], []
        with database.connection() as db:
            for url in parsed["urls"]:
                result = db.execute("INSERT OR IGNORE INTO sources(url,name,added_at) VALUES(?,?,?)", (url, url.split("://", 1)[1], timestamp()))
                added += result.rowcount
                source = db.execute("SELECT id,status FROM sources WHERE url=?", (url,)).fetchone()
                ids.append(source["id"])
                if source["status"] == "new":
                    to_scan.append(source["id"])
        if payload.get("index", True):
            for source_id in to_scan:
                queue_scan(database, source_id)
        return jsonify(added=added, existing=len(ids) - added, invalid=parsed["invalid"], source_ids=ids)

    @app.post("/api/sources/scan")
    def scan_sources():
        payload = body()
        ids = payload.get("ids")
        with database.connection() as db:
            if ids is None:
                condition = "enabled=1" + (" AND status='error'" if payload.get("failed_only") else "")
                ids = [r[0] for r in db.execute("SELECT id FROM sources WHERE " + condition)]
        if not isinstance(ids, list) or len(ids) > 5000:
            raise ValueError("Choose a valid set of servers.")
        for source_id in ids:
            queue_scan(database, int(source_id), restart=bool(payload.get("restart")))
        return jsonify(queued=len(ids))

    @app.post("/api/sources/<int:source_id>/action")
    def source_action(source_id):
        action = body().get("action")
        if action == "resume":
            queue_scan(database, source_id)
        elif action in ("pause", "disable"):
            with database.connection() as db:
                db.execute("UPDATE scans SET status='paused',updated_at=? WHERE source_id=? AND status IN ('running','queued')", (timestamp(), source_id))
                db.execute("UPDATE sources SET status=?,enabled=? WHERE id=?", ("paused" if action == "pause" else "disabled", action != "disable", source_id))
        else:
            raise ValueError("Unknown server action.")
        return jsonify(ok=True)

    @app.delete("/api/sources/<int:source_id>")
    def remove_source(source_id):
        with database.connection() as db:
            source = db.execute("SELECT id,name,records FROM sources WHERE id=?", (source_id,)).fetchone()
            if not source:
                abort(404, "Server not found.")
            # A running worker checks its queue row between requests. Marking a
            # job queued makes it stop and choose another remaining source.
            db.execute("UPDATE scans SET status='paused',updated_at=? WHERE source_id=? AND status IN ('running','queued')",
                       (timestamp(), source_id))
            db.execute("UPDATE downloads SET status='queued',source_id=NULL,error='',updated_at=? WHERE source_id=? AND status='running'",
                       (timestamp(), source_id))
            db.execute("DELETE FROM sources WHERE id=?", (source_id,))
            orphaned = [row[0] for row in db.execute("SELECT b.id FROM books b WHERE NOT EXISTS(SELECT 1 FROM copies c WHERE c.book_id=b.id)")]
            if orphaned:
                marks = ",".join("?" for _ in orphaned)
                db.execute(f"DELETE FROM saved WHERE book_id IN ({marks})", orphaned)
                db.execute(f"DELETE FROM downloads WHERE book_id IN ({marks})", orphaned)
                db.execute(f"DELETE FROM book_tags WHERE book_id IN ({marks})", orphaned)
                db.execute(f"DELETE FROM books WHERE id IN ({marks})", orphaned)
        return jsonify(removed=source["name"], records=source["records"], orphaned=len(orphaned))

    @app.get("/api/sources/export")
    def export_sources():
        with database.connection() as db:
            content = "\n".join(r[0] for r in db.execute("SELECT url FROM sources ORDER BY id")) + "\n"
        return app.response_class(content, mimetype="text/plain", headers={"Content-Disposition": 'attachment; filename="openshelf-servers.txt"'})

    @app.post("/api/downloads")
    def download_books():
        payload = body()
        ids = payload.get("ids")
        if ids is None and isinstance(payload.get("filters"), dict):
            condition, args = book_condition(payload["filters"])
            with database.connection() as db:
                ids = [r[0] for r in db.execute("SELECT b.id FROM books b WHERE " + condition, args)]
        if not isinstance(ids, list):
            raise ValueError("Select books to download.")
        return jsonify(queue_books(database, ids, payload.get("format", "auto")))

    @app.get("/api/downloads")
    def downloads():
        status = request.args.get("status", "")
        page = integer(request.args.get("page"))
        condition, args = ("d.status=?", [status]) if status else ("1", [])
        with database.connection() as db:
            counts = {r["status"]: r["n"] for r in db.execute("SELECT status,COUNT(*) n FROM downloads GROUP BY status")}
            total = db.execute("SELECT COUNT(*) FROM downloads d WHERE " + condition, args).fetchone()[0]
            rows = [dict(r) for r in db.execute(f"""SELECT d.*,b.title,b.authors FROM downloads d JOIN books b ON b.id=d.book_id WHERE {condition}
                ORDER BY CASE d.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'paused' THEN 2 WHEN 'failed' THEN 3 ELSE 4 END,d.id DESC LIMIT 60 OFFSET ?""", [*args, (page - 1) * 60])]
            complete_bytes = db.execute("SELECT COALESCE(SUM(bytes),0) FROM downloads WHERE status='complete'").fetchone()[0]
        return jsonify(downloads=rows, counts=counts, total=total, page=page, pages=max(1, math.ceil(total / 60)), complete_bytes=complete_bytes, settings=database.settings())

    @app.post("/api/downloads/<int:download_id>/action")
    def download_action(download_id):
        action = body().get("action")
        with database.connection() as db:
            row = db.execute("SELECT * FROM downloads WHERE id=?", (download_id,)).fetchone()
            if not row:
                abort(404, "Download not found.")
            status = {"pause": "paused", "resume": "queued", "retry": "queued", "cancel": "cancelled"}.get(action)
            allowed = {"pause": ("queued", "running"), "resume": ("paused",), "retry": ("failed", "cancelled"), "cancel": ("queued", "running", "paused")}
            if not status or row["status"] not in allowed[action]:
                raise ValueError("This action is not available for that download.")
            try:
                db.execute("UPDATE downloads SET status=?,error='',updated_at=?,finished_at=NULL WHERE id=?", (status, timestamp(), download_id))
            except sqlite3.IntegrityError:
                raise ValueError("Another download of this book is already queued.")
        return jsonify(ok=True)

    @app.get("/api/downloads/<int:download_id>/file")
    def completed_file(download_id):
        with database.connection() as db:
            row = db.execute("SELECT * FROM downloads WHERE id=? AND status='complete'", (download_id,)).fetchone()
        if not row or not row["path"]:
            abort(404, "The download is not complete.")
        path = Path(row["path"])
        if not path.is_file() or path.is_symlink():
            abort(404, "The file has been moved or removed from its download folder.")
        return send_file(path, as_attachment=True, download_name=path.name, conditional=True)

    @app.get("/api/settings")
    def settings():
        values = database.settings()
        directory = Path(values["download_dir"])
        values["free_bytes"] = shutil.disk_usage(directory if directory.exists() else config.data_dir).free
        return jsonify(values)

    @app.patch("/api/settings")
    def update_settings():
        payload = body()
        allowed = {"download_dir", "preferred_format", "downloads_paused", "indexing_paused", "reserve_gb", "max_file_mb"}
        if set(payload) - allowed:
            raise ValueError("Unknown setting.")
        for name in ("downloads_paused", "indexing_paused"):
            if name in payload and not isinstance(payload[name], bool):
                raise ValueError("Pause settings must be true or false.")
        if "download_dir" in payload:
            path = Path(str(payload["download_dir"])).expanduser()
            if not path.is_absolute():
                raise ValueError("Choose an absolute folder path.")
            try:
                path.mkdir(parents=True, exist_ok=True)
                if not os.access(path, os.W_OK):
                    raise OSError("Folder is not writable")
            except OSError as exc:
                raise ValueError("The download folder must be writable by Open Shelf.") from exc
            payload["download_dir"] = str(path.resolve())
        if "preferred_format" in payload and payload["preferred_format"] not in FORMATS:
            raise ValueError("Choose a supported format.")
        for name, lower, upper in (("reserve_gb", 0.25, 10000), ("max_file_mb", 1, 10240)):
            if name in payload:
                value = float(payload[name])
                if not math.isfinite(value) or not lower <= value <= upper:
                    raise ValueError(f"{name} must be between {lower} and {upper}.")
                payload[name] = value
        database.set_settings(payload)
        return jsonify(database.settings())

    @app.get("/api/covers")
    def cover_status():
        ids = [v for v in request.args.get("ids", "").split(",")[:100] if re.fullmatch(r"[a-f0-9]{32}", v)]
        return jsonify(ready=[i for i in ids if covers.path(i).exists()])

    @app.get("/cover/<book_id>")
    def cover(book_id):
        if not re.fullmatch(r"[a-f0-9]{32}", book_id):
            abort(404)
        path = covers.path(book_id)
        if path.exists():
            return send_file(path, mimetype="image/webp", max_age=86400, conditional=True)
        covers.schedule(book_id)
        from html import escape
        import textwrap
        with database.connection() as db:
            row = db.execute("SELECT title,authors FROM books WHERE id=?", (book_id,)).fetchone()
        if row is None:
            abort(404)
        hues = ("#315446", "#485b70", "#79604f", "#665875", "#62704b", "#83514e")
        color = hues[int(book_id[:4], 16) % len(hues)]
        lines = textwrap.wrap(row["title"], 20)[:6]
        titles = "".join(f'<tspan x="150" y="{145+i*31}">{escape(line)}</tspan>' for i, line in enumerate(lines))
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" viewBox="0 0 300 450"><rect width="300" height="450" fill="{color}"/><rect x="17" y="17" width="266" height="416" fill="none" stroke="#ffffff40"/><path d="M122 68h56M142 58h16" stroke="#d7dfc4" stroke-width="2"/><text text-anchor="middle" fill="#f6f0df" font-family="Georgia,serif" font-size="25">{titles}</text><text x="150" y="397" text-anchor="middle" fill="#eee9d8" font-family="sans-serif" font-size="12">{escape(row["authors"][:35])}</text></svg>'
        return app.response_class(svg, mimetype="image/svg+xml", headers={"Cache-Control": "no-cache"})

    return app
