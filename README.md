# Open Shelf

A personal search catalog for remote Calibre and OPDS libraries. Import the
servers you use, index their metadata, browse books and series, and download
selected files to a folder for Calibre or your preferred reader.

Open Shelf starts empty. No servers, catalog snapshots, covers, or ebooks are
bundled. A local ebook collection is not required.

## Run with Docker

First get a copy of this project. Choose one:

**With Git**

```sh
git clone https://github.com/cosoloco/openshelf.git
cd openshelf
```

**Without Git**

On the GitHub repository page, choose **Code → Download ZIP**, extract the ZIP,
and open a terminal in the extracted `openshelf` folder. It must contain
`pyproject.toml` and `compose.yaml`.

Install Docker with Compose (Docker Desktop includes both), then run Open Shelf.
On macOS or Linux:

```sh
./start.sh
```

`start.sh` is a macOS/Linux launcher. On Windows, open PowerShell in the
extracted or cloned project folder and run:

```powershell
mkdir data
mkdir downloads
docker compose up --build -d
```

Docker Desktop includes Compose; confirm it with `docker compose version`.

The launcher creates the folders and runs the container with your user and group
IDs so downloaded files belong to you. Open <http://localhost:8099>.
The default Compose configuration publishes only to this computer.

**Downloaded books are saved in the `downloads` folder inside the Open Shelf
project folder on the computer running Docker.** For example, cloning into
`~/openshelf` puts completed books in `~/openshelf/downloads`.

| Contents | Folder on the Docker host, relative to `compose.yaml` | Path inside the container |
| --- | --- | --- |
| Catalog, settings, and download history | `./data` | `/data` |
| Completed book files | `./downloads` | `/downloads` |

These are bind mounts: the files are stored on your computer and remain there
when the container stops or is replaced. The `/downloads` path shown in the
app refers to the host folder above.

To use a different host folder, set `OPENSHELF_DOWNLOADS` before starting
Compose, or edit the bind mount in `compose.yaml`.

```sh
OPENSHELF_DOWNLOADS=/path/to/books ./start.sh
```

If port 8099 is already in use, choose another with
`OPENSHELF_PORT=8100 ./start.sh`, then open `http://localhost:8100`.
Stop the container with `docker compose stop`; its catalog and downloads remain
in their folders.

In Settings, `/downloads` is the container's view of that host folder. Keep it
set to `/downloads` unless you deliberately mount another path.

To update with Git, stop Open Shelf, run `git pull` in the project folder, then
start it again. To update a ZIP installation, download and extract the new ZIP
into a new folder. For Docker, stop the old container and copy its `data` and
`downloads` folders into the new project folder before starting the new copy.
Keep the old folder until you have confirmed the updated app opens normally.

## Run directly

First get a copy of this project. With Git:

```sh
git clone https://github.com/cosoloco/openshelf.git
cd openshelf
```

Without Git, choose **Code → Download ZIP** on the GitHub repository page and
extract it. Open a terminal in the extracted `openshelf` folder; it must contain
`pyproject.toml`. You do not run these commands from the `uv` installation
folder or Python installation folder.

Install Python 3.12 or newer and [uv](https://docs.astral.sh/uv/), then run:

```sh
uv sync --locked
uv run openshelf --port 8099
```

Run both commands in the project folder containing `pyproject.toml`. On Windows,
open PowerShell in the extracted or cloned project folder and run the commands
above.

When running directly with Python, downloads default to `~/Downloads/OpenShelf`.
Metadata, including the catalog, server list, saved books, settings, and
download history, is stored in `~/Library/Application Support/OpenShelf` on
macOS and `~/.local/share/OpenShelf` on Linux and Windows. These paths apply to
the native Python installation; Docker uses the project folders described above.
Override `OPENSHELF_DATA_DIR` and `OPENSHELF_DOWNLOAD_DIR` to choose different
locations for a native installation.

To back up a native installation, stop Open Shelf and copy both the metadata
folder above (which contains `openshelf.sqlite`) and your configured download
folder. For Docker, back up the project’s `data` and `downloads` folders.

To update with Git, stop Open Shelf, run `git pull`, then run `uv sync --locked`
and start it again. To update a ZIP installation, download and extract the new
ZIP into a new folder, then run `uv sync --locked` there. Native catalog and
download folders are outside the project folder by default, so the new copy
will use the same library state.

## Using the app

1. Open **Servers → Import servers**. Paste one address, a list, a CSV, or a
   pasted table containing HTTP/HTTPS addresses. You can also choose a text file.
2. Import with indexing enabled. Books become searchable as pages arrive.
3. Search the catalog and filter by language, subject, format, or server.
   Series pages show members known to your index, not a complete bibliography.
4. Download a book, selected books, or all results matching your current filters.
   EPUB is the default preference; other available formats can be selected.
5. Follow progress under **Downloads**. When a job shows **Complete**, its file
   is in the configured folder on the computer running Open Shelf
   (`openshelf/downloads/` with the default Docker setup). Import it into Calibre
   from that folder.
6. To save a copy through your browser, open the completed book and click
   **Save a browser copy**, or use the download arrow on its completed row in
   **Downloads**. Your browser chooses where to save this copy on the computer
   you are browsing from. Queuing a book in Open Shelf does not automatically
   save it to your browser's usual Downloads folder.

**Save for later** adds a book to your shortlist without downloading it.

Indexing and download queues persist across restarts. Pausing retains completed
work; failed server scans can resume from their last committed page. A completed
refresh marks missing remote records unavailable. Failed or interrupted scans
retain the previous catalog. Availability and metadata reflect the most recent
successful observation and are not a guarantee that a source is still online.

## Scope

- Calibre Content Server's JSON catalog, including multiple libraries.
- OPDS 1.x Atom feeds, including navigation and pagination, as a fallback.
- Metadata grouping by normalized full title, authors, and language. Ambiguous
  author records stay separate. Editions can still be duplicated or grouped.
- Configurable download folder, preferred format, file-size limit, and disk reserve.
- One request/transfer at a time per origin, three index workers and two download
  workers. File validation and atomic publication keep incomplete downloads out
  of the destination folder. HTTP range resume is used when a source supplies
  a suitable validator and supports it; otherwise the current file restarts.
- Local-first single-user operation. Authenticated sources and multi-user public
  hosting are not supported in this first version. TLS verification stays enabled.
- Cross-origin acquisition and cover links are not followed automatically; import
  the other server explicitly. There is no DRM removal or automatic Internet-wide discovery.

See [catalog protocols](docs/PROTOCOLS.md) for interoperability details.

Use sources you are authorized to access and download from. The software's MIT
license covers the application, not books supplied by independent servers.

## Development

```sh
uv sync --group dev
uv run pytest
node --check openshelf/static/app.js
```

For the browser workflow test (single and bulk import, search, series, saved
books, downloads, settings, and mobile navigation):

```sh
uv run playwright install chromium
OPENSHELF_BROWSER_TESTS=1 uv run pytest
```

Tests create synthetic catalogs and ebook files on loopback servers; they do not
contact a real library. Runtime data and downloads are ignored by Git and excluded
from the Docker build context. Back up the data directory to retain your catalog,
saved books, server list, settings, and download history.

## Refined search

The search box has a **Keywords / Author / Title / Series** selector. Keywords
searches titles, authors, series, subjects, and descriptions; an author name can
therefore appear in another book's blurb. Choose **Author** to search author
names only. Author searches match whole words in a single author's name and
accept either `Dan Simmons` or `Simmons, Dan` in the default **All words** mode.

Open **Advanced search** to combine author, title, series, subject/genre,
publisher, description, ISBN/identifier and its type, publication-year range,
series-number range, library, and saved/downloaded status. Every supplied
condition must match, including the language, format, and server filters above.
For example, **Author = Dan Simmons** and **Series = Hyperion** includes series
names such as *Hyperion Cantos*. An exact series selected from the Series page
remains an exact series filter.

Field matching supports all words, exact phrase, and exact whole-field value.
It ignores capitalization, accents, and punctuation. ISBN/identifier matching
ignores punctuation and allows partial values unless exact-field matching is
selected. Quoted phrases also work in the Keywords search box.

Applied filters appear as removable chips and persist in the page URL, including
browser back/forward and reload. **Download results** uses those same filters.
Search operates on indexed metadata; fields missing from a source cannot be
inferred.
