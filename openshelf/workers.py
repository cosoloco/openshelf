import logging
import threading

from .downloads import run_download
from .indexer import run_scan
from .network import Network
from .db import timestamp

log = logging.getLogger(__name__)


class Workers:
    def __init__(self, database):
        self.database = database
        self.network = Network(database.config)
        self.stop = threading.Event()
        self.threads = []
        self.claim_lock = threading.Lock()
        self.active = {"scans": set(), "downloads": set()}

    def start(self):
        self.database.recover()
        for table, count, handler, pause in (
            ("scans", self.database.config.index_workers, run_scan, "indexing_paused"),
            ("downloads", self.database.config.download_workers, run_download, "downloads_paused"),
        ):
            for i in range(count):
                thread = threading.Thread(target=self._loop, args=(table, handler, pause), name=f"openshelf-{table}-{i}", daemon=True)
                thread.start()
                self.threads.append(thread)

    def _loop(self, table, handler, pause):
        while not self.stop.is_set():
            job = None
            try:
                if self.database.settings()[pause]:
                    self.stop.wait(0.5)
                    continue
                with self.claim_lock:
                    job = self.database.claim(table, self.active[table])
                    if job:
                        self.active[table].add(job["id"])
                if job:
                    try:
                        handler(self.database, self.network, job, self.stop)
                    finally:
                        with self.claim_lock:
                            self.active[table].discard(job["id"])
                else:
                    self.stop.wait(0.5)
            except Exception as exc:
                log.exception("Worker failed; the queue will continue")
                if job:
                    try:
                        with self.database.connection() as db:
                            db.execute(f"UPDATE {table} SET status='failed',error=?,updated_at=?,finished_at=? WHERE id=? AND status='running'",
                                       (str(exc)[:400], timestamp(), timestamp(), job["id"]))
                            if table == "scans":
                                db.execute("UPDATE sources SET status='error',error=? WHERE id=? AND status='indexing'",
                                           (str(exc)[:400], job["source_id"]))
                    except Exception:
                        log.exception("Could not record worker failure")
                self.stop.wait(2)

    def close(self):
        self.stop.set()
