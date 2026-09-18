import json
from pathlib import Path

from backend.app.modules.automation.scheduler_health import AutomationSchedulerHealth


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def set(self, key, value, ex=None):
        self.values[key] = value
        self.ttls[key] = int(ex or 0)
        return True

    def get(self, key):
        return self.values.get(key)

    def ttl(self, key):
        return self.ttls.get(key, -2)


def test_scheduler_health_heartbeat_is_ttl_bounded_and_visible():
    fake = FakeRedis()
    health = AutomationSchedulerHealth(client=fake)

    health.beat(30)
    status = health.status()

    assert status["configured"] is True
    assert status["active"] is True
    assert status["heartbeat_ttl_seconds"] == 120
    assert status["poll_seconds"] == 30
    assert isinstance(status["last_beat_at"], float)

    stored = json.loads(fake.values[health.key])
    assert stored["poll_seconds"] == 30


def test_scheduler_health_without_redis_fails_closed():
    health = AutomationSchedulerHealth(redis_url="", client=None)
    status = health.status()

    assert status == {
        "configured": False,
        "active": False,
        "heartbeat_ttl_seconds": 0,
        "last_beat_at": None,
        "poll_seconds": None,
    }


def test_scheduler_worker_publishes_heartbeat_before_each_cycle():
    source = Path("scripts/automation_scheduler.py").read_text(encoding="utf-8")
    heartbeat = source.index("automation_scheduler_health.beat(poll)")
    run_cycle = source.index("summary = run_due_schedules_once()")
    assert heartbeat < run_cycle
