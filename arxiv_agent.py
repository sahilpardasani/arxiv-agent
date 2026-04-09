#!/usr/bin/env python3
import os
import json
import re
import asyncio
import time
from datetime import datetime
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
        "COLM", "AISTATS"
    },
    "Computer Vision": {
        "CVPR", "IEEE/CVF Conference on Computer Vision",
        "ICCV", "IEEE International Conference on Computer Vision",
        "ECCV", "European Conference on Computer Vision", "WACV"
    },
    "Natural Language Processing": {
        "ACL", "Association for Computational Linguistics",
        "EMNLP", "Empirical Methods in Natural Language Processing",
        "NAACL", "North American Chapter of the Association for Computational Linguistics",
        "COLING", "EACL"
    },
    "Robotics": {
        "ICRA", "International Conference on Robotics and Automation",
        "IROS", "Intelligent Robots and Systems",
        "CoRL", "Conference on Robot Learning", "RSS"
    },
    "Data Mining & Applied ML": {
        "KDD", "WWW", "ICDM"
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
        "CHI", "ACM Conference on Human Factors in Computing Systems"
    },
    "Security": {
        "SecDev", "Secure Development",
        "USENIX", "USENIX Security", "CCS", "ACM CCS", "NDSS"
    },
    "Databases": {
        "VLDB", "SIGMOD", "PODS", "ICDE"
    },
    "Software Engineering": {
        "ICSE"
    },
    "Other": {
        "ISMIR"
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
    """
    Extract conference information from paper's comment field.
    """
    comment = paper.get('comment', '').strip()
    
    if not comment:
        return None
    
    comment_lower = comment.lower()
    
    for conf in TOP_TIER_CONFERENCES:
        if conf.lower() in comment_lower:
            conf_pos = comment_lower.find(conf.lower())
            search_area = comment[conf_pos:conf_pos+50]
            
            year_match = re.search(r"'(\d{2})|(\d{4})", search_area)
            
            if year_match:
                if year_match.group(1):
                    year = "20" + year_match.group(1)
                else:
                    year = year_match.group(2)
            else:
                year_match = re.search(r"(20\d{2}|19\d{2})", comment)
                year = year_match.group(1) if year_match else "Unknown"
            
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
    """Check if paper is from a top-tier conference."""
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
  "bottleneck_addressed": "Which AI bottleneck does this address? (e.g., memory usage, inference latency, training efficiency, context length, etc.)",
  "executive_summary": "2-3 sentences on why this matters to the current AI landscape. Reference specific use cases or pain points.",
  "key_metrics": {{
    "primary_metric": "The main improvement claim (e.g., '6-7x memory reduction')",
    "metric_value": "Numerical value if available",
    "baseline": "What it's compared against",
    "improvement_percentage": "Percentage improvement if quantifiable"
  }},
  "technical_breakdown": {{
    "method": "How the solution works at a technical level (3-4 sentences)",
    "architecture": "Key architectural components or novel techniques",
    "implementation_details": "Specific algorithms, data structures, or techniques used",
    "code_complexity": "Computational complexity or training time implications"
  }},
  "relevance_tags": ["tag1", "tag2", "tag3"],
  "confidence": "high/medium/low - how confident this analysis is based on available info"
}}

Be precise and technical. If the paper doesn't provide specific numbers, say 'Not disclosed'. If details are unclear from the summary, mark confidence as 'medium'."""

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


async def run_daily_pipeline() -> list:
    """Main pipeline: fetch papers → filter for conference papers → analyze"""
    print(f"[{datetime.now().isoformat()}] Starting arXiv paper analysis pipeline...")
    
    # Step 1: Fetch papers
    print("\n" + "="*60)
    print("STEP 1: Fetching papers from arXiv (respecting rate limits)...")
    print("="*60)
    papers = await fetch_arxiv_papers_single_session(max_results=100)
    print(f"\n✓ Total fetched: {len(papers)} papers\n")
    
    if len(papers) == 0:
        print("⚠️  WARNING: No papers fetched from arXiv!")
        print("Try running again in a few minutes.")
        return []
    
    # Step 2: Filter for conference papers
    print("="*60)
    print("STEP 2: Filtering for top-tier conference papers...")
    print("="*60)
    conference_papers = [p for p in papers if is_conference_paper(p)]
    
    conference_info = {}
    for p in conference_papers:
        conf_data = extract_conference_info(p)
        if conf_data:
            conf_name = conf_data["conference"]
            conference_info[conf_name] = conference_info.get(conf_name, 0) + 1
    
    print(f"✓ Found {len(conference_papers)} papers from top-tier conferences")
    if conference_info:
        print("   Conferences found:")
        for conf, count in sorted(conference_info.items(), key=lambda x: x[1], reverse=True):
            print(f"     - {conf}: {count} paper(s)")
    
    if len(conference_papers) == 0:
        print("⚠ No conference papers found")
        return []
    
    # Step 3: Analyze TOP 24 PAPERS
    print("\n" + "="*60)
    print("STEP 3: Analyzing top 24 papers with Groq...")
    print("="*60)
    analyzed_papers = []
    
    top_24_papers = conference_papers[:24]
    
    for i, paper in enumerate(top_24_papers):
        conf_info = extract_conference_info(paper)
        conf_display = f"{conf_info['conference']} {conf_info['year']}" if conf_info else "Unknown"
        print(f"  [{i+1:2d}/{len(top_24_papers)}] {paper['arxiv_id']:12s} ({conf_display:30s})...", end=" ", flush=True)
        
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
    
    # SORT BY CATEGORY FIRST, THEN BY PUBLICATION DATE (NEWEST FIRST)
    print("\n" + "="*60)
    print("STEP 4: Sorting papers by category and date...")
    print("="*60)
    
    # Sort by: category, then by published date (descending = newest first)
    analyzed_papers.sort(
        key=lambda x: (
            x['conference_info'].get('category', 'Other') if x.get('conference_info') else 'Other',
            x['paper'].get('published', ''),
        ),
        reverse=False  # Categories alphabetically, then dates newest first within category
    )
    
    # Re-sort within each category by date (descending)
    from collections import defaultdict
    papers_by_category = defaultdict(list)
    
    for paper in analyzed_papers:
        category = paper['conference_info'].get('category', 'Other') if paper.get('conference_info') else 'Other'
        papers_by_category[category].append(paper)
    
    # Sort each category by published date (newest first)
    sorted_papers = []
    for category in sorted(papers_by_category.keys()):
        category_papers = papers_by_category[category]
        category_papers.sort(
            key=lambda x: x['paper'].get('published', ''),
            reverse=True  # Newest first
        )
        sorted_papers.extend(category_papers)
    
    print(f"✓ Sorted {len(sorted_papers)} papers by category and date")
    print("   Paper order: Category → Latest date first")
    
    return sorted_papers


def save_results(results: list, output_file: str = "papers.json"):
    """Save analyzed papers to JSON for web delivery."""
    output = {
        "last_updated": datetime.now().isoformat(),
        "total_papers": len(results),
        "papers": results,
        "categories": list(CONFERENCE_CATEGORIES.keys())
    }
    
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2)
    
    print(f"✓ Results saved to {output_file}")
    return output


async def main():
    """Run the complete pipeline."""
    results = await run_daily_pipeline()
    data = save_results(results)
    return data


if __name__ == "__main__":
    data = asyncio.run(main())
    print("\n✅ Pipeline complete!")
    print(f"Results: {len(data['papers'])} papers analyzed (sorted by category & date)")