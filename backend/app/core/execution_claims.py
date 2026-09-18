from __future__ import annotations

from threading import Lock

from redis import Redis
from redis.exceptions import RedisError

from backend.app.core.config.settings import settings


class InMemoryExecutionClaims:
    def __init__(self):
        self._keys: set[str] = set()
        self._lock = Lock()

    def claim(self, key: str, *, ttl_seconds: int) -> bool:
        with self._lock:
            if key in self._keys:
                return False
            self._keys.add(key)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self._keys.discard(key)


class RedisExecutionClaims:
    def __init__(self, client: Redis, fallback: InMemoryExecutionClaims | None = None):
        self.client = client
        self.fallback = fallback

    def claim(self, key: str, *, ttl_seconds: int) -> bool:
        try:
            result = self.client.set(
                f"xvond:execution-claim:{key}",
                "1",
                nx=True,
                ex=max(300, int(ttl_seconds)),
            )
            return bool(result)
        except RedisError:
            if self.fallback is not None:
                return self.fallback.claim(key, ttl_seconds=ttl_seconds)
            return False

    def release(self, key: str) -> None:
        try:
            self.client.delete(f"xvond:execution-claim:{key}")
        except RedisError:
            if self.fallback is not None:
                self.fallback.release(key)


def build_execution_claims():
    memory = InMemoryExecutionClaims()
    if not settings.REDIS_URL:
        return memory
    client = Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )
    return RedisExecutionClaims(
        client,
        fallback=None if settings.is_production else memory,
    )


execution_claims = build_execution_claims()
