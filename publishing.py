"""Optional GitHub Contents publication for completed paper snapshots."""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
OUTPUT_NAMES = ("papers.json", "papers_archive.json")


def _publish_one(data_dir: Path, content_name: str, token: str, repo: str) -> None:
    """Read from DATA_DIR, while publishing to the repository-relative filename."""
    raw = (data_dir / content_name).read_bytes()
    encoded_name = urllib.parse.quote(content_name, safe="/")
    url = f"https://api.github.com/repos/{repo}/contents/{encoded_name}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        "User-Agent": "arxiv-agent-worker",
    }
    sha = None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=10) as response:
            sha = json.loads(response.read()).get("sha")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    payload = {
        "message": f"Auto-update {content_name} - {datetime.now(timezone.utc).isoformat()}",
        "content": base64.b64encode(raw).decode(),
    }
    if sha:
        payload["sha"] = sha
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="PUT", headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


def publish_outputs(stdout: str, data_dir: str | Path) -> bool:
    """Publish both outputs unless the pipeline safety guard prevented new data."""
    if "SAFETY GUARD" in stdout:
        logger.warning("Safety guard triggered; skipping GitHub publication")
        return False
    token, repo = os.environ.get("GITHUB_TOKEN", ""), os.environ.get("GITHUB_REPO", "")
    if not token or not repo:
        logger.info("GitHub publishing disabled because credentials/repository are not configured")
        return False
    try:
        directory = Path(data_dir).resolve()
        for name in OUTPUT_NAMES:
            _publish_one(directory, name, token, repo)
        return True
    except Exception:
        # Publishing was best-effort in the original deployment. A transient GitHub
        # failure must not rerun a paid analysis or corrupt the local snapshots.
        logger.exception("GitHub publication failed")
        return False
