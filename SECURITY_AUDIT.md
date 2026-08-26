# Security audit and hardening report

Date: 2026-08-26
Scope: the FastAPI dashboard, MCP endpoint, background analysis pipeline, browser rendering, and Docker deployment files in this archive.

## Executive result

Safari Web Inspector only changes the current visitor's local page. Before this review, however, it made several unsafe server capabilities easy to discover and call: an unauthenticated paid analysis job, a route serving the complete application directory, and a stored DOM-XSS sink. Those high-risk paths have been fixed in this hardened copy.

No application can promise immunity from a sufficiently large distributed denial-of-service attack. This code now has appropriate application-level controls, but a public deployment should also use the hosting provider's edge rate limiting/WAF and run only one in-process scheduler (or move scheduling to a dedicated worker).

## Fixed findings

| Severity | Finding | Resolution |
|---|---|---|
| High | `POST /api/trigger-analysis` was public and could consume Groq quota, hold the pipeline for 20 minutes, and push data to GitHub. | Requires a constant-time checked `ADMIN_API_TOKEN`, fails closed if unset, retains the single-run lock, and is rate-limited. See `main.py:191-225`. |
| High | `/static` served the whole working directory, potentially exposing source, `.env`, `.git`, and data. | Removed the broad static mount. Regression tests verify sensitive paths return 404. See `main.py:146-147` and `tests/test_security.py`. |
| High | External arXiv metadata and AI-generated text were inserted with `innerHTML`, enabling stored DOM XSS. | Every dynamic field is escaped, arXiv IDs are allowlisted/encoded, external links use `rel="noopener noreferrer"`, and browser security headers/CSP were added. See `simple_dashboard.html:680-688` and `simple_dashboard.html:824-858`. |
| Medium-high | Public API and MCP requests had no resource controls; MCP reparsed the archive for every request and accepted unbounded search text. | Added per-client request buckets, a 1 MiB body limit, Uvicorn concurrency/keep-alive limits, MCP argument caps, and mtime-based JSON snapshot caching. See `main.py:32-62`, `main.py:164-188`, `mcp_server.py:27-50`, and `dockerfile:35`. |
| Medium | Alexa's GET endpoint mutated a shared JSON file without locking and used blocking file I/O. | Removed shared rotation state; selection is now stateless. See `main.py:277-315`. |
| Medium | Pipeline JSON files were overwritten in place while requests could read them. | Writes now flush, fsync, and atomically replace the destination. See `arxiv_agent.py:18-34` and `arxiv_agent.py:786-797`. |
| Medium | arXiv data was fetched over cleartext HTTP. | Changed the feed endpoint to HTTPS. See `arxiv_agent.py:475`. |
| Medium | Runtime mounted host `.git`, SSH keys, and gitconfig, disabled SSH host checking, and installed git unnecessarily. | Removed all of those mounts/options and removed git from the image. See `docker-compose.yml:7-14` and `dockerfile:7-10`. |
| Medium | Dependencies were unpinned. | Locked the complete tested dependency set in `requirements.txt`. The resolved environment reported no known vulnerabilities through `pip-audit` on the audit date. |
| Low | Internal exception strings were returned in HTTP 500 responses. | Exceptions are logged server-side and clients receive generic errors. |

## Verification performed

- Python syntax compilation passed for all application modules and security tests.
- JavaScript syntax validation passed for the dashboard script.
- Eight application-level security regression tests passed, covering authentication, fail-closed configuration, rate limiting, request-size limits, security headers, invalid input, and source/secret non-exposure.
- `pip-audit -r requirements.txt` reported: `No known vulnerabilities found` on 2026-08-26.
- The locked dependency set successfully resolved for the Docker image's CPython 3.11 / Linux target.

Run the regression suite with:

```bash
python -m unittest -v tests/test_security.py
```

## Required deployment configuration

1. Generate a token with `python -c 'import secrets; print(secrets.token_urlsafe(32))'` and set it as the server-side `ADMIN_API_TOKEN`. Never place it in browser JavaScript.
2. Trigger manually with `Authorization: Bearer <token>`.
3. If the old version was ever public, rotate Groq/GitHub credentials and review repository access logs because the former `/static` route may have exposed runtime files.
4. Put the service behind provider-level DDoS protection and rate limiting. The in-memory limiter protects one process; it is not a substitute for an edge control against distributed traffic.
5. Run one application worker/replica while the scheduler remains in-process. Multiple workers or replicas each start a scheduler and have independent locks/rate limits. For horizontal scaling, move scheduled analysis to one dedicated worker with a distributed lock.
6. Keep `/mcp` public only if public research access is intentional. If it is private, require gateway authentication for that path as well.

## Remaining defense-in-depth work

- The container still runs as root because the current bind-mounted JSON files do not define a portable non-root ownership model. Moving persistent state under a managed volume with a fixed application UID would allow `USER` hardening safely.
- The single-file dashboard needs inline script/style CSP allowances. Moving JavaScript and CSS into separate files would allow a stricter nonce/hash-free policy.
- Package pins prevent surprise upgrades but do not include artifact hashes. Generate a platform-aware hashed lock file in CI if stronger supply-chain reproducibility is required.
