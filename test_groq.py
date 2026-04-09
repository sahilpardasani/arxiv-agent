#!/usr/bin/env python3
"""
Test script for Groq-based arXiv paper agent
Run this to verify the setup before running the full pipeline
"""

import os
import asyncio
import sys

# Test 1: Check API key
print("🔐 Checking Groq API key...")
groq_key = os.getenv("GROQ_API_KEY")
if not groq_key:
    print("❌ GROQ_API_KEY not set!")
    print("   Run: export GROQ_API_KEY='your-key-from-groq.com'")
    sys.exit(1)
print("✓ API key found")

# Test 2: Import modules
print("\n📦 Importing modules...")
try:
    from groq import Groq
    import feedparser
    import aiohttp
    print("✓ All imports successful")
except ImportError as e:
    print(f"❌ Import error: {e}")
    print("   Run: pip install groq aiohttp feedparser")
    sys.exit(1)

# Test 3: Test arXiv API
print("\n🔍 Testing arXiv API fetch...")
async def test_arxiv():
    try:
        async with aiohttp.ClientSession() as session:
            url = "http://export.arxiv.org/api/query"
            params = {
                "search_query": "cat:cs.LG",
                "start": 0,
                "max_results": 5,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    feed = feedparser.parse(text)
                    if hasattr(feed, 'entries') and len(feed.entries) > 0:
                        print(f"✓ Fetched {len(feed.entries)} papers from arXiv")
                        print(f"   First paper: {feed.entries[0].get('title', '')[:50]}...")
                        return feed.entries
                    else:
                        print("❌ No entries returned from arXiv")
                        return None
                else:
                    print(f"❌ arXiv API returned status {resp.status}")
                    return None
    except Exception as e:
        print(f"❌ Error fetching from arXiv: {e}")
        return None

entries = asyncio.run(test_arxiv())

# Test 4: Test Groq API
print("\n🤖 Testing Groq API...")
try:
    client = Groq(api_key=groq_key)
    
    test_message = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {
                "role": "user",
                "content": "Respond with: {\"test\": \"success\"}"
            }
        ],
        max_tokens=100,
        temperature=0.1,
    )
    
    response_text = test_message.choices[0].message.content
    if "success" in response_text.lower():
        print("✓ Groq API working")
        print(f"   Response: {response_text[:60]}...")
    else:
        print(f"⚠ Groq response unexpected: {response_text}")
except Exception as e:
    print(f"❌ Groq API error: {e}")
    sys.exit(1)

# Test 5: Test full paper analysis (if we have papers)
if entries:
    print("\n📄 Testing full paper analysis...")
    test_paper = {
        "title": entries[0].get("title", "Test Paper"),
        "arxiv_id": entries[0].get("id", "").split("/abs/")[-1],
        "authors": [a.get("name", "") for a in entries[0].get("authors", [])],
        "published": entries[0].get("published", ""),
        "summary": entries[0].get("summary", "").replace("\n", " ")[:200],
        "categories": entries[0].get("arxiv_primary_category", {}).get("term", "cs.LG") if hasattr(entries[0], 'arxiv_primary_category') else "cs.LG",
        "comment": entries[0].get("arxiv_comment", ""),
    }
    
    print(f"   Testing with: {test_paper['title'][:60]}...")
    
    analysis_prompt = f"""Analyze this paper briefly and respond with ONLY a JSON object:

Title: {test_paper['title']}
Summary: {test_paper['summary']}
Comment: {test_paper['comment']}

{{
  "problem_statement": "What problem does it solve?",
  "bottleneck_addressed": "Memory/Latency/Throughput/Other?",
  "confidence": "high/medium/low"
}}"""

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": analysis_prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        
        response_text = response.choices[0].message.content
        print("✓ Paper analysis successful")
        print(f"   Response: {response_text[:100]}...")
        
    except Exception as e:
        print(f"❌ Paper analysis failed: {e}")
        sys.exit(1)

print("\n" + "="*50)
print("✅ All tests passed!")
print("="*50)
print("\nYou're ready to run the full pipeline:")
print("  python3 arxiv_agent.py")
