"""Independent Outbox dispatcher / refund reconciler, sharing the application's SQL DB."""

import argparse
import signal
import threading

from app.config.settings import Settings
from app.services.refund_delivery import RefundDeliveryWorker
from app.web.services import build_api_services


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=["dispatch", "reconcile", "both"], default="both"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = Settings()
    services = build_api_services(config)
    workflow = services.after_sale.refund_workflow
    if workflow is None:
        parser.error("Refund worker requires PROVIDER_MODE=sandbox and SQL persistence")
    worker = RefundDeliveryWorker(
        workflow, lease_seconds=max(30, config.provider_timeout_seconds + 5)
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        worked = False
        if args.mode in {"dispatch", "both"}:
            worked = worker.dispatch_once() or worked
        if args.mode in {"reconcile", "both"}:
            worked = worker.reconcile_once() or worked
        if args.once:
            return
        if not worked:
            stop.wait(config.run_poll_seconds)


if __name__ == "__main__":
    main()
