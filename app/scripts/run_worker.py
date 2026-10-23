"""Run independently of the API: python -m app.scripts.run_worker [--once]."""

import argparse
import signal
import threading

from app.config.settings import Settings
from app.services.runs import RunWorker
from app.web.services import build_api_services


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--owner", default=None)
    args = parser.parse_args()
    config = Settings()
    if config.persistence_backend != "sql":
        parser.error("Worker requires PERSISTENCE_BACKEND=sql")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    worker = RunWorker(build_api_services(config), owner=args.owner)
    while not stop.is_set():
        worked = worker.run_once()
        if args.once:
            return
        if not worked:
            stop.wait(config.run_poll_seconds)


if __name__ == "__main__":
    main()
