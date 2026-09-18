import json
import time

from redis import Redis

from backend.app.core.config.settings import settings


class AutomationSchedulerHealth:
    key = "xvond:automation:scheduler-heartbeat"

    def __init__(self, redis_url: str | None = None, client=None):
        self.redis_url = redis_url if redis_url is not None else settings.REDIS_URL
        self.client = client
        if self.client is None and self.redis_url:
            self.client = Redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=5,
                health_check_interval=30,
            )

    @property
    def configured(self) -> bool:
        return self.client is not None

    def beat(self, poll_seconds: int) -> None:
        if self.client is None:
            return
        poll = max(5, min(int(poll_seconds), 300))
        ttl = max(30, min(poll * 4, 1200))
        payload = {
            "beat_at": time.time(),
            "poll_seconds": poll,
        }
        self.client.set(
            self.key,
            json.dumps(payload, separators=(",", ":")),
            ex=ttl,
        )

    def status(self) -> dict:
        if self.client is None:
            return {
                "configured": False,
                "active": False,
                "heartbeat_ttl_seconds": 0,
                "last_beat_at": None,
                "poll_seconds": None,
            }

        raw = self.client.get(self.key)
        ttl = int(self.client.ttl(self.key)) if raw else 0
        payload = {}
        if raw:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = {}

        return {
            "configured": True,
            "active": bool(raw and ttl > 0),
            "heartbeat_ttl_seconds": max(0, ttl),
            "last_beat_at": payload.get("beat_at"),
            "poll_seconds": payload.get("poll_seconds"),
        }


automation_scheduler_health = AutomationSchedulerHealth()
