#!/usr/bin/env python3
import json
import os
import subprocess
import base64
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
import aiofiles
from apscheduler.schedulers.background import BackgroundScheduler
import pytz


app = FastAPI()

# Serve dashboard
app.mount("/static", StaticFiles(directory=".", html=True), name="static")

EST = pytz.timezone('America/New_York')


def push_file_to_github(filepath: str):
    """Push a single file to GitHub via REST API."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")  # e.g. sahilpardasani/arxiv-agent

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
                sha = None  # File doesn't exist yet, will create it
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
    """Only push to GitHub if the pipeline actually saved data (safety guard check)."""
    if "SAFETY GUARD" in stdout:
        print("⚠️  Skipping GitHub push — pipeline returned 0 papers (safety guard triggered)")
        return
    push_file_to_github("papers.json")
    push_file_to_github("papers_archive.json")


# Background task for daily analysis
def daily_paper_analysis():
    """Run the daily arXiv paper analysis"""
    try:
        print("\n" + "="*60)
        print("🚀 Running scheduled daily analysis...")
        print("="*60)
        result = subprocess.run(
            ["python", "arxiv_agent.py"],
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ}
        )
        print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)
        print("✅ Daily analysis completed")

        # Only push to GitHub if pipeline produced data
        safe_push_to_github(result.stdout)

    except Exception as e:
        print(f"❌ Error running daily analysis: {e}")


# Initialize scheduler
scheduler = BackgroundScheduler(timezone=EST)
scheduler.add_job(daily_paper_analysis, 'cron', hour=0, minute=30, timezone=EST)
scheduler.start()

@app.on_event("startup")
async def startup_event():
    print(f"[{datetime.now().isoformat()}] Server started")
    print(f"Scheduler running: {scheduler.running}")

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.get("/api/papers")
async def get_papers(date: str = None):
    """
    Get papers by date

    Query parameters:
    - date: YYYY-MM-DD format (optional, defaults to yesterday)

    Examples:
    - /api/papers (gets yesterday's papers)
    - /api/papers?date=2026-04-09 (gets April 9's papers)
    """
    try:
        # Load papers.json (current/today's data)
        if os.path.exists("papers.json"):
            async with aiofiles.open("papers.json", "r") as f:
                content = await f.read()
                data = json.loads(content)
        else:
            return {"error": "No papers available yet", "total_papers": 0}

        # If no date specified, return today's papers
        if not date:
            return data

        # Load archive for historical data
        if os.path.exists("papers_archive.json"):
            async with aiofiles.open("papers_archive.json", "r") as f:
                content = await f.read()
                archive = json.loads(content)

            if date in archive.get("dates", {}):
                archive_data = archive["dates"][date]
                current_count = archive_data.get("count", 0)

                # Calculate previous day metrics from archive
                from datetime import datetime, timedelta
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
            return {
                "error": "Archive not available",
                "total_papers": 0
            }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/dates")
async def get_available_dates():
    """Get all available dates with paper counts"""
    try:
        dates_info = {}

        # Add current date (from papers.json)
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

        # Add historical dates from archive
        if os.path.exists("papers_archive.json"):
            async with aiofiles.open("papers_archive.json", "r") as f:
                content = await f.read()
                archive = json.loads(content)

            for date_key, date_data in archive.get("dates", {}).items():
                if date_key not in dates_info:
                    dates_info[date_key] = {}
                dates_info[date_key]["count"] = date_data.get("count", 0)
                dates_info[date_key]["status"] = "archived"

        # Sort dates in descending order (newest first)
        sorted_dates = sorted(dates_info.items(), key=lambda x: x[0], reverse=True)

        return {
            "total_dates": len(dates_info),
            "dates": dict(sorted_dates),
            "last_updated": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/trigger-analysis")
async def trigger_analysis(background_tasks: BackgroundTasks):
    """Manually trigger analysis — returns immediately, runs in background.
    This prevents cron-job.org from timing out while waiting for the pipeline."""
    background_tasks.add_task(daily_paper_analysis)
    print("\n" + "="*60)
    print("📤 Manual trigger received - running in background...")
    print("="*60)
    return {
        "status": "started",
        "message": "Analysis running in background",
        "timestamp": datetime.now().isoformat()
    }


@app.get("/api/alexa/paper")
async def get_alexa_paper(category: str = None):
    """
    Returns a random paper for Alexa, rotating so the same paper
    is never returned twice in a row for the same category.
    Query param: category (e.g. "Natural Language Processing")
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
        rotation = {}
        if os.path.exists(ROTATION_FILE):
            try:
                with open(ROTATION_FILE, "r") as f:
                    rotation = json.load(f)
            except Exception:
                rotation = {}
        rotation_key = (category or "all").lower().replace(" ", "_")
        last_index = rotation.get(rotation_key, -1)
        indices = list(range(len(filtered)))
        random.shuffle(indices)
        chosen_index = indices[0]
        if len(indices) > 1 and chosen_index == last_index:
            chosen_index = indices[1]
        paper_item = filtered[chosen_index]
        rotation[rotation_key] = chosen_index
        with open(ROTATION_FILE, "w") as f:
            json.dump(rotation, f)
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
            <head>
                <title>arXiv Conference Paper Agent</title>
                <style>
                    body { font-family: Arial; margin: 40px; }
                    .info { background: #f0f0f0; padding: 20px; border-radius: 5px; }
                </style>
            </head>
            <body>
                <h1>arXiv Conference Paper Agent</h1>
                <div class="info">
                    <p>Dashboard coming soon...</p>
                    <p><a href="/api/papers">View today's papers</a></p>
                    <p><a href="/api/dates">View all available dates</a></p>
                </div>
            </body>
        </html>
        """)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)