# Catalog protocols

Open Shelf reads remote catalog metadata over HTTP or HTTPS. It supports:

- **Calibre Content Server:** JSON library discovery, paginated book listings,
  and metadata for each library. Download links and cover locations are taken
  from catalog responses.
- **OPDS 1.x:** Atom navigation and acquisition feeds, including pagination.
  Direct acquisition and open-access acquisition links are supported.

The index keeps source locations alongside book metadata. Sources are checked
again when downloading; an indexed record is not a guarantee of availability.
Requests are serialized per server, and TLS certificates are verified.
Authenticated catalogs, OPDS 2.0 JSON feeds, and cross-origin acquisition links
are not supported in this release.

Protocol references:

- [Calibre Content Server manual](https://manual.calibre-ebook.com/server.html)
- [Calibre JSON endpoint definitions](https://github.com/kovidgoyal/calibre/blob/master/src/calibre/srv/ajax.py)
- [OPDS 1.2 specification](https://specs.opds.io/opds-1.2)

Open Shelf is distributed under the MIT license. Third-party dependencies retain
their own licenses; exact dependency versions are recorded in `uv.lock` and
`requirements.lock`.
