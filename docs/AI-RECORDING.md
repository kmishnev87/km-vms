# AI & Recording

**AI & Recording** is available starting with NAS 1.0.3 and Android 0.8.15. The web and Android controls manage the same NAS configuration; an owner or administrator changes recording policies. An update does not opt cameras into event-based recording or NAS AI. Existing cameras keep their continuous recording behavior and any manual pause until you choose another policy.

## Choose a policy

| Use case | Policy | Before a trigger |
|---|---|---|
| Keep all footage | Continuous, optionally with camera events or local AI | Records the configured recording stream |
| Minimize traffic and storage | Camera events, Economical | No recording stream is held for this policy; there is no pre-event footage |
| Retain some context with less idle traffic | Camera events or local AI, Balanced | Buffers the configured preview stream in RAM; saves it until the recording stream is ready |
| Keep pre-event footage at recording quality | Camera events or local AI, Full quality | Buffers the configured recording stream in RAM |
| Record a second viewpoint on the same episode | Linked recording | Follows an explicitly selected camera or lens; uses its own configured video |

In **AI & Recording**, choose a mode, then its source and profile. For an event policy, set the prebuffer where applicable and choose how long to keep recording after the most recent matching activity (1–5 minutes). The profile uses the streams assigned in camera setup. The camera's factory channel names do not determine which stream KM VMS uses.

With **Continuous** plus camera events or NAS AI, the **Alerts** profile offers only **After** (1–5 minutes, default 1). This is the quiet gap before another episode can start, not a recording duration. Continuous recording has no prebuffer choice.

Only the Economical profile can wait without holding video for that recording policy. Timing, keyframes, network availability and available RAM can reduce the actual prebuffer. Balanced can keep parts in preview and recording quality rather than forcing different formats into a single raw stream.

A linked camera follows the source's recording window, not whether the source successfully writes a file. A source video outage does not cut off the other viewpoint while that window is still active. Pausing the source makes the linked camera wait; it does not turn the linked camera off. The linked camera uses its own stream and respects its own pause and storage limits.

## Configure local rules

1. Choose **NAS AI**, open the camera's AI rules and get a fresh frame.
2. Select a rule and draw or adjust its zone or line. Choose the objects, condition, timing and optional schedule. Exclusions affect analysis, not the original video.
3. The first matching detection in an episode enters shared history. With **By event**, matching activity starts or extends recording; with **Continuous**, the video is unchanged. Choose which cameras can alert you and when in **Camera Events → Notifications**.
4. A fixed view needs just one setup frame. Ordinary lighting changes, including IR, do not invalidate the geometry. For a PTZ lens, spatial rules wait for the configured view after a turn or zoom. If the actual framing changes, check the contours before reusing them.

The web editor keeps its rule panel open in fullscreen, so you can edit the rules and points without leaving the expanded frame. The **AI** button in Live is available only for cameras with saved lines, zones or exclusions. The NAS identifies each ONVIF lens separately. An unverified lens profile or mismatched framing is indicated instead of drawing shapes at unverified coordinates.

The local engine recognizes people, vehicles and supported animal classes. A **Major scene change** rule can optionally also react to selected object classes without a large change to the view. For example, a PTZ camera can trigger when it turns to follow an event and when it detects someone while parked. There is no identification of faces or vehicle plates, and it does not scan existing archive recordings automatically. A zone is meaningful only in the camera view used when it was drawn.

## Inspect an event

Open **Camera Events → History**. An AI event has a detection frame when available, its type and the matched rule. Select **Source → NAS AI** in web history to choose a rule from the selected camera and period or to filter by condition. Android also opens available snapshots of camera events through the same event details control.

For a confirmed PTZ lens, if a NAS AI **Major scene change** is the first detection of the episode, the snapshot waits for a frame at least one second after detection so the camera has time to turn. Recording and the event timestamp do not wait. If an object opens the episode instead, it follows the normal snapshot path; later detections don't replace the opening image. If the delayed image is unavailable in time, the phone can still receive a text alert.

Starting with NAS 1.0.4, the first accepted detection opens the camera's episode without waiting for the recorder. Further matching activity on that camera, including a different selected event type or AI rule, extends it without another history entry or alert. A new episode can start after **After** minutes of quiet. For event recording this is the same interval that extends recording. Ending one detection or rolling to a new file does not rearm alerts. If you mute the opening alert, continuing activity does not send it again after you re-enable alerts.

**Watch event** opens only an available recording that covers the event. If a segment is still being written, check again after it closes. A snapshot can exist without video. A saved clip is a separate copy; it does not pin the original files or alter retention. Marking a false positive does not retrain AI.

Choose **Camera events** in **AI & Recording** to start the NAS subscription for an ONVIF camera. **Conditions** selects all or specific event types for both continuous and event-based recording. Saving a different source ends that subscription for this camera. This does not modify detection rules in the camera itself. Personal settings control delivery by camera and schedule for both NAS AI and camera events; they don't change what triggers recording. A muted or out-of-schedule event can still be in history without a badge or push. An Android device must also have delivery enabled and the OS notification permission. Local video analysis does not require internet access, but delivery to a sleeping phone depends on its configured push transport and connectivity.

## Understand an interruption

- **Waiting** is normal when an event policy has no matching activity. An empty successful analysis is not a fault.
- **System Health** distinguishes a camera subscription failure, a local analyzer failure and a manual pause. An issue reports actual recording state, not just the chosen policy. If automatic recovery is exhausted, an owner or administrator can retry from the issue when offered.
- **Emergency recording** is a NAS-wide opt-in for event-based cameras. It permits temporary continuous recording during a verified detection outage, not during ordinary inactivity. It cannot make unavailable video or storage usable.
- Check the camera's own detection settings when relying on camera events. A camera that answers requests but produces no alarms cannot reliably be distinguished from a quiet scene.

Capacity varies with codecs, frame rate, keyframes, scene and hardware. On a weak NAS, test one camera and confirm that recording, viewing and storage remain healthy before enabling local AI on more inputs. A compatible Intel GPU is used when available; CPU fallback is limited to avoid monopolizing the server.
