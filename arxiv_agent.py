#!/usr/bin/env python3
import os
import json
import re
import asyncio
import time
from datetime import datetime, date, timedelta
from typing import Optional
import feedparser
import aiohttp
from groq import Groq

# Initialize Groq client
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Conference categories by field
CONFERENCE_CATEGORIES = {
    "General ML/AI": {
        "NeurIPS", "NEURIPS", "ICML", "ICLR", "AAAI", "IJCAI", "UAI",
        "COLM", "AISTATS", "AMLDS", "International Conference on Advanced Machine Learning and Data Science"
    },
    "Computer Vision": {
        "CVPR", "IEEE/CVF Conference on Computer Vision",
        "ICCV", "IEEE International Conference on Computer Vision",
        "ECCV", "European Conference on Computer Vision", "WACV", "BMVC"
    },
    "Natural Language Processing": {
        "ACL", "Association for Computational Linguistics",
        "EMNLP", "Empirical Methods in Natural Language Processing",
        "NAACL", "North American Chapter of the Association for Computational Linguistics",
        "COLING", "EACL", "SIGIR"
    },
    "Robotics": {
        "ICRA", "International Conference on Robotics and Automation",
        "IROS", "Intelligent Robots and Systems",
        "CoRL", "Conference on Robot Learning", "RSS"
    },
    "Data Mining & Applied ML": {
        "KDD", "WWW", "ICDM", "WSDM"
    },
    "Theory & Foundations": {
        "STOC", "FOCS", "SODA"
    },
    "Systems & Architecture": {
        "OSDI", "Operating Systems Design and Implementation",
        "SOSP", "Symposium on Operating Systems Principles",
        "ATC", "USENIX Annual Technical Conference",
        "EuroSys", "ASPLOS", "SIGCOMM"
    },
    "Human-Computer Interaction": {
        "CHI", "ACM Conference on Human Factors in Computing Systems",
        "FAccT", "ACM Conference on Fairness, Accountability, and Transparency"
    },
    "Security": {
        "SecDev", "Secure Development",
        "USENIX", "USENIX Security", "CCS", "ACM CCS", "NDSS", "JNIC",
        "X National Cybersecurity Research Conference"
    },
    "Databases": {
        "VLDB", "SIGMOD", "PODS", "ICDE"
    },
    "Software Engineering": {
        "ICSE", "International Conference on Software Engineering",
        "EASE", "Empirical Assessment of Software Engineering"
    },
    "AI/NLP/Multimedia": {
        "ACMMM", "ACM Multimedia",
        "LREC", "Language Resources and Evaluation",
        "ICASSP", "IEEE International Conference on Acoustics, Speech and Signal Processing",
        "Interspeech"
    },
    "High Performance Computing": {
        "ISC", "ISC High Performance",
        "SC", "International Conference for High Performance Computing"
    },
    "Quantum Computing": {
        "QCNC", "Quantum Computing and Networks"
    },
    "Medical Image & Signal Processing": {
        "MICCAI", "International Conference on Medical Image Computing and Computer Assisted Intervention",
        "TIP", "IEEE Transactions on Image Processing"
    },
    "Hardware & Design Automation": {
        "ICCAD", "IEEE/ACM International Conference On Computer Aided Design"
    },
    "Computer Vision & Multimedia": {
        "IEEE Conference on Multimedia Expo"
    },
    "AI Applications & Data Science": {
        "ACDSA", "International Conference on Artificial Intelligence, Computer, Data Sciences and Applications"
    },
    "General Computer Science": {
        "CSA", "Computer Science Applications"
    },
    "Other": {
        "ISMIR", "IEEE TCOM"
    }
}

# Flatten for backward compatibility
TOP_TIER_CONFERENCES = set()
for conferences in CONFERENCE_CATEGORIES.values():
    TOP_TIER_CONFERENCES.update(conferences)

ARXIV_CATEGORIES = [
    "cs.AI",      # Artificial Intelligence
    "cs.LG",      # Machine Learning
    "cs.CL",      # Computation and Language (NLP)
    "cs.CV",      # Computer Vision
    "cs.SE",      # Software Engineering
    "stat.ML",    # Machine Learning (Statistics)
]

def get_yesterday_date_str() -> str:
    """Get yesterday's date in ISO format (YYYY-MM-DD)"""
    return (date.today() - timedelta(days=1)).isoformat()

def is_paper_from_yesterday(paper: dict) -> bool:
    """Check if paper was published yesterday"""
    published = paper.get('published', '')
    if not published:
        return False
    
    # Extract date from ISO format (2026-04-10T...)
    paper_date = published.split('T')[0]
    yesterday_date = get_yesterday_date_str()
    
    return paper_date == yesterday_date

def load_archive(archive_file: str = "papers_archive.json") -> dict:
    """Load historical archive of papers by date"""
    try:
        if os.path.exists(archive_file):
            with open(archive_file, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Could not load archive: {e}")
    
    return {
        "last_updated": datetime.now().isoformat(),
        "dates": {}
    }

def load_previous_metrics(archive: dict, date_str: str) -> dict:
    """Load metrics from previous day in archive"""
    yesterday = (datetime.fromisoformat(date_str) - timedelta(days=1)).date().isoformat()
    
    if yesterday in archive.get("dates", {}):
        prev_data = archive["dates"][yesterday]
        return {
            "total_papers": len(prev_data.get("papers", [])),
            "date": yesterday
        }
    
    return {"total_papers": 0, "date": "unknown"}

async def fetch_arxiv_papers_single_session(max_results: int = 100) -> list:
    """
    Fetch recent arXiv papers using a SINGLE persistent session.
    This respects arXiv rate limits: 1 request per 3 seconds.
    """
    papers = []
    
    # Create ONE session for all requests
    async with aiohttp.ClientSession() as session:
        for index, category in enumerate(ARXIV_CATEGORIES):
            query = f"cat:{category}"
            
            url = "http://export.arxiv.org/api/query"
            params = {
                "search_query": query,
                "start": 0,
                "max_results": max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
            
            try:
                print(f"  Fetching {category}...", end=" ", flush=True)
                
                # Wait 3 seconds between requests (arXiv rate limit)
                if index > 0:
                    print(f"⏳ waiting...", end=" ", flush=True)
                    await asyncio.sleep(3)
                
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        feed = feedparser.parse(text)
                        
                        if hasattr(feed, 'entries') and len(feed.entries) > 0:
                            print(f"✓ {len(feed.entries)} papers")
                            for entry in feed.entries:
                                try:
                                    paper = {
                                        "title": entry.get("title", ""),
                                        "arxiv_id": entry.get("id", "").split("/abs/")[-1],
                                        "authors": [author.get("name", "") for author in entry.get("authors", [])],
                                        "published": entry.get("published", ""),
                                        "summary": entry.get("summary", "").replace("\n", " "),
                                        "categories": entry.get("arxiv_primary_category", {}).get("term", category) if hasattr(entry, 'arxiv_primary_category') else category,
                                        "comment": entry.get("arxiv_comment", ""),
                                    }
                                    papers.append(paper)
                                except Exception as e:
                                    pass
                        else:
                            print(f"⚠ No entries found")
                    elif resp.status == 429:
                        print(f"❌ RATE LIMITED")
                        print(f"\n    arXiv: Rate limit exceeded. Waiting 30 seconds...\n")
                        await asyncio.sleep(30)
                        
                        # Retry after wait
                        print(f"  Retrying {category}...", end=" ", flush=True)
                        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp2:
                            if resp2.status == 200:
                                text = await resp2.text()
                                feed = feedparser.parse(text)
                                if hasattr(feed, 'entries') and len(feed.entries) > 0:
                                    print(f"✓ {len(feed.entries)} papers")
                                    for entry in feed.entries:
                                        try:
                                            paper = {
                                                "title": entry.get("title", ""),
                                                "arxiv_id": entry.get("id", "").split("/abs/")[-1],
                                                "authors": [author.get("name", "") for author in entry.get("authors", [])],
                                                "published": entry.get("published", ""),
                                                "summary": entry.get("summary", "").replace("\n", " "),
                                                "categories": entry.get("arxiv_primary_category", {}).get("term", category) if hasattr(entry, 'arxiv_primary_category') else category,
                                                "comment": entry.get("arxiv_comment", ""),
                                            }
                                            papers.append(paper)
                                        except Exception as e:
                                            pass
                            else:
                                print(f"❌ Retry failed: HTTP {resp2.status}")
                    else:
                        text = await resp.text()
                        print(f"❌ HTTP {resp.status}: {text[:100]}")
                        
            except asyncio.TimeoutError:
                print(f"❌ TIMEOUT")
            except aiohttp.ClientError as e:
                print(f"❌ ERROR: {type(e).__name__}")
            except Exception as e:
                print(f"❌ ERROR: {str(e)}")
    
    return papers


def extract_conference_info(paper: dict) -> Optional[dict]:
    """Extract conference information from paper's comment field."""
    comment = paper.get('comment', '').strip()
    
    if not comment:
        return None
    
    comment_lower = comment.lower()
    
    # Sort conferences by length (longest first) to match longer names first
    sorted_conferences = sorted(TOP_TIER_CONFERENCES, key=len, reverse=True)
    
    for conf in sorted_conferences:
        conf_lower = conf.lower()
        
        # Look for the conference name
        if conf_lower in comment_lower:
            conf_pos = comment_lower.find(conf_lower)
            search_area = comment[conf_pos:conf_pos+100]
            
            # Try to extract year
            year_match = re.search(r"'(\d{2})|(\d{4})", search_area)
            
            if year_match:
                if year_match.group(1):
                    year = "20" + year_match.group(1)
                else:
                    year = year_match.group(2)
            else:
                year_match = re.search(r"(20\d{2}|19\d{2})", comment)
                year = year_match.group(1) if year_match else "Unknown"
            
            # Find which category this conference belongs to
            category = "Other"
            for cat, confs in CONFERENCE_CATEGORIES.items():
                if conf in confs:
                    category = cat
                    break
            
            return {
                "conference": conf,
                "year": year,
                "comment": comment,
                "raw_comment": paper.get('comment', ''),
                "category": category
            }
    
    return None


def is_conference_paper(paper: dict) -> bool:
    """Check if paper is from a top-tier conference (must be ACCEPTED, not just submitted)."""
    comment = paper.get('comment', '').lower()
    
    # Exclude papers that are only submitted/under review (not accepted)
    if comment:
        if ("submitted to" in comment or "under review" in comment) and "accepted to" not in comment:
            return False
    
    conf_info = extract_conference_info(paper)
    return conf_info is not None


def generate_paper_analysis(paper: dict) -> dict:
    """Use Groq to analyze a paper."""
    
    analysis_prompt = f"""Analyze this arXiv paper and provide a comprehensive breakdown:

PAPER DETAILS:
Title: {paper['title']}
ArXiv ID: {paper['arxiv_id']}
Comment: {paper['comment']}
Summary: {paper['summary']}

Provide a JSON response with these fields:
{{
  "problem_statement": "One sentence describing the core problem this paper solves",
  "bottleneck_addressed": "Which AI bottleneck does this address?",
  "executive_summary": "2-3 sentences on why this matters to the current AI landscape.",
  "key_metrics": {{
    "primary_metric": "The main improvement claim",
    "metric_value": "Numerical value if available",
    "baseline": "What it's compared against",
    "improvement_percentage": "Percentage improvement if quantifiable"
  }},
  "technical_breakdown": {{
    "method": "How the solution works at a technical level",
    "architecture": "Key architectural components",
    "implementation_details": "Specific algorithms or techniques used",
    "code_complexity": "Computational complexity or training time implications"
  }},
  "relevance_tags": ["tag1", "tag2", "tag3"],
  "confidence": "high/medium/low"
}}

Be precise and technical."""

    try:
        message = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": analysis_prompt}],
            temperature=0.3,
            max_tokens=1500,
        )
        
        response_text = message.choices[0].message.content
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        
        if json_match:
            return json.loads(json_match.group())
    except Exception as e:
        print(f"Error analyzing paper {paper['arxiv_id']}: {e}")
    
    return None


async def run_daily_pipeline() -> tuple:
    """Main pipeline: fetch papers → filter for conference papers → analyze"""
    print(f"[{datetime.now().isoformat()}] Starting arXiv paper analysis pipeline...")
    yesterday_date = get_yesterday_date_str()
    print(f"📅 Filtering for papers from YESTERDAY: {yesterday_date}")
    
    # Load archive for historical data
    archive = load_archive()
    previous_metrics = load_previous_metrics(archive, yesterday_date)
    
    # Step 1: Fetch papers
    print("\n" + "="*60)
    print("STEP 1: Fetching papers from arXiv...")
    print("="*60)
    papers = await fetch_arxiv_papers_single_session(max_results=100)
    print(f"\n✓ Total fetched: {len(papers)} papers\n")
    
    if len(papers) == 0:
        print("⚠️  WARNING: No papers fetched from arXiv!")
        return [], archive
    
    # Step 2: Filter for papers from YESTERDAY
    print("="*60)
    print(f"STEP 2: Filtering for papers from YESTERDAY ({yesterday_date})...")
    print("="*60)
    yesterday_papers = [p for p in papers if is_paper_from_yesterday(p)]
    print(f"✓ Found {len(yesterday_papers)} papers published yesterday\n")
    
    if len(yesterday_papers) == 0:
        print("⚠️  No papers published yesterday")
        return [], archive
    
    # Step 3: Filter for conference papers and DEDUPLICATE
    print("="*60)
    print("STEP 3: Filtering for ACCEPTED conference papers...")
    print("="*60)
    
    seen_arxiv_ids = set()
    conference_papers = []
    
    for p in yesterday_papers:
        arxiv_id = p.get('arxiv_id', '')
        if arxiv_id and arxiv_id not in seen_arxiv_ids and is_conference_paper(p):
            seen_arxiv_ids.add(arxiv_id)
            conference_papers.append(p)
    
    conference_info = {}
    for p in conference_papers:
        conf_data = extract_conference_info(p)
        if conf_data:
            conf_name = conf_data["conference"]
            conference_info[conf_name] = conference_info.get(conf_name, 0) + 1
    
    print(f"✓ Found {len(conference_papers)} unique ACCEPTED conference papers")
    print("   Exhaustive conference check:")
    
    all_conferences_count = {}
    for conf in TOP_TIER_CONFERENCES:
        all_conferences_count[conf] = conference_info.get(conf, 0)
    
    for conf, count in sorted(all_conferences_count.items(), key=lambda x: (-x[1], x[0])):
        status = "✓" if count > 0 else "○"
        print(f"     {status} {conf}: {count} paper(s)")
    
    if len(conference_papers) == 0:
        print("\n⚠ No ACCEPTED conference papers from yesterday")
        return [], archive
    
    # Step 4: Analyze papers
    print("\n" + "="*60)
    print(f"STEP 4: Analyzing all {len(conference_papers)} papers with Groq...")
    print("="*60)
    analyzed_papers = []
    
    for i, paper in enumerate(conference_papers):
        conf_info = extract_conference_info(paper)
        conf_display = f"{conf_info['conference']} {conf_info['year']}" if conf_info else "Unknown"
        print(f"  [{i+1:2d}/{len(conference_papers)}] {paper['arxiv_id']:12s} ({conf_display:30s})...", end=" ", flush=True)
        
        analysis = generate_paper_analysis(paper)
        if analysis:
            analyzed_papers.append({
                "paper": paper,
                "analysis": analysis,
                "analyzed_at": datetime.now().isoformat(),
                "conference_info": conf_info,
            })
            print("✓")
        else:
            print("⚠")
        
        await asyncio.sleep(1)
    
    print(f"\n✓ Successfully analyzed {len(analyzed_papers)} papers")
    
    # Step 5: Sort by category and date
    print("\n" + "="*60)
    print("STEP 5: Sorting papers by category and date...")
    print("="*60)
    
    from collections import defaultdict
    papers_by_category = defaultdict(list)
    
    for paper in analyzed_papers:
        category = paper['conference_info'].get('category', 'Other') if paper.get('conference_info') else 'Other'
        papers_by_category[category].append(paper)
    
    sorted_papers = []
    for category in sorted(papers_by_category.keys()):
        category_papers = papers_by_category[category]
        category_papers.sort(
            key=lambda x: x['paper'].get('published', ''),
            reverse=True
        )
        sorted_papers.extend(category_papers)
    
    print(f"✓ Sorted {len(sorted_papers)} papers by category and date")
    
    # Calculate metrics
    current_count = len(sorted_papers)
    previous_count = previous_metrics.get("total_papers", 0)
    day_change = current_count - previous_count
    
    print("\n" + "="*60)
    print("METRICS DASHBOARD")
    print("="*60)
    print(f"Previous day ({previous_metrics.get('date', 'unknown')}): {previous_count} papers")
    print(f"Today ({yesterday_date}): {current_count} papers")
    if day_change > 0:
        print(f"📈 Change: +{day_change} papers (UP)")
    elif day_change < 0:
        print(f"📉 Change: {day_change} papers (DOWN)")
    else:
        print(f"➡️  Change: No change")
    
    return sorted_papers, {
        "previous_date": previous_metrics.get('date', 'unknown'),
        "previous_count": previous_count,
        "current_date": yesterday_date,
        "current_count": current_count,
        "day_change": day_change,
        "archive": archive
    }


def save_results(results: tuple, output_file: str = "papers.json", archive_file: str = "papers_archive.json"):
    """Save current papers and update archive."""
    papers, metrics = results
    yesterday_date = get_yesterday_date_str()
    
    # Current papers (for today's view)
    current_output = {
        "last_updated": datetime.now().isoformat(),
        "total_papers": len(papers),
        "papers": papers,
        "categories": list(CONFERENCE_CATEGORIES.keys()),
        "filter_date": yesterday_date,
        "metrics": {
            "dashboard": {
                "previous_date": metrics.get("previous_date"),
                "previous_count": metrics.get("previous_count"),
                "current_date": metrics.get("current_date"),
                "current_count": metrics.get("current_count"),
                "day_change": metrics.get("day_change"),
                "trend": "📈 UP" if metrics.get("day_change", 0) > 0 else ("📉 DOWN" if metrics.get("day_change", 0) < 0 else "➡️  STABLE")
            }
        }
    }
    
    with open(output_file, "w") as f:
        json.dump(current_output, f, indent=2)
    print(f"✓ Current papers saved to {output_file}")
    
    # Archive (historical data with date index)
    archive = metrics.get("archive", {"last_updated": datetime.now().isoformat(), "dates": {}})
    
    # Add today's papers to archive
    archive["dates"][yesterday_date] = {
        "count": len(papers),
        "papers": papers,
        "updated_at": datetime.now().isoformat()
    }
    archive["last_updated"] = datetime.now().isoformat()
    
    with open(archive_file, "w") as f:
        json.dump(archive, f, indent=2)
    print(f"✓ Archive updated: {archive_file} (total dates: {len(archive['dates'])})")
    
    return current_output, archive


async def main():
    """Run the complete pipeline."""
    results = await run_daily_pipeline()
    current_data, archive_data = save_results(results)
    return current_data


if __name__ == "__main__":
    data = asyncio.run(main())
    print("\n✅ Pipeline complete!")
    if isinstance(data, dict) and "papers" in data:
        print(f"Results: {len(data['papers'])} papers analyzed")