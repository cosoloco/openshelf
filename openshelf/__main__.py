import argparse
import atexit
import logging
import os
import signal
import sys

from waitress import serve

from .app import create_app
from .config import Config


def acquire_app_lock(path):
    """Hold an advisory one-byte lock for the lifetime of this process."""
    lock = path.open("a+b")
    if os.name == "nt":
        import msvcrt

        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            lock.close()
            raise BlockingIOError from error
    else:
        import fcntl

        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise
    return lock


def main():
    parser = argparse.ArgumentParser(description="Open Shelf remote catalog and download manager")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8099, type=int)
    parser.add_argument("--no-workers", action="store_true", help="Serve the interface without background jobs")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    config.prepare()
    # A single process owns the durable queues. Threads share per-origin limits.
    try:
        lock = acquire_app_lock(config.data_dir / ".app.lock")
    except BlockingIOError:
        parser.error("Another Open Shelf process is already using this data directory.")
    app = create_app(config, start_workers=not args.no_workers)
    workers = app.extensions["workers"]
    def close():
        workers.close()
        app.extensions["covers"].close()

    atexit.register(close)

    def shutdown(*_):
        close()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"Open Shelf: http://{args.host}:{args.port}\nCatalog: {config.data_dir}\nDownloads: {app.extensions['database'].settings()['download_dir']}", flush=True)
    serve(app, host=args.host, port=args.port, threads=8, max_request_body_size=2_100_000)


if __name__ == "__main__":
    main()
