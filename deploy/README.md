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
