#!/usr/bin/env python3
"""Stateless web service. Scheduling and analysis run in separate processes."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import random
import re
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, Response

from data_store import repository
from jobs import enqueue_analysis, redis_client
from legacy import legacy_mode_enabled, legacy_runtime
from mcp_server import mcp, mcp_app

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
_response_lock = threading.Lock()
_response_cache: dict[str, tuple[object, bytes, str]] = {}
_local_rate_lock = threading.Lock()
_local_request_history: dict[str, deque[float]] = defaultdict(deque)


def _trusted_proxy_networks():
    result = []
    for value in os.environ.get("TRUSTED_PROXY_CIDRS", "").split(","):
        if value.strip():
            try:
                result.append(ipaddress.ip_network(value.strip(), strict=False))
            except ValueError:
                logger.error("Ignoring invalid trusted proxy: %s", value)
    return result


TRUSTED_PROXIES = _trusted_proxy_networks()


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    try:
        trusted = any(ipaddress.ip_address(peer) in network for network in TRUSTED_PROXIES)
    except ValueError:
        trusted = False
    if trusted:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return peer


def _rate_spec(request: Request):
    path, client = request.url.path, _client_ip(request)
    if path.startswith("/mcp"):
        return (f"mcp:{client}", int(os.environ.get("MCP_RATE_LIMIT", "30")), 60)
    if path == "/api/trigger-analysis":
        return (f"trigger:{client}", int(os.environ.get("TRIGGER_RATE_LIMIT", "10")), 300)
    if path.startswith("/api/"):
        return (f"api:{client}", int(os.environ.get("API_RATE_LIMIT", "300")), 60)
    return None


def _local_rate_limit(key: str, limit: int, window: int):
    now = time.monotonic()
    with _local_rate_lock:
        try:
            max_keys = max(100, min(int(os.environ.get("LOCAL_RATE_LIMIT_MAX_KEYS", "10000")), 100_000))
        except ValueError:
            max_keys = 10_000
        if key not in _local_request_history and len(_local_request_history) >= max_keys:
            stale = [k for k, values in _local_request_history.items() if not values or values[-1] <= now - 300]
            for stale_key in stale:
                _local_request_history.pop(stale_key, None)
            if len(_local_request_history) >= max_keys:
                return window
        history = _local_request_history[key]
        while history and history[0] <= now - window:
            history.popleft()
        if len(history) >= limit:
            return max(1, int(history[0] + window - now) + 1)
        history.append(now)
        if len(_local_request_history) > max_keys:
            for stale_key in [k for k, values in _local_request_history.items() if not values or values[-1] <= now - 300]:
                _local_request_history.pop(stale_key, None)
    return None


def _rate_limited(spec):
    key, limit, window = spec
    try:
        client = redis_client()
        if client is not None:
            bucket = int(time.time()) // window
            redis_key = f"arxiv-agent:rate:{key}:{bucket}"
            count = client.incr(redis_key)
            if count == 1:
                client.expire(redis_key, window + 1)
            return window - (int(time.time()) % window) if count > limit else None
    except Exception:
        logger.warning("Shared rate limiter unavailable; using per-process limit", exc_info=True)
    return _local_rate_limit(key, limit, window)


def _harden(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    )
    return response


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    app.state.accepting_traffic = True
    await run_in_threadpool(repository.current)
    if legacy_mode_enabled():
        try:
            await run_in_threadpool(legacy_runtime.start)
        except Exception:
            logger.exception("Could not start legacy compatibility scheduler")
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        app.state.accepting_traffic = False
        await run_in_threadpool(legacy_runtime.stop)


app = FastAPI(lifespan=app_lifespan, docs_url=None, redoc_url=None)
app.mount("/mcp", mcp_app)


@app.middleware("http")
async def operational_middleware(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > int(os.environ.get("MAX_REQUEST_BODY_BYTES", "1048576")):
                return _harden(HTMLResponse("Request body too large", status_code=413))
        except ValueError:
            return _harden(HTMLResponse("Invalid Content-Length", status_code=400))
    spec = _rate_spec(request)
    if spec:
        retry = await run_in_threadpool(_rate_limited, spec)
        if retry is not None:
            return _harden(HTMLResponse("Too many requests", status_code=429, headers={"Retry-After": str(retry)}))
    return _harden(await call_next(request))


def _require_admin(request: Request) -> None:
    expected = os.environ.get("ADMIN_API_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Manual analysis is disabled until ADMIN_API_TOKEN is configured")
    authorization = request.headers.get("authorization", "")
    supplied = authorization[7:] if authorization.lower().startswith("bearer ") else request.headers.get("x-admin-token", "")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing admin token", headers={"WWW-Authenticate": "Bearer"})


def _cached_json(cache_key: str, version: object, value_factory, max_age: int = 60) -> Response:
    cached = _response_cache.get(cache_key)
    if cached is None or cached[0] != version:
        with _response_lock:
            cached = _response_cache.get(cache_key)
            if cached is None or cached[0] != version:
                body = json.dumps(value_factory(), ensure_ascii=False, separators=(",", ":")).encode()
                cached = (version, body, '"' + hashlib.sha256(body).hexdigest() + '"')
                if len(_response_cache) >= int(os.environ.get("RESPONSE_CACHE_MAX_ENTRIES", "256")):
                    _response_cache.pop(next(iter(_response_cache)))
                _response_cache[cache_key] = cached
    return Response(content=cached[1], media_type="application/json", headers={
        "ETag": cached[2], "Cache-Control": f"public, max-age={max_age}, stale-while-revalidate=300"})


@app.get("/health/live")
@app.get("/health")
async def liveness():
    return {"status": "alive", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/health/ready")
async def readiness(request: Request):
    ready = getattr(request.app.state, "accepting_traffic", False) and await run_in_threadpool(repository.ready)
    if not ready:
        raise HTTPException(status_code=503, detail="Not ready")
    return {"status": "ready"}


@app.post("/api/trigger-analysis", status_code=202)
async def trigger_analysis(request: Request):
    _require_admin(request)
    if legacy_mode_enabled():
        started = await run_in_threadpool(legacy_runtime.dispatch, "manual")
        return {"status": "started" if started else "already_running", "job_id": None}
    try:
        queued, job = await run_in_threadpool(enqueue_analysis, "manual")
    except Exception:
        logger.exception("Unable to enqueue analysis")
        raise HTTPException(status_code=503, detail="Job queue unavailable")
    return {"status": "queued" if queued else "already_queued_or_running", "job_id": job if queued else None}


@app.get("/api/papers")
async def get_papers(date: str | None = None):
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(status_code=422, detail="date must use YYYY-MM-DD")
    current, archive = await run_in_threadpool(lambda: (repository.current(), repository.archive()))
    version = repository.version()
    if not current:
        return {"error": "No papers available yet", "total_papers": 0}
    if not date:
        return _cached_json("papers:current", version[0], lambda: current)

    block = (archive.get("dates", {}) or {}).get(date)
    if not isinstance(block, dict):
        # Do not let arbitrary valid-format dates create unbounded cache entries.
        return {"error": f"No papers found for date {date}",
                "available_dates": list((archive.get("dates", {}) or {}).keys()), "total_papers": 0}

    def build_date():
        previous_date = (datetime.fromisoformat(date) - timedelta(days=1)).date().isoformat()
        previous = (archive.get("dates", {}) or {}).get(previous_date)
        count = int(block.get("count", len(block.get("papers", []))) or 0)
        previous_count = len(previous.get("papers", [])) if isinstance(previous, dict) else 0
        change = count - previous_count
        return {"last_updated": block.get("updated_at"), "total_papers": count, "papers": block.get("papers", []),
                "categories": current.get("categories", []), "filter_date": date,
                "metrics": {"dashboard": {"current_date": date, "current_count": count,
                    "previous_date": previous_date if previous else "unavailable", "previous_count": previous_count,
                    "day_change": change, "trend": "UP" if change > 0 else ("DOWN" if change < 0 else "STABLE")}}}
    return _cached_json(f"papers:{date}", version, build_date)


@app.get("/api/dates")
async def dates():
    current, archive = await run_in_threadpool(lambda: (repository.current(), repository.archive()))
    version = repository.version()
    def build():
        info = {}
        current_date = current.get("filter_date")
        if current_date:
            info[current_date] = {"count": current.get("total_papers", 0), "status": "current"}
        for key, block in (archive.get("dates", {}) or {}).items():
            info.setdefault(key, {"count": block.get("count", 0), "status": "archived"})
        return {"total_dates": len(info), "dates": dict(sorted(info.items(), reverse=True))}
    return _cached_json("dates", version, build, max_age=300)


@app.get("/api/alexa/paper")
async def alexa_paper(category: str | None = None):
    if category and len(category) > 100:
        raise HTTPException(status_code=422, detail="category is too long")
    data = await run_in_threadpool(repository.current)
    papers = data.get("papers", [])
    if category:
        papers = [p for p in papers if category.lower() in str((p.get("conference_info", {}) or {}).get("category", "")).lower()]
    if not papers:
        return {"error": "No papers found", "speech": "Sorry, there are no papers available right now."}
    item = random.choice(papers)
    paper, analysis, conf = item.get("paper", {}), item.get("analysis", {}), item.get("conference_info", {})
    arxiv_id, title = str(paper.get("arxiv_id", "")), str(paper.get("title", "Untitled"))
    summary, problem = str(analysis.get("executive_summary", "")), str(analysis.get("problem_statement", ""))
    speech = f"Here's a recent research finding. The paper is titled: {title}."
    if problem:
        speech += f" It addresses: {problem}"
    return {"title": title, "conference": str(conf.get("conference", "")), "category": conf.get("category", "Other"),
            "arxiv_id": arxiv_id, "arxiv_url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else "",
            "problem_statement": problem, "executive_summary": summary, "speech": speech,
            "total_available": len(papers), "filter_date": data.get("filter_date")}


@app.get("/")
async def root():
    dashboard = BASE_DIR / "simple_dashboard.html"
    return FileResponse(dashboard) if dashboard.exists() else HTMLResponse("<h1>arXiv Conference Paper Agent</h1>")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, server_header=False, limit_concurrency=200, timeout_keep_alive=5)
