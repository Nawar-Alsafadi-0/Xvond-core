from backend.app.modules.channels.whatsapp_queue import WhatsAppJobQueue


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.ttls = {}

    def set(self, key, value, ex=None):
        self.data[key] = value
        if ex is not None:
            self.ttls[key] = int(ex)
        return True

    def get(self, key):
        return self.data.get(key)

    def ttl(self, key):
        if key not in self.data:
            return -2
        return self.ttls.get(key, -1)

    def expire(self, key, ttl):
        if key not in self.data:
            return 0
        self.ttls[key] = int(ttl)
        return 1


def make_queue():
    queue = WhatsAppJobQueue(redis_url="")
    queue.client = FakeRedis()
    return queue


def test_human_marker_expires_after_ten_minutes_by_default():
    queue = make_queue()
    queue.mark_human("phone-1", "customer-1", "echo-1")

    key = queue._human_key("phone-1", "customer-1")
    assert queue.client.get(key) == "echo-1"
    assert queue.client.ttl(key) == 600


def test_legacy_human_marker_without_ttl_is_bounded_on_read():
    queue = make_queue()
    key = queue._human_key("phone-1", "customer-1")
    queue.client.set(key, "legacy-echo")

    assert queue.client.ttl(key) == -1
    assert queue.human_marker("phone-1", "customer-1") == "legacy-echo"
    assert queue.client.ttl(key) == 600
