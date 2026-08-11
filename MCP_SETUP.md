# MCP endpoint

This repository exposes the existing curated arXiv feed as a read-only Model Context Protocol (MCP) server.

## Endpoint

After Render deploys the current `main` branch, connect MCP clients to:

`https://<your-render-host>/mcp`

The same endpoint is intended for compatible clients such as Claude, ChatGPT/OpenAI clients, Mistral, and other MCP hosts.

## Tools

- `search(query, limit=10, include_archive=true)` — searches paper metadata, abstracts, conference metadata, and generated analyses.
- `fetch(id)` — returns the complete stored record for an arXiv ID; version suffix is optional.
- `recent_papers(date?, conference?, field?, rank?, limit=20)` — lists/filter recent papers.
- `available_dates()` — reports dates and counts in the rolling archive.
- `paper_stats(date?)` — summarizes conference, field, rank, and arXiv-category counts.

## Pipeline isolation

The MCP server only reads `papers.json` and `papers_archive.json`. It does not import `arxiv_agent.py`, call Groq, trigger `/api/trigger-analysis`, modify data files, or push to GitHub. The existing scheduled pipeline and REST/dashboard endpoints remain separate.

## Render

Render provides `RENDER_EXTERNAL_HOSTNAME`; the MCP server automatically allows that hostname. For a custom domain, add the hostname to `MCP_ALLOWED_HOSTS`. Additional browser origins can be comma-separated in `MCP_ALLOWED_ORIGINS`.
