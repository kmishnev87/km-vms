# KM VMS Installation

KM VMS is installed on NAS/server systems through `scripts/install.sh`. The installer is POSIX `sh` compatible and does not require host Python, Node, npm, or global package installation.

Supported platform class: TerraMaster, Synology, QNAP, TrueNAS, Unraid, and generic Linux servers with Docker and Docker Compose. The installer does not hardcode vendor storage paths; choose an app directory explicitly.

Interactive install:

```sh
sh scripts/install.sh --app-dir "$HOME/km-vms"
```

Non-interactive install:

```sh
sh scripts/install.sh --app-dir "$HOME/km-vms" --http-port 8088 --project-name km-vms --yes
```

Public download shape for future releases:

```sh
curl -fsSL https://raw.githubusercontent.com/kmishnev87/km-vms/main/scripts/install.sh | sh -s -- --app-dir "$HOME/km-vms"
wget -qO- https://raw.githubusercontent.com/kmishnev87/km-vms/main/scripts/install.sh | sh -s -- --app-dir "$HOME/km-vms"
```

## Prerequisites

- Docker must already be installed.
- Docker Compose plugin (`docker compose`) is preferred; `docker-compose` fallback is supported.
- If Compose is installed outside `PATH`, set `KM_VMS_DOCKER_COMPOSE` to `docker`, `docker-compose`, or an executable compose path. Command strings with spaces or shell metacharacters are rejected.
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

The compose file keeps the existing production-compatible default project identity and `vms-*` container names. Disposable installs use explicit `--project-name` / `COMPOSE_PROJECT_NAME` and `KM_VMS_CONTAINER_PREFIX` values for isolation.

Stage 1 uses `<app-dir>/data/archive` as a provisional host archive mount when the user has not selected a storage root. The stable container archive path remains `/storage/archive`. Rich storage discovery and folder selection are deferred to Stage 2.

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

## Development Source Mode

For disposable validation before public GitHub raw files are available:

```sh
sh scripts/install.sh --app-dir /tmp/km-vms-stage1-install-test --http-port 18088 --project-name km-vms-stage1-installer-test --source-dir /path/to/km-vms --yes
```

`--source-dir` copies product source while excluding `.git`, `.env`, node build output, runtime data, logs, service artifacts, archives, and temporary installer test directories.

`--source-dir` and `--app-dir` must be separate paths. The installer rejects equal paths, nested paths in either direction, dangerous source paths, and source trees missing expected KM VMS markers. Source-dir mode is only for development/testing validation; it is not the public default install path.

## Future Stages

Stage 2 will add storage discovery and folder selection.
Stage 3 will add the full first-run wizard.
Stage 4 will add restart, upgrade, rollback, and validation hardening.
