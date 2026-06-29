# Kubernetes deployment

A container image and Helm chart that run the whole Momentum System — including
its **PostgreSQL datastore** — inside a single Kubernetes namespace.

## What gets deployed

| Workload | Kind | Notes |
| --- | --- | --- |
| PostgreSQL | StatefulSet + PVC | Single source of truth. Schema auto-creates on the app's first connection from `storage/pg_schema.sql`. |
| App loop + dashboard | Deployment (`replicas: 1`, `Recreate`) | `run_live_paper.py` with the **embedded** dashboard on :8010. Stateful singleton — never scale up. |
| journal / eod-replay / nightly-tune | CronJobs | US/Eastern (`spec.timeZone`), weekdays. Share the app's `data/` PVC. |
| Grafana | Deployment + Service | Optional (`grafana.enabled`). Datasource auto-points at the in-cluster Postgres. |

The app loop and the CronJobs run the **same image** (built from the repo
`Dockerfile`), just with different commands.

## Local development with Docker Compose

For a single-machine stack (same components, no Kubernetes) use the root
`docker-compose.yml`. It builds the app from the `Dockerfile`, brings up
PostgreSQL with a persistent volume, and runs the loop with its embedded
dashboard. Config comes from a local `.env` (optional — without `ALPACA_*` keys
the loop runs in dry mode; the dashboard and DB still work). `DATABASE_URL` is
overridden to the in-network `postgres` service, so the stack is self-contained.

```bash
cp .env.example .env        # optional: add ALPACA_* keys for live data

docker compose up -d --build            # postgres + app loop + dashboard (:8010)
docker compose logs -f app              # watch the loop
open http://localhost:8010              # dashboard

docker compose --profile monitoring up -d   # also start Grafana (:3000)
```

The scheduled jobs sit under the `jobs` profile so they don't auto-start; run
them on demand (or from host cron / a scheduler on the ET times in `.env`):

```bash
docker compose run --rm nightly-tune
docker compose run --rm eod-replay
docker compose run --rm journal
```

Postgres credentials default to `momentum`/`momentum`/`momentum`; override with
`POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` in `.env`. Named volumes
`pg-data`, `app-data`, and `grafana-data` persist across restarts (the jobs share
`app-data` with the app so `nightly_tune.py`'s `learned_params.json` reaches the
loop). The Compose Grafana uses its own datasource override at
`deploy/compose/grafana/datasources/momentum.yaml` (pointed at the `postgres`
service); the legacy `grafana/docker-compose.yml` is unrelated and left for
Grafana-only use against an external DB.

## Build & publish the image

Pushes to the working branch / `main` / `v*` tags trigger
`.github/workflows/docker-publish.yml`, which builds and pushes to
`ghcr.io/fez5587/momentum-system`. Point the chart at a pushed tag via
`image.tag` (a commit SHA, or `latest` on the default branch).

Local build:

```bash
docker build -t momentum:dev .
```

## Install

```bash
helm install momentum ./deploy/helm/momentum \
  -n momentum --create-namespace \
  --set image.tag=latest \
  --set secrets.alpacaApiKey=YOUR_KEY \
  --set secrets.alpacaSecretKey=YOUR_SECRET
```

Without Alpaca keys the loop runs in **dry mode** (no live data/orders); the
dashboard and database still work.

### Secrets

The chart builds `DATABASE_URL` from the in-cluster Postgres service and a
password that is generated on first install (or set `postgres.password` to pin
it). Broker keys come from `secrets.*`.

For real keys, prefer an existing Secret over `values.yaml`:

```bash
kubectl -n momentum create secret generic momentum-broker \
  --from-literal=DATABASE_URL=... \
  --from-literal=ALPACA_API_KEY=... --from-literal=ALPACA_SECRET_KEY=... \
  --from-literal=APCA_API_KEY_ID=... --from-literal=APCA_API_SECRET_KEY=...
helm install momentum ./deploy/helm/momentum -n momentum \
  --set secrets.existingSecret=momentum-broker
```

When `secrets.existingSecret` is set the chart renders no Secret of its own and
expects that one to carry `DATABASE_URL`, the `ALPACA_*`/`APCA_*` keys, and
`POSTGRES_PASSWORD`.

## Reach the dashboard

```bash
kubectl -n momentum port-forward svc/momentum-app 8010:8010
open http://localhost:8010
```

Set `ingress.enabled=true` (with `ingress.host`/`className`) to expose it
instead.

## Verify

```bash
helm lint deploy/helm/momentum
helm template momentum deploy/helm/momentum | less

# After install:
kubectl -n momentum get pods
kubectl -n momentum exec sts/momentum-postgres -- psql -U momentum -d momentum -c '\dt'
kubectl -n momentum create job --from=cronjob/momentum-nightly-tune tune-test
```

## Notes & constraints

- **Singleton loop.** The trading loop holds in-memory state and one broker
  session; `replicas` must stay `1` and the strategy is `Recreate`.
- **Shared `data/` PVC is ReadWriteOnce.** The CronJob pods mount the same PVC
  as the app (so `nightly_tune.py` can write `learned_params.json` for the loop
  to read), which schedules them onto the app pod's node. For multi-node
  clusters, use an RWX StorageClass or split the volumes.
- **CronJob `spec.timeZone`** requires Kubernetes ≥ 1.27.
- The legacy `grafana/docker-compose.yml` is left in place for local use; the
  chart provisions its own Grafana against the in-cluster Postgres.

## Future hardening (roadmap)

The package deploys and runs, but the items below separate "it comes up" from
"runs reliably for a trading system." They are **not implemented yet** — captured
here so they can be picked up later. Each notes what, why, and the rough approach.

### Reliability

- **Loop-heartbeat liveness** *(needs an app-code change).* Today `/api/health`
  (`api/main.py:343`) returns a static `{"ok": true}`, which only proves the
  dashboard *thread* is alive. The trading loop runs in the main thread
  (`run_live_paper.py:328`), so a **crashed** loop exits the process and Kubernetes
  restarts it (fine), but a **hung** loop (stuck network call, deadlock) keeps
  answering health checks `200` and runs undetected. Approach: have the loop stamp a
  last-tick timestamp into `DashboardState` each iteration, make `/api/health` (or a
  new `/healthz`) return `503` when the last tick is older than a threshold, and
  point the Deployment's `livenessProbe` at it. Highest-value item.
- **Postgres backup CronJob.** The event store is the source of truth and nothing
  backs it up. Approach: a chart CronJob running `pg_dump` (postgres image, creds
  from the Secret) to the app `data` PVC with N-day retention; optionally offload to
  object storage.
- **Schema bootstrap hook.** The schema auto-applies on the app's first connection,
  so a first-run race between the app and a CronJob is possible. Approach: a
  `pre-install`/`pre-upgrade` Helm-hook Job that applies `storage/pg_schema.sql`
  deterministically, with `PG_SKIP_SCHEMA=1` set on the app and CronJobs.

### Security

- **Pod hardening.** Add a `securityContext` across workloads: `runAsNonRoot`, drop
  ALL capabilities, `seccompProfile: RuntimeDefault`, and `readOnlyRootFilesystem`
  where feasible (the app needs `/app/data` writable — already a mounted volume).
- **NetworkPolicy.** Default-deny in the namespace; allow only the app, CronJobs, and
  Grafana to reach Postgres:5432, and restrict who can reach the dashboard.
- **Grafana auth.** The chart inherits anonymous-admin from the original compose
  (`templates/grafana.yaml` env). Make it off by default in-cluster, with a real
  admin password sourced from the Secret (`GF_SECURITY_ADMIN_PASSWORD`).

### CI / operational

- **Chart CI validation.** Add a CI job running `helm lint` + `helm template` +
  `kubeconform` on every push — catches template/schema errors automatically (the
  chart could not be rendered in the sandbox where it was authored, since image-blob
  CDNs were egress-blocked).
- **Image digest pinning.** For production installs, pin `image` by SHA digest rather
  than the mutable `latest` tag.
- **PodDisruptionBudget / singleton guard.** A PDB for the singleton loop plus a
  guard so it is never scaled above `1` or evicted to zero unexpectedly.
