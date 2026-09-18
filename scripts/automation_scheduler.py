import logging
import os
import signal
import time

from backend.app.modules.automation.scheduler import run_due_schedules_once


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

        elapsed = time.monotonic() - started
        sleep_for = max(1.0, poll - elapsed)
        end = time.monotonic() + sleep_for
        while running and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))

    logger.info("Automation scheduler worker stopped")


if __name__ == "__main__":
    main()
