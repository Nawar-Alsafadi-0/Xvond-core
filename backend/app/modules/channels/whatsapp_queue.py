import json
import hashlib
import time
import uuid
from datetime import datetime, timezone

from redis import Redis

from backend.app.core.config.settings import settings


class WhatsAppJobQueue:
    queue_key = "xvond:whatsapp:jobs"
    processing_key = "xvond:whatsapp:processing"
    dead_key = "xvond:whatsapp:dead"
    retry_key = "xvond:whatsapp:retry"
    worker_lock_key = "xvond:whatsapp:worker-lock"
    retry_delays = (5, 30, 120, 600)
    human_marker_ttl_seconds = 600

    def __init__(self, redis_url: str | None = None):
        self.redis_url = redis_url or settings.REDIS_URL
        self.client = (
            Redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=15,
                health_check_interval=30,
            )
            if self.redis_url
            else None
        )

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def enqueue(
        self,
        body: str,
        signature: str,
    ) -> str:
        if self.client is None:
            raise RuntimeError("WhatsApp queue is not configured")

        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "body": body,
            "signature": signature,
            "attempts": 0,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
        }
        self.client.lpush(
            self.queue_key,
            json.dumps(job),
        )
        return job_id

    def acquire_worker_lock(self, owner: str, ttl_seconds: int = 30) -> bool:
        if self.client is None:
            return False
        ttl = max(10, min(int(ttl_seconds), 3600))
        return bool(
            self.client.set(
                self.worker_lock_key,
                owner,
                nx=True,
                ex=ttl,
            )
        )

    def refresh_worker_lock(self, owner: str, ttl_seconds: int = 30) -> bool:
        if self.client is None:
            return False
        ttl = max(10, min(int(ttl_seconds), 3600))
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('EXPIRE', KEYS[1], ARGV[2])
        end
        return 0
        """
        return bool(
            self.client.eval(
                script,
                1,
                self.worker_lock_key,
                owner,
                ttl,
            )
        )

    def release_worker_lock(self, owner: str) -> bool:
        if self.client is None:
            return False
        script = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
            return redis.call('DEL', KEYS[1])
        end
        return 0
        """
        return bool(
            self.client.eval(
                script,
                1,
                self.worker_lock_key,
                owner,
            )
        )

    def recover_interrupted(self) -> int:
        if self.client is None:
            return 0

        recovered = 0
        while True:
            item = self.client.rpoplpush(
                self.processing_key,
                self.queue_key,
            )
            if item is None:
                return recovered
            recovered += 1

    def promote_due(self, now: float | None = None, limit: int = 100) -> int:
        if self.client is None:
            return 0

        script = """
        local items = redis.call(
            'ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1],
            'LIMIT', 0, ARGV[2]
        )
        for _, item in ipairs(items) do
            redis.call('ZREM', KEYS[1], item)
            redis.call('LPUSH', KEYS[2], item)
        end
        return #items
        """
        return int(
            self.client.eval(
                script,
                2,
                self.retry_key,
                self.queue_key,
                now if now is not None else time.time(),
                max(1, min(int(limit), 1000)),
            )
        )

    def reserve(self, timeout: int = 5):
        if self.client is None:
            return None

        self.promote_due()

        raw = self.client.brpoplpush(
            self.queue_key,
            self.processing_key,
            timeout=timeout,
        )
        if raw is None:
            return None

        return raw, json.loads(raw)

    def acknowledge(self, raw: str):
        if self.client is not None:
            self.client.lrem(
                self.processing_key,
                1,
                raw,
            )

    @staticmethod
    def _human_key(phone_number_id: str, wa_id: str) -> str:
        digest = hashlib.sha256(f"{phone_number_id}:{wa_id}".encode()).hexdigest()
        return f"xvond:whatsapp:human:{digest}"

    def mark_human(
        self,
        phone_number_id: str,
        wa_id: str,
        event_id: str,
        ttl_seconds: int | None = None,
    ) -> None:
        if self.client is not None:
            ttl = max(
                60,
                min(
                    int(ttl_seconds or self.human_marker_ttl_seconds),
                    86400,
                ),
            )
            self.client.set(
                self._human_key(phone_number_id, wa_id),
                event_id,
                ex=ttl,
            )

    def human_marker(self, phone_number_id: str, wa_id: str) -> str | None:
        if self.client is None:
            return None
        key = self._human_key(phone_number_id, wa_id)
        marker = self.client.get(key)
        if marker and int(self.client.ttl(key)) < 0:
            # Transitional safety for markers written by older releases without
            # an expiry: bound them instead of allowing permanent human mode.
            self.client.expire(key, self.human_marker_ttl_seconds)
        return marker

    def clear_human_marker(self, phone_number_id: str, wa_id: str, expected: str | None) -> None:
        if self.client is not None and expected is not None:
            # A concurrent newer echo must survive an explicit return to AI.
            self.client.eval(
                "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0",
                1,
                self._human_key(phone_number_id, wa_id),
                expected,
            )

    def stats(self) -> dict:
        if self.client is None:
            return {
                "configured": False,
                "worker_active": False,
                "worker_lease_ttl_seconds": 0,
                "queued": 0,
                "processing": 0,
                "retrying": 0,
                "dead": 0,
            }

        worker_owner = self.client.get(self.worker_lock_key)
        lease_ttl = int(self.client.ttl(self.worker_lock_key)) if worker_owner else 0
        return {
            "configured": True,
            "worker_active": bool(worker_owner and lease_ttl > 0),
            "worker_lease_ttl_seconds": max(0, lease_ttl),
            "queued": int(self.client.llen(self.queue_key)),
            "processing": int(self.client.llen(self.processing_key)),
            "retrying": int(self.client.zcard(self.retry_key)),
            "dead": int(self.client.llen(self.dead_key)),
        }

    def dead_jobs(self, limit: int = 50) -> list[dict]:
        if self.client is None:
            return []

        safe_limit = max(1, min(int(limit), 200))
        items = self.client.lrange(
            self.dead_key,
            0,
            safe_limit - 1,
        )
        result = []

        for raw in items:
            try:
                job = json.loads(raw)
            except (TypeError, ValueError):
                result.append({
                    "id": None,
                    "attempts": None,
                    "last_error": "Invalid dead-letter payload",
                })
                continue

            # Never expose message bodies or webhook signatures.
            result.append({
                "id": job.get("id"),
                "attempts": job.get("attempts"),
                "enqueued_at": job.get("enqueued_at"),
                "last_failed_at": job.get("last_failed_at"),
                "last_error": job.get("last_error"),
            })

        return result

    def requeue_dead(self, limit: int = 100) -> int:
        if self.client is None:
            return 0

        safe_limit = max(1, min(int(limit), 500))
        script = """
        local raw = redis.call('RPOP', KEYS[1])
        if not raw then
            return 0
        end
        local ok, job = pcall(cjson.decode, raw)
        if not ok or type(job) ~= 'table' then
            redis.call('RPUSH', KEYS[1], raw)
            return -1
        end
        job['attempts'] = 0
        job['last_error'] = nil
        job['last_failed_at'] = nil
        job['retry_after_seconds'] = nil
        redis.call('LPUSH', KEYS[2], cjson.encode(job))
        return 1
        """
        requeued = 0
        for _ in range(safe_limit):
            moved = int(
                self.client.eval(
                    script,
                    2,
                    self.dead_key,
                    self.queue_key,
                )
            )
            if moved <= 0:
                break
            requeued += moved
        return requeued

    def retry_or_dead_letter(
        self,
        raw: str,
        job: dict,
        error: Exception,
        max_attempts: int = 5,
    ) -> str:
        if self.client is None:
            return "unavailable"

        job["attempts"] = int(job.get("attempts", 0)) + 1
        job["last_error"] = type(error).__name__
        job["last_failed_at"] = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(job)

        if job["attempts"] >= max_attempts:
            transaction = self.client.pipeline(transaction=True)
            transaction.lpush(self.dead_key, encoded)
            transaction.lrem(self.processing_key, 1, raw)
            transaction.execute()
            return "dead"

        delay_index = min(
            job["attempts"] - 1,
            len(self.retry_delays) - 1,
        )
        delay_seconds = self.retry_delays[delay_index]
        job["retry_after_seconds"] = delay_seconds
        encoded = json.dumps(job)

        transaction = self.client.pipeline(transaction=True)
        transaction.zadd(
            self.retry_key,
            {
                encoded: time.time() + delay_seconds,
            },
        )
        transaction.lrem(self.processing_key, 1, raw)
        transaction.execute()
        return "retry"


whatsapp_job_queue = WhatsAppJobQueue()
