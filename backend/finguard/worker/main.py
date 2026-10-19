"""Worker processes.

    python -m finguard.worker.main worker      # RQ worker: OCR jobs and other queued work
    python -m finguard.worker.main scheduler   # durable scheduler loop: due payments, reconciliation, expiry, recovery
    python -m finguard.worker.main tick        # run one scheduler cycle and exit (cron-friendly)

The scheduler never depends on a browser. All state is in PostgreSQL; on restart it resumes from the database
(stale claims are released, orphaned payout intents are reconciled before any retry).
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import time

from finguard.core.config import get_settings
from finguard.core.logging import configure_logging

log = logging.getLogger("finguard.worker")


def run_scheduler() -> None:
    from finguard.worker.jobs import scheduler_tick

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    interval = get_settings().scheduler_tick_seconds
    log.info("scheduler started (tick every %ss)", interval)
    while not stop["flag"]:
        try:
            log.info("tick %s", json.dumps(scheduler_tick()))
        except Exception:
            log.exception("scheduler tick failed")
        for _ in range(interval):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("scheduler stopped")


def run_worker() -> None:
    from redis import Redis
    from rq import Queue, Worker

    from finguard.worker.dispatch import QUEUE_NAME

    conn = Redis.from_url(get_settings().redis_url)
    Worker([Queue(QUEUE_NAME, connection=conn)], connection=conn).work(with_scheduler=False)


def main() -> None:
    configure_logging(get_settings().log_level)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scheduler"
    if cmd == "worker":
        run_worker()
    elif cmd == "tick":
        from finguard.worker.jobs import scheduler_tick

        print(json.dumps(scheduler_tick(), indent=2))
    else:
        run_scheduler()


if __name__ == "__main__":
    main()
