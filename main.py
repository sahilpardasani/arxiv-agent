#!/usr/bin/env python3
import json
import os
import subprocess
import base64
import urllib.request
import urllib.error
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

        # Push updated data to GitHub so it persists across deploys
        push_file_to_github("papers.json")
        push_file_to_github("papers_archive.json")
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
                return {
                    "last_updated": archive_data.get("updated_at"),
                    "total_papers": archive_data.get("count", 0),
                    "papers": archive_data.get("papers", []),
                    "categories": data.get("categories", []),
                    "filter_date": date,
                    "metrics": {
                        "dashboard": {
                            "date": date,
                            "total_papers": archive_data.get("count", 0),
                            "note": "Historical data from archive"
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
async def trigger_analysis():
    """Manually trigger analysis (for cron jobs)"""
    try:
        print("\n" + "="*60)
        print("📤 Manual trigger received - running analysis...")
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

        # Push updated data to GitHub so it persists across deploys
        push_file_to_github("papers.json")
        push_file_to_github("papers_archive.json")

        return {
            "status": "success",
            "message": "Analysis completed",
            "timestamp": datetime.now().isoformat()
        }
    
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Analysis timed out")
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