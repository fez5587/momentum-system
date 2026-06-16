# Grafana — live remote monitoring

A polished, remote, auto-refreshing view of the momentum system over the
Postgres event store. Complements `momentum_cli.py watch` (terminal) and pgAdmin.

## Run
```bash
cd grafana
docker compose up -d
# open http://localhost:3000  (anonymous admin, no login)
```
The **Momentum** Postgres datasource and the **Momentum — Live** dashboard are
auto-provisioned. The dashboard refreshes every 5s.

## Panels
- **Account equity / Ready signals / Symbols evaluated** — top-line stats.
- **Evaluation board** — every symbol it's looking at with status
  (ready/late/blocked, colour-coded), score, gap%, RVOL, and the reason —
  the same determinations as `momentum watch`, sorted by score.
- **Ready signals** and **Risk events** (circuit breaker / back-outs).
- **Minute bars ingested** and **Account equity** over time.

## Datasource note
`provisioning/datasources/momentum.yaml` points at `192.168.1.5:5432`
(your Postgres on the LAN). From inside the Grafana container `127.0.0.1` is the
container itself — use the LAN IP, or `host.docker.internal:5432` on Docker
Desktop/WSL. Credentials are the dev `admin` / `password`; change for anything
beyond local use.

## How it reads the data
Everything is plain SQL over the `events` table, extracting fields from the JSON
payload, e.g.:
```sql
SELECT payload_json::jsonb->>'symbol'                              AS symbol,
       payload_json::jsonb->'criteria_results'->>'status'         AS status,
       (payload_json::jsonb->>'success_score_pct')::float         AS score
FROM events WHERE event_type='criteria_evaluated';
```
So you can build your own panels/alerts (e.g. alert when a `risk_rule_triggered`
row with `rule_type='daily_loss'` appears).
