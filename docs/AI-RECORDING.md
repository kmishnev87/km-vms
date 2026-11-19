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

In the **AI & Recording** page, choose a mode, then its source and profile. For an event policy, set the prebuffer where applicable and choose how long to keep recording after the most recent matching activity (1–5 minutes). The profile uses the streams assigned in camera setup. The camera's factory channel names do not determine which stream KM VMS uses.

Only the Economical profile can wait without holding video for that recording policy. Timing, keyframes, network availability and available RAM can reduce the actual prebuffer. Balanced can keep parts in preview and recording quality rather than forcing different formats into a single raw stream.

A linked camera does not inherit another camera's credentials, stream or settings. Pausing the source makes the linked camera wait; it does not turn the linked camera off. Its own pause and storage limits still apply.

## Configure local rules

1. Choose **NAS AI**, open the camera's AI rules and get a fresh frame.
2. Select a rule and draw or adjust its zone or line. Choose the objects, condition, timing and optional schedule. Exclusions affect analysis, not the original video.
3. Decide whether the rule should start recording, notify you, or do both. Global and personal notification preferences do not pause recording.
4. If a PTZ move or a stream change makes the camera view unreliable, spatial rules wait for a recognized view. Restore the view or set up the shapes again when appropriate.

The local engine recognizes people, vehicles and supported animal classes. Basic scene change is separate. There is no identification of faces or vehicle plates, and it does not scan existing archive recordings automatically. A zone is meaningful only in the camera view used when it was drawn.

## Inspect an event

Open **Camera Events → History**. An AI event has a detection frame when available, its type and the selected rule. Filter by source, period, object and rule name. A repeated start can be hidden from history while still extending an active recording.

**Watch event** opens only an available recording that covers the event. If a segment is still being written, check again after it closes. A snapshot can exist without video. A saved clip is a separate copy; it does not pin the original files or alter retention. Marking a false positive does not retrain AI.

Notification filters apply independently. A muted or out-of-schedule event can still be in history without a badge or push. An Android device must also have delivery enabled and the OS notification permission. Local video analysis does not require internet access, but delivery to a sleeping phone depends on its configured push transport and connectivity.

## Understand an interruption

- **Waiting** is normal when an event policy has no matching activity. An empty successful analysis is not a fault.
- **System Health** distinguishes a camera subscription failure, a local analyzer failure and a manual pause. An issue reports actual recording state, not just the chosen policy. If automatic recovery is exhausted, an owner or administrator can retry from the issue when offered.
- **Emergency recording** is a NAS-wide opt-in for event-based cameras. It permits temporary continuous recording during a verified detection outage, not during ordinary inactivity. It cannot make unavailable video or storage usable.
- Check the camera's own detection settings when relying on camera events. A camera that answers requests but produces no alarms cannot reliably be distinguished from a quiet scene.

Capacity varies with codecs, frame rate, keyframes, scene and hardware. On a weak NAS, test one camera and confirm that recording, viewing and storage remain healthy before enabling local AI on more inputs. A compatible Intel GPU is used when available; CPU fallback is limited to avoid monopolizing the server.
