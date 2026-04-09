"""
FastAPI backend for Conference Paper Agent
Handles daily scheduling, persistence, and API endpoints
"""

from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import json
import os
from datetime import datetime
from pathlib import Path
import asyncio
import aiofiles
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Import the agent module
import sys
sys.path.insert(0, os.path.dirname(__file__))
from arxiv_agent import run_daily_pipeline, save_results

app = FastAPI(title="Conference Paper Agent API")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data file - look in current directory first (where arxiv_agent.py saves it)
PAPERS_FILE = Path("papers.json")
if not PAPERS_FILE.exists():
    # Also check in data directory as fallback
    DATA_DIR = Path("./data")
    DATA_DIR.mkdir(exist_ok=True)
    PAPERS_FILE = DATA_DIR / "papers.json"

# Initialize scheduler
scheduler = AsyncIOScheduler()


@app.on_event("startup")
async def startup_event():
    """Initialize scheduler on startup"""
    scheduler.add_job(daily_paper_analysis, 'cron', hour=0, minute=0)  # Run daily at midnight
    scheduler.start()
    print("✅ Scheduler started - daily analysis will run at 00:00")


@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown scheduler"""
    scheduler.shutdown()


async def daily_paper_analysis():
    """Background task: run the arXiv agent daily"""
    print(f"\n📡 Starting daily paper analysis at {datetime.now()}")
    try:
        results = await run_daily_pipeline()
        save_results(results, str(PAPERS_FILE))
        print(f"✅ Daily analysis complete: {len(results)} papers analyzed")
    except Exception as e:
        print(f"❌ Error in daily analysis: {e}")


@app.get("/")
async def root():
    """Serve the dashboard HTML"""
    try:
        # Try simple_dashboard.html first
        return FileResponse("simple_dashboard.html")
    except FileNotFoundError:
        try:
            # Fall back to dashboard.html
            return FileResponse("dashboard.html")
        except FileNotFoundError:
            # If neither exists, return error
            return JSONResponse(
                status_code=500,
                content={"error": "Dashboard HTML file not found"}
            )


@app.get("/api/papers")
async def get_papers():
    """Get all analyzed papers"""
    print(f"[DEBUG] Looking for papers at: {PAPERS_FILE}")
    print(f"[DEBUG] File exists: {PAPERS_FILE.exists()}")
    
    if PAPERS_FILE.exists():
        async with aiofiles.open(PAPERS_FILE, 'r') as f:
            content = await f.read()
            data = json.loads(content)
            print(f"[DEBUG] Found {len(data.get('papers', []))} papers")
            return JSONResponse(data)
    else:
        print(f"[DEBUG] File not found at {PAPERS_FILE}")
        print(f"[DEBUG] Current directory: {os.getcwd()}")
        print(f"[DEBUG] Files in current dir: {os.listdir('.')}")
    
    return {"papers": [], "last_updated": datetime.now().isoformat()}


@app.get("/api/papers/filter")
async def filter_papers(bottleneck: str = None, tag: str = None):
    """Filter papers by bottleneck or tag"""
    if not PAPERS_FILE.exists():
        return {"papers": []}
    
    async with aiofiles.open(PAPERS_FILE, 'r') as f:
        data = json.loads(await f.read())
    
    papers = data.get("papers", [])
    
    if bottleneck:
        papers = [p for p in papers if p["analysis"]["bottleneck_addressed"].lower() == bottleneck.lower()]
    
    if tag:
        papers = [p for p in papers if tag.lower() in [t.lower() for t in p["analysis"]["relevance_tags"]]]
    
    return {"papers": papers, "total": len(papers)}


@app.get("/api/bottlenecks")
async def get_bottlenecks():
    """Get list of all bottleneck types"""
    if not PAPERS_FILE.exists():
        return {"bottlenecks": []}
    
    async with aiofiles.open(PAPERS_FILE, 'r') as f:
        data = json.loads(await f.read())
    
    bottlenecks = list(set(p["analysis"]["bottleneck_addressed"] for p in data.get("papers", [])))
    return {"bottlenecks": sorted(bottlenecks)}


@app.post("/api/trigger-analysis")
async def trigger_analysis(background_tasks: BackgroundTasks):
    """Manually trigger paper analysis (admin endpoint)"""
    background_tasks.add_task(daily_paper_analysis)
    return {"status": "Analysis triggered", "message": "Check back in a few minutes"}


@app.get("/api/stats")
async def get_stats():
    """Get dashboard statistics"""
    if not PAPERS_FILE.exists():
        return {
            "total_papers": 0,
            "bottlenecks": {},
            "last_updated": None,
            "confidence_distribution": {}
        }
    
    async with aiofiles.open(PAPERS_FILE, 'r') as f:
        data = json.loads(await f.read())
    
    papers = data.get("papers", [])
    
    # Count by bottleneck
    bottlenecks = {}
    confidence_dist = {"high": 0, "medium": 0, "low": 0}
    
    for paper in papers:
        analysis = paper.get("analysis", {})
        bottleneck = analysis.get("bottleneck_addressed", "Unknown")
        bottlenecks[bottleneck] = bottlenecks.get(bottleneck, 0) + 1
        
        confidence = analysis.get("confidence", "medium")
        confidence_dist[confidence] = confidence_dist.get(confidence, 0) + 1
    
    return {
        "total_papers": len(papers),
        "bottlenecks": bottlenecks,
        "confidence_distribution": confidence_dist,
        "last_updated": data.get("last_updated")
    }


@app.get("/debug")
async def debug_info():
    """Debug endpoint - shows papers file location and content preview"""
    files_in_dir = os.listdir(".")
    papers_info = {
        "papers_file_path": str(PAPERS_FILE),
        "papers_file_exists": PAPERS_FILE.exists(),
        "files_in_directory": files_in_dir,
    }
    
    if PAPERS_FILE.exists():
        with open(PAPERS_FILE, 'r') as f:
            data = json.load(f)
            papers_info["papers_count"] = len(data.get("papers", []))
            papers_info["last_updated"] = data.get("last_updated")
            if data.get("papers"):
                papers_info["first_paper_title"] = data["papers"][0].get("paper", {}).get("title", "N/A")
    
    return papers_info


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "papers_file_exists": PAPERS_FILE.exists(),
        "papers_file_path": str(PAPERS_FILE),
        "current_directory": os.getcwd(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
