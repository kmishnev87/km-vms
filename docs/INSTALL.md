# KM VMS Installation

KM VMS is installed on NAS/server systems through `scripts/install.sh`. The installer is POSIX `sh` compatible and does not require host Python, Node, npm, or a package manager bootstrap.

Supported platform class: TerraMaster, Synology, QNAP, TrueNAS, Unraid, and generic Linux servers with Docker and Docker Compose. The installer does not hardcode vendor storage paths; choose an app directory explicitly.

## Supported Install Shapes

1. Local unpacked repository already present on the NAS.
2. GitHub tarball acquisition without `git`.
3. Generic `git clone` acquisition only when you explicitly pass `--repo-url`.

Local unpacked repository install:

```sh
sh scripts/install.sh --app-dir "$HOME/km-vms" --http-port 8088 --project-name km-vms --yes
```

Private GitHub repository install without `git`:

```sh
sh /path/to/km-vms-install.sh \
  --app-dir "$HOME/km-vms" \
  --github-repo OWNER/REPO \
  --branch main \
  --github-private \
  --http-port 8088 \
  --project-name km-vms
```

The private GitHub path prompts for a token with hidden input when no secure env/file token source is supplied. Use a read-only token with repository contents access only. Obtain `km-vms-install.sh` from an authorized source for your private repository; do not rely on a public `raw.githubusercontent.com` URL for a private repo. If the token is exposed, revoke and rotate it.

## Prerequisites

- Docker must already be installed.
- Docker Compose plugin (`docker compose`) is preferred; `docker-compose` fallback is supported.
- Compose detection order is: explicit override (`KM_VMS_DOCKER_COMPOSE` / `KMVMS_DOCKER_COMPOSE`), `docker compose` in `PATH`, `docker-compose` in `PATH`, then known NAS vendor paths such as TerraMaster DockerEngine locations.
- If you need an explicit override, set `KM_VMS_DOCKER_COMPOSE` to `docker`, `docker-compose`, exact friendly alias `docker compose`, or an executable compose path. Other command strings with spaces or shell metacharacters are rejected; helpers do not use `eval` or `sh -c` for this value.
- The current user must be allowed to run Docker commands.
- The installer never installs Docker or packages automatically.

## App Directory

Pass `--app-dir` or `KM_VMS_APP_DIR`. Non-interactive and dry-run modes require an explicit app directory.

The installer rejects dangerous paths such as `/`, `/etc`, `/usr`, `/var`, `/root`, and ambiguous non-empty directories. It creates the app directory only after confirmation or `--yes`, tests bounded write/delete access, and does not delete existing contents.

## Environment Generation

When `.env` is absent, the installer generates it with strong random values for:

- `POSTGRES_PASSWORD`
- `JWT_SECRET`
- `ENCRYPTION_KEY`
- internal bootstrap `ADMIN_PASSWORD`

Secrets are not printed. Existing `.env` is preserved; overwrite is not implemented in Stage 1.0.

The generated environment includes the compose/backend variables used by the current product, including `SURVEILLANCE_ROOT`, `HTTP_PORT`, `API_PORT`, `NEXT_PUBLIC_API_BASE_URL`, `COMPOSE_PROJECT_NAME`, and `KM_VMS_CONTAINER_PREFIX`. If `KM_VMS_API_PORT` is not provided, the installer derives a non-default API port from the selected HTTP port so disposable installs do not collide with an existing stack.

The compose file keeps the existing production-compatible default project identity and `vms-*` container names. Disposable installs use explicit `--project-name` / `COMPOSE_PROJECT_NAME` and `KM_VMS_CONTAINER_PREFIX` values for isolation. Project names must start with a lowercase letter and contain only lowercase letters, digits, dashes or underscores. Uppercase or unsafe names are rejected with a lowercase suggestion instead of being silently transformed.

Stage 1 uses `<app-dir>/data/archive` as a provisional host archive mount when the user has not selected a storage root. The stable container archive path remains `/storage/archive`.

The installer also records non-secret provenance files:

- `.km-vms-install.json`: app dir, ports, compose command, source mode, setup URL;
- `.km-vms-source.json`: GitHub repo/ref and commit SHA when available, or source-dir / git-clone provenance without secrets.

## Storage Discovery And Selection

Stage 2 adds host-side storage discovery for setup mode. The installer runs:

```sh
sh scripts/km-vms-storage-discovery.sh --app-dir "$HOME/km-vms"
```

The helper writes a sanitized, non-secret snapshot to `<app-dir>/data/install-control/storage-discovery.json`. Setup mode reads that snapshot, shows only primary user-facing NAS roots in the main dropdown, offers a controlled manual-root fallback, and keeps `/storage/archive` as the stable container bind path.

The setup page accepts a single folder name under a discovered or manually approved host root. Absolute paths, separators, traversal, control characters, dangerous system paths, pseudo filesystems, and non-empty unmarked folders are rejected server-side. Safe Unicode/Cyrillic folder names are supported. The selected folder receives `.km-vms-storage-root.json` after a write/delete probe succeeds.

After choosing storage in setup mode, KM VMS now queues activation automatically through the bounded setup helper. The operator does not run `km-vms-storage-apply.sh` or `km-vms-restart.sh` manually from the shell after clicking in the wizard.

`km-vms-storage-apply.sh` still updates only `SURVEILLANCE_ROOT` in `.env`, writes a non-secret `data/install-control/storage-apply-status.json` status, validates compose config, and reports `applied_restart_required` until the restart helper verifies the active mount. It does not print `.env`, regenerate secrets, create users/settings, delete DB/volumes/archive data, run `docker compose down -v`, or run `docker system prune`.

Restart diagnostics:
- validate config first: `KM_VMS_DOCKER_COMPOSE="docker compose" sh scripts/km-vms-restart.sh --app-dir "$HOME/km-vms" --project-name km-vms`;
- missing `.env`, missing storage selection, Docker unreachable, bad compose override, invalid project name, unwritable selected storage and failed compose config are reported as explicit errors;
- previous Git HEAD may be recorded in service artifacts as diagnostic context only;
- full backup/restore/rollback belongs to the future Database / Backup / Upgrade Safety PRO chapter. For now, do not use `down -v`, do not prune Docker, and do not delete production DB, volumes, archive roots or recordings.

The API writes both JSON status files for the wizard and shell-safe line-based control files for host helpers. The apply helper reads `<app-dir>/data/install-control/storage-selection.control`, revalidates the selected single child folder on the host, blocks symlink/path escapes and non-empty unmarked folders, writes the KM VMS marker only after a write/delete probe, updates only `SURVEILLANCE_ROOT` in `.env`, creates a `.env.stage2-storage.bak` backup, and does not print secrets. The setup helper reads `<app-dir>/data/install-control/storage-activation-request.control`, then runs the safe restart helper and waits until the recreated API and recorder containers expose the selected marker through `/storage/archive` before the wizard unlocks `Next`. After first-run setup completes, the API writes `data/install-control/setup-complete.json`; the setup helper exits and becomes inert. The API container does not mount `/var/run/docker.sock`; Docker socket access is confined to the setup-only helper path.

After activation, authorized settings/storage screens show the selected NAS/server host archive path as the primary archive path. `/storage/archive` remains the stable Docker/container path used by the API and recorder containers.

## Setup Mode

After compose starts, open:

```text
http://localhost:<http-port>/setup
```

or:

```text
http://<NAS-IP>:<http-port>/setup
```

The first owner account is created only through the setup page. Fresh uninitialized startup does not create a hidden admin. Existing initialized systems keep their users and owner state.

## Restart After Setup

Use the safe restart helper:

```sh
sh scripts/km-vms-restart.sh --app-dir "$HOME/km-vms"
```

The helper validates compose files, reuses existing `.env`, does not regenerate secrets, does not wipe database/storage, and does not create owner/settings.

## Terminal Update

Installed KM VMS instances can be updated in place from a GitHub tarball without requiring `git` on the NAS/server:

```sh
cd "$HOME/km-vms"
sh scripts/update.sh --github-repo OWNER/REPO --branch main --yes
```

Run a non-mutating plan first:

```sh
cd "$HOME/km-vms"
sh scripts/update.sh --github-repo OWNER/REPO --branch main --dry-run
```

For a private GitHub repository, use one of the secure token paths:

```sh
KM_VMS_GITHUB_TOKEN_FILE=/path/to/read-only-token \
  sh scripts/update.sh --github-repo OWNER/REPO --branch main --github-private --yes

KM_VMS_GITHUB_TOKEN=... \
  sh scripts/update.sh --github-repo OWNER/REPO --branch main --github-private --yes

sh scripts/update.sh \
  --github-repo OWNER/REPO \
  --branch main \
  --github-private \
  --github-token-env KM_VMS_GITHUB_TOKEN \
  --yes
```

The token is used only for GitHub API requests and must be read-only for repository contents. The updater does not print the token, does not embed it in URLs, and does not write it to `.km-vms-update.json`, `.km-vms-source.json`, reports or logs.

The updater overlays only product source/configuration paths: `apps/`, `deploy/`, `docs/`, `scripts/`, compose files, `.dockerignore`, `.gitignore`, and `.env.example`. It preserves `.env`, `.env.*`, `data/`, PostgreSQL/Redis data, previews, exports, install-control files, selected storage roots and recordings. It validates the app directory, validates the downloaded source tree, rejects path traversal and symlinks in the update source, reuses `scripts/km-vms-compose-common.sh`, runs compose config validation, then runs `up -d --build`.

The updater must not run `down -v`, `docker system prune`, delete Docker volumes, regenerate secrets, change the selected storage path, create users/settings, or automatically run database migrations. If a future release needs explicit migration orchestration, use the dedicated migration/update stage instead of treating `update.sh` as a hidden migrator.

The rollback is not implemented in Stage 6.0.7. If the update fails before the overlay phase, app source files are not changed. If it fails after overlay starts, source files may be partially updated; fix the reported failed phase and rerun the same update, or restore from an external backup if the app cannot recover. The updater writes sanitized status to `.km-vms-update.json`.

The read-only update status and release manifest check API is available through protected owner/admin endpoints. The in-app update apply UI, helper container and progress polling remain future stage work. Terminal update is still the bounded base mechanism for applying updates.

## Database Schema Versioning

Stage 2 of Database / Backup / Upgrade Safety introduces API-owned schema version metadata. The API bootstrap keeps the existing `create_all` and manual compatibility ALTER flow for now, then records the current managed schema baseline and an immutable adoption/history event in DB metadata tables.

Schema version and app/build version are separate values. Existing unversioned databases are adopted only after a bounded schema-shape classification. Known safe drift is reported, while unknown, future, downgrade or partial adoption states block automatic upgrade work. Stage 2 does not implement the deterministic migration runner, backup-before-upgrade, restore or rollback; those remain later stages.

The schema status is available to owner/admin users through the protected schema status API. Recorder startup remains a legacy schema SQL participant for recording/runtime tables, but it does not own, write or interpret schema version metadata in Stage 2.

Stage 3 adds the API-owned deterministic migration runner contract on top of `schema_version_state` and `schema_migration_history`. The runner builds a read-only ordered plan before legacy `create_all` and manual compatibility ALTER can mask unsafe drift, classifies migrations as `metadata_only`, `additive_safe`, `risky_requires_backup`, or `manual_only`, and executes only eligible safe migrations through the controlled runner path.

The current product schema is already at the accepted Stage 2 baseline, so Stage 3 does not register a real production schema migration. Runner behavior is validated with isolated test-only migration registries and disposable PostgreSQL scenarios. `risky_requires_backup` and `manual_only` migrations are planned but not executed before backup safety and manual authorization rules are satisfied. Existing live production adoption/version metadata remains deferred unless explicitly authorized. `APP_BUILD_VERSION` remains a temporary metadata value; installed build/release/source-channel versioning belongs to Stage 7.

Stage 4 adds the backup-before-upgrade safety contract. The backend can build a read-only backup plan, create a controlled DB backup for PostgreSQL or file-backed SQLite/test DBs, write a manifest and sanitized metadata snapshot next to the backup artifact, and verify existence, size, checksum, recency and manifest consistency. DB backup artifacts are sensitive: they may contain users, password hashes, encrypted camera credentials, audit metadata and recording metadata. They must be stored outside the product Git tree, outside Working folder/service artifacts, outside archive/video folders, with restrictive permissions where the platform supports them. Video archive files and recordings are excluded from DB backup. Service/code/diagnostic archives must not include real DB dumps or backups.

Production/runtime PostgreSQL backups are created by the API container using `pg_dump`; the production API image includes PostgreSQL client tooling for this path. The default runtime backup destination is the container path `KMVMS_DB_BACKUP_ROOT=/storage/backups/db`, mounted from the persistent host runtime directory `KMVMS_HOST_DB_BACKUP_ROOT=./data/backups/db`. The installer creates this host directory with restrictive permissions where supported. This directory is runtime data, is gitignored through `data/`, and must be excluded from code, service and diagnostic archives.

The DB backup root must stay separate from `SURVEILLANCE_ROOT` and the video archive mount. Do not set `KMVMS_HOST_DB_BACKUP_ROOT` equal to or inside the selected archive root, and do not point `KMVMS_DB_BACKUP_ROOT` at `/storage/archive`, previews, exports, `/tmp`, Working folder, service artifacts or source-controlled code. Backup files are not recording segments and are outside retention, delete and reconciliation archive-data scope.

Backup manifests record `restore_validation_status = not_performed_stage5_deferred`; Stage 4 verification is not restore validation. Stage 5 is responsible for restore/rollback validation. A valid recent backup manifest can satisfy the backup precondition for `risky_requires_backup` migration plans, but it does not make `manual_only` migrations automatic and does not enable production startup auto-execution. Production adoption/migration must still be explicitly authorized.

Stage 5 adds restore validation for Stage 4 PostgreSQL custom-format DB backups. Restore validation is backend-owned and internal: it restores with `pg_restore` only into an explicitly disposable PostgreSQL target whose database name uses the Stage 5 disposable prefix, refuses the source/current/live database, refuses targets that already contain KM VMS product tables, validates owner login contract, users, cameras, settings, schema version/history, migration plan readability, recording/archive metadata and audit summary, then writes a separate restore-validation manifest. The original backup dump and manifest are not mutated. Video archive files are not restored by Stage 5; only DB metadata paths are validated.

Stage 6 adds a read-only upgrade report for diagnostics. The report summarizes app/build version source, schema version, migration history, migration runner plan, production adoption/migration state, backup status, restore-validation status, backup-root contract/evidence and operator warnings. It is exposed through the protected `/system/upgrade/report` API and included in diagnostic archives as `upgrade/report.json` plus `upgrade/summary.txt`. Report generation does not create backups, run restore validation, execute migrations, write schema/adoption metadata or run backup-root marker/write probes. When no safe product-owned backup or restore-validation status source is connected, the report uses `source_unavailable`/unknown semantics instead of claiming `not_performed`; service-level test manifests are labelled test/disposable and are not accepted from browser/API users. The report separates configured backup-root contract from proven persistence evidence, so a configured persistent contract is not treated as host-mount proof. Redaction status is scoped to checked outputs rather than a blanket artifact pass. Backup paths are represented as sanitized labels, and diagnostic archives must not include real DB dumps, backup artifacts, restore artifacts, `.env` files, secrets, password hashes, RTSP credentials or video archive files. Installed build identity is supplied by the Stage 7 metadata model, and video archive restore remains outside DB restore validation.

Stage 6.0.8 adds the safe product update status and release manifest check contract. Installed source/update identity is read from sanitized `.km-vms-source.json` and `.km-vms-update.json` metadata plus non-secret build metadata/env (`KMVMS_BUILD_METADATA_FILE`, `KMVMS_BUILD_ID`, `KMVMS_GIT_COMMIT`, `KMVMS_BUILD_TIME`, `KMVMS_INSTALL_SOURCE`, `KMVMS_SOURCE_CHANNEL_ID`) with a development fallback when release metadata is unavailable; app/build version remains separate from DB schema version. Update checking is disabled/not configured by default. This stage supports only a trusted server-configured local/static release manifest through `KMVMS_UPDATE_MANIFEST_PATH` and optional `KMVMS_UPDATE_CHANNEL_ID`; it does not accept arbitrary URLs, repo/ref/token/path values from API/UI users and does not implement remote internet fetching. Protected owner/admin endpoints `/system/update/status` and `/system/update/check` expose only normalized sanitized status, release summary, current/latest comparison and blockers/warnings. Diagnostic archives include cached update status as `update/status.json` and in the upgrade report, but archive creation does not trigger network/update checks and must not include update packages, DB dumps, restore artifacts, `.env` files or secrets.

Stage 6.0.9 adds bounded in-app apply orchestration. Owner/admin can request update apply only after explicit confirmation and only for the trusted server-configured manifest result. The API writes a sanitized request to `data/update-control`, reads sanitized helper status from the same control area, and still has no Docker socket and does not run Docker, Compose or `scripts/update.sh` directly. The dedicated `update-helper` service has no public ports, owns Docker socket access for the update operation model, runs `scripts/update.sh` with server-side configured source/token settings, writes progress/status, and remains inert without a valid request. In-app apply is pinned to the exact trusted manifest commit; the manifest branch/ref is display metadata, while the helper downloads the GitHub tarball by commit and verifies post-apply metadata before reporting success. Because Docker bind mount sources are resolved by the host daemon, `KM_VMS_HOST_APP_DIR` must point to the real host app directory and be mounted into `update-helper`; otherwise the helper refuses apply before overlay/recreate. UI users cannot provide tokens, URLs, repositories, refs, filesystem paths, backup paths, commands, images or environment variables. If a private source needs a token, configure the token server-side for the helper; it must not be entered in the browser or written to reports/artifacts.

Startup integration remains intentionally conservative: the pre-bootstrap runner hook is preflight/block-only for ready migration plans and does not auto-execute production migrations during startup. Controlled execution exists as an internal runner path for eligible migrations, but production startup execution requires later backup/rollback safety work and explicit authorization.

Legacy `Base.metadata.create_all` and narrow manual compatibility SQL remain temporarily for fresh install and historical compatibility. Future stages should move additional bounded schema changes under the ordered runner and keep recorder outside schema migration ownership.

## Development Source Mode

For disposable validation before public GitHub raw files are available:

```sh
sh scripts/install.sh --app-dir /tmp/km-vms-stage1-install-test --http-port 18088 --project-name km-vms-stage1-installer-test --source-dir /path/to/km-vms --yes
```

`--source-dir` copies product source while excluding `.git`, `.env`, node build output, runtime data, logs, service artifacts, archives, temporary installer test directories, `.ssh`, `id_rsa`, `id_ed25519`, `*.pem`, `*.key`, `*.p12`, `*.pfx` and related secret/credential paths.

`--source-dir` and `--app-dir` must be separate paths. The installer rejects equal paths, nested paths in either direction, dangerous source paths, and source trees missing expected KM VMS markers. Source-dir mode is only for development/testing validation; it is not the public default install path.

## Future Stages

Future work may add a trusted remote/signed release channel and a separate explicit update-apply workflow. Stage 7 only checks and reports.
