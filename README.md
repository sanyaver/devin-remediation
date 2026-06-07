# Devin Autonomous Security Remediation

Event-driven pipeline that automatically dispatches Devin to remediate
security findings, producing pull requests with zero human involvement.

## How it works

1. A security scan fires (simulated or real webhook)
2. The orchestrator builds a structured Playbook-style prompt per finding
3. Parallel Devin sessions are dispatched — one per issue
4. Each session reads the repo, makes changes, runs tests, opens a PR
5. Results are tracked and displayed on a live dashboard

## Run it

```bash
cp .env.example .env
# Fill in your keys, then:
docker compose up
```

Open dashboard: http://localhost:8000/dashboard

Trigger the demo scan:
```bash
curl -X POST http://localhost:8000/scan/trigger
```
