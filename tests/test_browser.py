import os
import re
import threading
import time

import pytest
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server, WSGIRequestHandler


@pytest.mark.skipif(not os.environ.get("OPENSHELF_BROWSER_TESTS"), reason="Set OPENSHELF_BROWSER_TESTS=1 with Chromium installed")
def test_remove_server_uses_app_dialog_and_recovers_from_failure(app, client):
    source_id = client.post("/api/sources/import", json={"text": "http://example.test", "index": False}).json["source_ids"][0]
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width":390, "height":844})
            errors, deletes = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: deletes.append(request.url) if request.method == "DELETE" else None)
            page.add_init_script("window.confirm = () => { throw new Error('Native dialogs must not be used'); };")
            page.goto(f"http://127.0.0.1:{server.server_port}/#servers")
            remove = page.locator(f'[data-remove-source="{source_id}"]')
            dialog = page.locator('#remove-source-dialog')
            remove.click()
            expect(dialog).to_be_visible()
            expect(page.locator('#remove-source-cancel')).to_be_focused()
            expect(dialog).to_contain_text('http://example.test')
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            page.locator('#remove-source-cancel').click()
            expect(dialog).to_be_hidden()
            expect(remove).to_be_focused()
            remove.click()
            page.keyboard.press('Escape')
            expect(dialog).to_be_hidden()
            remove.click()
            page.mouse.click(1, 1)
            expect(dialog).to_be_hidden()
            assert deletes == []
            assert len(client.get('/api/sources').json['sources']) == 1
            pattern = f'**/api/sources/{source_id}'
            page.route(pattern, lambda route: route.fulfill(status=503, content_type='application/json', body='{"error":"Please retry removal."}'))
            remove.click()
            page.locator('#remove-source-submit').click()
            expect(page.locator('#remove-source-error')).to_have_text('Please retry removal.')
            expect(dialog).to_be_visible()
            expect(page.locator('#remove-source-submit')).to_be_enabled()
            page.unroute(pattern)
            page.locator('#remove-source-submit').click()
            expect(dialog).to_be_hidden()
            expect(page.locator('#results-count')).to_have_text('No servers connected')
            assert client.get('/api/sources').json['sources'] == []
            assert len(deletes) == 2
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(not os.environ.get("OPENSHELF_BROWSER_TESTS"), reason="Set OPENSHELF_BROWSER_TESTS=1 with Chromium installed")
def test_empty_install_import_browse_save_and_download(app, fake_server, tmp_path):
    remote, opds = fake_server(), fake_server(opds=True)

    class Quiet(WSGIRequestHandler):
        def log(self, *_args, **_kwargs):
            pass

    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    app.extensions["workers"].start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width":1440,"height":1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base)
            expect(page.locator(".empty-state")).to_contain_text("A whole shelf of possibilities")
            page.get_by_role("button", name="Connect your first server").click()
            page.locator("#server-text").fill(remote["url"])
            expect(page.locator("#import-preview")).to_contain_text("1 addresses found")
            page.locator("#import-submit").click()
            expect(page.locator(".source-table tbody tr")).to_have_count(1)
            page.get_by_role("button",name="Import servers",exact=True).click()
            page.locator("#server-text").fill(f"> 1 | {remote['url']} | true\n> 2 | {opds['url']} | true")
            expect(page.locator("#import-preview")).to_contain_text("1 already imported")
            page.locator("#import-submit").click()
            expect(page.locator(".source-table tbody tr")).to_have_count(2)
            page.wait_for_function("async () => { const d=await (await fetch('/api/sources')).json(); return d.sources.length === 2 && d.sources.every(s=>s.status==='ready'); }", polling=250)
            page.locator('.nav-link[data-page="catalog"]').click()
            expect(page.locator(".book-card")).to_have_count(5, timeout=15000)
            page.locator("#language-filter").select_option("eng")
            expect(page.locator(".book-card")).to_have_count(4)
            page.locator("#search").fill("synthetic")
            expect(page.locator(".book-card")).to_have_count(2)
            page.locator(".book-title").first.click()
            expect(page.locator("#book-dialog")).to_be_visible()
            page.locator('#book-detail [data-save]').click()
            expect(page.locator('#book-detail [data-save]')).to_contain_text("Saved for later")
            page.locator(".detail-series-link").click()
            expect(page.locator("#page-title")).to_have_text("Synthetic Stories")
            expect(page.locator("#language-filter")).to_have_value("")
            expect(page.locator("#sort")).to_have_value("series")
            expect(page.locator(".book-title").first).to_have_text("A Synthetic Journey")
            page.locator("#select-page").check()
            page.get_by_role("button",name="Download 2 selected").click()
            page.locator("#queue-format").select_option("EPUB")
            page.locator("#queue-submit").click()
            expect(page).to_have_url(re.compile(r"#downloads$"))
            page.wait_for_function("async () => { const d=await (await fetch('/api/downloads')).json(); return d.counts.complete===2; }", polling=250)
            page.reload()
            expect(page.locator(".download-row")).to_have_count(2)
            with page.expect_download() as received:
                page.locator('.download-actions a').first.click()
            downloaded = received.value
            downloaded.save_as(tmp_path / "browser-copy.epub")
            assert (tmp_path / "browser-copy.epub").read_bytes() == remote["epub"]
            page.locator('.nav-link[data-page="settings"]').click()
            page.locator("#setting-format").select_option("PDF")
            page.get_by_role("button",name="Save settings").click()
            expect(page.locator("#toast")).to_contain_text("Preferences saved")
            page.reload()
            expect(page.locator("#setting-format")).to_have_value("PDF")
            page.set_viewport_size({"width":390,"height":844})
            page.locator("#mobile-toggle").click()
            page.locator('.nav-link[data-page="saved"]').click()
            expect(page.locator(".book-card")).to_have_count(1)
            expect(page.locator("#mobile-toggle")).to_have_attribute("aria-expanded","false")
            page.wait_for_timeout(250)
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            page.locator("#mobile-toggle").click()
            page.locator('.nav-link[data-page="servers"]').click()
            expect(page.locator(".source-table tbody tr")).to_have_count(2)
            page.wait_for_timeout(250)
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth"), page.evaluate("""() => Array.from(document.querySelectorAll('body *')).filter(e => e.getBoundingClientRect().right > innerWidth+1 && !e.closest('.table-wrap,.symbols')).map(e=>({tag:e.tagName,id:e.id,class:e.className,width:e.getBoundingClientRect().width}))""")
            assert errors == []
            browser.close()
    finally:
        app.extensions["workers"].close()
        for worker in app.extensions["workers"].threads:
            worker.join(timeout=3)
        server.shutdown()
        server.server_close()


@pytest.mark.skipif(not os.environ.get("OPENSHELF_BROWSER_TESTS"), reason="Set OPENSHELF_BROWSER_TESTS=1 with Chromium installed")
def test_advanced_search_combines_filters_and_survives_navigation(app, search_catalog):
    class Quiet(WSGIRequestHandler):
        def log(self, *_args, **_kwargs):
            pass

    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width":1440,"height":1100})
            errors = []
            page.on("pageerror",lambda error:errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}")
            expect(page.locator(".book-card")).to_have_count(8)
            page.locator("#search-scope").select_option("author")
            page.locator("#search").fill("dan brown")
            expect(page.locator(".book-card")).to_have_count(1)
            expect(page.locator(".book-title")).to_have_text("The Synthetic Code")
            page.get_by_role("button",name="Remove Author: dan brown",exact=True).click()
            expect(page.locator(".book-card")).to_have_count(8)
            page.locator("#advanced-toggle").click()
            page.locator("#advanced-author").fill("Dan Simmons")
            page.locator("#advanced-series-query").fill("Hyperion")
            page.get_by_role("button",name="Apply search",exact=True).click()
            expect(page.locator(".book-card")).to_have_count(2)
            expect(page.locator("#active-filters")).to_contain_text("Author: Dan Simmons")
            expect(page.locator("#active-filters")).to_contain_text("Series: Hyperion")
            page.reload()
            expect(page.locator("#advanced-author")).to_have_value("Dan Simmons")
            expect(page.locator("#advanced-series-query")).to_have_value("Hyperion")
            expect(page.locator(".book-card")).to_have_count(2)
            page.locator("#advanced-year-from").fill("1990")
            page.get_by_role("button",name="Apply search",exact=True).click()
            expect(page.locator(".book-card")).to_have_count(1)
            page.go_back()
            expect(page.locator(".book-card")).to_have_count(2)
            expect(page.locator("#advanced-year-from")).to_have_value("")
            page.locator("#advanced-toggle").click()
            expect(page.locator("#advanced-form")).to_be_hidden()
            page.get_by_role("button",name="Download results",exact=True).click()
            expect(page.locator("#queue-summary")).to_contain_text("Queue 2 books")
            page.locator("#queue-submit").click()
            expect(page).to_have_url(re.compile(r"#downloads$"))
            expect(page.locator(".download-row")).to_have_count(2)
            page.go_back()
            expect(page.locator(".book-card")).to_have_count(2)
            page.set_viewport_size({"width":390,"height":844})
            if not page.locator("#advanced-form").is_visible():
                page.locator("#advanced-toggle").click()
            expect(page.locator("#advanced-form")).to_be_visible()
            page.wait_for_timeout(250)
            assert not page.evaluate("() => document.documentElement.scrollWidth > innerWidth")
            page.locator("#advanced-year-from").fill("2000")
            page.locator("#advanced-year-to").fill("1990")
            page.get_by_role("button",name="Apply search",exact=True).click()
            expect(page.locator("#advanced-error")).to_contain_text("minimum cannot exceed")
            page.locator("#clear-advanced").click()
            expect(page.locator(".book-card")).to_have_count(8)
            expect(page.locator("#advanced-author")).to_have_value("")
            page.locator("#search").fill("dan brown")
            expect(page.locator(".book-card")).to_have_count(1)
            page.locator("#advanced-text-match").select_option("exact")
            page.get_by_role("button",name="Apply search",exact=True).click()
            expect(page).to_have_url(re.compile("text_match=exact"))
            page.reload()
            expect(page.locator("#advanced-form")).to_be_visible()
            expect(page.locator("#advanced-text-match")).to_have_value("exact")
            expect(page.locator("#active-filters")).to_contain_text("Matching: Exact field value")
            assert errors == []
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
