# Devin Autonomous Remediation Pipeline

Event-driven system that receives security scan findings as webhook events,
dispatches parallel Devin sessions — one per issue — and delivers pull requests
with zero engineer involvement.

Built against the [Apache Superset](https://github.com/sanyaver/superset) fork
as a demonstration of autonomous code health automation.

---

## Architecture

```
Security scanner / webhook trigger
          │
          ▼
POST /webhook/scan  ◄──────────────────────────────────────────┐
          │                                                     │
          │  For each finding:                                  │
          │  1. Fetch GitHub issue (title + body)               │
          │  2. Build structured prompt                         │
          │  3. Attach org Playbook  (!remediate)               │
          │  4. Attach Knowledge     (superset-codebase-context)│
          │  5. Create Devin session (v3 API, org-scoped)       │
          │  6. Post "dispatched" comment to GitHub issue       │
          │  7. Spawn async poll loop                           │
          ▼
    Devin session  ──►  clone repo  ──►  fix code  ──►  run tests  ──►  open PR
          │
          │  Every 30s: GET /organizations/{org}/sessions/{id}
          │  On PR detected:
          │    - Update DB → completed
          │    - Post "PR ready" comment to GitHub issue
          │    - Trigger session insights generation
          ▼
   SQLite DB  ──►  Dashboard  ──►  /api/metrics (Devin analytics API)
```

**Key Devin primitives used:**
- **Sessions** — v3 org-scoped API (`/organizations/{org}/sessions`)
- **Playbook** — `!remediate` standard procedure, attached to every session
- **Knowledge** — `superset-codebase-context`, auto-retrieved by Devin when relevant
- **Schedule** — weekly scan every Monday 06:00 UTC, created via API on startup
- **Session insights** — generated post-completion for task categorisation
- **Metrics API** — `/organizations/{org}/metrics/sessions` + `/metrics/prs`

---

## Issues remediated

| # | Category | Issue | PR |
|---|---|---|---|
| [#1](https://github.com/sanyaver/superset/issues/1) | BUG | `extract_column_dtype()` misclassifies boolean columns as STRING | [PR #6](https://github.com/sanyaver/superset/pull/6) |
| [#2](https://github.com/sanyaver/superset/issues/2) | TEST | `find_duplicates`, `remove_duplicates` etc. have no test coverage | [PR #4](https://github.com/sanyaver/superset/pull/4) |
| [#3](https://github.com/sanyaver/superset/issues/3) | PERF | `get_time_grain_expressions()` recomputes on every chart request | [PR #5](https://github.com/sanyaver/superset/pull/5) |

---

## Run it

```bash
cp .env.example .env
# Fill in DEVIN_API_KEY, DEVIN_ORG_ID, GITHUB_TOKEN, GITHUB_REPO
docker compose up
```

| Endpoint | Description |
|---|---|
| `http://localhost:8000/dashboard` | Live remediation dashboard |
| `POST /scan/trigger` | Fire all four issues simultaneously through the pipeline |
| `POST /webhook/scan` | Real webhook — body: `{"findings": [{"issue_number": N}]}` |
| `DELETE /demo/reset` | Clear session rows for a fresh demo run (preserves org config) |
| `GET /api/sessions` | Raw session data (JSON) |
| `GET /api/metrics` | Devin org-level analytics (30-day window) |
| `GET /api/config` | Playbook / knowledge / schedule IDs |
| `GET /health` | Liveness check |

---

## Environment variables

| Variable | Description |
|---|---|
| `DEVIN_API_KEY` | Devin service user API key (`cog_…`) |
| `DEVIN_ORG_ID` | Devin organisation ID (`org-…`) |
| `GITHUB_TOKEN` | GitHub personal access token |
| `GITHUB_REPO` | Target repo in `owner/repo` format |
| `DB_PATH` | SQLite path (default: `/app/data/sessions.db`) |

---

## Startup behaviour

On first boot the app calls `setup_devin()` which:
1. Creates the `!remediate` **playbook** in your Devin org (or finds it if it already exists)
2. Creates the `superset-codebase-context` **knowledge note** (or finds it if it exists)
3. Creates a **weekly schedule** (Monday 06:00 UTC) that scans for new findings

IDs are persisted to the local SQLite DB — subsequent restarts skip the API calls.
The app starts and serves `/health` successfully even if Devin credentials are invalid.
