from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import os
from pathlib import Path
import threading
import time

from PIL import Image, ImageOps


class Covers:
    def __init__(self, database, network):
        self.database, self.network = database, network
        self.folder = database.config.data_dir / "covers"
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="openshelf-cover")
        self.lock, self.pending, self.retry_after = threading.Lock(), set(), {}
        self.closed = False

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=False, cancel_futures=True)

    def path(self, book_id):
        return self.folder / (book_id + ".webp")

    def schedule(self, book_id):
        with self.lock:
            if self.closed or book_id in self.pending or len(self.pending) >= 80 or self.retry_after.get(book_id, 0) > time.monotonic():
                return
            self.pending.add(book_id)
            self.pool.submit(self._fetch, book_id)

    def _fetch(self, book_id):
        try:
            with self.database.connection() as db:
                rows = db.execute("""SELECT c.cover_url,s.url FROM copies c JOIN sources s ON s.id=c.source_id
                                    WHERE c.book_id=? AND c.active=1 AND s.enabled=1 AND c.cover_url<>''
                                    ORDER BY s.status='error',c.last_seen DESC LIMIT 3""", (book_id,)).fetchall()
            for row in rows:
                try:
                    data, _ = self.network.read(row["cover_url"], base=row["url"], limit=8_000_000)
                    with Image.open(BytesIO(data)) as img:
                        if img.width * img.height > 25_000_000:
                            continue
                        img = ImageOps.exif_transpose(img).convert("RGB")
                        img.thumbnail((340, 510))
                        temporary = self.folder / (book_id + ".tmp")
                        img.save(temporary, "WEBP", quality=82)
                        os.replace(temporary, self.path(book_id))
                    return
                except Exception:
                    continue
        finally:
            with self.lock:
                self.pending.discard(book_id)
                if not self.path(book_id).exists():
                    self.retry_after[book_id] = time.monotonic() + 300
