import threading

from openshelf.downloads import run_download
from openshelf.indexer import run_scan, queue_scan
from openshelf.metadata import upsert_record
from openshelf.network import source_link
from test_flow import scan_all
from pathlib import Path


def test_metadata_without_download_formats_and_preserved_saved_book(app, client):
    database = app.extensions["database"]
    source = client.post("/api/sources/import", json={"text": "http://example.test", "index": False}).json["source_ids"][0]
    with database.connection() as db:
        book_id = upsert_record(db, source, 1, {"library":"main", "remote_id":"1", "title":"Metadata Only", "authors":["Fixture Author"], "language":"eng", "files":[]})
    listing = client.get("/api/books").json
    assert listing["books"][0]["formats"] == []
    details = client.get("/api/books/" + book_id).json
    assert details["formats"] == []
    assert client.post("/api/downloads", json={"ids":[book_id]}).json == {"queued":0,"skipped":1}


def test_claim_excludes_jobs_still_finishing_after_pause(app, client):
    source = client.post("/api/sources/import", json={"text":"http://example.test"}).json["source_ids"][0]
    db = app.extensions["database"]
    job = db.claim("scans")
    client.post(f"/api/sources/{source}/action",json={"action":"pause"})
    client.post(f"/api/sources/{source}/action",json={"action":"resume"})
    assert db.claim("scans", {job["id"]}) is None
    assert db.claim("scans")["id"] == job["id"]


def test_refresh_does_not_resume_an_obsolete_failed_scan(app, client, fake_server):
    remote = fake_server()
    remote["fail_page"] = True
    source = client.post("/api/sources/import", json={"text": remote["url"]}).json["source_ids"][0]
    scan_all(app)
    database = app.extensions["database"]
    remote["fail_page"] = False
    fresh_id = queue_scan(database, source, restart=True)
    scan_all(app)
    assert queue_scan(database, source) > fresh_id
    scan_all(app)
    assert client.get("/api/books").json["total"] == 4


def test_download_preserves_existing_user_file(app, client, fake_server):
    remote = fake_server()
    client.post("/api/sources/import", json={"text": remote["url"]})
    scan_all(app)
    book = client.get("/api/books?q=synthetic%20journey").json["books"][0]
    client.post("/api/downloads", json={"ids":[book["id"]]})
    database = app.extensions["database"]
    job = database.claim("downloads")
    from openshelf.downloads import safe_filename
    original = database.config.download_dir / safe_filename(book["title"], "Test Author", book["id"], "EPUB")
    original.write_bytes(b"Existing user file")
    run_download(database, app.extensions["workers"].network, job, threading.Event())
    result = client.get("/api/downloads").json["downloads"][0]
    assert result["status"] == "complete"
    assert original.read_bytes() == b"Existing user file"
    assert Path(result["path"]).read_bytes() == remote["epub"]


def test_standard_https_upgrade_is_allowed_without_cross_host_links():
    assert source_link("http://example.org", "https://example.org/books/1") == "https://example.org/books/1"
    import pytest
    with pytest.raises(ValueError):
        source_link("https://example.org", "http://example.org/book")
    with pytest.raises(ValueError):
        source_link("http://example.org:8080", "https://example.org/book")


def test_importing_duplicate_does_not_restart_or_reenable_source(app, client, fake_server):
    remote = fake_server()
    source = client.post("/api/sources/import", json={"text":remote["url"]}).json["source_ids"][0]
    scan_all(app)
    result = client.post("/api/sources/import", json={"text":remote["url"]}).json
    assert result["added"] == 0 and result["existing"] == 1
    assert client.get("/api/sources").json["sources"][0]["status"] == "ready"
    client.post(f"/api/sources/{source}/action", json={"action":"disable"})
    client.post("/api/sources/import", json={"text":remote["url"]})
    assert client.get("/api/sources").json["sources"][0]["status"] == "disabled"
    with app.extensions["database"].connection() as db:
        assert db.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1
