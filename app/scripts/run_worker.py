"""Run independently of the API: python -m app.scripts.run_worker [--once]."""

import argparse
import signal
import threading

from app.config.settings import Settings
from app.services.runs import RunWorker
from app.web.services import build_api_services
from sqlalchemy.exc import OperationalError


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
    from app.platform.knowledge import KnowledgeWorker

    knowledge = (
        KnowledgeWorker(config, stopping=stop.is_set)
        if config.knowledge_versioned_enabled
        else None
    )
    from app.platform.connector_delivery import InventoryWorker

    inventory = InventoryWorker(config) if config.mcp_identity_secret else None
    while not stop.is_set():
        try:
            worked = worker.run_once()
            if knowledge:
                worked = knowledge.run_once() or worked
            if inventory:
                worked = inventory.run_once() or worked
        except OperationalError:
            # A short database outage must not turn into an unbounded busy loop
            # or silently abandon the worker. Leases handle uncertain commits.
            if args.once:
                raise
            stop.wait(min(5, config.run_poll_seconds * 5))
            continue
        if args.once:
            return
        if not worked:
            stop.wait(config.run_poll_seconds)


if __name__ == "__main__":
    main()
