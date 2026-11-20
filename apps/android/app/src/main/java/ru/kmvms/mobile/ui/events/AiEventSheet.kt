package ru.kmvms.mobile.ui.events

import android.graphics.BitmapFactory
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import ru.kmvms.mobile.R
import ru.kmvms.mobile.core.EventAction
import ru.kmvms.mobile.events.*
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.SoftAccent
import java.time.ZoneId
import java.time.format.DateTimeFormatter

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable internal fun AiEventSheet(
    service: EventsApi, event: CameraEvent, zone: ZoneId, canReview: Boolean, canClip: Boolean, opening: Boolean,
    onDismiss: () -> Unit, open: (EventAction) -> Unit, onReview: () -> Unit, onClip: (EventClip, String) -> Unit,
    profileId: String? = null,
) {
    var row by remember(event.id) { mutableStateOf(event) }
    var evidence by remember(event.id) { mutableStateOf<EventEvidence?>(null) }
    val context = androidx.compose.ui.platform.LocalContext.current
    var frame by remember(profileId, event.id) { mutableStateOf(EventFrameCache.memory(profileId, event.id)?.asImageBitmap()) }
    var fetchingFrame by remember(profileId, event.id) { mutableStateOf(event.snapshotAvailable && frame == null) }
    var error by remember(event.id) { mutableStateOf(false) }
    var checking by remember(event.id) { mutableStateOf(false) }
    var reviewing by remember(event.id) { mutableStateOf(false) }
    var reload by remember(event.id) { mutableIntStateOf(0) }
    val scope = rememberCoroutineScope()
    LaunchedEffect(event.id, service, reload) {
        checking = true
        error = false
        try {
            evidence = service.evidence(event, zone).also { row = it.event }
        } catch (cancelled: CancellationException) { throw cancelled }
        catch (_: Exception) { evidence = null; error = true }
        finally { checking = false }
    }
    LaunchedEffect(event.id, service, profileId, row.snapshotAvailable) {
        if (frame == null && row.snapshotAvailable) try {
            fetchingFrame = true
            frame = EventFrameCache.load(context, profileId, event.id) { service.snapshot(event.id) }?.asImageBitmap()
        } catch (cancelled: CancellationException) { throw cancelled }
        catch (_: Exception) { /* Keep the event useful without its thumbnail. */ }
        finally { fetchingFrame = false }
    }
    val ai = row.ai
    val title = stringResource(if (ai == null) R.string.camera_event_title else R.string.ai_event_title)
    val labels = eventLabels(row.kind, row.objects, ai?.condition).map { stringResource(it) }
    val video = evidence?.state
    ModalBottomSheet(onDismissRequest = onDismiss, containerColor = Color.White) {
        Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).padding(horizontal = 18.dp)
                .padding(bottom = 20.dp).testTag(if (ai == null) "camera-event-detail" else "ai-event-detail"), verticalArrangement = Arrangement.spacedBy(9.dp)) {
            Text(title, style = MaterialTheme.typography.titleLarge)
            Box(Modifier.fillMaxWidth().heightIn(max = 190.dp).aspectRatio(16f / 9)
                .clip(RoundedCornerShape(12.dp)).background(SoftAccent), contentAlignment = Alignment.Center) {
                val image = frame
                if (image != null) Image(image, stringResource(R.string.ai_event_frame), Modifier.fillMaxSize(), contentScale = ContentScale.Fit)
                else Text(stringResource(if (fetchingFrame) R.string.event_photo_loading else R.string.ai_event_no_frame),
                    color = Muted, style = MaterialTheme.typography.bodySmall)
            }
            Text(row.camera, fontWeight = FontWeight.SemiBold)
            Text(row.occurred.atZone(zone).format(DateTimeFormatter.ofPattern("dd.MM.yyyy HH:mm:ss")),
                color = Muted, style = MaterialTheme.typography.labelMedium)
            Text(labels.joinToString(" · "), style = MaterialTheme.typography.bodyMedium)
            if (ai != null) {
                val scopeLabel = when (ai.scope) {
                    "zone" -> R.string.ai_event_unnamed_zone
                    "line" -> R.string.ai_event_unnamed_line
                    else -> R.string.ai_event_whole_frame
                }
                Text(stringResource(R.string.ai_event_rule) + ": " + ai.ruleName.ifBlank { stringResource(scopeLabel) },
                    color = Muted, style = MaterialTheme.typography.bodySmall)
            }
            HorizontalDivider()
            if (error) Text(stringResource(R.string.server_error), color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            if (checking) LinearProgressIndicator(Modifier.fillMaxWidth())
            if (video != null) Text(stringResource(when (video) {
                EventArchiveState.READY -> R.string.ai_event_ready
                EventArchiveState.PENDING -> R.string.ai_event_pending
                EventArchiveState.UNAVAILABLE -> R.string.events_video_unavailable
                EventArchiveState.FORBIDDEN -> R.string.ai_event_forbidden
            }), color = Muted, style = MaterialTheme.typography.bodySmall)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
                OutlinedButton(onClick = { open(EventAction.LIVE) }, enabled = !opening && row.cameraId != null && !row.ambiguous) {
                    Text(stringResource(R.string.push_watch_live))
                }
                if (video == EventArchiveState.READY) Button(onClick = { open(EventAction.RECORDING) }, enabled = !opening && !checking) {
                    Text(stringResource(R.string.push_watch_event))
                } else if (video != EventArchiveState.FORBIDDEN) OutlinedButton(onClick = { reload++ }, enabled = !checking) {
                    Text(stringResource(R.string.ai_event_check))
                }
            }
            val clip = evidence?.clip
            if (canClip && clip != null) {
                OutlinedButton(onClick = { onClip(clip, "$title · ${row.camera}") }, enabled = !checking) {
                    Text(stringResource(R.string.ai_event_save_clip))
                }
                Text(stringResource(R.string.ai_event_clip_help), color = Muted, style = MaterialTheme.typography.bodySmall)
            }
            if (ai != null && (canReview || ai.falsePositive)) {
                HorizontalDivider()
                if (ai.falsePositive) Text(stringResource(R.string.ai_event_false), style = MaterialTheme.typography.bodySmall)
                if (canReview) {
                    TextButton(onClick = {
                        reviewing = true
                        scope.launch {
                            try { row = service.review(row, zone, !ai.falsePositive); onReview() }
                            catch (cancelled: CancellationException) { throw cancelled }
                            catch (_: Exception) { error = true }
                            finally { reviewing = false }
                        }
                    }, enabled = !reviewing && !checking, modifier = Modifier.testTag("ai-event-review")) {
                        Text(stringResource(if (ai.falsePositive) R.string.ai_event_clear_false else R.string.ai_event_mark_false))
                    }
                    Text(stringResource(R.string.ai_event_review_help), color = Muted, style = MaterialTheme.typography.bodySmall)
                }
            }
        }
    }
}
