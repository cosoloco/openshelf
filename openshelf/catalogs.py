"""Independent clients for Calibre's HTTP catalog and standard OPDS 1.x feeds."""
import hashlib
import mimetypes
from urllib.parse import quote, urljoin, urlsplit

from defusedxml import ElementTree

from .network import RemoteError, source_link


MIME_FORMATS = {
    "application/epub+zip": "EPUB", "application/pdf": "PDF",
    "application/x-mobipocket-ebook": "MOBI", "application/vnd.amazon.ebook": "AZW3",
    "application/x-fictionbook+xml": "FB2", "text/plain": "TXT",
    "image/vnd.djvu": "DJVU", "application/vnd.comicbook+zip": "CBZ",
}
FORMATS = ("EPUB", "AZW3", "MOBI", "PDF", "FB2", "TXT", "DJVU", "CBZ", "DOCX", "RTF", "ODT", "HTMLZ")
NS = {"a": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/terms/", "dce": "http://purl.org/dc/elements/1.1/"}


def valid_link(base, value):
    try:
        return source_link(base, value)
    except ValueError:
        return ""


def calibre_record(base, library, remote_id, item):
    files = []
    links = {}
    for name in ("main_format", "other_formats"):
        if isinstance(item.get(name), dict):
            links.update(item[name])
    format_metadata = item.get("format_metadata") or {}
    for fmt, link in links.items():
        fmt = str(fmt).upper()
        url = valid_link(base, link)
        size = (format_metadata.get(fmt.lower()) or format_metadata.get(fmt) or {}).get("size")
        if url and fmt in FORMATS:
            files.append({"format": fmt, "url": url, "size": size if isinstance(size, int) and size >= 0 else None})
    return {
        "remote_id": str(remote_id), "library": library, "title": item.get("title"),
        "authors": item.get("authors"), "language": item.get("languages") or item.get("language"),
        "description": item.get("comments"), "tags": item.get("tags"),
        "year": item.get("pubdate"), "publisher": item.get("publisher"),
        "series": item.get("series"), "series_position": item.get("series_index"),
        "identifiers": item.get("identifiers"), "files": files,
        "cover_url": valid_link(base, item.get("thumbnail") or item.get("cover")),
    }


def read_feed(network, url, base):
    body, effective = network.read(url, base=base)
    try:
        root = ElementTree.fromstring(body)
    except Exception as exc:
        raise RemoteError("This address did not return a supported OPDS feed.") from exc
    if root.tag not in ("{http://www.w3.org/2005/Atom}feed", "{http://www.w3.org/2005/Atom}entry"):
        raise RemoteError("This address did not return an OPDS catalog.")
    return root, effective


def opds_record(entry, effective, base):
    files, cover = [], ""
    for link in entry.findall("a:link", NS):
        rel = link.get("rel", "")
        href = valid_link(base, urljoin(effective, link.get("href", "")))
        if not href:
            continue
        if rel in ("http://opds-spec.org/image/thumbnail", "http://opds-spec.org/image"):
            cover = cover or href
        if rel not in ("http://opds-spec.org/acquisition", "http://opds-spec.org/acquisition/open-access"):
            continue
        mime = link.get("type", "").split(";")[0].lower()
        fmt = MIME_FORMATS.get(mime, "")
        if not fmt:
            extension = urlsplit(href).path.rsplit(".", 1)[-1].upper()
            fmt = extension if extension in FORMATS else ""
        if not fmt:
            for candidate in FORMATS:
                if f"/get/{candidate.lower()}/" in href.lower():
                    fmt = candidate
                    break
        if fmt:
            length = link.get("length", "")
            files.append({"format": fmt, "url": href, "size": int(length) if length.isdigit() else None})
    if not files:
        return None
    description = entry.find("a:content", NS)
    if description is None:
        description = entry.find("a:summary", NS)
    text = lambda path: entry.findtext(path, default="", namespaces=NS)
    series, position = "", None
    for el in entry.iter():
        if el.tag.endswith("}series"):
            series, position = el.get("name") or el.text or "", el.get("index")
    return {
        "remote_id": text("a:id") or hashlib.sha256(files[0]["url"].encode()).hexdigest(),
        "library": "opds", "title": text("a:title"),
        "authors": [a.findtext("a:name", "", NS) for a in entry.findall("a:author", NS)],
        "language": text("dc:language") or text("dce:language"),
        "description": "" if description is None else "".join(description.itertext()),
        "tags": [c.get("label") or c.get("term") for c in entry.findall("a:category", NS)],
        "year": text("dc:issued") or text("a:published"), "publisher": text("dc:publisher"),
        "series": series, "series_position": position, "identifiers": {}, "files": files, "cover_url": cover,
    }


class CatalogClient:
    def __init__(self, network, page_size=150):
        self.network, self.page_size = network, page_size

    def discover(self, source):
        base = source["url"]
        if "/opds" not in urlsplit(base).path.lower():
            try:
                info, effective = self.network.json(base + "/ajax/library-info", base=base)
                mapping = info.get("library_map")
                if isinstance(mapping, dict) and mapping:
                    return {"kind": "calibre", "base": base, "libraries": list(mapping)[:1000], "library": 0, "offset": 0, "processed": 0, "totals": {}}
            except RemoteError:
                pass
            try:
                sample, _ = self.network.json(base + "/ajax/search", base=base, params={"num": 0})
                if "book_ids" in sample or "total_num" in sample:
                    return {"kind": "calibre", "base": base, "libraries": [""], "library": 0, "offset": 0, "processed": 0, "totals": {}}
            except RemoteError as exc:
                if "authentication" in str(exc) or "certificate" in str(exc) or "Cannot reach" in str(exc):
                    raise
        candidates = [base] if "/opds" in urlsplit(base).path.lower() else [base + "/opds", base]
        error = None
        for url in candidates:
            try:
                read_feed(self.network, url, base)
                return {"kind": "opds", "base": base, "pending": [url], "visited": [], "processed": 0}
            except RemoteError as exc:
                error = exc
        raise error or RemoteError("No supported catalog was found at this address.")

    def advance(self, state):
        return self._calibre(state) if state["kind"] == "calibre" else self._opds(state)

    def _calibre(self, state):
        state = dict(state)
        base = state["base"]
        library = state["libraries"][state["library"]]
        suffix = "/" + quote(str(library), safe="") if library else ""
        result, _ = self.network.json(base + "/ajax/search" + suffix, base=base,
            params={"num": self.page_size, "offset": state["offset"], "sort": "title", "sort_order": "asc"})
        ids = result.get("book_ids")
        if not isinstance(ids, list):
            raise RemoteError("Calibre returned an unsupported book listing.")
        total = result.get("total_num")
        if not isinstance(total, int) or total < 0:
            total = state["offset"] + len(ids)
        state["totals"][str(library)] = total
        fingerprint = hashlib.sha256(str(ids).encode()).hexdigest()
        if ids and state["offset"] and fingerprint == state.get("last_page"):
            raise RemoteError("Server repeated a page. Progress is saved; retry this scan later.")
        records = []
        if ids:
            items, _ = self.network.json(base + "/ajax/books" + suffix, base=base,
                params={"ids": ",".join(str(i) for i in ids), "category_urls": "false"})
            missing = [i for i in ids if str(i) not in items]
            if missing:
                raise RemoteError("Server returned an incomplete metadata page. Progress is saved.")
            records = [calibre_record(base, str(library), i, items[str(i)]) for i in ids if isinstance(items.get(str(i)), dict)]
        state["processed"] += len(records)
        state["offset"] += len(ids)
        state["last_page"] = fingerprint
        if not ids and state["offset"] < total:
            raise RemoteError("Server returned an empty page before its advertised end.")
        if state["offset"] >= total:
            state["library"] += 1
            state["offset"] = 0
            state.pop("last_page", None)
        done = state["library"] >= len(state["libraries"])
        total_known = sum(state["totals"].values()) if len(state["totals"]) == len(state["libraries"]) else None
        return records, state, done, total_known, f"Library {min(state['library'] + 1, len(state['libraries']))} of {len(state['libraries'])}"

    def _opds(self, state):
        state = {**state, "pending": list(state["pending"]), "visited": list(state["visited"])}
        url = state["pending"].pop(0)
        feed, effective = read_feed(self.network, url, state["base"])
        entries = feed.findall("a:entry", NS) if feed.tag.endswith("}feed") else [feed]
        records = [r for e in entries if (r := opds_record(e, effective, state["base"]))]
        state["visited"].append(url)
        known = set(state["visited"]) | set(state["pending"])
        links = [e for e in feed.findall("a:link", NS) if e.get("rel") == "next"]
        if not records:
            navigation = []
            for entry in entries:
                title = entry.findtext("a:title", "", NS).casefold()
                for link in entry.findall("a:link", NS):
                    if "application/atom+xml" in link.get("type", "") and link.get("rel", "subsection") not in ("search", "self", "start", "up"):
                        priority = 0 if "all books" in title or link.get("rel", "").endswith("/sort/title") else 1
                        navigation.append((priority, link))
            all_books = [link for priority, link in navigation if priority == 0]
            links.extend(all_books or [link for _, link in navigation])
        for link in links:
            target = valid_link(state["base"], urljoin(effective, link.get("href", "")))
            if target and target not in known:
                known.add(target)
                state["pending"].append(target)
        if len(known) > 20000:
            raise RemoteError("OPDS crawl reached the 20,000-page limit; narrow the imported feed.")
        state["processed"] += len(records)
        return records, state, not state["pending"], None, f"OPDS · {len(state['visited']):,} pages"
