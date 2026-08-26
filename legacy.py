"""Bounded compatibility runtime for existing single-host, no-Redis deployments."""
from __future__ import annotations

import fcntl
import logging
import os
import threading
from pathlib import Path

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from data_store import DATA_DIR
from jobs import execute_pipeline

logger = logging.getLogger(__name__)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def legacy_mode_enabled() -> bool:
    """Redis always selects scalable mode; otherwise compatibility defaults on."""
    if os.environ.get("REDIS_URL", "").strip():
        return False
    return _bool_env("ENABLE_LEGACY_MODE", True)


class LegacyRuntime:
    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.data_dir = data_dir
        self.scheduler = None
        self._election_file = None
        self._thread_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_election = threading.Event()
        self._election_thread = None

    def start(self) -> bool:
        """Elect exactly one scheduler across local web worker processes."""
        if not legacy_mode_enabled() or self.scheduler is not None:
            return False
        if self._try_elect():
            return True
        # During rolling deploys, a new process can initially lose to the retiring
        # scheduler owner. Retry election so scheduling transfers after it exits.
        if self._election_thread is None or not self._election_thread.is_alive():
            self._stop_election.clear()
            self._election_thread = threading.Thread(target=self._election_loop, daemon=True, name="legacy-election")
            self._election_thread.start()
        return False

    def _election_loop(self) -> None:
        interval = max(1, int(os.environ.get("LEGACY_ELECTION_RETRY_SECONDS", "15")))
        while not self._stop_election.wait(interval):
            if self._try_elect():
                return

    def _try_elect(self) -> bool:
        if not legacy_mode_enabled():
            return False
        with self._state_lock:
            if self.scheduler is not None:
                return False
            return self._try_elect_locked()

    def _try_elect_locked(self) -> bool:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        election = (self.data_dir / ".legacy-scheduler.lock").open("a+")
        try:
            fcntl.flock(election.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            election.close()
            logger.info("Another web process owns the legacy scheduler election lock")
            return False
        self._election_file = election
        scheduler = BackgroundScheduler(timezone=pytz.timezone(os.environ.get("SCHEDULE_TIMEZONE", "America/New_York")))
        scheduler.add_job(
            lambda: self.dispatch("schedule"), "cron",
            hour=int(os.environ.get("SCHEDULE_HOUR", "23")), minute=int(os.environ.get("SCHEDULE_MINUTE", "30")),
            max_instances=1, coalesce=True, misfire_grace_time=300,
        )
        scheduler.start()
        self.scheduler = scheduler
        logger.warning("Legacy no-Redis scheduler enabled; migrate to separate scheduler/worker services")
        return True

    def stop(self) -> None:
        self._stop_election.set()
        if self._election_thread is not None and self._election_thread is not threading.current_thread():
            self._election_thread.join(timeout=2)
        self._election_thread = None
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None
        if self._election_file is not None:
            fcntl.flock(self._election_file.fileno(), fcntl.LOCK_UN)
            self._election_file.close()
            self._election_file = None

    def dispatch(self, source: str) -> bool:
        """Start one local analysis thread; a filesystem lock spans web processes."""
        if not legacy_mode_enabled() or not self._thread_lock.acquire(blocking=False):
            return False

        def run() -> None:
            lock_file = None
            try:
                lock_file = (self.data_dir / ".legacy-analysis.lock").open("a+")
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    logger.info("Legacy analysis is already running in another process")
                    return
                execute_pipeline({"source": source, "id": "legacy"})
            except Exception:
                logger.exception("Legacy pipeline failed")
            finally:
                if lock_file is not None:
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    finally:
                        lock_file.close()
                self._thread_lock.release()

        threading.Thread(target=run, name="legacy-analysis", daemon=True).start()
        return True


legacy_runtime = LegacyRuntime()
