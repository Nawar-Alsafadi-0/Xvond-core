import logging
import os
import signal
import time

from backend.app.modules.automation.scheduler import (
    run_due_schedules_once,
    run_due_waits_once,
)
from backend.app.modules.automation.event_outbox import (
    dispatch_pending_automation_events_once,
)
from backend.app.modules.automation.scheduler_health import automation_scheduler_health
from backend.app.modules.billing.renewal import run_due_service_renewals_once


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("xvond.automation.scheduler.worker")
running = True


def stop_worker(_signum, _frame):
    global running
    running = False


def _poll_seconds() -> int:
    try:
        value = int(os.getenv("AUTOMATION_SCHEDULER_POLL_SECONDS", "30"))
    except ValueError:
        value = 30
    return max(5, min(value, 300))


def main():
    global running
    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)

    poll = _poll_seconds()
    logger.info("Automation scheduler worker started; poll_seconds=%s", poll)
    while running:
        started = time.monotonic()
        try:
            automation_scheduler_health.beat(poll)
        except Exception:
            logger.exception("Automation scheduler heartbeat failed")
        try:
            events = dispatch_pending_automation_events_once()
            if events["dispatched"] or events["pending"] or events["failed"]:
                logger.info(
                    "Automation event cycle; checked=%s dispatched=%s pending=%s failed=%s",
                    events["checked"],
                    events["dispatched"],
                    events["pending"],
                    events["failed"],
                )
        except Exception:
            logger.exception("Automation event cycle failed")

        try:
            waits = run_due_waits_once()
            if waits["resumed"] or waits["failed"]:
                logger.info(
                    "Automation wait cycle; checked=%s resumed=%s failed=%s",
                    waits["checked"],
                    waits["resumed"],
                    waits["failed"],
                )
        except Exception:
            logger.exception("Automation wait cycle failed")

        try:
            summary = run_due_schedules_once()
            if summary["executed"] or summary["failed"]:
                logger.info(
                    "Automation scheduler cycle; checked=%s executed=%s failed=%s",
                    summary["checked"],
                    summary["executed"],
                    summary["failed"],
                )
        except Exception:
            logger.exception("Automation scheduler cycle failed")

        try:
            renewal = run_due_service_renewals_once()
            if renewal["enabled"] and (
                renewal["submitted"]
                or renewal["captured"]
                or renewal["blocked"]
                or renewal["unknown"]
                or renewal["failed"]
            ):
                logger.info(
                    "Billing renewal cycle; checked=%s submitted=%s captured=%s blocked=%s unknown=%s failed=%s",
                    renewal["checked"],
                    renewal["submitted"],
                    renewal["captured"],
                    renewal["blocked"],
                    renewal["unknown"],
                    renewal["failed"],
                )
        except Exception:
            logger.exception("Billing renewal cycle failed")

        elapsed = time.monotonic() - started
        sleep_for = max(1.0, poll - elapsed)
        end = time.monotonic() + sleep_for
        while running and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))

    logger.info("Automation scheduler worker stopped")


if __name__ == "__main__":
    main()
