import json
import math
import re

from .metadata import language
from .search import fts_terms, keyword_terms


def book_condition(filters):
    clauses, args = [], []
    mode = filters.get("text_match") or "words"
    if mode not in ("words", "phrase", "exact"):
        raise ValueError("Choose all words, an exact phrase, or an exact field value.")

    def scoped_text(field, value):
        value = str(value).strip()[:500]
        if not value:
            return
        columns = {"title": "title", "author": "authors", "series_query": "series", "subject": "tags", "description": "description"}
        terms = fts_terms(value, mode)
        if not terms:
            clauses.append("0")
            return
        if field in columns:
            clauses.append("b.rowid IN (SELECT rowid FROM book_search WHERE book_search MATCH ?)")
            args.append(f"{columns[field]} : ({terms})")
        if field == "author":
            # All words must belong to the same author, not separate coauthors.
            clauses.append("EXISTS(SELECT 1 FROM json_each(b.authors_json) a WHERE metadata_matches(a.value,?,?))")
        elif field == "subject":
            clauses.append("EXISTS(SELECT 1 FROM book_tags t WHERE t.book_id=b.id AND metadata_matches(t.tag,?,?))")
        else:
            column = "series" if field == "series_query" else field
            clauses.append(f"metadata_matches(b.{column},?,?)")
        args.extend((value, mode))

    scope = filters.get("q_field") or "all"
    if scope not in ("all", "author", "title", "series_query"):
        raise ValueError("Choose a supported search field.")
    query = str(filters.get("q", "")).strip()[:500]
    if query and scope != "all":
        scoped_text(scope, query)
    elif query:
        terms = keyword_terms(query)
        clauses.append("b.rowid IN (SELECT rowid FROM book_search WHERE book_search MATCH ?)" if terms else "0")
        if terms:
            args.append(terms)
    for field in ("title", "author", "series_query", "publisher", "subject", "description"):
        if filters.get(field):
            scoped_text(field, filters[field])
    identifier, kind = str(filters.get("identifier", "")).strip()[:500], str(filters.get("identifier_type", "")).strip()[:80]
    if identifier or kind:
        clauses.append("identifier_matches(b.identifiers_json,?,?,?)")
        args.extend((identifier, kind, mode))

    for column, lower, upper, label, integer in (
        ("year", "year_from", "year_to", "Publication year", True),
        ("series_position", "series_from", "series_to", "Series number", False),
    ):
        bounds = []
        for key, operator in ((lower, ">="), (upper, "<=")):
            value = filters.get(key)
            if value is None or value == "":
                bounds.append(None)
                continue
            try:
                number = float(value)
                if not math.isfinite(number) or (integer and (number != int(number) or not 1 <= number <= 2999)) or (not integer and abs(number) >= 1e7):
                    raise ValueError()
            except (ValueError, TypeError, OverflowError):
                raise ValueError(f"{label} must be {'a whole year from 1 to 2999' if integer else 'a valid number'}.")
            bounds.append(number)
            clauses.append(f"b.{column}{operator}?")
            args.append(int(number) if integer else number)
        if all(v is not None for v in bounds) and bounds[0] > bounds[1]:
            raise ValueError(f"{label}: the minimum cannot exceed the maximum.")
    if filters.get("language"):
        clauses.append("b.language=?")
        args.append(language(filters["language"]))
    if filters.get("tag"):
        clauses.append("EXISTS(SELECT 1 FROM book_tags t WHERE t.book_id=b.id AND t.tag=? COLLATE NOCASE)")
        args.append(str(filters["tag"])[:120])
    if filters.get("series"):
        clauses.append("b.series=? COLLATE NOCASE")
        args.append(str(filters["series"])[:500])
    if filters.get("saved") in (True, "1", "true"):
        clauses.append("EXISTS(SELECT 1 FROM saved v WHERE v.book_id=b.id)")
    if filters.get("downloaded") in (True, "1", "true"):
        clauses.append("EXISTS(SELECT 1 FROM downloads d WHERE d.book_id=b.id AND d.status='complete')")
    copy_clauses = ["c.book_id=b.id", "c.active=1", "s.enabled=1"]
    if filters.get("source"):
        copy_clauses.append("s.id=?")
        try:
            args.append(int(filters["source"]))
        except (ValueError, TypeError):
            raise ValueError("Invalid server filter.")
    if filters.get("library"):
        copy_clauses.append("metadata_matches(c.library,?,?)")
        args.extend((str(filters["library"]).strip()[:500], mode))
    if filters.get("format"):
        copy_clauses.append("EXISTS(SELECT 1 FROM files f WHERE f.copy_id=c.id AND f.format=?)")
        args.append(str(filters["format"]).upper()[:12])
    if filters.get("availability") != "all" or filters.get("source") or filters.get("format") or filters.get("library"):
        clauses.append("EXISTS(SELECT 1 FROM copies c JOIN sources s ON s.id=c.source_id WHERE " + " AND ".join(copy_clauses) + ")")
    return " AND ".join(clauses) if clauses else "1", args


def book_json(row):
    result = dict(row)
    for key in ("authors_json", "tags_json", "identifiers_json"):
        if key in result:
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
    if "formats" in result:
        result["formats"] = sorted(set(result["formats"].split(","))) if result["formats"] else []
    result["saved"] = bool(result.get("saved"))
    result["downloaded"] = bool(result.get("downloaded"))
    return result


BOOK_FIELDS = """b.id,b.title,b.authors,b.authors_json,b.language,b.year,b.publisher,b.tags_json,b.series,b.series_position,b.created_at,
 (SELECT GROUP_CONCAT(DISTINCT f.format) FROM copies c JOIN files f ON f.copy_id=c.id JOIN sources s ON s.id=c.source_id
  WHERE c.book_id=b.id AND c.active=1 AND s.enabled=1) AS formats,
 (SELECT COUNT(DISTINCT c.source_id) FROM copies c JOIN sources s ON s.id=c.source_id WHERE c.book_id=b.id AND c.active=1 AND s.enabled=1) AS source_count,
 EXISTS(SELECT 1 FROM saved v WHERE v.book_id=b.id) AS saved,
 EXISTS(SELECT 1 FROM downloads d WHERE d.book_id=b.id AND d.status='complete') AS downloaded"""


def search_books(db, filters, page=1, per_page=48):
    condition, args = book_condition(filters)
    total = db.execute("SELECT COUNT(*) FROM books b WHERE " + condition, args).fetchone()[0]
    order = {"title": "b.title COLLATE NOCASE,b.id", "author": "b.authors COLLATE NOCASE,b.title COLLATE NOCASE",
             "newest": "b.created_at DESC,b.rowid DESC", "year": "b.year DESC,b.title COLLATE NOCASE",
             "series": "b.series_position IS NULL,b.series_position,b.title COLLATE NOCASE"}.get(filters.get("sort"), "b.title COLLATE NOCASE,b.id")
    rows = db.execute(f"SELECT {BOOK_FIELDS} FROM books b WHERE {condition} ORDER BY {order} LIMIT ? OFFSET ?", [*args, per_page, (page - 1) * per_page])
    return {"books": [book_json(r) for r in rows], "total": total, "page": page, "per_page": per_page, "pages": max(1, (total + per_page - 1) // per_page)}
