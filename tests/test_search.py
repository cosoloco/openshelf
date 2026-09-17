import pytest


def ids(client, **filters):
    response = client.get("/api/books", query_string=filters)
    assert response.status_code == 200, response.json
    return {b["id"] for b in response.json["books"]}


def test_author_ignores_mentions_prefixes_and_separate_coauthors(client, search_catalog):
    assert ids(client, author="dan brown") == {search_catalog["brown"]}
    assert ids(client, q="dan brown", q_field="author") == {search_catalog["brown"]}
    assert search_catalog["mention"] in ids(client, q="dan brown")
    assert ids(client, author="brown dan") == {search_catalog["brown"]}


def test_author_and_series_intersect_and_queue_the_same_books(client, search_catalog):
    filters = {"author":"dan simmons", "series_query":"hyperion", "language":"eng", "format":"EPUB"}
    wanted = {search_catalog["hyperion"], search_catalog["fall"]}
    assert ids(client, **filters) == wanted
    assert ids(client, **filters, title="fall") == {search_catalog["fall"]}
    assert client.post("/api/downloads", json={"filters":filters,"format":"EPUB"}).json == {"queued":2,"skipped":0}
    assert {r["book_id"] for r in client.get("/api/downloads").json["downloads"]} == wanted
    assert client.post("/api/downloads", json={"filters":filters}).json == {"queued":0,"skipped":2}


def test_metadata_fields_ranges_and_series_browse_remain_distinct(client, search_catalog):
    assert ids(client, publisher="orbit", year_from=1989, year_to=1990, series_from=2, series_to=2, subject="science fiction", description="travellers return", library="main") == {search_catalog["fall"]}
    assert ids(client, series="Hyperion") == set()
    assert ids(client, series="Hyperion Cantos") == {search_catalog["hyperion"],search_catalog["fall"]}
    assert ids(client, series_from=0, series_to=0.5) == {search_catalog["accent"]}
    assert search_catalog["coauthors"] not in ids(client, subject="science fiction")


def test_exact_modes_accents_and_identifiers(client, search_catalog):
    assert ids(client, author="jose alvarez", publisher="editions du port", text_match="phrase") == {search_catalog["accent"]}
    assert ids(client, title="hyperion", text_match="exact") == {search_catalog["hyperion"]}
    assert ids(client, title="fall hyperion", text_match="phrase") == set()
    assert ids(client, title="fall of hyperion", text_match="phrase") == {search_catalog["fall"]}
    assert ids(client, identifier="9780123456789", identifier_type="ISBN") == {search_catalog["brown"]}
    assert ids(client, identifier="978-0-123456-78-9", text_match="exact") == {search_catalog["brown"]}
    assert ids(client, identifier="B0TESTBOOK", identifier_type="asin") == {search_catalog["fall"]}
    assert ids(client, identifier="9780123456789", identifier_type="asin") == set()


def test_quoted_keywords_and_literal_input(client, search_catalog):
    assert ids(client, q='"fall of hyperion"') == {search_catalog["fall"]}
    assert ids(client, q='"fall hyperion"') == set()
    assert ids(client, title="100%_life", text_match="phrase") == {search_catalog["accent"]}
    assert ids(client, author="%_") == set()
    assert ids(client, author='brown" OR 1=1 --') == set()
    assert ids(client, author="<script>alert(1)</script>") == set()


@pytest.mark.parametrize("filters", [
    {"year_from":1990,"year_to":1980}, {"series_from":2,"series_to":1},
    {"year_from":"2020.5"}, {"year_to":"NaN"}, {"series_from":"Infinity"},
    {"text_match":"sql"}, {"q_field":"unknown"},
])
def test_invalid_advanced_filters_return_readable_errors(client, filters):
    response = client.get("/api/books", query_string=filters)
    assert response.status_code == 400
    assert response.json["error"]
