# AI Research Digest

A security-hardened, horizontally scalable FastAPI service that curates conference-linked arXiv papers, analyzes them with Groq, and serves a fast searchable dashboard plus a read-only MCP interface.

## What is included

- Responsive, accessible dashboard with search, date/category/rank filters, shareable filter URLs, loading/error/empty states, and dark/light themes.
- Stateless web tier with immutable per-version snapshots, pre-serialized JSON responses, ETags, cache headers, readiness/liveness probes, shared rate limits, and bounded concurrency.
- Reliable Redis job queue with deduplication, leased in-flight jobs, retries, dead-letter handling, and token-safe acknowledgements.
- Separate worker and singleton scheduler processes, protected by a renewable distributed analysis lock.
- Backward-compatible no-Redis mode that preserves the existing daily scheduler and GitHub Contents publishing during migration.
- Nginx edge cache, connection/rate limits, request limits, timeouts, retries, and stale-cache behavior.
- Non-root, read-only web containers and a pinned, audited Python dependency set.
- Security and scalability regression tests.

See [DEPLOYMENT.md](DEPLOYMENT.md) for production architecture, scaling requirements, failure behavior, and honest scale limits. See [SECURITY_AUDIT.md](SECURITY_AUDIT.md) for the security review.

## Architecture

```text
CDN / WAF / load balancer
           │
         Nginx
           │
   stateless FastAPI replicas ───── Redis rate limits + shared cache
           │                               │
           │                        reliable job queue
           │                               │
 shared immutable JSON snapshots ← analysis worker
                                           ↑
                                  singleton scheduler
                                           │
                                  arXiv + Groq APIs
```

In scalable mode (`REDIS_URL` configured), the web tier never starts a scheduler, subprocess, or paid analysis job directly. An authenticated request only enqueues work; a separate worker processes it.

To avoid interrupting existing deployments, no-Redis installations default to bounded legacy compatibility mode: one process is elected with a filesystem lock to own the daily scheduler, analysis remains single-flight, and successful snapshots continue publishing through the GitHub Contents API. Redis always disables this fallback, preventing duplicate scheduling. Follow the ordered migration steps in [DEPLOYMENT.md](DEPLOYMENT.md).

## Quick start with Docker Compose

Requirements: Docker with Compose v2.

```bash
cp .env.example .env
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Put the generated value in `.env` as `ADMIN_API_TOKEN`, then set `GROQ_API_KEY`. Configure the optional GitHub publishing variables only if automatic publishing is required.

```bash
docker compose up --build
```

Open [http://localhost:8000](http://localhost:8000).

Scale the read-only web tier on one host with:

```bash
docker compose up --build --scale web=4
```

Compose is a single-host reference, not a highly available production platform. Multi-host deployments need managed Redis, shared POSIX storage or a versioned object store/database, a CDN/WAF, and platform autoscaling.

## Local development

Requirements: Python 3.11+ and Redis when testing job dispatch.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
ENABLE_LEGACY_MODE=false python -m uvicorn main:app --reload --port 8000
```

The checked-in snapshots make the read-only dashboard available without Redis. The command above disables automatic analysis for ordinary frontend development. Existing no-Redis production deployments keep their historical scheduler by default.

Run one worker and one scheduler in separate terminals:

```bash
export REDIS_URL=redis://localhost:6379/0
PROCESS_ROLE=worker python jobs.py
PROCESS_ROLE=scheduler python jobs.py
```

## Configuration

The main variables are documented in `.env.example`:

- `ADMIN_API_TOKEN`: required for manual analysis; keep server-side.
- `GROQ_API_KEY`: required by the analysis pipeline.
- `GROQ_MODEL`: Groq model ID; defaults to `qwen/qwen3.8-27b`.
- `GROQ_REASONING_EFFORT`: Qwen reasoning mode; defaults to `none` for reliable JSON output.
- `ANALYSIS_PROMPT_FILE`: optional trusted UTF-8 prompt template using `${title}`, `${arxiv_id}`, `${comment}`, and `${summary}` placeholders.
- `CONFERENCE_CONFIG_FILE`: optional trusted JSON catalog with `categories` and `ranks` objects. When absent, the established built-in catalog is unchanged.
- `REDIS_URL`: required for shared rate limiting and job dispatch.
- `ENABLE_LEGACY_MODE`: no-Redis compatibility switch; defaults on only when Redis is absent. Set `false` for read-only development.
- `DATA_DIR`: shared snapshot directory; defaults to the application directory locally.
- `WEB_CONCURRENCY` and `UVICORN_LIMIT_CONCURRENCY`: per-container web capacity controls.
- `TRUSTED_PROXY_CIDRS`: proxies allowed to supply the client IP header.
- `PIPELINE_TIMEOUT_SECONDS`, `PIPELINE_LOCK_TTL_SECONDS`, and `JOB_LEASE_SECONDS`: worker safety limits.
- `API_RATE_LIMIT` and `MCP_RATE_LIMIT`: application-level limits; edge controls remain required.
- `LOCAL_RATE_LIMIT_MAX_KEYS`: hard cap for fallback per-process client buckets.
- `MAX_ARXIV_RESPONSE_BYTES`: decompressed response cap per arXiv category fetch.

Example prompt and conference files live in `config/`. An explicitly configured
conference file replaces the complete built-in catalog for that pipeline process;
copy every category and alias you want retained. Configuration files are validated
for type, size, duplicate venues, placeholders, and rank values before processing.

## HTTP endpoints

| Endpoint | Method | Purpose |
|---|---:|---|
| `/` | GET | Dashboard |
| `/api/papers` | GET | Current digest; add `?date=YYYY-MM-DD` for an archived date |
| `/api/dates` | GET | Available digest dates and counts |
| `/api/alexa/paper` | GET | Random paper, optionally filtered by `category` |
| `/api/trigger-analysis` | POST | Enqueue analysis; requires admin bearer token |
| `/mcp` | POST | Read-only MCP transport |
| `/health/live` | GET | Liveness probe |
| `/health/ready` | GET | Data-aware readiness probe |

Trigger analysis without exposing the token to browser code:

```bash
curl -X POST \
  -H "Authorization: Bearer $ADMIN_API_TOKEN" \
  http://localhost:8000/api/trigger-analysis
```

## Validation

```bash
python -m unittest discover -s tests -v
python -m py_compile main.py data_store.py jobs.py mcp_server.py arxiv_agent.py pipeline_config.py
docker compose config
```

The included concurrent-read test is a regression check, not a capacity benchmark. Before production launch, run distributed load and failure testing against the real CDN, load balancer, Redis, storage, and expected dataset.

## Scale boundaries

This design removes avoidable per-request parsing and supports horizontal read scaling, but it does not claim unlimited capacity. Very large archives should move from JSON to a database/search service with pagination and bounded queries. Review the residual limitations and operational checklist in [DEPLOYMENT.md](DEPLOYMENT.md) before launch.
