# Installing KM VMS

This guide covers a new KM VMS installation on a NAS or Linux server, first-run browser setup, and the normal update path. It describes the current user workflow, not historical development stages.

KM VMS is a Docker-based VMS/NVR for RTSP and ONVIF cameras. The web interface supports **English, Simplified Chinese (简体中文), and Russian (Русский)**.

## Before you start

Prepare the following on the NAS/server:

- Docker Engine **25.0 or later** and Docker Compose **2.20.2 or later**, or an implementation compatible with `healthcheck.start_interval`.
- Python 3, `curl`, a POSIX `sh`, archive utilities, and a working `getfacl` for filesystem permission inspection.
- A user with permission to run Docker and write to the intended application directory.
- Sufficient persistent storage for Docker images, application data, and your planned video archive.
- Network access to GitHub and the image/package registries used by the initial downloads and Docker builds.
- An available HTTP port; the default is **8088**.

Docker and host packages are not installed automatically. Host Node.js, npm, and Git are not required for the normal public GitHub installation.

TerraMaster NAS is the current hardware-validation environment. Other Linux/NAS systems need the same capabilities; a NAS brand name alone does not establish compatibility. Hardware acceleration is optional and depends on the host, drivers, and video format.

A GPU and `/dev/dri` are **not required**. The shared installer/update/restart launcher adds DRM device passthrough only when it is available on the Docker host; otherwise KM VMS uses software video processing. Helpers check the Docker host rather than mistaking their own private `/dev` for it. No periodic GPU probe runs in the background. To explicitly install in CPU-only mode, prefix the install command with `LIVE_HWACCEL_MODE=off`; this setting is retained in the installation's private `.env`.

## 1. Install a published release

The following commands select **[0.9.13](https://github.com/kmishnev87/km-vms/releases/tag/v0.9.13)** explicitly. Run them in a writable directory in the NAS/server terminal or over SSH:

```sh
curl -fsSL https://raw.githubusercontent.com/kmishnev87/km-vms/v0.9.13/scripts/install.sh -o km-vms-install.sh
sh km-vms-install.sh --branch v0.9.13
```

The first command downloads the installer. You can inspect it before running the second command.

The interactive default is `$HOME/km-vms` for application files/data and port `8088` for the web interface. Confirm the directory-creation prompt when it matches your intended location.

To choose your NAS's persistent application directory, replace `/path/to/km-vms` below with your own unused directory:

```sh
sh km-vms-install.sh --branch v0.9.13 --app-dir /path/to/km-vms
```

For unattended installation, both an explicit directory and confirmation flag are required:

```sh
sh km-vms-install.sh --branch v0.9.13 --app-dir /path/to/km-vms --yes
```

Optional flags:

| Flag | Purpose |
| --- | --- |
| `--app-dir PATH` | Set the application/configuration/data directory. |
| `--http-port PORT` | Change the web port from `8088`. |
| `--project-name NAME` | Set a separate Compose/container identity; useful for isolated installations. |
| `--branch vX.Y.Z` | Select an immutable published release tag. `--ref` is an alias. |
| `--dry-run` | Validate inputs and print an installation plan without starting installation; requires an explicit application directory. |

Use a project name beginning with a lowercase letter and containing only lowercase letters, digits, dashes, or underscores. Separate installations need separate application directories, project identities, and ports.

Check [GitHub Releases](https://github.com/kmishnev87/km-vms/releases) for newer published versions. If selecting another release, use its tag in both the installer download URL and `--branch`. Do not omit the release selection for an ordinary installation: the installer's default `main` is a moving development source, not automatic selection of the latest published release.

The public repository does not require a GitHub token. Git clone is not part of this normal installation path.

## 2. Let the installer prepare the services

The installer downloads the selected source, prepares Docker images/services, generates initial configuration and secrets, and starts setup mode. Initial installation includes downloads and builds, so its duration depends on your server and network.

It refuses dangerous application paths and unsuitable pre-existing directories. It does not install Docker, erase unrelated directories, or create a default administrator account.

The application directory and video archive location are separate:

- The **application directory** holds configuration, database/application state, and update-control data. Choose persistent storage with suitable permissions.
- The **archive location** holds video and is selected through browser setup. Do not place database backups inside the video archive.

Do not change permissions recursively across your NAS to work around an error. Fix the specific missing prerequisite or unsuitable target directory instead.

## 3. Complete browser setup

After the installer reports that setup mode is starting, open:

```text
http://<NAS-IP>:8088/setup
```

Replace `<NAS-IP>` with your server's address, and use the chosen port if you changed it.

Follow the wizard to select archive storage, complete the displayed initial settings, and create your owner account. Storage activation is handled by the wizard; normal setup does not require manually running storage/restart scripts after choosing the archive.

There is no shipped default owner/admin password. Keep your new account credentials private.

After setup, open the normal application address:

```text
http://<NAS-IP>:8088/
```

## 4. Add cameras and choose your language

In **Cameras**, add a camera using its RTSP or ONVIF connection details. Verify the connection, choose the appropriate lens/video source and streams where available, set recording options, and save.

ONVIF Media1/Media2 configuration is available when the camera exposes the required services and options. RTSP, H.265/browser playback, and hardware acceleration are not universal across all devices and clients.

Use **Settings → Language** to choose **English**, **简体中文**, or **Русский**.

## Camera events

Use the bell in the top navigation to open camera events. In camera setup or editing, enable **Receive camera events** under **Recording and events** after ONVIF capability confirmation. Reception is off by default for new cameras and cameras present before an upgrade; it is independent of continuous recording. One managed subscription serves each opted-in physical device. Turning reception off works even when a camera is offline and preserves its history. **Notifications** changes only your KM VMS camera/type/time preferences; **Detection**, available to camera managers, reads the camera's own rules. A detection switch is shown only when the device exposes a supported ONVIF boolean control. Devices without such controls remain usable for video and for events they provide.

An event can open its camera's live view or available archive footage around five seconds before the event. Chronology also has event markers and previous/next navigation. Missing recordings are not created by clicking an event. Notifications require the browser application to be open; native mobile background push and event-only recording are not included.

Use the eye/check icon above history to mark selected events, or all retained events when none are selected. This is a personal acknowledgement, not deletion. Events arriving after the loaded list snapshot remain unviewed. The normal published updater applies the registered schema migration for the camera reception choice; no manual database editing is required.

## Updating an existing installation

Prefer **Settings → Maintenance** to check and apply an available published release. The update workflow checks the candidate, prepares it, and verifies activation. A reported failure is not a completed update; inspect the provided status/diagnostics before retrying.

Do not rerun the new-install command over an existing installation. Do not replace application files manually, delete Docker volumes, or regenerate the existing configuration/secrets to force an update.

For the terminal update path, substitute your real application directory and an actual published tag. A non-mutating plan can be inspected first:

```sh
sh /path/to/km-vms/data/update-runtime/bootstrap/current/km-vms-update-launcher.sh \
  --app-dir /path/to/km-vms --branch vX.Y.Z --dry-run
```

After reviewing the plan, run the same launcher with `--yes` instead of `--dry-run`. The launcher's path is inside the existing installation; it is not the downloaded new-install script.

Keep configuration, database state, archive mounts, and required backup evidence intact. The updater's checks must not be disabled to force an incompatible migration or an unresolved recovery state.

## Backups and recovery

Database/configuration backups contain sensitive application metadata and must remain private. They do **not** include your video archive. Arrange a separate video-backup strategy if the footage needs another copy.

Keep backup storage separate from archive retention. Never use `docker compose down -v`, delete database volumes, or prune active resources as a routine repair step for a failed installation/update.

## Remote access

The quick start exposes **HTTP**, not HTTPS, and is intended for initial setup on a trusted network. It does not automatically configure router forwarding, certificates, or a secure remote-access path.

For remote access, use a properly secured HTTPS reverse proxy or VPN with appropriate access restrictions. Do not expose Docker control interfaces. Creating a login account does not encrypt HTTP traffic.

## Health and restarts

The configured Docker health checks are frequent during startup so service dependencies can become ready. After the first successful check, the steady-state interval is **24 hours**; HTTP probes use native `curl` rather than repeatedly starting Python.

Persistent services use Docker restart policies. A Docker health status alone does not restart a live but unhealthy process, and a daily check is not a promise of immediate hang detection. Use the application's System Health and explicit diagnostics when investigating a problem.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Docker/Compose not found or permission denied | Install the supported Docker/Compose implementation and grant the current user appropriate Docker access. |
| Missing Python, `getfacl`, or another host utility | Provide the named prerequisite using the NAS/server's supported administration method; the installer does not install packages. |
| Application directory rejected | Use a specific persistent directory that is new/unused or is explicitly suitable for this operation, not a system root or unrelated non-empty folder. |
| Port already in use | Select a free `--http-port` and check the installer's API-port validation. |
| Browser cannot open setup | Wait for the installer to finish, verify the printed server/port, and check host firewall and container status. |
| Camera connects but a setting is unavailable | Confirm the device's ONVIF service/encoder options, credentials, streams, and codec/browser compatibility. |
| Update does not finish | Read the operation result and diagnostics; do not erase state or replace files to hide the failure. |

For private diagnostic delivery, use **Settings → Security**: describe the problem, choose normal or extended diagnostics, confirm consent, and click **Send report**. The receiver at **кмпрайм.рф** accepts the ZIP over HTTPS; an accepted report gets an ID in the form. The developer-only receiving service is not an additional component of your installation. Sending is optional, one-way, and only happens after your explicit action. You can download the ZIP instead when sending is unavailable.

Reports include application diagnostics and logs, potentially with device names and addresses; known secret fields are redacted and video recordings are not included. Do not include passwords in the description. For sanitized public discussion, use [GitHub Issues](https://github.com/kmishnev87/km-vms/issues). Never publish diagnostic ZIPs, credentials, full camera connection URLs, tokens, database dumps, or private footage.
