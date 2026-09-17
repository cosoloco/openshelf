import argparse
import atexit
import logging
import os
import signal
import sys

from waitress import serve

from .app import create_app
from .config import Config


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
    import fcntl
    lock = (config.data_dir / ".app.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
