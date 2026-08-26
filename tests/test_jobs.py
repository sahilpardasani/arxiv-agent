import json
import time
import unittest
from unittest.mock import patch

import jobs


class _Pipeline:
    def __init__(self, client): self.client, self.ops = client, []
    def hset(self, *args): self.ops.append(("hset", args)); return self
    def lpush(self, *args): self.ops.append(("lpush", args)); return self
    def execute(self):
        for name, args in self.ops: getattr(self.client, name)(*args)


class FakeRedis:
    def __init__(self):
        self.strings, self.hashes, self.lists, self.zsets = {}, {}, {}, {}
    def pipeline(self, transaction=True): return _Pipeline(self)
    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.strings: return False
        self.strings[key] = str(value); return True
    def get(self, key):
        value = self.strings.get(key); return value.encode() if value is not None else None
    def delete(self, key): self.strings.pop(key, None)
    def hset(self, key, field, value): self.hashes.setdefault(key, {})[str(field)] = value
    def hget(self, key, field): return self.hashes.get(key, {}).get(str(field))
    def lpush(self, key, value): self.lists.setdefault(key, []).insert(0, str(value))
    def zadd(self, key, mapping, xx=False):
        target = self.zsets.setdefault(key, {})
        for member, score in mapping.items():
            if not xx or str(member) in target: target[str(member)] = float(score)
    def zrangebyscore(self, key, low, high):
        return [member.encode() for member, score in self.zsets.get(key, {}).items() if score <= float(high)]
    def eval(self, script, count, *args):
        keys, argv = args[:count], args[count:]
        if script.startswith("-- claim"):
            queue = self.lists.setdefault(keys[0], [])
            while queue:
                job_id = queue.pop()
                payload = self.hget(keys[1], job_id)
                if payload is not None:
                    self.zsets.setdefault(keys[2], {})[job_id] = float(argv[0])
                    self.hashes.setdefault(keys[4], {})[job_id] = str(argv[1])
                    return [job_id.encode(), payload.encode() if isinstance(payload, str) else payload]
            return None
        if script.startswith("-- renew-lock"):
            if self.strings.get(keys[0]) == str(argv[0]): return 1
            return 0
        if script.startswith("-- ack"):
            job_id = str(argv[0])
            if self.hashes.get(keys[3], {}).get(job_id) != str(argv[1]): return 0
            self.zsets.get(keys[0], {}).pop(job_id, None); self.hashes.get(keys[1], {}).pop(job_id, None); self.hashes.get(keys[3], {}).pop(job_id, None)
            if self.strings.get(keys[2]) == job_id: self.strings.pop(keys[2], None)
            return 1
        if script.startswith("-- requeue"):
            job_id, payload, cutoff, token = str(argv[0]), argv[1], float(argv[2]), str(argv[3])
            if self.hashes.get(keys[3], {}).get(job_id) != token: return 0
            score = self.zsets.get(keys[0], {}).get(job_id)
            if score is None or score > cutoff: return 0
            self.hashes.setdefault(keys[1], {})[job_id] = payload
            self.zsets[keys[0]].pop(job_id); self.hashes[keys[3]].pop(job_id); self.lists.setdefault(keys[2], []).append(job_id); return 1
        if script.startswith("-- dead"):
            job_id, cutoff, token = str(argv[0]), float(argv[2]), str(argv[3])
            if self.hashes.get(keys[4], {}).get(job_id) != token: return 0
            score = self.zsets.get(keys[0], {}).get(job_id)
            if score is None or (cutoff >= 0 and score > cutoff): return 0
            self.zsets.get(keys[0], {}).pop(job_id, None); self.hashes.get(keys[1], {}).pop(job_id, None); self.hashes.get(keys[4], {}).pop(job_id, None)
            self.lists.setdefault(keys[2], []).insert(0, argv[1])
            if self.strings.get(keys[3]) == job_id: self.strings.pop(keys[3], None)
            return 1
        if script.startswith("-- renew-lease"):
            job_id, token = str(argv[0]), str(argv[1])
            if self.hashes.get(keys[1], {}).get(job_id) != token: return 0
            self.zsets.get(keys[0], {})[job_id] = float(argv[2]); return 1
        if script.startswith("-- adopt-orphan"):
            job_id, cutoff, new_token = str(argv[0]), float(argv[1]), str(argv[2])
            score = self.zsets.get(keys[0], {}).get(job_id)
            if score is None or score > cutoff: return None
            token = self.hashes.setdefault(keys[1], {}).setdefault(job_id, new_token)
            return token.encode()
        # compare-and-delete helper
        if self.strings.get(keys[0]) == str(argv[0]): self.strings.pop(keys[0], None); return 1
        return 0


class ReliableQueueTests(unittest.TestCase):
    def test_claim_keeps_payload_inflight_until_ack(self):
        client = FakeRedis()
        with patch.object(jobs, "redis_client", return_value=client):
            queued, job_id = jobs.enqueue_analysis("test")
        self.assertTrue(queued)
        claimed_id, payload, claim_token = jobs._claim(client)
        self.assertEqual(claimed_id, job_id)
        self.assertIn(job_id, client.zsets[jobs.INFLIGHT])
        self.assertIn(job_id, client.hashes[jobs.PAYLOADS])
        self.assertEqual(json.loads(payload)["id"], job_id)
        jobs._ack(client, job_id, claim_token)
        self.assertNotIn(job_id, client.zsets[jobs.INFLIGHT])
        self.assertNotIn(job_id, client.hashes[jobs.PAYLOADS])

    def test_startup_requeues_only_stale_orphan_and_increments_attempt(self):
        client = FakeRedis(); job_id = "orphan"
        client.hashes[jobs.PAYLOADS] = {job_id: json.dumps({"id": job_id, "attempts": 0})}
        client.zsets[jobs.INFLIGHT] = {job_id: time.time() - 5000}
        with patch.dict("os.environ", {"JOB_LEASE_SECONDS": "60", "JOB_MAX_ATTEMPTS": "3"}):
            self.assertEqual(jobs._recover_orphans(client), 1)
        self.assertIn(job_id, client.lists[jobs.QUEUE])
        self.assertEqual(json.loads(client.hashes[jobs.PAYLOADS][job_id])["attempts"], 1)

    def test_malformed_orphan_is_dead_lettered_not_requeued(self):
        client = FakeRedis(); job_id = "poison"
        client.hashes[jobs.PAYLOADS] = {job_id: "not-json"}
        client.zsets[jobs.INFLIGHT] = {job_id: time.time() - 5000}
        with patch.dict("os.environ", {"JOB_LEASE_SECONDS": "60"}): jobs._recover_orphans(client)
        self.assertNotIn(job_id, client.lists.get(jobs.QUEUE, []))
        self.assertTrue(client.lists[jobs.DEAD])

    def test_lock_renewal_is_token_safe(self):
        client = FakeRedis(); client.strings[jobs.LOCK] = "owner"
        self.assertTrue(jobs._renew_lock(client, "owner", 30))
        self.assertFalse(jobs._renew_lock(client, "attacker", 30))
        self.assertIn("redis.call('get'", jobs.RENEW_LOCK_SCRIPT)
        self.assertIn("redis.call('expire'", jobs.RENEW_LOCK_SCRIPT)

    def test_stale_worker_cannot_ack_a_reclaimed_job(self):
        client = FakeRedis()
        with patch.object(jobs, "redis_client", return_value=client):
            _, job_id = jobs.enqueue_analysis("test")
        _, raw, old_token = jobs._claim(client)
        payload = json.loads(raw)
        self.assertTrue(jobs._requeue(client, job_id, payload, time.time(), old_token))
        _, _, new_token = jobs._claim(client)
        self.assertFalse(jobs._ack(client, job_id, old_token))
        self.assertIn(job_id, client.hashes[jobs.PAYLOADS])
        self.assertTrue(jobs._ack(client, job_id, new_token))


if __name__ == "__main__": unittest.main()
