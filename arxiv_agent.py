#!/usr/bin/env python3
"""
Conference-filtered arXiv Paper Agent
Fetches ONLY papers from top-tier conferences (matching arXiv format).
"""

import os
import json
import re
import asyncio
from datetime import datetime
from typing import Optional
import feedparser
import aiohttp
from groq import Groq

# Initialize Groq client
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# Top-tier conferences - exact names as they appear in arXiv
TOP_TIER_CONFERENCES = {
    # ML/AI (Tier 1)
    "NeurIPS", "NEURIPS",
    "ICML", 
    "ICLR",
    "IJCAI",
    "AAAI",
    
    # Computer Vision
    "CVPR", "IEEE/CVF Conference on Computer Vision",
    "ICCV", "IEEE International Conference on Computer Vision",
    "ECCV", "European Conference on Computer Vision",
    
    # NLP
    "ACL", "Association for Computational Linguistics",
    "EMNLP", "Empirical Methods in Natural Language Processing",
    "NAACL", "North American Chapter of the Association for Computational Linguistics",
    
    # Robotics
    "ICRA", "International Conference on Robotics and Automation",
    "IROS", "Intelligent Robots and Systems",
    "CoRL", "Conference on Robot Learning",
    
    # Systems
    "OSDI", "Operating Systems Design and Implementation",
    "SOSP", "Symposium on Operating Systems Principles",
    "ATC", "USENIX Annual Technical Conference",
    "EuroSys",
    "ASPLOS",
    
    # Databases
    "VLDB",
    "SIGMOD",
    "PODS",
    "ICDE",
    
    # Other Top-Tier
    "KDD",
    "WWW",
    "ISMIR",
    "SecDev", "Secure Development",  # Added based on your example
    "USENIX", "USENIX Security",
    "CCS", "ACM CCS",
    "NDSS",
}

ARXIV_CATEGORIES = [
    "cs.AI",      # Artificial Intelligence
    "cs.LG",      # Machine Learning
    "cs.CL",      # Computation and Language (NLP)
    "cs.CV",      # Computer Vision
    "stat.ML",    # Machine Learning (Statistics)
]

async def fetch_arxiv_papers(days: int = 1, max_results: int = 50) -> list:
    """
    Fetch recent arXiv papers from AI/ML categories.
    """
    papers = []
    
    for category in ARXIV_CATEGORIES:
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
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        feed = feedparser.parse(text)
                        
                        if hasattr(feed, 'entries') and len(feed.entries) > 0:
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
                                    print(f"Error parsing entry: {e}")
                        else:
                            print(f"⚠ No entries found for {category}")
        except Exception as e:
            print(f"Error fetching from {category}: {e}")
    
    return papers


def extract_conference_info(paper: dict) -> Optional[dict]:
    """
    Extract conference information from paper's comment field.
    
    arXiv format examples:
    - "SecDev 2026 in Montreal, Canada, 10 pages, maximum 16 pages"
    - "Proceedings of the 2026 ACM Secure Development Conference (SecDev 2026)"
    - "To appear in ICML 2024"
    - "Accepted to NeurIPS 2024"
    
    Returns: {"conference": "SecDev", "year": "2026", "comment": full_comment}
    """
    comment = paper.get('comment', '').strip()
    
    if not comment:
        return None
    
    comment_lower = comment.lower()
    
    # Look for any top-tier conference name in the comment
    for conf in TOP_TIER_CONFERENCES:
        if conf.lower() in comment_lower:
            # Try to extract year if present (4 digits)
            year_match = re.search(r'(20\d{2}|19\d{2})', comment)
            year = year_match.group(1) if year_match else "Unknown"
            
            return {
                "conference": conf,
                "year": year,
                "comment": comment,
                "raw_comment": paper.get('comment', '')
            }
    
    return None


def is_conference_paper(paper: dict) -> bool:
    """
    Check if paper is from a top-tier conference.
    Looks for conference info in the comment field (as arXiv presents it).
    """
    conf_info = extract_conference_info(paper)
    return conf_info is not None


def generate_paper_analysis(paper: dict) -> dict:
    """
    Use Groq to analyze a paper and generate:
    1. Problem statement
    2. Executive summary (relevance to AI bottlenecks)
    3. Technical breakdown (for engineers)
    """
    
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
            messages=[
                {
                    "role": "user",
                    "content": analysis_prompt
                }
            ],
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
    """
    Main pipeline: fetch papers → filter for conference papers → analyze
    """
    print(f"[{datetime.now().isoformat()}] Starting arXiv paper analysis pipeline...")
    
    # Step 1: Fetch papers
    print("Fetching recent papers from arXiv...")
    papers = await fetch_arxiv_papers(days=1, max_results=100)
    print(f"✓ Fetched {len(papers)} papers")
    
    # Step 2: Filter for conference papers
    print("Filtering for papers from top-tier conferences...")
    conference_papers = [p for p in papers if is_conference_paper(p)]
    
    # Extract conference info for logging
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
        print("⚠ No papers found from top-tier conferences")
    
    # Step 3: Analyze TOP 10 PAPERS ONLY
    print("Analyzing top 24 papers with Groq (llama-3.1-8b-instant)...")
    analyzed_papers = []
    
    top_24_papers = conference_papers[:24]
    
    for i, paper in enumerate(top_24_papers):
        conf_info = extract_conference_info(paper)
        conf_display = f"{conf_info['conference']} {conf_info['year']}" if conf_info else "Unknown Conference"
        print(f"  [{i+1}/{len(top_24_papers)}] Analyzing {paper['arxiv_id']} ({conf_display})...")
        
        analysis = generate_paper_analysis(paper)
        if analysis:
            analyzed_papers.append({
                "paper": paper,
                "analysis": analysis,
                "analyzed_at": datetime.now().isoformat(),
                "conference_info": conf_info,
            })
        else:
            print(f"    ⚠ Failed to analyze, skipping...")
        
        await asyncio.sleep(1)  # Rate limit
    
    print(f"✓ Successfully analyzed {len(analyzed_papers)} papers")
    return analyzed_papers


def save_results(results: list, output_file: str = "papers.json"):
    """Save analyzed papers to JSON for web delivery."""
    output = {
        "last_updated": datetime.now().isoformat(),
        "total_papers": len(results),
        "papers": results,
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
    # Run the pipeline
    data = asyncio.run(main())
    print("\n✅ Pipeline complete!")
    print(f"Results: {len(data['papers'])} papers analyzed (from top-tier conferences)")
