"""Read-only MCP interface for the arXiv Conference Paper Agent.

This module deliberately does NOT import or call arxiv_agent.py. It only reads
papers.json and papers_archive.json, so MCP clients cannot trigger the Groq
analysis pipeline, mutate the archive, or push to GitHub.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

BASE_DIR = Path(__file__).resolve().parent
PAPERS_FILE = BASE_DIR / "papers.json"
ARCHIVE_FILE = BASE_DIR / "papers_archive.json"

mcp = MCPServer("arXiv Conference Research")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _current_data() -> dict[str, Any]:
    return _read_json(PAPERS_FILE)


def _archive_data() -> dict[str, Any]:
    return _read_json(ARCHIVE_FILE)


def _all_papers(include_archive: bool = True) -> list[dict[str, Any]]:
    """Return newest copy of each arXiv ID, deduplicated across the archive."""
    current = _current_data()
    archive = _archive_data() if include_archive else {}
    candidates: list[tuple[str, dict[str, Any]]] = []
    current_date = str(current.get("filter_date") or "")
    for item in current.get("papers", []) or []:
        if isinstance(item, dict):
            candidates.append((current_date, item))
    if include_archive:
        for date, block in (archive.get("dates", {}) or {}).items():
            if not isinstance(block, dict):
                continue
            for item in block.get("papers", []) or []:
                if isinstance(item, dict):
                    candidates.append((str(date), item))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for source_date, item in candidates:
        paper = item.get("paper", {}) or {}
        arxiv_id = str(paper.get("arxiv_id") or "").strip()
        dedupe_key = arxiv_id or str(paper.get("title") or "").strip().lower()
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        copied = dict(item)
        copied["source_date"] = source_date
        output.append(copied)
    return output


def _paper_text(item: dict[str, Any]) -> str:
    paper = item.get("paper", {}) or {}
    analysis = item.get("analysis", {}) or {}
    conf = item.get("conference_info", {}) or {}
    tech = analysis.get("technical_breakdown", {}) or {}
    metrics = analysis.get("key_metrics", {}) or {}
    authors = paper.get("authors", [])
    authors_text = " ".join(str(a) for a in authors) if isinstance(authors, list) else str(authors or "")
    tags = analysis.get("relevance_tags", [])
    tags_text = " ".join(str(t) for t in tags) if isinstance(tags, list) else str(tags or "")
    fields = [paper.get("title"), paper.get("summary"), paper.get("categories"), paper.get("comment"), authors_text,
              analysis.get("problem_statement"), analysis.get("bottleneck_addressed"), analysis.get("executive_summary"), tags_text,
              metrics.get("primary_metric"), metrics.get("baseline"), tech.get("method"), tech.get("architecture"),
              tech.get("implementation_details"), conf.get("conference"), conf.get("year"), conf.get("category"), conf.get("rank")]
    return " ".join(str(v) for v in fields if v).lower()


def _terms(query: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9][a-z0-9.+#_-]*", query.lower()) if len(t) > 1]


def _score(item: dict[str, Any], query: str) -> float:
    terms = _terms(query)
    if not terms:
        return 1.0
    paper = item.get("paper", {}) or {}
    analysis = item.get("analysis", {}) or {}
    conf = item.get("conference_info", {}) or {}
    title = str(paper.get("title") or "").lower()
    tags_raw = analysis.get("relevance_tags", []) or []
    tags = " ".join(str(x) for x in tags_raw).lower() if isinstance(tags_raw, list) else str(tags_raw).lower()
    conference = " ".join(str(conf.get(k) or "") for k in ("conference", "category", "rank")).lower()
    haystack = _paper_text(item)
    score = 0.0
    query_lower = query.lower().strip()
    if query_lower and query_lower in title:
        score += 12.0
    elif query_lower and query_lower in haystack:
        score += 6.0
    for term in terms:
        if term in title: score += 5.0
        if term in tags: score += 4.0
        if term in conference: score += 3.0
        score += min(haystack.count(term), 4)
    score += sum(1 for term in set(terms) if term in haystack) * 1.5
    return score


def _compact(item: dict[str, Any]) -> dict[str, Any]:
    paper = item.get("paper", {}) or {}
    analysis = item.get("analysis", {}) or {}
    conf = item.get("conference_info", {}) or {}
    arxiv_id = str(paper.get("arxiv_id") or "")
    return {"id": arxiv_id, "title": paper.get("title"), "authors": paper.get("authors", []), "published": paper.get("published"),
            "source_date": item.get("source_date"), "conference": conf.get("conference"), "conference_year": conf.get("year"),
            "field": conf.get("category"), "rank": conf.get("rank"), "problem_statement": analysis.get("problem_statement"),
            "executive_summary": analysis.get("executive_summary"), "relevance_tags": analysis.get("relevance_tags", []),
            "url": f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None}


@mcp.tool()
def search(query: str, limit: int = 10, include_archive: bool = True) -> dict[str, Any]:
    """Search curated, conference-linked arXiv papers and their AI-generated analyses."""
    limit = max(1, min(int(limit), 50))
    scored = [(_score(item, query), item) for item in _all_papers(include_archive)]
    scored = [(s, item) for s, item in scored if s > 0]
    scored.sort(key=lambda pair: (pair[0], pair[1].get("source_date", "")), reverse=True)
    results = []
    for score, item in scored[:limit]:
        result = _compact(item)
        result["search_score"] = round(score, 2)
        results.append(result)
    return {"query": query, "count": len(results), "results": results}


@mcp.tool()
def fetch(id: str) -> dict[str, Any]:
    """Fetch a complete analyzed record by arXiv ID, with or without a version suffix."""
    wanted = id.strip().lower()
    wanted_base = re.sub(r"v\d+$", "", wanted)
    for item in _all_papers(include_archive=True):
        paper = item.get("paper", {}) or {}
        arxiv_id = str(paper.get("arxiv_id") or "")
        candidate = arxiv_id.lower()
        if candidate == wanted or re.sub(r"v\d+$", "", candidate) == wanted_base:
            result = dict(item)
            result["url"] = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None
            return result
    return {"error": "Paper not found", "id": id}


@mcp.tool()
def recent_papers(date: str | None = None, conference: str | None = None, field: str | None = None,
                  rank: str | None = None, limit: int = 20) -> dict[str, Any]:
    """List recent curated papers, optionally filtered by date, conference, field, or rank."""
    limit = max(1, min(int(limit), 100))
    items = _all_papers(include_archive=True)
    def matches(item: dict[str, Any]) -> bool:
        conf = item.get("conference_info", {}) or {}
        if date and item.get("source_date") != date: return False
        if conference and conference.lower() not in str(conf.get("conference") or "").lower(): return False
        if field and field.lower() not in str(conf.get("category") or "").lower(): return False
        if rank and rank.lower() != str(conf.get("rank") or "").lower(): return False
        return True
    filtered = [item for item in items if matches(item)]
    filtered.sort(key=lambda item: item.get("source_date", ""), reverse=True)
    return {"count": min(len(filtered), limit), "total_matching": len(filtered), "papers": [_compact(item) for item in filtered[:limit]]}


@mcp.tool()
def available_dates() -> dict[str, Any]:
    """Return dates currently available in the rolling paper archive and paper counts."""
    current = _current_data()
    archive = _archive_data()
    counts: dict[str, int] = {}
    for date, block in (archive.get("dates", {}) or {}).items():
        if isinstance(block, dict):
            counts[str(date)] = int(block.get("count", len(block.get("papers", []) or [])) or 0)
    current_date = current.get("filter_date")
    if current_date:
        counts[str(current_date)] = int(current.get("total_papers", len(current.get("papers", []) or [])) or 0)
    ordered = dict(sorted(counts.items(), reverse=True))
    return {"current_date": current_date, "last_updated": current.get("last_updated"), "total_dates": len(ordered), "dates": ordered}


@mcp.tool()
def paper_stats(date: str | None = None) -> dict[str, Any]:
    """Summarize paper counts by conference, research field, rank, and arXiv category."""
    items = _all_papers(include_archive=True)
    if date:
        items = [item for item in items if item.get("source_date") == date]
    conferences: Counter[str] = Counter(); fields: Counter[str] = Counter(); ranks: Counter[str] = Counter(); arxiv_categories: Counter[str] = Counter()
    for item in items:
        paper = item.get("paper", {}) or {}; conf = item.get("conference_info", {}) or {}
        if conf.get("conference"): conferences[str(conf["conference"])] += 1
        if conf.get("category"): fields[str(conf["category"])] += 1
        if conf.get("rank"): ranks[str(conf["rank"])] += 1
        if paper.get("categories"): arxiv_categories[str(paper["categories"])] += 1
    return {"date": date or "rolling_archive", "total_unique_papers": len(items), "by_conference": dict(conferences.most_common()),
            "by_field": dict(fields.most_common()), "by_rank": dict(ranks.most_common()), "by_arxiv_category": dict(arxiv_categories.most_common())}


def _allowed_hosts() -> list[str]:
    hosts = {"localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"}
    render_host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if render_host:
        hosts.add(render_host); hosts.add(f"{render_host}:*")
    for host in os.getenv("MCP_ALLOWED_HOSTS", "").split(","):
        host = host.strip()
        if host:
            hosts.add(host)
            if ":" not in host: hosts.add(f"{host}:*")
    return sorted(hosts)


def _allowed_origins() -> list[str]:
    origins = set()
    render_host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if render_host: origins.add(f"https://{render_host}")
    for origin in os.getenv("MCP_ALLOWED_ORIGINS", "").split(","):
        origin = origin.strip()
        if origin: origins.add(origin)
    return sorted(origins)


transport_security = TransportSecuritySettings(allowed_hosts=_allowed_hosts(), allowed_origins=_allowed_origins())
mcp_app = mcp.streamable_http_app(streamable_http_path="/", transport_security=transport_security)
