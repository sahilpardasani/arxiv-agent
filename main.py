#!/usr/bin/env python3
import json
import os
import subprocess
import base64
import urllib.request
import urllib.error
import threading
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
import aiofiles
from apscheduler.schedulers.background import BackgroundScheduler
import pytz


app = FastAPI()

# Serve dashboard
app.mount("/static", StaticFiles(directory=".", html=True), name="static")

EST = pytz.timezone('America/New_York')

# Global lock to prevent concurrent pipeline runs
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
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json"
        }

        # Get current SHA (required for updates)
        req = urllib.request.Request(api_url, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                sha = json.loads(resp.read())["sha"]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                sha = None
            else:
                raise

        payload = {
            "message": f"📊 Auto-update {filepath} - {datetime.now().isoformat()}",
            "content": encoded,
        }
        if sha:
            payload["sha"] = sha

        data = json.dumps(payload).encode()
        req = urllib.request.Request(api_url, data=data, method="PUT", headers=headers)
        with urllib.request.urlopen(req) as resp:
            resp.read()

        print(f"✅ Pushed {filepath} to GitHub")
        return True

    except Exception as e:
        print(f"❌ Failed to push {filepath} to GitHub: {e}")
        return False


def safe_push_to_github(stdout: str):
    """Only push to GitHub if the pipeline actually saved data."""
    if "SAFETY GUARD" in stdout:
        print("⚠️  Skipping GitHub push — pipeline returned 0 papers (safety guard triggered)")
        return
    push_file_to_github("papers.json")
    push_file_to_github("papers_archive.json")


def run_pipeline_thread():
    """
    Run the pipeline in a proper daemon thread.
    Uses threading.Lock to prevent concurrent runs.
    Unlike FastAPI BackgroundTasks, daemon threads persist after
    the HTTP response is sent on Render.
    """
    global _pipeline_running

    if not _pipeline_lock.acquire(blocking=False):
        print("⚠️  Pipeline already running — skipping duplicate trigger")
        return

    _pipeline_running = True
    try:
        print("\n" + "="*60)
        print("🚀 Running pipeline in background thread...")
        print("="*60)

        result = subprocess.run(
            ["python", "arxiv_agent.py"],
            capture_output=True,
            text=True,
            timeout=1200,  # 20 minute timeout
            env={**os.environ}
        )

        print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)

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
    """Scheduled daily analysis — spawns a daemon thread."""
    t = threading.Thread(target=run_pipeline_thread, daemon=True)
    t.start()


# Initialize scheduler
scheduler = BackgroundScheduler(timezone=EST)
scheduler.add_job(daily_paper_analysis, 'cron', hour=23, minute=30, timezone=EST)
scheduler.start()


@app.on_event("startup")
async def startup_event():
    print(f"[{datetime.now().isoformat()}] Server started")
    print(f"Scheduler running: {scheduler.running}")


@app.get("/health")
async def health_check():
    """Health check endpoint — also used by cron-job.org to keep Render awake."""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "pipeline_running": _pipeline_running
    }


@app.post("/api/trigger-analysis")
async def trigger_analysis():
    """
    Trigger analysis — spawns a daemon thread and returns immediately.
    Uses threading.Thread instead of FastAPI BackgroundTasks to ensure
    the thread survives after the HTTP response is sent on Render.
    """
    global _pipeline_running

    if _pipeline_running:
        return {
            "status": "already_running",
            "message": "Pipeline is already running",
            "timestamp": datetime.now().isoformat()
        }

    t = threading.Thread(target=run_pipeline_thread, daemon=True)
    t.start()

    print("\n" + "="*60)
    print("📤 Manual trigger received - thread started")
    print("="*60)

    return {
        "status": "started",
        "message": "Analysis thread started",
        "timestamp": datetime.now().isoformat()
    }


@app.get("/api/papers")
async def get_papers(date: str = None):
    try:
        if os.path.exists("papers.json"):
            async with aiofiles.open("papers.json", "r") as f:
                content = await f.read()
                data = json.loads(content)
        else:
            return {"error": "No papers available yet", "total_papers": 0}

        if not date:
            return data

        if os.path.exists("papers_archive.json"):
            async with aiofiles.open("papers_archive.json", "r") as f:
                content = await f.read()
                archive = json.loads(content)

            if date in archive.get("dates", {}):
                archive_data = archive["dates"][date]
                current_count = archive_data.get("count", 0)

                prev_date = (datetime.fromisoformat(date) - timedelta(days=1)).date().isoformat()
                prev_data = archive.get("dates", {}).get(prev_date)
                previous_count = len(prev_data.get("papers", [])) if prev_data else 0
                day_change = current_count - previous_count

                return {
                    "last_updated": archive_data.get("updated_at"),
                    "total_papers": current_count,
                    "papers": archive_data.get("papers", []),
                    "categories": data.get("categories", []),
                    "filter_date": date,
                    "metrics": {
                        "dashboard": {
                            "current_date": date,
                            "current_count": current_count,
                            "previous_date": prev_date if prev_data else "unavailable",
                            "previous_count": previous_count,
                            "day_change": day_change,
                            "trend": "📈 UP" if day_change > 0 else ("📉 DOWN" if day_change < 0 else "➡️  STABLE")
                        }
                    }
                }
            else:
                return {
                    "error": f"No papers found for date {date}",
                    "available_dates": list(archive.get("dates", {}).keys()),
                    "total_papers": 0
                }
        else:
            return {"error": "Archive not available", "total_papers": 0}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/dates")
async def get_available_dates():
    try:
        dates_info = {}

        if os.path.exists("papers.json"):
            async with aiofiles.open("papers.json", "r") as f:
                content = await f.read()
                data = json.loads(content)
                filter_date = data.get("filter_date")
                if filter_date:
                    dates_info[filter_date] = {
                        "count": data.get("total_papers", 0),
                        "status": "current"
                    }

        if os.path.exists("papers_archive.json"):
            async with aiofiles.open("papers_archive.json", "r") as f:
                content = await f.read()
                archive = json.loads(content)

            for date_key, date_data in archive.get("dates", {}).items():
                if date_key not in dates_info:
                    dates_info[date_key] = {}
                dates_info[date_key]["count"] = date_data.get("count", 0)
                dates_info[date_key]["status"] = "archived"

        sorted_dates = sorted(dates_info.items(), key=lambda x: x[0], reverse=True)

        return {
            "total_dates": len(dates_info),
            "dates": dict(sorted_dates),
            "last_updated": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/alexa/paper")
async def get_alexa_paper(category: str = None):
    """
    Returns papers in rotation — cycles through all papers before repeating.
    For general queries: rotates through all papers.
    For category queries: rotates through papers in that category only.
    """
    import random
    ROTATION_FILE = "alexa_rotation.json"
    try:
        if not os.path.exists("papers.json"):
            return {
                "error": "No papers available yet",
                "speech": "Sorry, I don't have any papers available right now."
            }
        async with aiofiles.open("papers.json", "r") as f:
            raw = await f.read()
            data = json.loads(raw)
        all_papers = data.get("papers", [])
        if not all_papers:
            return {"error": "No papers found", "speech": "Sorry, there are no papers available right now."}

        if category:
            filtered = [
                p for p in all_papers
                if category.lower() in (p.get("conference_info", {}).get("category", "") or "").lower()
            ]
            if not filtered:
                available = list(set(
                    p.get("conference_info", {}).get("category", "Other") for p in all_papers
                ))
                cats = ", ".join(available[:3])
                return {
                    "error": "No papers found for category " + category,
                    "available_categories": available,
                    "speech": "Sorry, I could not find papers in " + category + ". Available categories today include " + cats + " and more."
                }
        else:
            filtered = all_papers

        # Load rotation state
        rotation = {}
        if os.path.exists(ROTATION_FILE):
            try:
                with open(ROTATION_FILE, "r") as f:
                    rotation = json.load(f)
            except Exception:
                rotation = {}

        rotation_key = (category or "all").lower().replace(" ", "_")
        queue_key = rotation_key + "_queue"
        current_queue = rotation.get(queue_key, [])

        # If queue is empty or exhausted, rebuild it shuffled
        if not current_queue:
            indices = list(range(len(filtered)))
            random.shuffle(indices)
            current_queue = indices

        # Pop next index from front of queue
        chosen_index = current_queue.pop(0)

        # Guard against stale index if paper count changed
        if chosen_index >= len(filtered):
            indices = list(range(len(filtered)))
            random.shuffle(indices)
            current_queue = indices
            chosen_index = current_queue.pop(0)

        rotation[queue_key] = current_queue
        with open(ROTATION_FILE, "w") as f:
            json.dump(rotation, f)

        paper_item = filtered[chosen_index]
        paper = paper_item.get("paper", {})
        analysis = paper_item.get("analysis", {})
        conf_info = paper_item.get("conference_info", {})
        title = paper.get("title", "Untitled")
        conference = conf_info.get("conference", "a top conference")
        year = conf_info.get("year", "")
        conf_display = (conference + " " + year).strip()
        problem = analysis.get("problem_statement", "")
        summary = analysis.get("executive_summary", "")
        arxiv_id = paper.get("arxiv_id", "")
        arxiv_url = "https://arxiv.org/abs/" + arxiv_id if arxiv_id else ""

        speech_parts = ["Here's a recent research finding"]
        if category:
            speech_parts.append("in " + category)
        speech_parts.append("from " + conf_display + ".")
        speech_parts.append("The paper is titled: " + title + ".")
        if problem:
            speech_parts.append("It addresses: " + problem)
        if summary:
            sentences = summary.split(". ")
            short_summary = ". ".join(sentences[:2]) + "."
            speech_parts.append(short_summary)
        speech_parts.append("You can find this paper on arXiv.")
        speech = " ".join(speech_parts)

        return {
            "title": title,
            "conference": conf_display,
            "category": conf_info.get("category", "Other"),
            "arxiv_id": arxiv_id,
            "arxiv_url": arxiv_url,
            "problem_statement": problem,
            "executive_summary": summary,
            "speech": speech,
            "total_available": len(filtered),
            "papers_remaining_in_queue": len(current_queue),
            "filter_date": data.get("filter_date")
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/")
async def root():
    """Serve the dashboard"""
    if os.path.exists("simple_dashboard.html"):
        return FileResponse("simple_dashboard.html")
    else:
        return HTMLResponse("""
        <html>
            <head><title>arXiv Conference Paper Agent</title></head>
            <body>
                <h1>arXiv Conference Paper Agent</h1>
                <p><a href="/api/papers">View today's papers</a></p>
                <p><a href="/api/dates">View all available dates</a></p>
            </body>
        </html>
        """)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)