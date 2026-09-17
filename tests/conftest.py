from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit
import zipfile

import pytest
from PIL import Image

from openshelf.app import create_app
from openshelf.config import Config


def make_epub():
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as book:
        book.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        book.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        book.writestr("content.opf", '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">openshelf-test</dc:identifier><dc:title>A Synthetic Journey</dc:title><dc:creator>Test Author</dc:creator><dc:language>en</dc:language></metadata><manifest><item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="chapter"/></spine></package>')
        book.writestr("chapter.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Test</title></head><body><p>A wholly synthetic fixture created to test Open Shelf.</p></body></html>')
    return output.getvalue()


@pytest.fixture
def fake_server():
    servers = []

    def start(*, broken=False, opds=False):
        state = {"broken": broken, "fail_page": False, "requests": [], "epub": make_epub(), "opds": opds, "removed": False}
        cover = BytesIO()
        Image.new("RGB", (100, 150), "#305642").save(cover, "PNG")
        state["cover"] = cover.getvalue()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send(self, value, mime="application/json", status=200, headers=None):
                if not isinstance(value, bytes):
                    value = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(value)))
                for key, value_ in (headers or {}).items():
                    self.send_header(key, value_)
                self.end_headers()
                self.wfile.write(value)

            def do_GET(self):
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                state["requests"].append(self.path)
                if url.path.startswith("/files/"):
                    if state["broken"]:
                        return self.send(b"<html><body>Sign in to continue</body></html>", "text/html")
                    data = state["epub"]
                    headers = {"ETag": '"fixture-v1"'}
                    if self.headers.get("Range"):
                        offset = int(self.headers["Range"].split("=")[1].split("-")[0])
                        headers["Content-Range"] = f"bytes {offset}-{len(data)-1}/{len(data)}"
                        return self.send(data[offset:], "application/epub+zip", 206, headers)
                    return self.send(data, "application/epub+zip", headers=headers)
                if url.path == "/cover.png":
                    return self.send(state["cover"], "image/png")
                if state["opds"]:
                    if url.path == "/opds":
                        return self.send(b'<feed xmlns="http://www.w3.org/2005/Atom"><id>fixture</id><title>Catalog</title><entry><id>all</id><title>All books</title><link rel="subsection" type="application/atom+xml;profile=opds-catalog;kind=acquisition" href="/opds/all"/></entry></feed>', "application/atom+xml")
                    if url.path == "/opds/all":
                        return self.send(b'<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/"><id>all</id><title>All books</title><entry><id>book-one</id><title>An OPDS Adventure</title><author><name>Test Author</name></author><dc:language>en</dc:language><category term="Adventure"/><link rel="http://opds-spec.org/acquisition/open-access" type="application/epub+zip" href="/files/1.epub"/></entry><link rel="next" type="application/atom+xml" href="/opds/end"/></feed>', "application/atom+xml")
                    if url.path == "/opds/end":
                        return self.send(b'<feed xmlns="http://www.w3.org/2005/Atom"><id>end</id><title>End</title><link rel="next" type="application/atom+xml" href="/opds/all"/></feed>', "application/atom+xml")
                    return self.send({}, status=404)
                if url.path == "/ajax/library-info":
                    return self.send({"library_map": {"main": "Main", "annex": "Annex"}, "default_library": "main"})
                if url.path.startswith("/ajax/search"):
                    offset = int(query.get("offset", [0])[0])
                    if state["fail_page"] and offset:
                        return self.send({}, status=503)
                    ids = [1, 2] if state["removed"] else [1, 2, 3]
                    if url.path.endswith("/annex"):
                        ids = [4]
                    num = int(query.get("num", [100])[0])
                    return self.send({"total_num": len(ids), "book_ids": ids[offset:offset+num]})
                if url.path.startswith("/ajax/books"):
                    result = {}
                    for ident in query.get("ids", [""])[0].split(","):
                        result[ident] = {"title": {"1": "A Synthetic Journey", "2": "A Synthetic Return", "3": "A French Journey", "4": "An Annex Story"}[ident],
                            "authors": ["Test Author"], "languages": ["fra" if ident == "3" else "eng"],
                            "comments": "<p>An original test description.</p><script>bad()</script>", "tags": ["Fantasy", "Adventure"],
                            "series": "Synthetic Stories" if ident in ("1", "2") else None, "series_index": int(ident),
                            "pubdate": "2026-01-01T00:00:00Z", "publisher": "Fixture Press", "identifiers": {},
                            "main_format": {"epub": f"/files/{ident}.epub"}, "other_formats": {},
                            "format_metadata": {"epub": {"size": len(state["epub"])}}, "thumbnail": "/cover.png"}
                    return self.send(result)
                return self.send({}, status=404)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        state["url"] = f"http://127.0.0.1:{server.server_port}"
        return state

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def app(tmp_path):
    config = Config(tmp_path / "state", tmp_path / "downloads", request_delay=0, connect_timeout=1, read_timeout=2, page_size=2, testing=True)
    app = create_app(config)
    yield app
    app.extensions["workers"].close()
    app.extensions["covers"].pool.shutdown(wait=True, cancel_futures=True)


@pytest.fixture
def client(app):
    client = app.test_client()
    csrf = client.get("/api/bootstrap").json["csrf"]
    client.environ_base["HTTP_X_CSRF_TOKEN"] = csrf
    return client


@pytest.fixture
def search_catalog(app, client):
    from openshelf.metadata import upsert_record
    source = client.post("/api/sources/import", json={"text":"http://example.test", "index":False}).json["source_ids"][0]
    records = [
        ("brown", "The Synthetic Code", ["Dan Brown"], "Code Stories", 1, 2003, ["Mystery"], "A puzzle beneath the city.", "Harbor Press", {"isbn":"978-0-123456-78-9"}),
        ("hyperion", "Hyperion", ["Dan Simmons"], "Hyperion Cantos", 1, 1989, ["Science Fiction", "Space Opera"], "Travellers under distant stars.", "Harbor Press", {"isbn":"9780123456796"}),
        ("fall", "The Fall of Hyperion", ["Simmons, Dan"], "Hyperion Cantos", 2, 1990, ["Science Fiction"], "The travellers return to the stars.", "Orbit House", {"asin":"B0TESTBOOK"}),
        ("wrong_series", "Another Story", ["Dan Simmons"], "Different Series", 1, 2001, ["Fiction"], "An unrelated story.", "Orbit House", {}),
        ("mention", "Reading Dan Brown", ["A Different Writer"], "Author Studies", 1, 2011, ["Nonfiction"], "Comparisons of Dan Brown, Dan Simmons, and Hyperion.", "Harbor Press", {}),
        ("long_name", "A Different Code", ["Daniel Brown"], "Code Stories", 2, 2005, ["Mystery"], "A different author.", "Harbor Press", {}),
        ("coauthors", "A Shared Story", ["Dan Other", "Alex Brown"], "", None, None, ["Science", "Fiction"], "Two coauthors.", "Other Press", {}),
        ("accent", "Año: 100%_Life", ["José Álvarez"], "", 0.5, 2020, ["Essays"], "Memories by the sea.", "Éditions du Port", {"doi":"10.1234/test-book"}),
    ]
    ids = {}
    with app.extensions["database"].connection() as db:
        for key,title,authors,series,position,year,tags,description,publisher,identifiers in records:
            ids[key] = upsert_record(db, source, 0, {"library":"main", "remote_id":key, "title":title,"authors":authors,
                "series":series,"series_position":position,"year":year,"tags":tags,"description":description,
                "publisher":publisher,"identifiers":identifiers,"language":"eng",
                "files":[{"format":"EPUB","url":f"http://example.test/{key}.epub","size":1000}]})
    return ids
