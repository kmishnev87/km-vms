# KM VMS

## NAS-first, self-hosted video management system and network video recorder

**KM VMS is a web-based Video Management System (VMS) with Network Video Recorder (NVR) capabilities for RTSP and ONVIF IP cameras.** Run it on your own NAS or Linux server using Docker, keep your recordings on your own storage, and manage surveillance from a browser.

Designed for homes, holiday properties, homelabs, and small sites, KM VMS brings camera setup, multi-camera live viewing, recording, archive playback, storage management, and system diagnostics into one application.

**Interface languages: English · 简体中文 (Simplified Chinese) · Русский (Russian).**

[MIT License](LICENSE) — use, modify and distribute, including commercially, with the required copyright and license notices.

[Quick start](#quick-start) · [Features](#features) · [Screenshots](#screenshots) · [Installation guide](docs/INSTALL.md) · [Releases](https://github.com/kmishnev87/km-vms/releases) · [Report a problem](https://github.com/kmishnev87/km-vms/issues)

![KM VMS English-language dashboard with cameras, recordings, live view, chronology, storage, system health, and settings](docs/images/dashboard-en.png)

*English interface. The Android download tile is a placeholder for a future client, not an available mobile application.*

## Why KM VMS?

- **A complete surveillance workflow.** Connect cameras, watch live video, record, find an event in the archive, and export the relevant footage without switching between unrelated tools.
- **Your server, your archive.** Video recordings stay on storage you control. Local recording and viewing do not require a camera manufacturer's cloud account or cloud recording service.
- **Standards-based camera connections.** Use RTSP streams and ONVIF services rather than restricting the entire installation to one camera brand.
- **A shared view of time.** Chronology keeps available recordings together: when one needs data, the group waits instead of drifting apart. Confirmed archive gaps do not block the other cameras.
- **PTZ that follows the camera's capabilities.** Compatible cameras expose movement, zoom, speed, and saved positions without a separate manual PTZ setup. Low-latency video is used during control when the selected stream supports it.
- **Storage management as part of the product.** Retention rules, archive limits, free-space monitoring, and integrity tools are available alongside recording and playback.
- **More visibility when something goes wrong.** System Health, recording states, audit history, and diagnostics help distinguish a camera connection problem from a storage or recording problem.
- **Everyday administration in the browser.** Manage users, camera settings, backups, and published updates through the web interface after installation.

## Features

| Area | What you can do |
| --- | --- |
| IP cameras | Add and edit RTSP/ONVIF cameras, check connections, select video sources and main/sub streams, and configure recording. |
| ONVIF video settings | Discover Media1/Media2 services, read supported encoder settings, and apply camera-provided options for codec, resolution, frame rate, and bitrate where the device exposes them. |
| Camera events | Receive supported ONVIF PullPoint events, filter a retained history, mark events read individually or in bulk, and open the relevant live view or existing archive. Personal camera/type/time notification preferences do not change recording or camera detection. |
| Camera detection | Read ONVIF detection rules and change explicitly exposed boolean switches with conflict detection and confirmation. Not all devices expose these controls; KM VMS does not invent unavailable switches or overwrite detection zones. |
| Live monitoring | Add or remove multiple cameras, align tiles or automatically fit their frame aspect ratios, and use main/sub stream and audio controls in individual fullscreen views. Recover viewing after temporary connection interruptions. |
| PTZ control | Automatically detect supported PTZ capabilities for the selected camera/lens. Move, zoom, choose speed or saved positions, and use a movable fullscreen control panel. Point positioning and area zoom are available only through supported device capabilities/control paths. |
| Recording | Record video on your NAS/server, monitor recording state, and retry after temporary camera or network failures while recording remains enabled. After interruption, readable saved fragments can be finalized without rewriting video; unconfirmed files remain available for review. |
| Records and playback | Search and filter archive files, play recordings, and download source files. Retained recordings remain accessible after their camera is removed. |
| Video clips and evidence | Create clips up to three hours from Records or Chronology, including retained recordings of removed cameras. No fixed 4 GiB source-size cap; preparation checks available storage. Download the clip and its passport, clear the selection, or explicitly delete the derived result without deleting source footage. Completed clips stay until you delete them. |
| Synchronized chronology | Navigate a shared time point across cameras. Play waits for the required recordings; buffering pauses the group and its clock. Confirmed gaps are not blockers, failed requests are not disguised as gaps, and the last frame is held while the next segment loads. |
| Archive management | Configure retention and size limits, inspect storage usage with original names for removed cameras, and review archive findings by camera, problem, date, and state. Confirm individual or bulk actions. Filter and page the operation history, with grouped steps and a distinction between an earlier attempt and the issue's current relevance. |
| System Health | Distinguish current camera, recording, storage, and playback problems from findings about older archive files. Historical findings do not by themselves mean that current recording has stopped. |
| Users and permissions | Separate access through user accounts, roles, and server-enforced permissions. |
| Maintenance | Create and verify database/configuration backups, inspect diagnostics, and apply published updates with preflight and post-update verification. |
| Video acceleration | Select supported server-side hardware acceleration when the host, drivers, and video format provide a usable path. |
| Localization | Switch the interface between English, Simplified Chinese, and Russian. |

ONVIF capabilities are device-dependent: a camera must expose the relevant service and supported options. A panoramic lens and a PTZ lens on the same device can have different controls. Point positioning and area zoom are not promised for every PTZ camera. RTSP compatibility, browser playback, audio, low-latency video, and hardware acceleration also depend on the camera, codec, browser, and server. KM VMS does not claim compatibility with every camera or hardware combination.

## Quick start

The example below installs the published **[0.9.12](https://github.com/kmishnev87/km-vms/releases/tag/v0.9.12)** release on a **new installation**. It is not an update command for an existing installation. Check the [latest published release](https://github.com/kmishnev87/km-vms/releases/latest) for subsequent versions.

### 1. Prepare your NAS or Linux server

You need:

- Docker Engine **25.0+** and Docker Compose **2.20.2+**, or a compatible implementation of `healthcheck.start_interval`.
- Python 3, `curl`, a POSIX shell, archive utilities, and a working `getfacl` command.
- Permission to run Docker and write to the selected application directory.
- Network access to GitHub and the image/package registries needed for the initial downloads and Docker builds.

The installer does **not** install Docker or host packages for you. Host Node.js, npm, and Git are not required for the normal public installation.

### 2. Download and run the installer

Run these commands in the NAS/server terminal or over SSH:

```sh
curl -fsSL https://raw.githubusercontent.com/kmishnev87/km-vms/v0.9.12/scripts/install.sh -o km-vms-install.sh
sh km-vms-install.sh --branch v0.9.12
```

The interactive installer uses `$HOME/km-vms` as the default application directory and port `8088`. It asks for confirmation before creating a new application directory. To choose a persistent application directory on your NAS instead, add `--app-dir /path/to/km-vms` to the second command; replace that example path with your own.

The application directory contains configuration and application data. The video archive location is selected separately during browser setup. Initial installation includes Docker image preparation; download/build time depends on the NAS and network.

### 3. Finish in your browser

After the installer reports that setup is starting, open:

```text
http://<NAS-IP>:8088/setup
```

Follow the setup wizard to choose storage and create your owner account. There is no shipped default administrator password. Then add your cameras and configure recording.

For prerequisites, custom paths/ports, troubleshooting, and existing installations, see the [installation guide](docs/INSTALL.md). Check [GitHub Releases](https://github.com/kmishnev87/km-vms/releases) for newer published versions. The examples deliberately select an immutable release tag rather than the moving `main` development branch.

## Screenshots

These are real screenshots of KM VMS using the **English interface**. They contain no camera credentials, private camera addresses, or surveillance footage.

The dashboard shown above links camera configuration, records, live monitoring, synchronized chronology, storage, and system health in one web application.

### Settings and maintenance entry points

Language, timezone, recording format, hardware acceleration, security, maintenance, and user management are available from the settings workspace.

![Real KM VMS settings in English with language, timezone, recording format, hardware acceleration, maintenance, and user management](docs/images/settings-en.png)

## Deployment and compatibility

KM VMS is a Docker-based, self-hosted application for Linux/NAS environments. Its web interface runs in the browser; no separate desktop client is required for normal use.

The current development and hardware-validation environment is a TerraMaster NAS. Other Linux-based NAS/server environments must provide the documented Docker, Compose, Python, filesystem/ACL, and storage capabilities; their vendor name alone is not a compatibility guarantee. No vendor certification or ONVIF conformance certification is claimed.

A GPU is optional: hosts without `/dev/dri` use software video processing. Supported DRM devices are passed through automatically by the shared installation/update/restart launcher when available; a missing GPU does not prevent container startup.

The implementation uses a Python/FastAPI backend, PostgreSQL, Redis, FFmpeg-based video processing, a Next.js web frontend, and nginx. See [dependency notes](docs/DEPENDENCIES.md) for pinned component information.

### Implemented versus planned

Live viewing, recording recovery, records playback, strict synchronized chronology, managed clip exports, retention, archive review and bulk actions, readable operation history, ONVIF video configuration and camera events, conditional detection-rule switches, capability-based PTZ controls, frame-aware workspaces, users/roles, diagnostics, backups, and update workflows are implemented. Available camera controls and event classes still depend on the device and lens.

The following are **future work**, not current product promises:

- A native mobile client.
- Background mobile push notifications and recording triggered only by events. Browser event notifications currently work while KM VMS is open; opening an event uses existing recordings and does not create missing pre-event footage.
- AI video analytics and object detection.
- Multi-storage recording placement and archive tiering.

## Frequently asked questions

### Is KM VMS a VMS or an NVR?

Both descriptions are relevant. As an NVR, it records IP-camera video to server storage. As a VMS, it also manages cameras, viewing workspaces, synchronized archive navigation, users, permissions, storage policies, and diagnostics.

### Does local surveillance require a cloud service?

No camera-vendor cloud account is required for the local RTSP/ONVIF recording and viewing workflow. Installation and update downloads need internet access. Remote viewing requires you to configure a suitable network access path.

### Can I use cameras from different manufacturers?

The connection mechanisms are RTSP and ONVIF, not one manufacturer's cloud API. Actual support depends on the device's streams, services, authentication, and codec implementation. A compatible device is not automatically a tested device.

### Can I change a camera's video settings from KM VMS?

Yes, when the camera exposes the corresponding ONVIF Media1/Media2 services and encoder options. Available settings are read from the device rather than advertised as universal controls for every camera.

### Can I control a PTZ camera without configuring it separately?

Add the camera normally. If the selected lens exposes supported PTZ capabilities, KM VMS shows its controls automatically. Movement, zoom, speed, and camera-provided saved positions are supported where available. Additional point/area controls appear only when a supported control path is detected; a fixed panoramic lens does not gain PTZ controls just because another lens on the same device can move.

### How do camera event notifications work?

Open the bell in the top navigation to see camera event history, personal notification preferences, and, with camera-management permission, detection rules. Events can link to live video or available archive footage about five seconds before the event. Chronology shows event markers and previous/next controls. Repeated state reports are grouped, and an initial subscription state does not trigger a new alarm. History is automatically cleaned on a five-minute schedule with a target of 30 days and 100,000 events; a bounded recent backlog can exist between sweeps.

Camera detection, event reception, personal KM VMS notifications, and continuous recording are separate. Turning notifications off does not turn camera detection or recording off. Controls are offered only where ONVIF exposes a supported switch; some cameras report events but do not permit their detection settings to be changed through ONVIF. No claim of universal object recognition or mobile background push is made.

### Does marking an archive finding reviewed repair the video?

No. Keeping a file and marking its finding reviewed records your decision; it does not turn damaged or unconfirmed footage into a verified recording. Rechecking, correcting a supported recording state, and deleting recordings are separate, explicitly confirmed actions. Current recording health and historical archive findings are shown separately.

### What happens when a recording buffers in Chronology?

The recordings present at the selected moment wait for each other, and the shared clock pauses with them. A camera with a confirmed gap does not block the group. A network or file-access error is not treated as a gap: retry playback or remove the affected tile. Segment changes can require a loading pause; zero-delay playback on every network is not promised.

### Do saved clips expire or delete the original footage?

Completed clips stay until you explicitly delete them. In the shared clip window, use **Saved clips** to select, download, or delete a result and its passport. Deleting a clip does not delete the recordings it was made from. Original recordings still follow their separate archive-retention rules. Clips can also be created from retained footage of a removed camera.

### How do I update an existing installation?

Use **Settings → Maintenance** to check and apply an available published update. Do not rerun the new-install command over an existing application directory. The installation guide also documents the terminal update path.

### Does a database backup include the video archive?

No. Database backups protect application metadata and configuration; video files require a separate archive-backup strategy. Retention cleanup is not a backup.

### Can I expose the web interface to the internet?

The quick start serves HTTP and is intended for initial setup on a trusted network. It does not configure HTTPS or internet access automatically. For remote use, configure a properly secured HTTPS reverse proxy or VPN and appropriate network restrictions. Never expose the Docker control socket or treat an unprotected HTTP port as a secure remote deployment.

## Project status, support, and licensing

KM VMS is actively developed. Published releases and their notes are available under [Releases](https://github.com/kmishnev87/km-vms/releases). Compatibility and capacity should be evaluated for your own cameras, NAS, storage, and network; no universal camera-count or performance guarantee is claimed.

For a bug report with diagnostics, open **Settings → Security**, describe the problem, choose normal or extended diagnostics, and explicitly consent to sending the report to the KM VMS developer. The application sends it over HTTPS to the developer's support receiver at **кмпрайм.рф** and shows a report ID only after acceptance. This is one-way report delivery, not a live chat or a guaranteed response service. You can download the ZIP instead; no report is sent automatically.

Diagnostics may contain device names, addresses, application state and logs. Known secret fields are redacted; video recordings are not included. Do not type passwords into the description, and do not post diagnostic ZIPs publicly. The developer's receiver is separate infrastructure and is not installed on users' servers. Local recording/viewing do not depend on it.

For feature requests and sanitized public discussion, use [GitHub Issues](https://github.com/kmishnev87/km-vms/issues). Include the release version, environment, expected/actual behavior, and reproduction steps. Do not publish passwords, tokens, camera URLs containing credentials, database dumps, or private video. If private vulnerability reporting is available in the repository's Security section, use it for sensitive security reports rather than disclosing details in a public issue.

KM VMS project code is available under the [MIT License](LICENSE). Third-party components retain their own licenses; see [dependency notes](docs/DEPENDENCIES.md). The software is provided without warranty as described in the license.
