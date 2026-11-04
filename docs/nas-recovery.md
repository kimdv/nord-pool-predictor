# NAS recovery after the PostgreSQL driver failure

The confirmed failure was SQLAlchemy 2.1 selecting `psycopg` for an unspecified
synchronous driver while the image installed `psycopg2`. The correction explicitly
selects `psycopg2`. Do not install packages into the running container as a permanent fix.

Current main requires an API and **exactly one worker**, sharing the same backend
image digest, database, model directory, and backend network. The API does not run
scheduled work. The worker starts migrations and queues bootstrap automatically.
Use the same release's web image for the seven-day dashboard window.

## Fresh installations versus existing databases

For a genuinely empty installation, the repository's `postgres:18-alpine` service
mounts `pgdata` at `/var/lib/postgresql`; its default data directory is
`/var/lib/postgresql/18/docker`. Use a new project/volume, start only PostgreSQL,
and verify `SHOW server_version` and `SHOW data_directory` before starting writers.
Docker CI creates a sentinel row using this Compose service, forces container
recreation, and checks the row survived on disposable storage.
See the [official image's PGDATA documentation](https://github.com/docker-library/docs/blob/master/postgres/content.md#pgdata).

For **any existing database**, complete the inventory and backups below before
changing the PostgreSQL image, mount target, project name, or container. An empty
named `pgdata` volume does not prove the installation is empty: with the previous
mount target, the live cluster may be in an anonymous volume at
`/var/lib/postgresql`. Preserve the original container and **all** its mounts until
you identify the live data and verify a restore. Never move a populated volume to
the new target, delete it, prune volumes, or treat a fresh empty cluster as recovery.

## 1. Inventory the actual deployment

Run on the NAS over SSH. These commands do not print environment variables:

```sh
sudo docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
sudo docker inspect nordpool-app nordpool-web nordpool-db \
  --format '{{.Name}} image={{.Image}} mounts={{json .Mounts}} networks={{json .NetworkSettings.Networks}}'
sudo docker inspect nordpool-app \
  --format 'compose_dir={{index .Config.Labels "com.docker.compose.project.working_dir"}} compose_files={{index .Config.Labels "com.docker.compose.project.config_files"}}'
df -h
```

Record the app/web image IDs, model volume or bind-mount source, `MODEL_DIR`,
PostgreSQL storage mount, network names, existing ports, and reverse-proxy routing.
Inspect any worker as well. Locate the original Compose/project definition in the
NAS container manager if labels are absent. Do not substitute new volumes for the
existing ones. Keep credentials and full Compose configuration private.

Read current database state using the container's configured account:

```sh
sudo docker exec -i nordpool-db sh -c \
'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -P pager=off' <<'SQL'
BEGIN READ ONLY;
SET LOCAL statement_timeout = '30s';
SHOW server_version;
SHOW data_directory;
SELECT filename, applied_at FROM _migrations_applied ORDER BY filename;
SELECT area, model_version, feature_set_version, trained_at, is_active
FROM model_versions ORDER BY trained_at DESC LIMIT 12;
SELECT job_type, status, started_at, finished_at, details_json
FROM job_runs ORDER BY started_at DESC LIMIT 20;
SELECT DISTINCT ON (area) area, model_version, issued_at, status
FROM forecast_runs WHERE status IN ('completed', 'scored')
ORDER BY area, issued_at DESC;
COMMIT;
SQL
```

Record those server values (not just the image tag or `PGDATA` environment value).
Map the returned data directory to the inspected mount's destination and source,
including anonymous volume names. Record the database image ID/digest and save
that image for rollback. If the server cannot start or you cannot locate its live
data directory, stop here and have the operator investigate the preserved storage;
do not initialize a replacement over it.

If `_migrations_applied` is absent, stop and reconcile schema history before starting
current main. Migration `002` clears derived data and prices; it must already be
recorded on the known quarter-hour NAS deployment. Do not rerun it or invent
migration records to bypass it. Migration `004` deactivates incompatible `v1`
models; newer production migrations preserve legacy snapshots with provenance.

## 2. Back up before starting an upgraded backend

Stop the old app and all existing workers with a long shutdown timeout (at least
30 minutes for active training). Stop any other process writing this database or
model directory. Leave PostgreSQL running. Keep web available if desired, although
its API will be unavailable during the maintenance window.

```sh
umask 077
BACKUP_DIR="$HOME/nordpool-recovery-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
sudo docker stop --time 1800 nordpool-app
# Stop each worker discovered in step 1 as well, before taking backups.
sudo docker inspect nordpool-app nordpool-web nordpool-db > "$BACKUP_DIR/containers.json"
sudo docker exec nordpool-db sh -c \
  'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$BACKUP_DIR/database.dump"
test -s "$BACKUP_DIR/database.dump"
sudo docker exec -i nordpool-db pg_restore --list < "$BACKUP_DIR/database.dump" > "$BACKUP_DIR/database.contents"
sudo docker exec nordpool-db sh -c \
  'exec pg_dumpall -U "$POSTGRES_USER" --globals-only' > "$BACKUP_DIR/globals.sql"
test -s "$BACKUP_DIR/globals.sql"
```

Check each command's exit status; a nonempty file or a successful archive listing
alone does not verify a usable backup. The globals file includes roles and may
include password hashes; keep it private with the rest of the backup.

Also copy the original deployment files (including private environment files),
worker configuration if present, and model directory into this private backup.
Archive the **discovered** model volume/bind mount while all writers are stopped;
preserve ownership and permissions. Record the app/web image digests and save the
old images with `docker image save` so rollback does not depend on a moving `latest`
tag. Verify free space and backup readability. Restore-test globals (reconciling
the restore instance's already-created administrator) and the dump into a separate
disposable PostgreSQL instance with a new volume. Use `psql -v ON_ERROR_STOP=1` for
globals and `pg_restore --exit-on-error` into an empty database; preserve the
original database owner and required extensions. Compare table counts, key records,
schema/migration history, and application read-only checks against the stopped-writer
source. Keep a verified copy off the NAS. Never use `down -v` on the deployment.

### 2a. Migrate PostgreSQL storage only if needed

An application recovery does not require replacing a working PostgreSQL service.
If keeping its server version and layout, leave its exact image, mount, and project
mapping intact and proceed to step 3. The repository's new mount is for fresh
PostgreSQL 18 storage, not an instruction to remount your existing data.

If upgrading an older major version to 18, or correcting an existing 18 cluster's
storage layout, use a logical dump/restore into a **new, separately named** volume
mounted at `/var/lib/postgresql`, leaving the original storage untouched. Rehearse
the restore first as above, including extensions and roles. The source server's
dump tools can produce a dump for a newer server; use PostgreSQL 18's restore tools
for the new target. Never start PostgreSQL 18 directly on an older major version's
files. See PostgreSQL's [supported upgrade methods](https://www.postgresql.org/docs/18/upgrading.html)
and [SQL backup/restore guidance](https://www.postgresql.org/docs/18/backup-dump.html).

For the final cutover, keep all application/worker writers stopped, take and verify
the final backup, then stop the original database container without removing it or
its volumes. Restore into the new isolated PostgreSQL 18 service and verify the
server version, data directory, ownership, extensions, counts, and read-only
application checks again. Recreate only the **new target's** container and verify
known restored records survive before pointing app/worker at it. Update the actual
NAS project definition to refer to that exact new volume/service, retaining the
original project files and storage mapping for rollback. Do not allow both clusters
to accept application writes. Start application writers only after these checks.

Before accepting writes on the new cluster, rollback is to stop the new services
and restart the original database with its saved image/configuration and original
mounts. After writes or application migrations, also follow step 5: restoring the
pre-cutover state loses subsequent writes, so preserve a new backup and reconcile
those writes before rollback. Do not copy PostgreSQL 18 data files back into the
older cluster or restore an 18 dump into an older major version as a downgrade.

## 3. Select and deploy the tested release

Wait for Backend CI (PostgreSQL 18), Docker CI, Frontend CI, and the Release
workflow to succeed for the exact commit. Release calls all three checks from its
own commit and publishes only if all succeed. Publishing holds a shared lock and
checks that the revision is still `main` after acquiring it; superseded releases
skip publishing. The lock alone does not guarantee commit order
([GitHub concurrency semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#concurrency)).
Release publishes `sha-<full-commit>`
tags and OCI revision labels. Its summary records backend and web digests.
Copy those digests into the NAS project definition; SHA tags identify the build,
but digest references ensure deployments cannot move when tags are overwritten.

Adapt the existing NAS Compose/project definition, using `docker-compose.yml` in
the repository as the service reference. Remove `build` entries from these NAS
services so they use the selected published images:

- `app`: `ghcr.io/kimdv/nord-pool-predictor-app@sha256:...`.
- `worker`: the **identical backend digest**, command
  `["python", "-m", "nordpool_predictor.worker"]`, restart policy, and
  `stop_grace_period: 30m`. Run one instance only.
- Both: identical `DATABASE_URL` using `postgresql+asyncpg://`, identical `MODEL_DIR`,
  model storage mount, configured training/retention/area settings, and backend
  network. Mount custom `areas.yaml` into both if the deployment uses one.
- `web`: the release's web digest, existing ports/routing and API base URL.
  Depend on API startup rather than successful readiness so the recovery UI can load.
- Keep the **existing PostgreSQL service and storage** unless you completed the
  verified migration in step 2a; then use that migration's exact target. Do not
  inadvertently create an empty project-prefixed volume. Preserve network names and proxy
  connections. The model mount must be writable by the backend's UID/GID 1000.

Pull the selected digest images. Before starting app/worker (their startup applies
migrations), use a one-off container with the worker service's configuration:

```sh
# Run in the actual NAS Compose project directory; include its existing -f options.
sudo docker compose run --rm --no-deps worker python scripts/check_database.py
sudo docker compose run --rm --no-deps worker python scripts/check_database.py --connect
```

The first check loads the sync driver without database access. The second runs only
read-only `SELECT 1` checks through synchronous and asynchronous engines. Neither
prints credentials. If either fails, resolve it before starting the upgraded services.

Start app, worker, and web through that same project definition. Do not retain the
old scheduler-enabled app alongside the new worker. Record the deployed digest and
`org.opencontainers.image.revision` for each container. This procedure does not
require publishing PostgreSQL's port to the LAN.

## 4. Follow bootstrap, then verify recovery

```sh
sudo docker compose logs --tail 100 -f worker
```

Bootstrap may take substantial time for backfills and Optuna training. Do not queue
duplicate training while it is pending/running. Readiness returning HTTP 503 during
bootstrap or stale-input recovery is expected; process liveness is separate.

Use the same read-only `psql` wrapper from step 1 for these queries:

```sql
SELECT job_name, status, created_at, started_at, finished_at, error
FROM job_requests ORDER BY created_at DESC LIMIT 12;

SELECT area, model_version, feature_set_version, trained_at,
       metrics_json->'production_data' AS production_data
FROM model_versions WHERE is_active ORDER BY area;

SELECT fr.area, fr.model_version, fr.issued_at, fr.horizon_steps,
       COUNT(fv.ts) AS slots, MIN(fv.ts) AS forecast_start, MAX(fv.ts) AS forecast_end
FROM forecast_runs fr LEFT JOIN forecast_values fv USING (run_id)
WHERE fr.run_id IN (SELECT run_id FROM latest_forecast_runs)
GROUP BY fr.area, fr.model_version, fr.issued_at, fr.horizon_steps
ORDER BY fr.area;
```

Acceptance:

- Bootstrap completes and both areas have usable active `v2-asof` models, with
  model artifacts on the shared mount. No missing-driver failures appear.
- Each latest successful run contains 768 quarter-hour values (seven days plus a
  one-day refresh buffer), has issuance no
  older than 26 hours and has future values. Also inspect the newest job attempts:
  an old successful run must not conceal a new failure.
- `/api/health/live` and `/api/health/ready` return 200 after recovery; verify
  readiness from inside the API container, where `httpx` is installed:

  ```sh
  sudo docker compose exec app python -c \
    'import httpx; r=httpx.get("http://localhost:8000/api/health/ready"); print(r.status_code); r.raise_for_status()'
  ```

- Production metrics report coverage/provenance and explicitly identify a baseline
  without production. Source ingestion remains successful and one worker owns jobs.
- The dashboard shows seven days ahead, actual prices (including zero) take
  precedence, and missing values remain gaps. Check Home Assistant if configured.

If bootstrap fails, inspect the specific job error before retrying. Use the
dashboard's job controls, or `POST /api/jobs/trigger/{job_name}`,
for remaining `retrain_model`, `refresh_forecast`, or ingestion work **after**
bootstrap exits. These endpoints intentionally require no credentials; there is no
application admin token. Job names are allowlisted, requests are durably queued,
and duplicate pending/running jobs return HTTP 409; these are execution controls,
not access controls. The web proxy also forwards job triggers. Keep **both** the
dashboard and API within a trusted local network/VPN or behind access controls at
the reverse proxy/firewall. Compose's published ports listen on all host interfaces
by default; Docker network isolation and CORS do not authenticate callers. Verify
the NAS's actual routing and firewall rules rather than assuming they enforce this
boundary. Never manually toggle active models or edit
forecast values. Record both model versions and forecast issuance times.

Confirm the next scheduled forecast refresh and next scheduled weekly retraining
succeed. Score new forecasts as actuals arrive and compare against baselines before
drawing conclusions about model accuracy. Operational recovery is not an accuracy
guarantee.

## 5. Rollback

Stop new app/worker writers first. Restore the old deployment configuration and
saved images **together with** the pre-upgrade database and model archive, preserving
ownership. Preserve the failed/new cluster and model files before rollback. If
PostgreSQL was migrated, use the recorded original database image and storage layout,
with an isolated restore volume when restoring the backup; never overwrite the
preserved original volume. Have the NAS operator restore the pre-upgrade dump into
a clean database using the
recorded account/configuration, rather than layering it over migrated tables.
Verify consistency before restarting the old scheduler. This restores its former
state, including the original driver issue unless separately corrected; it is a
maintenance rollback, not a resolution. Do not assume old images understand the
migrated schema. A pre-upgrade restore discards later writes; retain a new backup
and reconcile them before rollback if the new deployment accepted writes.
Backups can contain credentials and must remain private.
