"""Literal metadata matching shared by the query builder and SQLite functions."""
from functools import lru_cache
import json
import re
import unicodedata


def words(value):
    text = unicodedata.normalize("NFKD", str(value or "")).lower()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return tuple(re.findall(r"[^\W_]+", text, re.UNICODE))


@lru_cache(maxsize=512)
def query_words(value):
    return words(value)


def metadata_matches(value, query, mode="words"):
    wanted = query_words(query)
    if not wanted:
        return False
    found = words(value)
    if mode == "exact":
        return found == wanted
    if mode == "phrase":
        return any(found[i:i + len(wanted)] == wanted for i in range(len(found) - len(wanted) + 1))
    return set(wanted).issubset(found)


def identifier_matches(value, query, kind, mode):
    identifiers = json.loads(value or "{}")
    wanted = "".join(query_words(query))
    kind = kind.lower().strip()
    for key, identifier in identifiers.items():
        if kind and key.lower() != kind:
            continue
        candidate = "".join(words(identifier))
        if not query and kind:
            return True
        if wanted and (candidate == wanted if mode == "exact" else wanted in candidate):
            return True
    return False


def fts_terms(value, mode="words", *, prefix=False):
    tokens = words(value)
    if not tokens:
        return None
    if mode in ("phrase", "exact"):
        return '"' + " ".join(tokens) + '"'
    return " AND ".join('"' + word + '"' + ("*" if prefix else "") for word in tokens[:40])


def keyword_terms(value):
    # Quoted text is a phrase; ordinary keywords retain the existing prefix search.
    chunks = re.findall(r'"([^"\n]+)"|([^"\s]+)', value)
    terms = [fts_terms(phrase, "phrase") if phrase else fts_terms(word, prefix=True)
             for phrase, word in chunks[:40]]
    return " AND ".join(f"({term})" for term in terms if term) or None
