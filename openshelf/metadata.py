import hashlib
from html.parser import HTMLParser
import json
import re
import unicodedata

from .db import timestamp


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("br", "p", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value, limit=20000):
    parser = PlainText()
    parser.feed(str(value or "")[:limit * 2])
    return re.sub(r"\n\s*\n+", "\n\n", "".join(parser.parts)).strip()[:limit]


def normalized(value):
    value = unicodedata.normalize("NFKD", str(value)).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", value, flags=re.UNICODE))


LANGUAGES = {"en": "eng", "english": "eng", "fr": "fra", "fre": "fra", "french": "fra", "de": "deu", "ger": "deu", "german": "deu", "es": "spa", "spanish": "spa", "it": "ita", "pt": "por", "nl": "nld", "dut": "nld", "ja": "jpn", "zh": "zho", "chi": "zho", "ru": "rus"}


def language(value):
    if isinstance(value, list):
        value = value[0] if value else ""
    value = str(value or "und").lower().replace("_", "-").split("-")[0]
    return LANGUAGES.get(value, value)[:12]


def identity(record, source_id):
    title = normalized(record["title"])
    authors = sorted(normalized(a) for a in record["authors"] if normalized(a))
    if not authors or all(a in ("unknown", "unknown author", "various", "anonymous") for a in authors):
        key = [title, record["language"], str(source_id), record["library"], record["remote_id"]]
    else:
        key = [title, authors, record["language"]]
    return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:32]


def clean_record(record):
    record = dict(record)
    record["title"] = plain(record.get("title"), 1000) or "Untitled"
    authors = record.get("authors") or []
    if isinstance(authors, str):
        authors = [authors]
    record["authors"] = [plain(a, 500) for a in authors[:30] if a]
    record["language"] = language(record.get("language"))
    record["description"] = plain(record.get("description"))
    record["publisher"] = plain(record.get("publisher"), 500)
    tags = record.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    record["tags"] = sorted({plain(t, 120) for t in tags[:100] if t}, key=str.casefold)
    record["series"] = plain(record.get("series"), 500)
    try:
        position = float(record.get("series_position"))
        record["series_position"] = position if abs(position) < 1e7 else None
    except (ValueError, TypeError):
        record["series_position"] = None
    date = str(record.get("year") or "")
    match = re.match(r"(\d{4})", date)
    record["year"] = int(match[1]) if match and 1 < int(match[1]) < 3000 else None
    record["identifiers"] = record.get("identifiers") if isinstance(record.get("identifiers"), dict) else {}
    record["remote_id"] = str(record["remote_id"])[:1000]
    return record


def upsert_record(db, source_id, scan_id, raw):
    record = clean_record(raw)
    book_id, now = identity(record, source_id), timestamp()
    previous = db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
    if previous:
        tags = sorted(set(json.loads(previous["tags_json"])) | set(record["tags"]), key=str.casefold)
        description = max((previous["description"], record["description"]), key=len)
        series = previous["series"] or record["series"]
        position = previous["series_position"] if previous["series"] else record["series_position"]
        db.execute("""UPDATE books SET description=?,tags=?,tags_json=?,series=?,series_position=?,
                      year=COALESCE(year,?),publisher=CASE WHEN publisher='' THEN ? ELSE publisher END,updated_at=? WHERE id=?""",
                   (description, ", ".join(tags), json.dumps(tags), series, position, record["year"], record["publisher"], now, book_id))
    else:
        tags = record["tags"]
        db.execute("""INSERT INTO books(id,title,authors,authors_json,description,language,year,publisher,tags,tags_json,
                      series,series_position,identifiers_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (book_id, record["title"], " & ".join(record["authors"]) or "Unknown author", json.dumps(record["authors"]),
                    record["description"], record["language"], record["year"], record["publisher"], ", ".join(tags), json.dumps(tags),
                    record["series"], record["series_position"], json.dumps(record["identifiers"]), now, now))
    db.executemany("INSERT OR IGNORE INTO book_tags VALUES(?,?)", [(book_id, t) for t in tags])
    copy_id = db.execute("""INSERT INTO copies(book_id,source_id,library,remote_id,title,cover_url,metadata,observed_scan,last_seen)
                         VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id,library,remote_id) DO UPDATE SET
                         book_id=excluded.book_id,title=excluded.title,cover_url=excluded.cover_url,metadata=excluded.metadata,
                         observed_scan=excluded.observed_scan,last_seen=excluded.last_seen,active=1 RETURNING id""",
                        (book_id, source_id, record["library"], record["remote_id"], record["title"], record.get("cover_url", ""),
                         json.dumps(record, ensure_ascii=False), scan_id, now)).fetchone()[0]
    db.execute("DELETE FROM files WHERE copy_id=?", (copy_id,))
    db.executemany("INSERT OR IGNORE INTO files(copy_id,format,url,size) VALUES(?,?,?,?)",
                   [(copy_id, f["format"], f["url"], f.get("size")) for f in record.get("files", [])])
    return book_id
