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

The following commands select **[0.9.29](https://github.com/kmishnev87/km-vms/releases/tag/v0.9.29)** explicitly. Run them in a writable directory in the NAS/server terminal or over SSH:

```sh
curl -fsSL https://raw.githubusercontent.com/kmishnev87/km-vms/v0.9.29/scripts/install.sh -o km-vms-install.sh
sh km-vms-install.sh --branch v0.9.29
```

The first command downloads the installer. You can inspect it before running the second command.

The interactive default is `$HOME/km-vms` for application files/data and port `8088` for the web interface. Confirm the directory-creation prompt when it matches your intended location.

To choose your NAS's persistent application directory, replace `/path/to/km-vms` below with your own unused directory:

```sh
sh km-vms-install.sh --branch v0.9.29 --app-dir /path/to/km-vms
```

For unattended installation, both an explicit directory and confirmation flag are required:

```sh
sh km-vms-install.sh --branch v0.9.29 --app-dir /path/to/km-vms --yes
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

**Find on network** is available for both connection methods. Local ONVIF WS-Discovery runs on the NAS host network, not inside the API's isolated Docker network. A separate restricted helper exposes no public control port and runs searches only on request; normal operation does not poll cameras in the background.

For a routed LAN or VPN, select a detected route or enter specific private IPv4 addresses/CIDRs. A search is limited to 1,024 addresses and twelve ports per protocol; the default ports include ONVIF 2020 for compatible devices and RTSP 554/8554. Advanced ports can be entered explicitly. Progress counts checked addresses; a time-limited or interrupted search is reported as partial rather than as a complete empty result. Existing cameras remain visible in discovery, and selecting a result does not modify or delete an existing camera.

Discovery does not configure a VPN, forward multicast through routers, guess camera passwords, or prove that every device in a subnet is reachable. A device must answer ONVIF discovery, the anonymous ONVIF device request, or RTSP OPTIONS on a tested port. Some cameras require a different port, manual address/path, or a protocol enabled in their own settings. RTSP-only discovery identifies a server, not its authenticated stream path; the normal connection check still applies. IPv6-only discovery is not included.

In **Storage → Recording and retention → Configure cameras**, edit segment minutes, retention days, and archive limits without leaving Storage. Use shared values for selected cameras or edit rows individually. Saving validates and commits the changed rules together; conflicting external edits require a reload. Lowering retention or limits can make older footage eligible for the existing automatic cleanup. Connection details, event reception and recording enablement are not changed by this editor.

Use **Settings → Language** to choose **English**, **简体中文**, or **Русский**.

## Camera events

History and personal notification selection support eleven useful types: motion, person, vehicle, animal, zone intrusion, line crossing, loitering, tampering, sound, wrong direction, and smoke/flame. Only reported camera semantics are used. A rule can retain several object classes, and the same event can match either its rule or class filter. Clock synchronization, known service messages, and excluded abandoned/removed-object messages do not enter history. Unfamiliar signals are bounded receiver diagnostics, not generic detection spam. The camera's NTP and detection settings are not changed by these filters.

Use the bell in the top navigation to open camera events. In camera setup or editing, enable **Receive camera events** under **Recording and events** after ONVIF capability confirmation. Reception is off by default for new cameras and cameras present before an upgrade; it is independent of continuous recording. One managed subscription serves each opted-in physical device. Turning reception off works even when a camera is offline and preserves its history. Camera managers can change these same shared reception switches in the event reception tab, in either the web interface or Android. This controls NAS event collection, not the camera's own detection or AI rules.

**Notifications** changes only your account's camera/type/time preferences. Select common hours or individual camera schedules; hours use the NAS time zone, and an interval can cross midnight. Equal start and end times mean all day. Events arriving while excluded by your preferences remain in history as viewed, without increasing your unread count or sending a push. Previously unread events keep their status when you change preferences, and muted arrivals do not become unread later. The Android **Alerts on this phone** switch controls delivery to that phone only; it does not mark NAS events viewed or change reception.

An event can open its camera's live view or available archive footage around five seconds before the event. Chronology also has event markers and previous/next navigation. Missing recordings are not created by clicking an event; brown event markers remain visible without a recording. Browser notifications require the web application to be open. Native mobile push uses a compatible provider as described below; recording triggered only by events is not included.

Identical active triggers less than 45 seconds apart are discarded before history and push delivery. Each repeat advances the sliding interval; separate cameras, rules, types and reported object classes have independent intervals. ONVIF endings are still processed. The system does not create an expandable list of suppressed repeats.

Use the eye/check icon above history to mark selected events, or all retained events when none are selected. This is a personal acknowledgement, not deletion. Events arriving after the loaded list snapshot remain unviewed. The normal published updater applies the registered schema migration for the camera reception choice; no manual database editing is required.

With camera-management permission, the adjacent trash icon deletes selected events or the full retained snapshot after confirmation. After selecting the current page, **Select all matching** freezes the filtered selection across pages and permits individual exclusions. Newer arrivals are not silently added. Deletion affects shared event history and its read markers, not video files, camera configuration, personal notification settings or administrative audit history.

## Live mode: Fast or Stable

Open **Settings → Live mode**, immediately after the recording format. The same setting is available in Android **Settings → NAS settings**. An owner or administrator changes it for the whole NAS, not just the current account or phone.

- **Fast** sends live video continuously over WebSocket/fMP4 instead of waiting for HLS segments. It reduces viewing delay and usually improves startup, but provides less protection against connection interruptions.
- **Stable** is the default on new installations and upgrades. It preserves the existing HLS viewing path; opening a PTZ control panel enables the fast path for that camera only.

In Fast mode, a confirmed unsupported stream/client combination switches only that stream to HLS and shows an incompatibility explanation beside its camera name. The other cameras and the shared setting remain unchanged. Network interruptions use reconnection, not an incompatibility label. Main/sub selection, fullscreen and audio controls remain available. These modes do not change recording, clips, archive playback or chronology.

New or resumed viewing uses the current setting, and an already-open view receives changes through its normal media-authority renewal. Reopen another device's existing view to apply a change immediately. No separate continuous settings polling is introduced. The managed updater adds the shared setting with Stable as its default; no manual database changes are needed.

## Native Android client

1. Open **Settings → Download APK** in the NAS web interface or download **km-vms-android-0.8.12.apk** from the [0.9.29 release](https://github.com/kmishnev87/km-vms/releases/tag/v0.9.29). The NAS release includes its matching APK information; no separate web release check is required.
2. Install it on Android **8.0 or later**, allowing installation from the selected source when Android asks.
3. Add the NAS address (a local **IP:port** is sufficient) and sign in with its existing account. Saved server profiles and sessions are separate; no new NAS administrator or camera account is created.
4. Both HTTP and HTTPS work. HTTP requires confirmation of its unencrypted connection; HTTPS retains normal certificate and hostname verification. Installing the APK does not obtain a certificate, change the NAS site or configure remote access.
5. After the first login to each NAS, accept the notification setup offer to request Android permission and register an available **UnifiedPush** provider. Declining is remembered for that NAS; existing explicit disabling is preserved. Revisit **Events → Notifications → Alerts on this phone** to change the choice. Provider/network access, OS notification permission and battery restrictions apply. The app does not poll the NAS in the background or guarantee delivery on every untested Huawei/microG configuration.

The app checks the official APK metadata on the first foreground opening in a new day and on manual request. It checks size, hash, package/version and signing certificate before offering the system installer. Download and Android installation confirmation are explicit; the update keeps app data and NAS profiles. The NAS and mobile version numbers are independent. Use only the official signed asset for subsequent updates.

To add an optional home-screen notification widget, open **Settings → Notification widget**, choose **2×1 without a counter** or **3×1 with a counter**, select a saved NAS, and confirm Android's add-widget prompt. Nothing is added automatically during installation. Launcher cell sizes vary; both variants also support standard launcher configuration.

Tap the widget to open a compact translucent panel with your account's master alert switch and camera choices. These are the same personal preferences used by the web interface; common and per-camera schedules still apply in the NAS time zone. The widget does not disable event reception or recording, clear unread history, or enable visible phone alerts by itself. Each widget stays bound to its chosen NAS/account when you switch servers in the app.

The 3×1 variant shows the NAS account's actual unread-event count, including **0**. Tap the number to open that NAS's event history. Read/delete actions in the web or mobile app update the count using a separate silent channel over the existing UnifiedPush delivery service; it does not post an Android notification. No recurring NAS polling or permanent widget service is used. This variant requires an updated NAS and an available push distributor; one authenticated snapshot is also fetched when binding or opening the app. An unavailable initial count or signed-out account shows a dash, not a false zero. Android delivery/network restrictions can delay updates while the phone is offline or force-stopped. A reachable NAS is needed to load and save choices; the app explains missing NAS/push support during setup.

Live view, low-latency PTZ where supported, recordings, clips, synchronized chronology, camera management, storage, status, backups and NAS updates use the server's existing permissions and protected operations. Missing footage, an offline camera or unsupported PTZ capability is not silently replaced with another camera's video.

For mobile-client problems, open **Settings → Android app diagnostics → Send an app report**, add an optional comment and approximate time, then send. The APK uploads bounded diagnostics directly to the HTTPS support receiver; no ZIP file is created or saved on the phone. Success requires a matching report number from the receiver. A retry of the same in-memory report does not create a duplicate. Nothing is uploaded automatically.

The private journal is capped at 5 MiB and seven days; a report includes the latest 2,000 typed entries plus app/Android versions, notification permission/channel state, provider and registration state, and available process-exit reasons. It does not include passwords, access tokens, NAS addresses, camera images, video or raw notification packets. Do not put passwords in the comment. If the problem is with the NAS/server rather than the APK, send a server report from the **web interface** instead. APK diagnostics can be sent without contacting the NAS; internet access to the support receiver is still required.

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
