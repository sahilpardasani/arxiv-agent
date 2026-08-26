"""Read-optimized, process-local paper snapshots with optional shared Redis cache."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR))).resolve()


class PaperRepository:
    """Parse each immutable file version once per process.

    Writers must publish files with an atomic rename. Redis is only an acceleration
    layer: an unavailable cache never makes the API unavailable or stale forever.
    """

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = data_dir
        self._lock = threading.RLock()
        self._snapshots: dict[str, tuple[tuple[int, int, int], dict[str, Any]]] = {}
        self._redis = None
        redis_url = os.environ.get("REDIS_URL", "").strip()
        if redis_url:
            try:
                import redis

                self._redis = redis.Redis.from_url(
                    redis_url,
                    socket_connect_timeout=float(os.environ.get("REDIS_CONNECT_TIMEOUT", "0.2")),
                    socket_timeout=float(os.environ.get("REDIS_SOCKET_TIMEOUT", "0.5")),
                    health_check_interval=30,
                )
            except Exception:
                logger.warning("Redis cache disabled", exc_info=True)

    def _path(self, name: str) -> Path:
        if name not in {"papers.json", "papers_archive.json"}:
            raise ValueError("Unsupported data file")
        return self.data_dir / name

    def _signature(self, path: Path) -> tuple[int, int, int]:
        stat = path.stat()
        return (stat.st_ino, stat.st_mtime_ns, stat.st_size)

    def _redis_key(self, name: str, signature: tuple[int, int, int]) -> str:
        digest = hashlib.sha256(f"{name}:{signature}".encode()).hexdigest()
        return f"arxiv-agent:data:v1:{digest}"

    def get(self, name: str) -> dict[str, Any]:
        path = self._path(name)
        try:
            signature = self._signature(path)
        except OSError:
            return {}

        cached = self._snapshots.get(name)
        if cached and cached[0] == signature:
            return cached[1]

        with self._lock:
            cached = self._snapshots.get(name)
            if cached and cached[0] == signature:
                return cached[1]

            raw: bytes | None = None
            key = self._redis_key(name, signature)
            if self._redis is not None:
                try:
                    raw = self._redis.get(key)
                except Exception:
                    logger.warning("Redis cache read failed; using local file", exc_info=True)

            if raw is None:
                try:
                    raw = path.read_bytes()
                except OSError:
                    return {}
                if self._redis is not None:
                    try:
                        self._redis.setex(key, int(os.environ.get("CACHE_TTL_SECONDS", "300")), raw)
                    except Exception:
                        logger.warning("Redis cache write failed; continuing", exc_info=True)

            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                logger.error("Invalid JSON snapshot: %s", path)
                return cached[1] if cached else {}
            if not isinstance(value, dict):
                return {}
            self._snapshots[name] = (signature, value)
            return value

    def current(self) -> dict[str, Any]:
        return self.get("papers.json")

    def archive(self) -> dict[str, Any]:
        return self.get("papers_archive.json")

    def version(self) -> tuple[tuple[int, int, int] | None, tuple[int, int, int] | None]:
        versions = []
        for name in ("papers.json", "papers_archive.json"):
            try:
                versions.append(self._signature(self._path(name)))
            except OSError:
                versions.append(None)
        return (versions[0], versions[1])

    def ready(self) -> bool:
        current = self.current()
        return bool(current) and isinstance(current.get("papers"), list)


repository = PaperRepository()
