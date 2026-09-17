import hashlib
import json
from pathlib import Path
import threading

from openshelf.downloads import run_download
from openshelf.indexer import run_scan
from openshelf.network import parse_servers, source_link


def scan_all(app):
    database = app.extensions["database"]
    while job := database.claim("scans"):
        run_scan(database, app.extensions["workers"].network, job, threading.Event())


def test_remote_catalog_and_download_fallback(app, client, fake_server):
    first, second = fake_server(broken=True), fake_server()
    response = client.post("/api/sources/import", json={"text": f"> 3| {first['url']}|99|true\n{second['url']}\n{first['url']}"})
    assert response.status_code == 200
    assert response.json["added"] == 2
    scan_all(app)
    sources = client.get("/api/sources").json["sources"]
    assert all(s["status"] == "ready" and s["records"] == 4 for s in sources)
    result = client.get("/api/books?language=eng&format=EPUB&tag=Fantasy").json
    assert result["total"] == 3
    assert all(b["source_count"] == 2 for b in result["books"])
    book = client.get("/api/books?q=synthetic%20journey").json["books"][0]
    details = client.get("/api/books/" + book["id"]).json
    assert "bad()" not in details["description"]
    assert len(details["copies"]) == 2
    assert client.get("/api/series").json["series"][0]["books"] == 2
    assert client.post(f"/api/books/{book['id']}/save", json={"saved": True}).status_code == 200
    assert client.get("/api/books?saved=true").json["total"] == 1
    assert client.post("/api/downloads", json={"ids": [book["id"]]}).json["queued"] == 1
    database = app.extensions["database"]
    job = database.claim("downloads")
    run_download(database, app.extensions["workers"].network, job, threading.Event())
    downloaded = client.get("/api/downloads").json["downloads"][0]
    assert downloaded["status"] == "complete", downloaded
    assert Path(downloaded["path"]).read_bytes() == second["epub"]
    assert downloaded["sha256"] == hashlib.sha256(second["epub"]).hexdigest()
    assert not list(database.config.download_dir.glob("*.part"))
    assert client.get(f"/api/downloads/{job['id']}/file", headers={"Range": "bytes=0-15"}).status_code == 206
    assert client.post("/api/downloads", json={"ids": [book["id"]]}).json["skipped"] == 1


def test_failed_scan_resumes_and_only_complete_scan_retires_copies(app, client, fake_server):
    server = fake_server()
    server["fail_page"] = True
    source_id = client.post("/api/sources/import", json={"text": server["url"]}).json["source_ids"][0]
    scan_all(app)
    assert client.get("/api/books").json["total"] == 2
    assert client.get("/api/sources").json["sources"][0]["status"] == "error"
    before = len(server["requests"])
    server["fail_page"] = False
    assert client.post("/api/sources/scan", json={"ids": [source_id]}).status_code == 200
    scan_all(app)
    assert client.get("/api/books").json["total"] == 4
    assert any("offset=2" in r for r in server["requests"][before:])
    book_id = client.get("/api/books?q=french").json["books"][0]["id"]
    client.post(f"/api/books/{book_id}/save", json={"saved": True})
    server["removed"] = True
    client.post("/api/sources/scan", json={"ids": [source_id], "restart": True})
    scan_all(app)
    assert client.get("/api/books").json["total"] == 3
    assert client.get("/api/books?saved=true&availability=all").json["total"] == 1


def test_opds_navigation_pagination_and_cycle_detection(app, client, fake_server):
    server = fake_server(opds=True)
    client.post("/api/sources/import", json={"text": server["url"]})
    scan_all(app)
    sources = client.get("/api/sources").json["sources"]
    assert sources[0]["status"] == "ready", sources
    assert sources[0]["protocol"] == "opds"
    assert client.get("/api/books?language=eng").json["total"] == 1
    assert server["requests"].count("/opds/all") == 1


def test_queue_restart_partial_resume_and_mutation_protection(app, client, fake_server):
    server = fake_server()
    client.post("/api/sources/import", json={"text": server["url"]})
    scan_all(app)
    book = client.get("/api/books?q=synthetic%20journey").json["books"][0]
    client.post("/api/downloads", json={"ids": [book["id"]]})
    database = app.extensions["database"]
    first_job = database.claim("downloads")
    partial = database.config.data_dir / "partial" / f"{first_job['id']}.part"
    partial.write_bytes(server["epub"][:100])
    with database.connection() as db:
        db.execute("UPDATE downloads SET url=?,validator=?,bytes=100 WHERE id=?", (server["url"] + "/files/1.epub", '"fixture-v1"', first_job["id"]))
    database.recover()
    job = database.claim("downloads")
    run_download(database, app.extensions["workers"].network, job, threading.Event())
    assert client.get("/api/downloads").json["downloads"][0]["status"] == "complete"
    assert client.post("/api/settings", json={}).status_code == 405
    assert app.test_client().patch("/api/settings", json={"downloads_paused": True}).status_code == 403
    assert client.patch("/api/settings", json={"download_dir": "relative/path"}).status_code == 400
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400


def test_import_validation_and_user_supplied_origin_boundary():
    result = parse_servers('> http://EXAMPLE.org:80/| 100|true\nhttps://example.org/books\nhttp://example.org')
    assert result["urls"] == ["http://example.org", "https://example.org/books"]
    assert result["duplicates"] == 1
    assert not parse_servers("http://169.254.169.254/latest/")["urls"]
    assert not parse_servers("http://user:password@example.org/")["urls"]
    assert source_link("http://localhost:8090/prefix", "/book/1.epub") == "http://localhost:8090/book/1.epub"
    import pytest
    with pytest.raises(ValueError):
        source_link("http://example.org", "http://localhost:8080/admin")
