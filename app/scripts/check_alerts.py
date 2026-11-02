"""Print deployment alert transitions to stdout for a log collector, without sending messages."""

import argparse
import json
import time

from app.config.settings import Settings
from app.platform.alerts import AlertMonitor, evaluate_alerts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=15)
    parser.add_argument("--state-file", default="app/sessions/alert-state.json")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("interval must be >= 1 second")
    monitor = AlertMonitor(args.state_file)
    config = Settings()
    while True:
        try:
            snapshot = evaluate_alerts(config)
        except Exception:
            snapshot = {
                "checked_at": time.time(),
                "healthy": False,
                "alerts": [
                    {
                        "code": "MONITOR_UNAVAILABLE",
                        "severity": "critical",
                        "value": 1,
                        "threshold": 0,
                        "runbook": "docs/operations/incident-runbook.md#monitor-unavailable",
                    }
                ],
            }
        if not args.watch:
            print(json.dumps(snapshot, ensure_ascii=False))
            return 0 if snapshot["healthy"] else 2
        events = monitor.update(snapshot)
        if events:
            print(
                json.dumps(
                    {"checked_at": snapshot["checked_at"], "transitions": events},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
