"""Redis-backed job dispatch used by web, worker, and scheduler processes."""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import lru_cache

from publishing import publish_outputs

logger = logging.getLogger(__name__)
QUEUE = "arxiv-agent:jobs"
INFLIGHT = "arxiv-agent:jobs:inflight"
PAYLOADS = "arxiv-agent:jobs:payloads"
CLAIMS = "arxiv-agent:jobs:claims"
DEAD = "arxiv-agent:jobs:dead"
PENDING = "arxiv-agent:analysis:pending"
LOCK = "arxiv-agent:analysis:lock"

CLAIM_SCRIPT = """-- claim-v1
for i = 1, 10 do
  local id = redis.call('rpop', KEYS[1])
  if not id then return nil end
  local payload = redis.call('hget', KEYS[2], id)
  if payload then
    redis.call('zadd', KEYS[3], ARGV[1], id)
    redis.call('hset', KEYS[5], id, ARGV[2])
    return {id, payload}
  end
  redis.call('lpush', KEYS[4], 'missing-payload:' .. id)
end
return nil
"""
RENEW_LOCK_SCRIPT = """-- renew-lock-v1
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
ACK_SCRIPT = """-- ack-v1
if redis.call('hget', KEYS[4], ARGV[1]) ~= ARGV[2] then return 0 end
redis.call('zrem', KEYS[1], ARGV[1])
redis.call('hdel', KEYS[2], ARGV[1])
redis.call('hdel', KEYS[4], ARGV[1])
if redis.call('get', KEYS[3]) == ARGV[1] then redis.call('del', KEYS[3]) end
return 1
"""
REQUEUE_SCRIPT = """-- requeue-v1
if redis.call('hget', KEYS[4], ARGV[1]) ~= ARGV[4] then return 0 end
local score = redis.call('zscore', KEYS[1], ARGV[1])
if not score or tonumber(score) > tonumber(ARGV[3]) then return 0 end
redis.call('hset', KEYS[2], ARGV[1], ARGV[2])
redis.call('zrem', KEYS[1], ARGV[1])
redis.call('hdel', KEYS[4], ARGV[1])
redis.call('rpush', KEYS[3], ARGV[1])
return 1
"""
DEAD_SCRIPT = """-- dead-v1
if redis.call('hget', KEYS[5], ARGV[1]) ~= ARGV[4] then return 0 end
local score = redis.call('zscore', KEYS[1], ARGV[1])
if not score then return 0 end
if tonumber(ARGV[3]) >= 0 and tonumber(score) > tonumber(ARGV[3]) then return 0 end
redis.call('zrem', KEYS[1], ARGV[1])
redis.call('hdel', KEYS[2], ARGV[1])
redis.call('hdel', KEYS[5], ARGV[1])
redis.call('lpush', KEYS[3], ARGV[2])
if redis.call('get', KEYS[4]) == ARGV[1] then redis.call('del', KEYS[4]) end
return 1
"""
RENEW_LEASE_SCRIPT = """-- renew-lease-v1
if redis.call('hget', KEYS[2], ARGV[1]) == ARGV[2] then
  return redis.call('zadd', KEYS[1], 'XX', ARGV[3], ARGV[1])
end
return 0
"""
ADOPT_ORPHAN_SCRIPT = """-- adopt-orphan-v1
local score = redis.call('zscore', KEYS[1], ARGV[1])
if not score or tonumber(score) > tonumber(ARGV[2]) then return nil end
local token = redis.call('hget', KEYS[2], ARGV[1])
if not token then redis.call('hset', KEYS[2], ARGV[1], ARGV[3]); token = ARGV[3] end
return token
"""


def redis_client():
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return None
    return _redis_for_url(url)


@lru_cache(maxsize=4)
def _redis_for_url(url: str):
    import redis
    return redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=2, health_check_interval=30)


def enqueue_analysis(source: str) -> tuple[bool, str]:
    client = redis_client()
    if client is None:
        raise RuntimeError("REDIS_URL is required for distributed job dispatch")
    job_id = str(uuid.uuid4())
    ttl = int(os.environ.get("JOB_DEDUP_TTL_SECONDS", "1800"))
    if not client.set(PENDING, job_id, nx=True, ex=ttl):
        return False, "already_queued_or_running"
    payload = json.dumps({"id": job_id, "source": source, "attempts": 0, "queued_at": datetime.now(timezone.utc).isoformat()})
    try:
        pipe = client.pipeline(transaction=True)
        pipe.hset(PAYLOADS, job_id, payload)
        pipe.lpush(QUEUE, job_id)
        pipe.execute()
    except Exception:
        _delete_if_value(client, PENDING, job_id)
        raise
    return True, job_id


def _delete_if_value(client, key: str, value: str) -> None:
    client.eval(
        "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
        1,
        key,
        value,
    )


def _claim(client):
    token = str(uuid.uuid4())
    result = client.eval(CLAIM_SCRIPT, 5, QUEUE, PAYLOADS, INFLIGHT, DEAD, CLAIMS, time.time(), token)
    if not result:
        return None
    job_id = result[0].decode() if isinstance(result[0], bytes) else str(result[0])
    return job_id, result[1], token


def _renew_lock(client, token: str, ttl: int) -> bool:
    """Atomically renew only the lock owned by this worker token."""
    return bool(client.eval(RENEW_LOCK_SCRIPT, 1, LOCK, token, ttl))


def _ack(client, job_id: str, claim_token: str) -> bool:
    return bool(client.eval(ACK_SCRIPT, 4, INFLIGHT, PAYLOADS, PENDING, CLAIMS, job_id, claim_token))


def _dead_letter(client, job_id: str, reason: str, claim_token: str, cutoff: float = -1) -> bool:
    entry = json.dumps({"id": job_id, "reason": reason[:500], "failed_at": datetime.now(timezone.utc).isoformat()})
    return bool(client.eval(DEAD_SCRIPT, 5, INFLIGHT, PAYLOADS, DEAD, PENDING, CLAIMS,
                            job_id, entry, cutoff, claim_token))


def _requeue(client, job_id: str, payload: dict, cutoff: float, claim_token: str) -> bool:
    raw = json.dumps(payload, separators=(",", ":"))
    return bool(client.eval(REQUEUE_SCRIPT, 4, INFLIGHT, PAYLOADS, QUEUE, CLAIMS,
                            job_id, raw, cutoff, claim_token))


def _recover_orphans(client) -> int:
    cutoff = time.time() - int(os.environ.get("JOB_LEASE_SECONDS", "1800"))
    ids = client.zrangebyscore(INFLIGHT, "-inf", cutoff)
    recovered = 0
    max_attempts = int(os.environ.get("JOB_MAX_ATTEMPTS", "3"))
    for raw_id in ids:
        job_id = raw_id.decode() if isinstance(raw_id, bytes) else str(raw_id)
        adoption_token = str(uuid.uuid4())
        claim_token = client.eval(ADOPT_ORPHAN_SCRIPT, 2, INFLIGHT, CLAIMS, job_id, cutoff, adoption_token)
        if not claim_token:
            continue
        claim_token = claim_token.decode() if isinstance(claim_token, bytes) else str(claim_token)
        raw = client.hget(PAYLOADS, job_id)
        try:
            payload = json.loads(raw) if raw is not None else None
            if not isinstance(payload, dict):
                raise ValueError("invalid payload")
        except Exception:
            _dead_letter(client, job_id, "malformed orphan payload", claim_token, cutoff)
            continue
        payload["attempts"] = int(payload.get("attempts", 0)) + 1
        if payload["attempts"] >= max_attempts:
            _dead_letter(client, job_id, "maximum attempts reached during recovery", claim_token, cutoff)
        elif _requeue(client, job_id, payload, cutoff, claim_token):
            recovered += 1
    return recovered


class LockBusy(RuntimeError):
    pass


def execute_pipeline(payload: dict):
    """Run once in DATA_DIR, then preserve the original GitHub publication step."""
    timeout = int(os.environ.get("PIPELINE_TIMEOUT_SECONDS", "1200"))
    data_dir = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [os.environ.get("PYTHON_BIN", "python"), os.path.join(os.path.dirname(os.path.abspath(__file__)), "arxiv_agent.py")],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(os.environ),
        cwd=data_dir,
    )
    logger.info("Pipeline job %s exited %s", payload.get("id"), result.returncode)
    if result.stdout:
        logger.info("pipeline stdout: %s", result.stdout[-4000:])
    if result.stderr:
        logger.warning("pipeline stderr: %s", result.stderr[-4000:])
    if result.returncode:
        raise RuntimeError(f"pipeline exited {result.returncode}")
    publish_outputs(result.stdout, data_dir)
    return result


def _run_job(client, payload: dict, job_id: str, claim_token: str) -> None:
    lock_ttl = int(os.environ.get("PIPELINE_LOCK_TTL_SECONDS", "1800"))
    token = str(uuid.uuid4())
    if not client.set(LOCK, token, nx=True, ex=lock_ttl):
        raise LockBusy("Pipeline lock is held")
    stop_refresh = threading.Event()
    lease_ttl = int(os.environ.get("JOB_LEASE_SECONDS", "1800"))

    def refresh() -> None:
        interval = max(5, min(lock_ttl, lease_ttl) // 3)
        while not stop_refresh.wait(interval):
            renewed = _renew_lock(client, token, lock_ttl)
            if not renewed:
                return
            client.eval(RENEW_LEASE_SCRIPT, 2, INFLIGHT, CLAIMS, job_id, claim_token, time.time())

    refresher = threading.Thread(target=refresh, daemon=True)
    refresher.start()
    try:
        execute_pipeline(payload)
    finally:
        stop_refresh.set()
        refresher.join(timeout=2)
        _delete_if_value(client, LOCK, token)


def worker_main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    client = redis_client()
    if client is None:
        raise SystemExit("REDIS_URL is required")
    stopping = False

    def stop(*_args):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logger.info("Worker ready; recovered %s orphaned jobs", _recover_orphans(client))
    last_recovery = time.monotonic()
    while not stopping:
        if time.monotonic() - last_recovery >= 30:
            _recover_orphans(client)
            last_recovery = time.monotonic()
        claimed = _claim(client)
        if not claimed:
            time.sleep(1)
            continue
        job_id, raw_payload, claim_token = claimed
        payload = None
        try:
            payload = json.loads(raw_payload)
            if not isinstance(payload, dict) or payload.get("id") != job_id:
                raise ValueError("malformed or mismatched job payload")
            _run_job(client, payload, job_id, claim_token)
            _ack(client, job_id, claim_token)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            logger.warning("Dead-lettering malformed job %s: %s", job_id, exc)
            _dead_letter(client, job_id, "malformed payload", claim_token)
        except Exception as exc:
            logger.exception("Pipeline job failed")
            if isinstance(payload, dict):
                payload["attempts"] = int(payload.get("attempts", 0)) + 1
                if payload["attempts"] >= int(os.environ.get("JOB_MAX_ATTEMPTS", "3")):
                    _dead_letter(client, job_id, f"maximum attempts reached: {type(exc).__name__}", claim_token)
                else:
                    _requeue(client, job_id, payload, time.time(), claim_token)
            else:
                _dead_letter(client, job_id, "unreadable payload", claim_token)


def scheduler_main() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    import pytz

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    scheduler = BlockingScheduler(timezone=pytz.timezone(os.environ.get("SCHEDULE_TIMEZONE", "America/New_York")))

    def scheduled_job():
        try:
            enqueue_analysis("schedule")
        except Exception:
            logger.exception("Could not enqueue scheduled analysis")

    scheduler.add_job(
        scheduled_job,
        "cron",
        hour=int(os.environ.get("SCHEDULE_HOUR", "23")),
        minute=int(os.environ.get("SCHEDULE_MINUTE", "30")),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    scheduler.start()


if __name__ == "__main__":
    mode = os.environ.get("PROCESS_ROLE", "worker")
    scheduler_main() if mode == "scheduler" else worker_main()
