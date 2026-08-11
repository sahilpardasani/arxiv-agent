#!/usr/bin/env python3
import json
import os
import subprocess
import base64
import urllib.request
import urllib.error
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
import aiofiles
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

from mcp_server import mcp, mcp_app

EST = pytz.timezone('America/New_York')
_pipeline_lock = threading.Lock()
_pipeline_running = False


def push_file_to_github(filepath: str):
    """Push a single file to GitHub via REST API."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if not token or not repo:
        print(f"⚠️  GITHUB_TOKEN or GITHUB_REPO not set — skipping GitHub push for {filepath}")
        return False
    try:
        with open(filepath, 'r') as f:
            content = f.read()
        encoded = base64.b64encode(content.encode()).decode()
        api_url = f"https://api.github.com/repos/{repo}/contents/{filepath}"
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json", "Content-Type": "application/json"}
        req = urllib.request.Request(api_url, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                sha = json.loads(resp.read())["sha"]
        except urllib.error.HTTPError as e:
            if e.code == 404: sha = None
            else: raise
        payload = {"message": f"📊 Auto-update {filepath} - {datetime.now().isoformat()}", "content": encoded}
        if sha: payload["sha"] = sha
        data = json.dumps(payload).encode()
        req = urllib.request.Request(api_url, data=data, method="PUT", headers=headers)
        with urllib.request.urlopen(req) as resp: resp.read()
        print(f"✅ Pushed {filepath} to GitHub")
        return True
    except Exception as e:
        print(f"❌ Failed to push {filepath} to GitHub: {e}")
        return False


def safe_push_to_github(stdout: str):
    if "SAFETY GUARD" in stdout:
        print("⚠️  Skipping GitHub push — pipeline returned 0 papers (safety guard triggered)")
        return
    push_file_to_github("papers.json")
    push_file_to_github("papers_archive.json")


def run_pipeline_thread():
    global _pipeline_running
    if not _pipeline_lock.acquire(blocking=False):
        print("⚠️  Pipeline already running — skipping duplicate trigger")
        return
    _pipeline_running = True
    try:
        print("\n" + "="*60); print("🚀 Running pipeline in background thread..."); print("="*60)
        result = subprocess.run(["python", "arxiv_agent.py"], capture_output=True, text=True, timeout=1200, env={**os.environ})
        print(result.stdout)
        if result.stderr: print("STDERR:", result.stderr)
        print("✅ Pipeline completed")
        safe_push_to_github(result.stdout)
    except subprocess.TimeoutExpired:
        print("❌ Pipeline timed out after 20 minutes")
    except Exception as e:
        print(f"❌ Pipeline error: {e}")
    finally:
        _pipeline_running = False
        _pipeline_lock.release()


def daily_paper_analysis():
    t = threading.Thread(target=run_pipeline_thread, daemon=True)
    t.start()


scheduler = BackgroundScheduler(timezone=EST)
scheduler.add_job(daily_paper_analysis, 'cron', hour=23, minute=30, timezone=EST)
scheduler.start()


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """Run MCP session management without changing the existing paper pipeline."""
    print(f"[{datetime.now().isoformat()}] Server started")
    print(f"Scheduler running: {scheduler.running}")
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=app_lifespan)
app.mount("/mcp", mcp_app)
app.mount("/static", StaticFiles(directory=".", html=True), name="static")


@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat(), "pipeline_running": _pipeline_running}


@app.post("/api/trigger-analysis")
async def trigger_analysis():
    global _pipeline_running
    if _pipeline_running:
        return {"status": "already_running", "message": "Pipeline is already running", "timestamp": datetime.now().isoformat()}
    t = threading.Thread(target=run_pipeline_thread, daemon=True); t.start()
    print("\n" + "="*60); print("📤 Manual trigger received - thread started"); print("="*60)
    return {"status": "started", "message": "Analysis thread started", "timestamp": datetime.now().isoformat()}


@app.get("/api/papers")
async def get_papers(date: str = None):
    try:
        if os.path.exists("papers.json"):
            async with aiofiles.open("papers.json", "r") as f: data = json.loads(await f.read())
        else:
            return {"error": "No papers available yet", "total_papers": 0}
        if not date: return data
        if os.path.exists("papers_archive.json"):
            async with aiofiles.open("papers_archive.json", "r") as f: archive = json.loads(await f.read())
            if date in archive.get("dates", {}):
                archive_data = archive["dates"][date]; current_count = archive_data.get("count", 0)
                prev_date = (datetime.fromisoformat(date) - timedelta(days=1)).date().isoformat()
                prev_data = archive.get("dates", {}).get(prev_date); previous_count = len(prev_data.get("papers", [])) if prev_data else 0
                day_change = current_count - previous_count
                return {"last_updated": archive_data.get("updated_at"), "total_papers": current_count, "papers": archive_data.get("papers", []),
                        "categories": data.get("categories", []), "filter_date": date,
                        "metrics": {"dashboard": {"current_date": date, "current_count": current_count,
                        "previous_date": prev_date if prev_data else "unavailable", "previous_count": previous_count,
                        "day_change": day_change, "trend": "📈 UP" if day_change > 0 else ("📉 DOWN" if day_change < 0 else "➡️  STABLE")}}}
            return {"error": f"No papers found for date {date}", "available_dates": list(archive.get("dates", {}).keys()), "total_papers": 0}
        return {"error": "Archive not available", "total_papers": 0}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/dates")
async def get_available_dates():
    try:
        dates_info = {}
        if os.path.exists("papers.json"):
            async with aiofiles.open("papers.json", "r") as f: data = json.loads(await f.read())
            filter_date = data.get("filter_date")
            if filter_date: dates_info[filter_date] = {"count": data.get("total_papers", 0), "status": "current"}
        if os.path.exists("papers_archive.json"):
            async with aiofiles.open("papers_archive.json", "r") as f: archive = json.loads(await f.read())
            for date_key, date_data in archive.get("dates", {}).items():
                if date_key not in dates_info: dates_info[date_key] = {}
                dates_info[date_key]["count"] = date_data.get("count", 0); dates_info[date_key]["status"] = "archived"
        sorted_dates = sorted(dates_info.items(), key=lambda x: x[0], reverse=True)
        return {"total_dates": len(dates_info), "dates": dict(sorted_dates), "last_updated": datetime.now().isoformat()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/alexa/paper")
async def get_alexa_paper(category: str = None):
    import random
    ROTATION_FILE = "alexa_rotation.json"
    try:
        if not os.path.exists("papers.json"):
            return {"error": "No papers available yet", "speech": "Sorry, I don't have any papers available right now."}
        async with aiofiles.open("papers.json", "r") as f: data = json.loads(await f.read())
        all_papers = data.get("papers", [])
        if not all_papers: return {"error": "No papers found", "speech": "Sorry, there are no papers available right now."}
        if category:
            filtered = [p for p in all_papers if category.lower() in (p.get("conference_info", {}).get("category", "") or "").lower()]
            if not filtered:
                available = list(set(p.get("conference_info", {}).get("category", "Other") for p in all_papers)); cats = ", ".join(available[:3])
                return {"error": "No papers found for category " + category, "available_categories": available,
                        "speech": "Sorry, I could not find papers in " + category + ". Available categories today include " + cats + " and more."}
        else: filtered = all_papers
        rotation = {}
        if os.path.exists(ROTATION_FILE):
            try:
                with open(ROTATION_FILE, "r") as f: rotation = json.load(f)
            except Exception: rotation = {}
        rotation_key = (category or "all").lower().replace(" ", "_"); queue_key = rotation_key + "_queue"; current_queue = rotation.get(queue_key, [])
        if not current_queue:
            current_queue = list(range(len(filtered))); random.shuffle(current_queue)
        chosen_index = current_queue.pop(0)
        if chosen_index >= len(filtered):
            current_queue = list(range(len(filtered))); random.shuffle(current_queue); chosen_index = current_queue.pop(0)
        rotation[queue_key] = current_queue
        with open(ROTATION_FILE, "w") as f: json.dump(rotation, f)
        paper_item = filtered[chosen_index]; paper = paper_item.get("paper", {}); analysis = paper_item.get("analysis", {}); conf_info = paper_item.get("conference_info", {})
        title = paper.get("title", "Untitled"); conference = conf_info.get("conference", "a top conference"); year = conf_info.get("year", "")
        conf_display = (conference + " " + year).strip(); problem = analysis.get("problem_statement", ""); summary = analysis.get("executive_summary", "")
        arxiv_id = paper.get("arxiv_id", ""); arxiv_url = "https://arxiv.org/abs/" + arxiv_id if arxiv_id else ""
        speech_parts = ["Here's a recent research finding"]
        if category: speech_parts.append("in " + category)
        speech_parts.append("from " + conf_display + "."); speech_parts.append("The paper is titled: " + title + ".")
        if problem: speech_parts.append("It addresses: " + problem)
        if summary: speech_parts.append(". ".join(summary.split(". ")[:2]) + ".")
        speech_parts.append("You can find this paper on arXiv.")
        return {"title": title, "conference": conf_display, "category": conf_info.get("category", "Other"), "arxiv_id": arxiv_id,
                "arxiv_url": arxiv_url, "problem_statement": problem, "executive_summary": summary, "speech": " ".join(speech_parts),
                "total_available": len(filtered), "papers_remaining_in_queue": len(current_queue), "filter_date": data.get("filter_date")}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def root():
    if os.path.exists("simple_dashboard.html"):
        return FileResponse("simple_dashboard.html")
    return HTMLResponse("""<html><head><title>arXiv Conference Paper Agent</title></head><body><h1>arXiv Conference Paper Agent</h1><p><a href=\"/api/papers\">View today's papers</a></p><p><a href=\"/api/dates\">View all available dates</a></p></body></html>""")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
