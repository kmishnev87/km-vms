package ru.kmvms.mobile.ui.recording

import androidx.annotation.StringRes
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Info
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.data.NasException
import ru.kmvms.mobile.ui.Accent
import ru.kmvms.mobile.ui.DashboardImage
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.Outline
import ru.kmvms.mobile.ui.camera.LabeledChoice
import ru.kmvms.mobile.ui.events.label
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

@StringRes
private fun modeText(mode: String): Int = when (mode) {
    "events" -> R.string.rec_m_events
    "linked" -> R.string.rec_m_linked
    else -> R.string.rec_m_continuous
}

@StringRes
private fun profileText(profile: String) = when (profile) {
    "full_quality" -> R.string.rec_p_full_quality
    "balanced" -> R.string.rec_p_balanced
    else -> R.string.rec_p_economical
}

@StringRes
private fun profileHint(profile: String) = when (profile) {
    "full_quality" -> R.string.rec_hint_full_quality
    "balanced" -> R.string.rec_hint_balanced
    else -> R.string.rec_hint_economical
}

@StringRes
private fun actualStatus(camera: RecordingCamera): Int {
    if (!camera.enabled) return R.string.rec_status_paused
    if (camera.state in listOf("recording", "extended") && camera.emergencyRecording) return R.string.rec_emergency_active
    if (camera.state == "waiting" && camera.ai?.rulesView in setOf("view_unconfirmed", "invalid_rules")) return R.string.ai_rule_view_short
    return when (camera.state) {
        "waiting" -> R.string.rec_status_waiting
        "analysis_starting" -> R.string.rec_status_analysis_starting
        "waiting_linked_source" -> R.string.rec_status_waiting_linked_source
        "linked_source_unavailable" -> R.string.rec_status_linked_source_unavailable
        "linked_source_fault" -> R.string.rec_status_linked_source_fault
        "source_fault" -> R.string.rec_status_source_fault
        "events_disabled" -> R.string.rec_status_events_disabled
        "starting" -> R.string.rec_status_starting
        "restarting" -> R.string.rec_status_restarting
        "finalizing" -> R.string.rec_status_finalizing
        "recording" -> R.string.rec_status_recording
        "extended" -> R.string.rec_status_extended
        "idle" -> R.string.rec_status_idle
        "storage_suspended" -> R.string.rec_status_storage_suspended
        "storage_waiting" -> R.string.rec_status_storage_waiting
        "error" -> R.string.rec_status_error
        else -> R.string.rec_status_recorder_pending
    }
}

private fun tone(camera: RecordingCamera): Color = when {
    !camera.enabled -> Muted
    camera.state == "waiting" && camera.ai?.rulesView in setOf("view_unconfirmed", "invalid_rules") -> Color(0xFF805018)
    camera.state in listOf("recording", "extended") && camera.emergencyRecording -> Color(0xFF805018)
    camera.state in listOf("recording", "extended") -> Color(0xFF166448)
    camera.state in listOf("error", "source_fault", "events_disabled", "linked_source_fault", "linked_source_unavailable") -> Color(0xFFA03844)
    camera.state in listOf("storage_waiting", "storage_suspended", "restarting") -> Color(0xFF805018)
    else -> Muted
}

@StringRes
private fun diagnosis(camera: RecordingCamera): Int? {
    val reason = camera.incident?.reason.orEmpty()
    if (camera.ai != null) return when (camera.ai.reason) {
        "analysis_timeout" -> R.string.rec_ai_timeout
        "engine_failed" -> R.string.rec_ai_failed
        "capacity_limited", "resource_limit" -> R.string.rec_ai_capacity
        "no_usable_frames" -> R.string.rec_ai_no_frames
        else -> null
    }
    return when {
        camera.sourceStatus in listOf("link_cycle", "link_invalid") -> R.string.rec_reason_cycle
        camera.sourceStatus == "source_unavailable" -> R.string.rec_reason_source
        "timeout" in reason -> R.string.rec_reason_timeout
        "auth" in reason || "forbidden" in reason -> R.string.rec_reason_auth
        "unsupported" in reason -> R.string.rec_reason_unsupported
        reason.isNotEmpty() || camera.state in listOf("source_fault", "linked_source_fault") -> R.string.rec_reason_connection
        else -> null
    }
}

@StringRes
private fun saveError(error: Exception): Int = when ((error as? NasException)?.publicReason) {
    "recording_policy_changed", "recording_emergency_changed", "emergency_setting_changed" -> R.string.rec_error_changed
    "recording_link_source_unavailable" -> R.string.rec_source_missing
    "recording_link_cycle" -> R.string.rec_reason_cycle
    "camera_event_reception_required" -> R.string.rec_event_requires
    "ai_frame_reference_required" -> R.string.ai_rule_capture_first
    "ai_frame_reference_changed" -> R.string.ai_rule_calibration_changed
    "ai_record_rule_required" -> R.string.ai_rule_record_required
    else -> R.string.rec_error_save
}

private fun timeLabel(time: String?): String? = time?.let {
    runCatching { DateTimeFormatter.ofPattern("HH:mm z", Locale.getDefault()).withZone(ZoneId.systemDefault())
        .format(Instant.parse(it)) }.getOrNull()
}

@Composable
fun RecordingCenterScreen(api: NasApi, canSettings: Boolean, onMessage: (String) -> Unit) {
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    val service = remember(api) { RecordingCenterApi(api) }
    var rows by remember(service) { mutableStateOf<List<RecordingCamera>?>(null) }
    var emergency by remember(service) { mutableStateOf<Boolean?>(null) }
    var failed by remember(service) { mutableStateOf(false) }
    var refresh by remember(service) { mutableIntStateOf(0) }
    var busy by remember(service) { mutableStateOf(false) }
    var editor by remember(service) { mutableStateOf<RecordingCamera?>(null) }
    var draft by remember(service) { mutableStateOf<RecordingDraft?>(null) }
    var showRules by remember(service) { mutableStateOf(false) }
    var editError by remember(service) { mutableStateOf<Int?>(null) }
    var details by remember(service) { mutableStateOf<Long?>(null) }
    var help by remember(service) { mutableStateOf(false) }

    LaunchedEffect(service, refresh, canSettings, lifecycle) {
        lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
            while (true) {
                if (!busy) {
                    try {
                        val fresh = service.cameras()
                        if (!busy) { rows = fresh; failed = false }
                    } catch (error: CancellationException) { throw error }
                    catch (_: Exception) { if (!busy) failed = true }
                    if (canSettings && !busy) {
                        try {
                            val fresh = service.emergency()
                            if (!busy) emergency = fresh
                        } catch (error: CancellationException) { throw error }
                        catch (_: Exception) { if (!busy) emergency = null }
                    }
                }
                delay(10_000)
            }
        }
    }

    fun change(run: suspend () -> Unit, failedAction: (Exception) -> Unit) {
        if (busy) return
        busy = true
        scope.launch {
            try { run() }
            catch (error: CancellationException) { throw error }
            catch (error: Exception) { failedAction(error) }
            finally { busy = false; refresh++ }
        }
    }

    LazyColumn(Modifier.fillMaxSize().testTag("recording-center"),
        contentPadding = PaddingValues(horizontal = 16.dp, vertical = 12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp)) {
        item {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                DashboardImage(R.drawable.kmvms_ai_recording_v2, Modifier.size(36.dp))
                Spacer(Modifier.width(8.dp))
                Text(stringResource(R.string.rec_title), Modifier.weight(1f), style = MaterialTheme.typography.titleLarge)
                IconButton(onClick = { help = true }, modifier = Modifier.testTag("recording-help")) {
                    Icon(Icons.Default.Info, stringResource(R.string.rec_help), tint = Accent)
                }
            }
        }
        if (canSettings) item {
            Surface(color = Color.White, shape = RoundedCornerShape(10.dp), border = BorderStroke(1.dp, Outline)) {
                Row(Modifier.fillMaxWidth().padding(end = 12.dp, top = 7.dp, bottom = 7.dp),
                    verticalAlignment = Alignment.CenterVertically) {
                    val label = stringResource(R.string.rec_emergency)
                    Checkbox(checked = emergency == true, enabled = emergency != null && !busy,
                        modifier = Modifier.testTag("recording-emergency").semantics { contentDescription = label },
                        onCheckedChange = {
                            val previous = emergency
                            if (previous != null) change({ emergency = service.setEmergency(previous) },
                                { onMessage(context.getString(saveError(it))) })
                        })
                    Column {
                        Text(label, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
                        Text(stringResource(if (emergency != null) R.string.rec_emergency_help else R.string.rec_emergency_unknown),
                            fontSize = 12.sp, color = Muted)
                    }
                }
            }
        }
        if (failed) item {
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Text(stringResource(R.string.rec_error_load), Modifier.weight(1f), color = Muted)
                TextButton(onClick = { refresh++ }) { Text(stringResource(R.string.refresh)) }
            }
        }
        if (rows == null && !failed) item { Text(stringResource(R.string.rec_loading), color = Muted) }
        if (rows?.isEmpty() == true && !failed) item { Text(stringResource(R.string.rec_empty), color = Muted) }
        items(rows.orEmpty(), key = { it.id }) { row ->
            val source = rows?.find { it.id == row.draft.sourceId }?.name
            val summary = when {
                source != null && sourceIsContinuous(rows.orEmpty(), row.draft.sourceId, row.id) -> source
                row.draft.profile in recordingProfiles -> listOfNotNull(source, stringResource(profileText(row.draft.profile))).joinToString(" / ")
                row.draft.source == "ai" -> stringResource(if (row.aiAvailable) R.string.rec_a_ai else R.string.rec_ai_unavailable)
                row.draft.source == "camera" -> stringResource(R.string.rec_a_camera)
                else -> ""
            }
            RecordingRow(row, summary, canEdit = !busy, onEdit = { editor = row; draft = row.draft; editError = null; showRules = false },
                onDetail = { details = row.id })
        }
    }

    if (help) AlertDialog(onDismissRequest = { help = false }, title = { Text(stringResource(R.string.rec_help)) },
        text = { Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(stringResource(R.string.rec_help_body))
            Text(stringResource(R.string.rec_help_buffer))
            Text(stringResource(R.string.rec_help_link))
        } }, confirmButton = { TextButton(onClick = { help = false }) { Text(stringResource(R.string.close)) } })

    val edited = editor
    val choice = draft
    if (edited != null && choice != null) {
        val sources = linkedSources(rows.orEmpty(), edited.id)
        val constant = sourceIsContinuous(rows.orEmpty(), choice.sourceId, edited.id)
        if (showRules) AiRuleEditor(edited, choice, service, { rules ->
            draft = draft?.copy(aiRules = rules, aiRulesEdited = true)
        }, { showRules = false })
        else PolicyEditor(edited, choice, sources, constant, busy, editError, { draft = it },
            { if (!busy) editor = null }, { showRules = true }) {
            if (choice.isValid(edited, sources)) change({
                val saved = service.save(edited, choice)
                rows = rows?.map { if (it.id == saved.id) saved else it }
                editor = null
                onMessage(context.getString(R.string.rec_saved))
            }, { editError = saveError(it) })
        }
    }
    val checked = rows?.find { it.id == details }
    if (checked != null) RecordingDetails(checked, emergency, busy, { if (!busy) details = null }) { id ->
        change({ service.retry(id); details = null; onMessage(context.getString(R.string.rec_retry_queued)) },
            { onMessage(context.getString(R.string.rec_error_retry)) })
    }
}

@Composable
private fun RecordingRow(row: RecordingCamera, summary: String, canEdit: Boolean, onEdit: () -> Unit, onDetail: () -> Unit) {
    val hasDetails = row.ai != null || diagnosis(row) != null || row.incident != null || row.emergencyRequested || row.bufferLimited ||
        row.sourceIntent != null && !row.sourceConfirmed
    Surface(Modifier.fillMaxWidth().testTag("recording-row-${row.id}"), color = Color.White,
        border = BorderStroke(1.dp, Outline), shape = RoundedCornerShape(10.dp)) {
        Row(Modifier.fillMaxWidth().heightIn(min = 76.dp).padding(start = 11.dp, end = 2.dp, top = 8.dp, bottom = 8.dp),
            verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                Text(row.name, fontWeight = FontWeight.SemiBold, fontSize = 14.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(stringResource(if (row.draft.mode == "events" && row.draft.source == "ai") R.string.rec_m_ai_events else modeText(row.draft.mode)),
                    fontSize = 12.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
                if (summary.isNotEmpty()) Text(summary, color = Muted, fontSize = 11.sp, maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            Row(Modifier.weight(.9f).padding(start = 7.dp), verticalAlignment = Alignment.CenterVertically) {
                val color = tone(row)
                Box(Modifier.size(6.dp).background(color, CircleShape))
                Spacer(Modifier.width(5.dp))
                Text(stringResource(actualStatus(row)), Modifier.testTag("recording-status-${row.id}"),
                    color = color, fontSize = 12.sp, maxLines = 2, overflow = TextOverflow.Ellipsis)
            }
            Box(Modifier.size(42.dp), contentAlignment = Alignment.Center) {
                if (hasDetails) IconButton(onClick = onDetail, modifier = Modifier.testTag("recording-details-${row.id}")) {
                    Icon(Icons.Default.Info, stringResource(R.string.rec_diagnostics), Modifier.size(19.dp), tint = Accent)
                }
            }
            IconButton(onClick = onEdit, enabled = canEdit, modifier = Modifier.size(44.dp).testTag("recording-edit-${row.id}")) {
                Image(painterResource(R.drawable.ic_camera_edit), stringResource(R.string.rec_edit), Modifier.size(20.dp))
            }
        }
    }
}

@Composable
private fun PolicyEditor(camera: RecordingCamera, draft: RecordingDraft, sources: List<RecordingCamera>, constant: Boolean,
                         busy: Boolean, error: Int?, edit: (RecordingDraft) -> Unit, dismiss: () -> Unit,
                         openRules: () -> Unit, save: () -> Unit) {
    val event = draft.mode == "events"
    val linked = draft.mode == "linked"
    val profiled = event || linked && !constant
    var showKinds by remember(camera.id) { mutableStateOf(false) }
    val missingEventKey = if (camera.protocol == "onvif") R.string.rec_event_requires else R.string.rec_event_unsupported
    val selectableKinds = allowedKinds(draft.source)
    fun chooserLabel(mode: String) = if (mode == "events" && camera.aiAvailable) R.string.rec_m_events_choose else modeText(mode)
    AlertDialog(onDismissRequest = dismiss, modifier = Modifier.testTag("recording-editor"),
        title = { Text(camera.name, maxLines = 2, overflow = TextOverflow.Ellipsis) },
        text = {
            Column(Modifier.heightIn(max = 480.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(9.dp)) {
                LabeledChoice(stringResource(R.string.rec_scenario), stringResource(chooserLabel(draft.mode)),
                    listOf("continuous", "events", "linked").map { it to stringResource(chooserLabel(it)) },
                    !busy, "recording-mode") { edit(draft.withMode(it, camera)) }
                if (!linked) {
                    val none = "none" to stringResource(R.string.rec_a_none)
                    val own = "camera" to stringResource(R.string.rec_a_camera)
                    val ai = "ai" to stringResource(R.string.rec_a_ai)
                    val options = listOfNotNull(none.takeUnless { event }, own.takeIf { camera.canUseEvents }, ai.takeIf { camera.aiAvailable })
                    LabeledChoice(stringResource(if (event) R.string.rec_event_source else R.string.rec_analytics), when (draft.source) {
                        "camera" -> own.second; "ai" -> ai.second; else -> none.second
                    }, options, !busy && options.isNotEmpty(), "recording-analytics") {
                        edit(draft.withSource(it))
                    }
                    if (draft.source == "ai") Text(stringResource(if (camera.aiAvailable) R.string.rec_ai_help else R.string.rec_ai_unavailable), color = Muted, fontSize = 12.sp)
                    else if (!camera.canUseEvents) Text(stringResource(missingEventKey), color = Muted, fontSize = 12.sp)
                }
                if (linked) {
                    val picked = sources.find { it.id == draft.sourceId }?.name
                    LabeledChoice(stringResource(R.string.rec_source), picked ?: stringResource(
                        if (draft.sourceId == null) R.string.rec_source_choose else R.string.rec_source_missing),
                        sources.map { it.id.toString() to it.name }, !busy && sources.isNotEmpty(), "recording-source") {
                        edit(draft.copy(sourceId = it.toLongOrNull()))
                    }
                    Text(stringResource(if (constant) R.string.rec_source_constant else R.string.rec_inherit_duration), color = Muted, fontSize = 12.sp)
                }
                if (profiled) {
                    LabeledChoice(stringResource(R.string.rec_profile), stringResource(profileText(draft.profile)),
                        draft.profiles().map { it to stringResource(profileText(it)) }, !busy, "recording-profile") {
                        edit(draft.withProfile(it))
                    }
                    Text(stringResource(profileHint(draft.profile)), color = Muted, fontSize = 12.sp)
                    if (draft.profile != "economical") {
                        OutlinedTextField(draft.seconds, { value ->
                            if (value.length <= 3 && value.all { it in '0'..'9' }) edit(draft.copy(seconds = value))
                        }, label = { Text(stringResource(R.string.rec_prehistory)) }, enabled = !busy, singleLine = true,
                            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                            isError = draft.seconds.toIntOrNull()?.let { it !in 0..120 } ?: true,
                            modifier = Modifier.fillMaxWidth().testTag("recording-buffer-seconds"))
                        if (draft.profile == camera.draft.profile && camera.bufferStatus == "ready") Text(
                            stringResource(R.string.rec_prehistory_ready, camera.bufferedSeconds), color = Muted, fontSize = 12.sp)
                    }
                }
                if (event) {
                    LabeledChoice(stringResource(R.string.rec_postrecord), draft.minutes.toString(),
                        (1..5).map { it.toString() to it.toString() }, !busy, "recording-minutes") {
                        edit(draft.copy(minutes = it.toInt()))
                    }
                    if (draft.source != "ai" || draft.aiRules == null) {
                    TextButton(onClick = { showKinds = !showKinds }, enabled = !busy, modifier = Modifier.testTag("recording-event-types")) {
                        Text(stringResource(R.string.rec_event_types))
                    }
                    Text(if (draft.kinds == selectableKinds) stringResource(R.string.rec_event_all)
                        else stringResource(R.string.rec_event_count, draft.kinds.size), color = Muted, fontSize = 12.sp)
                    if (showKinds) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Checkbox(draft.kinds == selectableKinds, { edit(draft.copy(kinds = if (it) selectableKinds else emptySet())) }, enabled = !busy)
                            Text(stringResource(R.string.rec_event_all))
                        }
                        supportedKinds.filter { it.code in selectableKinds }.forEach { kind ->
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                Checkbox(kind.code in draft.kinds, { on ->
                                    edit(draft.copy(kinds = if (on) draft.kinds + kind.code else draft.kinds - kind.code))
                                }, enabled = !busy)
                                Text(stringResource(if (draft.source == "ai" && kind.code == "motion") R.string.rec_ai_scene else kind.label()))
                            }
                        }
                    }
                    if (draft.kinds.isEmpty()) Text(stringResource(R.string.rec_event_select_one), color = MaterialTheme.colorScheme.error)
                    }
                    if (draft.source == "camera" && !camera.canUseEvents) Text(stringResource(missingEventKey), color = MaterialTheme.colorScheme.error)
                }
                if (draft.source == "ai" && camera.aiAvailable) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        OutlinedButton(onClick = openRules, enabled = !busy, modifier = Modifier.testTag("recording-open-ai-rules")) {
                            Text(stringResource(R.string.ai_rule_edit))
                        }
                        draft.aiRules?.let { Text(stringResource(R.string.ai_rule_count, it.rules.size),
                            color = Muted, fontSize = 12.sp, modifier = Modifier.padding(start = 8.dp)) }
                    }
                    draft.aiRules?.issue(event)?.let { Text(stringResource(it), color = MaterialTheme.colorScheme.error) }
                }
                if (draft.source == "ai" && draft.rules.isNotEmpty()) Text(stringResource(R.string.rec_ai_rules_pending), color = Muted, fontSize = 12.sp)
                error?.let { Text(stringResource(it), color = MaterialTheme.colorScheme.error) }
            }
        },
        confirmButton = { TextButton(onClick = save, enabled = !busy && draft.isValid(camera, sources),
            modifier = Modifier.testTag("recording-save")) { Text(stringResource(R.string.save)) } },
        dismissButton = { TextButton(onClick = dismiss, enabled = !busy) { Text(stringResource(R.string.cancel)) } })
}

@Composable
private fun RecordingDetails(camera: RecordingCamera, allowed: Boolean?, busy: Boolean, close: () -> Unit, retry: (Long) -> Unit) {
    AlertDialog(onDismissRequest = close, title = { Text(camera.name) }, text = {
        Column(Modifier.heightIn(max = 450.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(7.dp)) {
            camera.ai?.let { ai ->
                Text(stringResource(when (ai.phase) {
                    "healthy" -> R.string.rec_ai_running
                    "recovering" -> R.string.rec_ai_recovering
                    "failed", "exhausted", "stalled" -> R.string.rec_ai_waiting_retry
                    else -> R.string.rec_status_analysis_starting
                }))
                if (ai.model != null && ai.device != null) Text(stringResource(R.string.rec_ai_device, ai.model, ai.device))
                if (ai.version != null && ai.runtime != null) Text(stringResource(R.string.rec_ai_version, ai.version, ai.runtime))
                val view = when (ai.rulesView) {
                    "checking_view" -> R.string.ai_rule_view_checking_view
                    "aligned" -> R.string.ai_rule_view_aligned
                    "view_unconfirmed" -> R.string.ai_rule_view_view_unconfirmed
                    "invalid_rules" -> R.string.ai_rule_view_invalid_rules
                    else -> null
                }
                view?.let { Text(stringResource(it)) }
            }
            diagnosis(camera)?.let { Text(stringResource(it)) }
            when {
                camera.emergencyRecording -> Text(stringResource(R.string.rec_emergency_active))
                camera.emergencyRequested -> Text(stringResource(R.string.rec_emergency_pending))
                allowed == false -> Text(stringResource(R.string.rec_emergency_off))
            }
            if (camera.sourceIntent != null && !camera.sourceConfirmed) Text(stringResource(R.string.rec_source_video_missing))
            if (camera.bufferLimited) Text(stringResource(R.string.rec_prehistory_limit))
            camera.incident?.let { incident ->
                if (incident.attempts > 0) Text(stringResource(R.string.rec_attempts, incident.attempts))
                timeLabel(incident.last)?.let { Text(stringResource(R.string.rec_last_attempt, it)) }
                timeLabel(incident.next)?.let { Text(stringResource(R.string.rec_next_attempt, it)) }
            }
            if (camera.retryCameraId != null) Text(stringResource(R.string.rec_exhausted))
        }
    }, confirmButton = {
        val id = camera.retryCameraId
        TextButton(onClick = { if (id != null) retry(id) else close() }, enabled = !busy,
            modifier = Modifier.testTag("recording-retry")) {
            Text(stringResource(if (id == null) R.string.close else if (camera.ai != null) R.string.rec_retry_ai else R.string.rec_retry))
        }
    }, dismissButton = {
        if (camera.retryCameraId != null) TextButton(onClick = close, enabled = !busy) { Text(stringResource(R.string.close)) }
    })
}
