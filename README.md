# 🚀 Conference Paper Agent - Daily AI Research Digest

A fully automated system that:
1. **Fetches papers daily** from arXiv filtered by conference/workshop acceptance
2. **Analyzes with Claude** to extract problem statements, bottlenecks, and technical breakdowns
3. **Delivers via live dashboard** that updates automatically every 24 hours

## 📊 What It Does

For each paper, the system generates:

| Section | Content |
|---------|---------|
| **Problem Statement** | One sentence defining the core issue |
| **Bottleneck Addressed** | Which AI limitation it tackles (memory, latency, etc.) |
| **Executive Summary** | Why it matters to the AI landscape right now |
| **Key Metrics** | Quantified improvements (e.g., "6-7x memory reduction") |
| **Technical Breakdown** | For engineers: methods, architecture, implementation details |

## 🏗️ Architecture

```
arXiv API
    ↓
[Daily Scheduler]
    ↓
[Paper Filtering] → Conference/Workshop mentions?
    ↓
[Claude Analysis] → Problem, bottleneck, technical details
    ↓
[PostgreSQL] → Store results
    ↓
[FastAPI Server]
    ↓
[Web Dashboard] ← Users see updated papers
```

## 🚀 Quick Start (Local Development)

### Prerequisites
- Python 3.11+
- `ANTHROPIC_API_KEY` environment variable set

### Installation

```bash
# Clone/setup your directory
cd conference-paper-agent

# Install dependencies
pip install -r requirements.txt

# Set your API key
export ANTHROPIC_API_KEY="sk-ant-..."

# Run the agent manually (test)
python arxiv_agent.py

# Run the FastAPI server
python -m uvicorn main:app --reload --port 8000
```

Visit: `http://localhost:8000`

The dashboard will load with mock data initially. To populate with real papers, hit the "Refresh Papers" button or POST to `/api/trigger-analysis`.

## 📦 Docker Deployment

### Option 1: Single Container

```bash
# Build the image
docker build -t arxiv-agent:latest .

# Run the container
docker run -it \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  arxiv-agent:latest
```

### Option 2: Docker Compose (Recommended)

```bash
# Create .env file with your API key
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env

# Launch the full stack (API + Nginx)
docker-compose up -d

# View logs
docker-compose logs -f arxiv-agent
```

The service will be available at `http://localhost` or `http://localhost:8000/api/papers`

## 🌍 Cloud Deployment

### Deploy to Render.com

1. **Push code to GitHub**
   ```bash
   git init
   git add .
   git commit -m "initial commit"
   git push -u origin main
   ```

2. **Connect to Render**
   - Go to https://render.com
   - Create new Web Service
   - Connect your GitHub repo
   - Set environment variables:
     - `ANTHROPIC_API_KEY`: Your key
     - `PYTHON_VERSION`: 3.11
   - Deploy!

3. **Schedule daily runs**
   - Render → Cron Jobs → Create Job
   - Cron expression: `0 0 * * *` (daily at midnight)
   - Target: `POST /api/trigger-analysis`

### Deploy to Railway.app

```bash
# Install Railway CLI
npm i -g @railway/cli

# Login
railway login

# Initialize project
railway init

# Deploy
railway up
```

### Deploy to AWS Lambda + EventBridge

1. Containerize with AWS Lambda base image
2. Push to ECR
3. Create Lambda function from container
4. Add EventBridge trigger (cron: `cron(0 0 * * ? *)`)

## 🔧 Configuration

### Daily Schedule

Edit `main.py` scheduler:
```python
scheduler.add_job(daily_paper_analysis, 'cron', hour=0, minute=0)  # Change time here
```

### arXiv Categories

Edit `arxiv_agent.py` to focus on specific areas:
```python
ARXIV_CATEGORIES = [
    "cs.AI",      # Artificial Intelligence
    "cs.LG",      # Machine Learning
    # Add/remove categories as needed
]
```

### Conference Keywords

Customize which conferences to track:
```python
CONFERENCE_KEYWORDS = {
    "NeurIPS", "ICML", "ICLR", "CVPR",  # Add your priorities
    # ...
}
```

## 📡 API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard HTML |
| `/api/papers` | GET | All analyzed papers |
| `/api/papers/filter` | GET | Filter by `?bottleneck=Memory` or `?tag=quantization` |
| `/api/bottlenecks` | GET | List of all bottleneck types |
| `/api/stats` | GET | Dashboard statistics |
| `/api/trigger-analysis` | POST | Manually run analysis (useful for testing) |
| `/health` | GET | Health check |

### Example Requests

```bash
# Get all papers
curl http://localhost:8000/api/papers

# Filter by bottleneck
curl http://localhost:8000/api/papers/filter?bottleneck=Memory

# Get statistics
curl http://localhost:8000/api/stats

# Trigger analysis
curl -X POST http://localhost:8000/api/trigger-analysis
```

## 💾 Data Storage

Papers are stored in `./data/papers.json`:
```json
{
  "last_updated": "2024-01-15T14:32:00",
  "total_papers": 42,
  "papers": [
    {
      "paper": { ... },
      "analysis": { ... },
      "analyzed_at": "2024-01-15T..."
    }
  ]
}
```

## 🚨 Troubleshooting

### Papers not updating?
1. Check logs: `docker-compose logs arxiv-agent`
2. Verify ANTHROPIC_API_KEY is set
3. Manually trigger: `curl -X POST http://localhost:8000/api/trigger-analysis`
4. Check rate limits on arXiv API

### Dashboard not loading?
1. Verify Nginx is running: `docker-compose ps`
2. Check port 80/8000 not in use: `lsof -i :8000`
3. Clear browser cache (Ctrl+Shift+Delete)

### High API costs?
- Reduce `max_results` in `arxiv_agent.py` fetch_arxiv_papers()
- Run analysis less frequently (adjust cron schedule)
- Filter to fewer categories

## 📈 Next Steps (Advanced)

1. **Add Database** → PostgreSQL instead of JSON file
   - Enable historical tracking
   - Full-text search
   - User saved papers

2. **Email/Slack Delivery**
   - New paper alerts via email
   - Slack bot with paper summaries
   - Digest every Monday morning

3. **Multi-Language** → Translate summaries to other languages

4. **Trending Analysis** → Show which bottlenecks papers are addressing most

5. **User Ratings** → Store which papers users found most useful

6. **Paper Similarity** → Recommend related papers using embeddings

## 🤖 How Claude Analysis Works

The system uses Claude's vision to:
1. **Extract problem statements** from paper titles + abstracts
2. **Categorize bottlenecks** (memory, latency, throughput, context, etc.)
3. **Map to current landscape** (why it matters now)
4. **Technical deep-dive** (how the solution actually works)

The prompt is carefully crafted to force structured JSON output for reliability.

## 📝 Example Output

**Paper**: TurboQuant: Efficient LLM Quantization

**Problem**: LLMs require massive memory, limiting deployment

**Bottleneck**: Memory usage

**Executive Summary**: Achieves 6-7x memory reduction, enabling larger models on consumer hardware

**Technical Breakdown**:
- Method: Dynamic mixed-precision (INT6/INT4 with selective FP16)
- Architecture: Sensitivity-aware quantization scheduler
- Implementation: INT4 for attention, INT6 for FFN, FP16 for skip connections
- Complexity: O(n) with single forward pass

---

## 💡 Pro Tips

- **Cost Optimization**: Use Claude 3 Haiku for initial filtering, save Claude 3 Opus for detailed analysis
- **Caching**: Cache paper summaries if analyzing same papers multiple times
- **Parallel Processing**: Analyze multiple papers concurrently with `asyncio.gather()`
- **Monitoring**: Add Sentry/Datadog for production error tracking

## 📄 License

MIT - Use and modify freely!

## 🤝 Contributing

Issues and PRs welcome! Especially:
- New conference keywords to track
- Better bottleneck categorization
- Dashboard improvements
- Deployment guides for other platforms
