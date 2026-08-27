# Production deployment and scaling

This version is horizontally scalable for read-heavy traffic, but it does not
claim unlimited capacity. Measure capacity against the real paper set and query
mix before launch.

## Migration without interrupting daily updates

Existing single-service deployments do not need Redis on the first rollout. When
`REDIS_URL` is absent, bounded legacy compatibility mode defaults on: a filesystem
election lock selects exactly one web process to own the daily scheduler, and a
second process lock prevents concurrent analysis across local workers. The
authenticated manual trigger uses the same locked path. Set
`ENABLE_LEGACY_MODE=false` to disable this fallback explicitly.

When `REDIS_URL` is present, scalable mode is always selected and legacy scheduling
is disabled even if `ENABLE_LEGACY_MODE=true`; deploy the separate `scheduler` and
`worker` services before adding `REDIS_URL`. This ordering avoids both a missed run
and duplicate scheduling. After observing successful queued runs, leave legacy mode
disabled and scale web replicas normally. Legacy election requires every local web
process to share the same writable `DATA_DIR`; it is a migration bridge, not the
recommended multi-host architecture.

Both scalable workers and legacy mode preserve the previous publication behavior:
after a successful pipeline they read `papers.json` and `papers_archive.json` from
`DATA_DIR` and publish them to those repository-relative names through the GitHub
Contents API. A pipeline safety-guard result skips publishing. Missing GitHub
configuration or a publishing failure is logged without rerunning paid analysis.

## Architecture

Traffic flows through a CDN/WAF and load balancer to stateless web replicas. Web
processes never start a scheduler, subprocess, or paid analysis; they only read
immutable snapshots and enqueue authenticated jobs.

Redis provides shared rate limits, snapshot acceleration, the job queue, job
de-duplication, and a distributed worker lock. One separately deployed scheduler
enqueues the daily job. Any number of workers may consume jobs, while the lock
allows one analysis at a time. The worker publishes JSON with atomic rename; web
replicas reload only when inode/mtime/size changes.

Jobs use an at-least-once queue: claiming atomically moves an ID from the ready
list into a leased inflight sorted set while retaining its payload. Success is
acknowledged only after the pipeline completes. Workers periodically recover only
expired leases, increment an attempt counter, and requeue them. Malformed payloads
and jobs exceeding `JOB_MAX_ATTEMPTS` go to a dead-letter list rather than looping.
Lock renewal is a single compare-token-and-expire Lua operation, so a stale worker
cannot extend a lock acquired by another worker. Monitor and drain the dead-letter
list operationally.

For one host, `docker compose up --build --scale web=4` starts Nginx, four web
replicas, one worker, one scheduler, and Redis. Copy `.env.example` to `.env`, set
strong secrets, and keep `.env` outside source control.

## Required infrastructure at large scale

- Managed load balancer plus CDN/WAF/DDoS protection. Cache `GET /api/papers` and
  `GET /api/dates`; never cache `/mcp`, Alexa, health, or the analysis trigger.
- Managed Redis with persistence, authentication/TLS, multi-AZ failover, alerts,
  and `noeviction` for queue/lock keys. At very high scale, separate the evictable
  data cache from the durable queue/lock Redis.
- Shared POSIX storage supporting atomic rename, or a versioned object-store/
  database implementation of `PaperRepository`. Container-local disks do not
  synchronize across hosts.
- Exactly one scheduler. Workers can scale independently, though this pipeline is
  intentionally serialized by a distributed lock.
- Autoscaling based on request rate, p95 latency, active connections, CPU, and
  memory. Start with one web process per CPU core and validate with load tests.
- Central logs, metrics and traces; alerts for queue depth, lock age, readiness,
  429/5xx rate, tail latency, and error-budget burn.
- A secret manager for `ADMIN_API_TOKEN`, `GROQ_API_KEY`, and publishing tokens.
- Pin `GROQ_MODEL` explicitly in deployment configuration. The default is
  `qwen/qwen3.8-27b`; changing models never requires a code edit.
- Mount optional `ANALYSIS_PROMPT_FILE` and `CONFERENCE_CONFIG_FILE` as trusted,
  read-only configuration. Invalid explicit files stop the pipeline before arXiv
  fetching or paid model calls instead of silently changing selection behavior.

## Health, failure behavior, and resource controls

- `/health/live` proves the process/event loop is alive.
- `/health/ready` confirms it accepts traffic and has a valid current snapshot.
- Uvicorn gets 30 seconds to drain after SIGTERM; configure a platform termination
  grace period of at least 40 seconds.
- Public reads remain available from snapshots during Redis failure. Job enqueue
  fails closed with 503. Rate limiting falls back to bounded per-process counters,
  which is weaker across replicas, so the edge WAF remains required.
- Nginx caps request body, client connections/rate, timeouts, and retries. Uvicorn
  caps concurrency, backlog, keep-alive, and graceful shutdown. Compose applies
  CPU/memory limits and runs a read-only non-root web container.
- Tune `JOB_LEASE_SECONDS` above the longest expected gap between worker renewal
  attempts and keep it aligned with the pipeline/lock timeout. Recovery is safely
  delayed until a lease expires, so crash recovery is not instantaneous.

## Validation

```sh
python -m unittest discover -s tests -v
python -m py_compile main.py data_store.py jobs.py mcp_server.py arxiv_agent.py pipeline_config.py
docker compose config
docker compose build
```

Then run a distributed load test against both CDN and origin. Exercise spikes,
slow clients, Redis loss, worker termination, scheduler restart, rolling deploys,
and a data publish during active reads. Define an SLO and stop increasing load when
p95/p99 latency or error rate exceeds it. The included 100-request concurrency test
is a regression check, not a capacity claim.

## Residual limitations

- JSON is a finite snapshot design. Parsing and indexing now happen once per file
  version, but multi-gigabyte archives should use a database/search service with
  pagination and bounded queries.
- Every web worker keeps a snapshot and MCP index in RAM. Account for this per
  process and replica when sizing memory.
- The Compose stack is a single-host reference, not high availability.
- Inline dashboard scripts/styles still require weaker CSP allowances; extracting
  those assets would permit a stricter CSP.
- A running analysis subprocess is not cancelled on worker SIGTERM. Allow enough
  termination time or add process-group supervision before aggressive preemption.
- Queue delivery is at-least-once. Atomic output publication and the distributed
  lock limit duplicate effects, but any future external side effects must also be
  idempotent.
- Legacy compatibility relies on POSIX `flock` semantics and is unsuitable for
  filesystems that do not provide reliable shared advisory locks. Use Redis mode
  for multi-host deployments.
