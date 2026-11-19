package ru.kmvms.mobile.ui.events

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.foundation.Canvas
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.res.painterResource
import androidx.activity.compose.BackHandler
import ru.kmvms.mobile.ui.camera.LabeledChoice
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.state.ToggleableState
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import ru.kmvms.mobile.R
import ru.kmvms.mobile.archive.ArchiveApi
import ru.kmvms.mobile.archive.ArchiveCamera
import ru.kmvms.mobile.core.EventAction
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.events.*
import ru.kmvms.mobile.ui.Accent
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.archive.ClipDraft
import ru.kmvms.mobile.ui.archive.ClipsScreen
import java.time.ZoneId
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

@OptIn(ExperimentalMaterial3Api::class)
@Composable fun EventsScreen(api: NasApi, canDelete: Boolean, canArchive: Boolean,
                             open: (MobileVideoRequest) -> Unit, message: (String) -> Unit, profileId: String? = null,
                             canClip: Boolean = false, focusedEvent: Pair<CameraEvent, ZoneId>? = null, onFocusHandled: () -> Unit = {}) {
    val service = remember(api) { EventsApi(api) }
    val controller = remember(service) { EventsController(service) }
    val state by controller.state.collectAsStateWithLifecycle()
    val widgetContext = androidx.compose.ui.platform.LocalContext.current
    LaunchedEffect(profileId, state.page?.unread) {
        if (profileId != null && state.page != null && ru.kmvms.mobile.widget.hasCountWidgets(widgetContext, profileId))
            ru.kmvms.mobile.widget.WidgetUnreadSync.enqueue(widgetContext, profileId)
    }
    val navigator = remember(api) { EventVideoNavigation(api) }
    val scope = rememberCoroutineScope()
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    var preferencesOpen by rememberSaveable { mutableStateOf(false) }
    var receivingOpen by rememberSaveable { mutableStateOf(false) }
    var filtersOpen by remember { mutableStateOf(false) }
    var editedFilter by remember { mutableStateOf(EventFilter()) }
    BackHandler(preferencesOpen) { preferencesOpen=false; receivingOpen=false }
    var deletion by remember { mutableStateOf<EventDeletion?>(null) }
    var opening by remember { mutableStateOf(false) }
    var detail by remember(service) { mutableStateOf<CameraEvent?>(null) }
    var detailZone by remember(service) { mutableStateOf<ZoneId?>(null) }
    var clip by remember(service) { mutableStateOf<Pair<ClipDraft, ArchiveCamera>?>(null) }
    LaunchedEffect(focusedEvent, service) {
        focusedEvent?.let { (event, zone) -> detail = event; detailZone = zone; onFocusHandled() }
    }
    val failure = stringResource(R.string.server_error)
    val unavailable = stringResource(R.string.events_video_unavailable)
    val fallback = stringResource(R.string.events_live_fallback)
    val filtersLabel = stringResource(R.string.events_filters)
    LaunchedEffect(state.failures) { if (state.failures > 0) message(failure) }
    DisposableEffect(controller, lifecycle) {
        controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED))
        val observer = LifecycleEventObserver { _, _ -> controller.foreground(lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED)) }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer); controller.close() }
    }
    fun navigate(event: CameraEvent, action: EventAction) {
        if (opening || state.busy) return
        opening = true
        scope.launch {
            try {
                val result = navigator.open(event, action, state.page?.zone ?: java.time.ZoneOffset.UTC)
                val request = result.request
                if (request == null) {
                    if (event.ai != null) { detailZone = null; detail = event } else message(unavailable)
                } else {
                    try { service.read(setOf(event.id)) }
                    catch (cancelled: CancellationException) { throw cancelled }
                    catch (_: Exception) { message(failure) }
                    if (result.liveFallback) message(fallback)
                    open(request)
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { message(failure) }
            finally { opening = false }
        }
    }
    clip?.let { (draft, camera) ->
        BackHandler { clip = null }
        ClipsScreen(remember(api) { ArchiveApi(api) }, listOf(camera), detailZone ?: state.page?.zone ?: ZoneOffset.UTC,
            { clip = null }, message, initial = draft)
        return
    }
    Column(Modifier.fillMaxSize()) {
        Surface(color = Color.White) {
            Column {
                Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    if(preferencesOpen)IconButton(onClick={preferencesOpen=false;receivingOpen=false},modifier=Modifier.testTag("events-history-tab")){Icon(Icons.AutoMirrored.Filled.ArrowBack,stringResource(R.string.back))}
                    Text(stringResource(if(preferencesOpen&&receivingOpen)R.string.events_receiving else if(preferencesOpen)R.string.events_alerts else R.string.events), style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
                    if(!preferencesOpen) {
                        IconButton(onClick={editedFilter=state.filter;filtersOpen=true},enabled=!state.busy,modifier=Modifier.testTag("events-open-filters")) {
                            Canvas(Modifier.size(22.dp).semantics { contentDescription=filtersLabel }) {
                                val color=if(state.filter.copy(unread=false)!=EventFilter())Accent else Muted
                                for((index,fraction)in listOf(.85f,.56f,.24f).withIndex()){
                                    val y=size.height*(.25f+index*.25f)
                                    drawLine(color,Offset(size.width*(1-fraction)/2,y),Offset(size.width*(1+fraction)/2,y),1.7.dp.toPx())
                                }
                            }
                        }
                        EventUnreadFilter(state.filter.unread, !state.busy) {
                            controller.filter(state.filter.copy(unread=it))
                        }
                        IconButton(onClick={preferencesOpen=true;controller.refreshSettings()},modifier=Modifier.testTag("events-alerts-tab")) {
                            Icon(painterResource(R.drawable.kmvms_menu_events),stringResource(R.string.events_alerts),tint=Color.Unspecified,modifier=Modifier.size(26.dp))
                        }
                        IconButton(onClick = { controller.refresh() }, enabled = !state.loading, modifier = Modifier.testTag("events-refresh")) { Icon(Icons.Default.Refresh, stringResource(R.string.refresh)) }
                    }
                    if(preferencesOpen&&receivingOpen)IconButton(onClick=controller::refreshSettings,enabled=!state.busy,modifier=Modifier.testTag("events-receiving-refresh")) {
                        Icon(Icons.Default.Refresh,stringResource(R.string.refresh))
                    }
                }
                if(preferencesOpen&&canDelete)Row(Modifier.fillMaxWidth().padding(horizontal=16.dp),horizontalArrangement=Arrangement.spacedBy(8.dp)) {
                    FilterChip(selected=!receivingOpen,onClick={receivingOpen=false},label={Text(stringResource(R.string.events_alerts))},modifier=Modifier.testTag("events-personal-tab"))
                    FilterChip(selected=receivingOpen,onClick={receivingOpen=true;controller.refreshSettings()},label={Text(stringResource(R.string.events_receiving))},modifier=Modifier.testTag("events-receiving-tab"))
                }
            }
        }
        Box(Modifier.fillMaxWidth().height(4.dp)) { if (state.loading || state.settingsLoading || state.busy || opening) LinearProgressIndicator(Modifier.fillMaxWidth()) }
        if (preferencesOpen && receivingOpen && canDelete) EventReceivingScreen(state, controller::receiving)
        else if (preferencesOpen) EventPreferencesScreen(state, controller::save, api, profileId, message)
        else {
            val page = state.page
            val allSelected = page?.items?.isNotEmpty() == true && page.items.all { state.isSelected(it.id) }
            val canSelectFiltered = allSelected && state.selection==null && (page?.total?:0)>page?.items.orEmpty().size
            Surface(color = Color.White) {
                Row(Modifier.fillMaxWidth().height(48.dp).padding(end = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    TriStateCheckbox(state = if (allSelected) ToggleableState.On else if (state.selectedCount>0) ToggleableState.Indeterminate else ToggleableState.Off,
                        onClick = { controller.selectPage(!allSelected) }, enabled = !state.busy && page?.items?.isNotEmpty() == true,
                        modifier = Modifier.testTag("events-select-page"))
                    EventSelectionSummary(state, canSelectFiltered, Modifier.weight(1f).fillMaxHeight()) {
                        if(state.selection!=null)controller.clearSelection()else controller.selectFiltered()
                    }
                    IconButton(onClick = controller::read, enabled = !state.busy && (state.selectedCount>0 || (page?.unread ?: 0) > 0),
                        modifier = Modifier.testTag("events-read")) { Icon(Icons.Default.Check, stringResource(if (state.selectedCount==0) R.string.events_read_all else R.string.events_read_selected)) }
                    if (canDelete) IconButton(onClick = { deletion = controller.deletion() }, enabled = !state.busy && (page?.historyCount ?: 0) > 0,
                        modifier = Modifier.testTag("events-delete")) { Icon(painterResource(R.drawable.ic_camera_trash), stringResource(if (state.selectedCount==0) R.string.events_delete_all else R.string.events_delete_selected),tint=Muted,modifier=Modifier.size(22.dp)) }
                }
            }
            LazyColumn(Modifier.weight(1f).testTag("events-list"), contentPadding = PaddingValues(vertical = 8.dp)) {
                if (page?.items.isNullOrEmpty() && !state.loading) item {
                    Box(Modifier.fillMaxWidth().height(160.dp), contentAlignment = Alignment.Center) { Text(stringResource(R.string.events_empty), color = Muted) }
                }
                items(page?.items.orEmpty(), key = { it.id }) { event ->
                    EventRow(event, state, canArchive, opening, { controller.select(event.id, it) },
                        { navigate(event, it) }, { detailZone = null; detail = event })
                    HorizontalDivider(color = MaterialTheme.colorScheme.outline)
                }
            }
            Row(Modifier.fillMaxWidth().height(44.dp).padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
                TextButton(onClick = { controller.refresh(((page?.offset ?: 0) - 30).coerceAtLeast(0)) }, enabled = !state.loading && (page?.offset ?: 0) > 0,
                    modifier = Modifier.testTag("events-previous")) { Text(stringResource(R.string.back)) }
                Text(if (page == null || page.total == 0) "0" else "${page.offset + 1}–${page.offset + page.items.size} / ${page.total}", color = Muted, style = MaterialTheme.typography.labelMedium)
                TextButton(onClick = { controller.refresh((page?.offset ?: 0) + 30) }, enabled = !state.loading && page?.more == true,
                    modifier = Modifier.testTag("events-next")) { Text(stringResource(R.string.next)) }
            }
        }
    }
    if(filtersOpen)ModalBottomSheet(onDismissRequest={filtersOpen=false},containerColor=Color.White) {
        Text(stringResource(R.string.events_filters),style=MaterialTheme.typography.titleLarge,modifier=Modifier.padding(horizontal=16.dp))
        Column(Modifier.weight(1f, fill = false).verticalScroll(rememberScrollState())) {
            EventFilters(state.copy(filter=editedFilter)){editedFilter=it}
        }
        Row(Modifier.fillMaxWidth().padding(16.dp),horizontalArrangement=Arrangement.spacedBy(10.dp)) {
            OutlinedButton(onClick={editedFilter=EventFilter(unread=editedFilter.unread)},modifier=Modifier.weight(1f)){Text(stringResource(R.string.events_reset_filters))}
            Button(onClick={controller.filter(editedFilter);filtersOpen=false},modifier=Modifier.weight(1f).testTag("events-apply-filters")){Text(stringResource(R.string.apply))}
        }
    }
    detail?.let { event ->
        AiEventSheet(service, event, detailZone ?: state.page?.zone ?: ZoneOffset.UTC, canDelete, canClip, opening,
            onDismiss = { detail = null; detailZone = null },
            open = { navigate(event, it) }, onReview = { controller.refresh(state.page?.offset ?: 0) },
            onClip = { range, title ->
                clip = ClipDraft(range.cameraId, title, range.start, range.end) to ArchiveCamera(range.cameraId, event.camera, false)
            })
    }
    deletion?.let { target -> AlertDialog(onDismissRequest = { deletion = null }, modifier = Modifier.semantics { testTagsAsResourceId = true },
        text = { Text(stringResource(R.string.events_delete_confirmation, target.count)) },
        confirmButton = { TextButton(onClick = { deletion = null; controller.delete(target) }, modifier = Modifier.testTag("events-confirm-delete")) { Text(stringResource(R.string.remove)) } },
        dismissButton = { TextButton(onClick = { deletion = null }) { Text(stringResource(R.string.cancel)) } }) }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable internal fun EventUnreadFilter(checked: Boolean, enabled: Boolean, choose: (Boolean) -> Unit) {
    TooltipBox(positionProvider=TooltipDefaults.rememberPlainTooltipPositionProvider(),
        tooltip={PlainTooltip { Text(stringResource(R.string.events_unread)) }}, state=rememberTooltipState()) {
        IconToggleButton(checked=checked, onCheckedChange=choose, enabled=enabled,
            modifier=Modifier.testTag("events-unread-only"),
            colors=IconButtonDefaults.iconToggleButtonColors(contentColor=Muted,
                checkedContentColor=Accent, checkedContainerColor=Accent.copy(alpha=.1f))) {
            Icon(painterResource(R.drawable.ic_event_unread), stringResource(R.string.events_unread), modifier=Modifier.size(22.dp))
        }
    }
}

@Composable internal fun EventSelectionSummary(state: EventsState, canSelectFiltered: Boolean,
                                               modifier: Modifier = Modifier, choose: () -> Unit) {
    Row(modifier.testTag("events-selection-summary"), verticalAlignment=Alignment.CenterVertically,
        horizontalArrangement=Arrangement.spacedBy(8.dp)) {
        Text(stringResource(if(state.selectedCount==0)R.string.events_found else R.string.events_selected,
            if(state.selectedCount==0)state.page?.total?:0 else state.selectedCount),
            style=MaterialTheme.typography.labelMedium, color=Muted, maxLines=1,
            modifier=Modifier.testTag("events-selection-count"))
        if(state.selection!=null || canSelectFiltered) {
            TextButton(onClick=choose, enabled=!state.busy&&!state.loading,
                modifier=Modifier.weight(1f).fillMaxHeight().testTag("events-select-filtered"),
                contentPadding=PaddingValues(0.dp)) {
                Text(stringResource(if(state.selection!=null)R.string.events_clear_selection else R.string.events_select_filtered,
                    state.page?.total?:0), style=MaterialTheme.typography.labelSmall, maxLines=1, overflow=TextOverflow.Ellipsis)
            }
        }
    }
}

@Composable internal fun EventRow(event: CameraEvent, state: EventsState, canArchive: Boolean, opening: Boolean,
                                  select: (Boolean) -> Unit, open: (EventAction) -> Unit, showDetails: (() -> Unit)? = null) {
    val color = if (event.read) Muted else MaterialTheme.colorScheme.onSurface
    Surface(color = Color.White) {
        Row(Modifier.fillMaxWidth().heightIn(min = 92.dp).padding(end = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            Checkbox(state.isSelected(event.id), select, enabled = !state.busy && state.canSelect(event.id), modifier = Modifier.testTag("event-check-${event.id}"))
            Column(Modifier.weight(1f).clickable(enabled = !opening && (event.ai != null || event.cameraId != null && !event.ambiguous)) {
                    if (event.ai != null && showDetails != null) showDetails() else open(if (canArchive) EventAction.OPEN else EventAction.LIVE)
                }
                .padding(vertical = 10.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Box(Modifier.fillMaxWidth()) {
                    Box(Modifier.align(Alignment.CenterStart).offset(x=(-9).dp).size(5.dp)
                        .background(if (event.read) Color.Transparent else Accent, CircleShape))
                    Text(event.camera, color = color, maxLines = 1, overflow = TextOverflow.Ellipsis, fontWeight = FontWeight.SemiBold,
                        style = MaterialTheme.typography.bodyMedium, modifier=Modifier.testTag("event-camera-${event.id}"))
                }
                val labels = eventLabels(event.kind, event.objects, event.ai?.condition).map { stringResource(it) }
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    if (event.ai != null) Text("AI", color = Accent, style = MaterialTheme.typography.labelSmall,
                        modifier = Modifier.background(Accent.copy(alpha = .09f), CircleShape).padding(horizontal = 5.dp, vertical = 1.dp))
                    Text(labels.joinToString(" · "),maxLines=2,overflow=TextOverflow.Ellipsis,
                        color = color, style = MaterialTheme.typography.bodySmall, modifier=Modifier.testTag("event-kind-${event.id}"))
                }
                event.ai?.let { ai ->
                    val note = listOfNotNull(ai.ruleName.takeIf(String::isNotBlank), if (ai.falsePositive) stringResource(R.string.ai_event_false) else null)
                    if (note.isNotEmpty()) Text(note.joinToString(" · "), maxLines = 1, overflow = TextOverflow.Ellipsis,
                        color = Muted, style = MaterialTheme.typography.labelSmall)
                }
                Text(event.occurred.atZone(state.page?.zone ?: java.time.ZoneOffset.UTC).format(DateTimeFormatter.ofPattern("dd.MM.yyyy HH:mm:ss")), color = Muted, style = MaterialTheme.typography.labelSmall, modifier=Modifier.testTag("event-time-${event.id}"))
                if (event.ambiguous) Text(stringResource(R.string.events_source_unknown), color = Muted, style = MaterialTheme.typography.labelSmall)
            }
            IconButton(onClick = { open(EventAction.LIVE) }, enabled = !opening && !state.busy && event.cameraId != null && !event.ambiguous,
                modifier = Modifier.testTag("event-live-${event.id}")) { EventActionIcon(true) }
            if (canArchive) IconButton(onClick = { open(EventAction.RECORDING) }, enabled = !opening && !state.busy && event.cameraId != null && !event.ambiguous,
                modifier = Modifier.testTag("event-archive-${event.id}")) { EventActionIcon(false) }
        }
    }
}

@Composable internal fun EventFilters(state: EventsState, choose: (EventFilter) -> Unit) {
    val filter = state.filter
    Surface(color = Color.White) {
        Column(Modifier.padding(horizontal = 12.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                EventChoice(state.cameras.find { it.id == filter.cameraId }?.name ?: stringResource(R.string.all_cameras),
                    listOf("" to stringResource(R.string.all_cameras)) + state.cameras.map { it.id.toString() to it.name }, Modifier.weight(1f), "events-filter-camera") {
                    choose(filter.copy(cameraId = it.toLongOrNull()))
                }
                val kinds = listOf("" to stringResource(R.string.events_all_types)) + CameraEventKind.useful.map { it.code to stringResource(it.label()) }
                EventChoice(kinds.find { it.first == filter.kind?.code }?.second ?: stringResource(R.string.events_all_types), kinds, Modifier.weight(1f), "events-filter-kind") {
                    choose(filter.copy(kind = it.takeIf(String::isNotBlank)?.let(CameraEventKind::parse)))
                }
            }
            val days = listOf("1" to stringResource(R.string.events_day), "7" to stringResource(R.string.events_week), "30" to stringResource(R.string.events_month))
            EventChoice(days.first { it.first == filter.days.toString() }.second, days, Modifier.fillMaxWidth(), "events-filter-days") { choose(filter.copy(days = it.toInt())) }
            val sources = listOf("" to stringResource(R.string.ai_event_both), "ai" to stringResource(R.string.ai_event_nas),
                "camera" to stringResource(R.string.ai_event_camera))
            EventChoice(sources.first { it.first == filter.source }.second, sources, Modifier.fillMaxWidth(), "events-filter-source") {
                choose(filter.copy(source = it, rule = "", condition = null))
            }
            if (filter.source == "ai") {
                val conditions = listOf("" to stringResource(R.string.ai_event_conditions)) + AiEventCondition.entries.map { it.code to stringResource(it.label()) }
                EventChoice(conditions.first { it.first == (filter.condition?.code ?: "") }.second, conditions, Modifier.fillMaxWidth(), "events-filter-ai-condition") {
                    choose(filter.copy(condition = AiEventCondition.parse(it)))
                }
                OutlinedTextField(filter.rule, { choose(filter.copy(rule = it.take(48))) }, singleLine = true,
                    label = { Text(stringResource(R.string.ai_event_rule)) }, modifier = Modifier.fillMaxWidth().testTag("events-filter-ai-rule"))
            }
        }
    }
}

@Composable private fun EventChoice(current: String, options: List<Pair<String, String>>, modifier: Modifier, tag: String, choose: (String) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    Box(modifier) {
        OutlinedButton(onClick = { expanded = true }, modifier = Modifier.fillMaxWidth().height(36.dp).testTag(tag), contentPadding = PaddingValues(horizontal = 10.dp)) {
            Text(current, maxLines = 1, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodySmall)
        }
        DropdownMenu(expanded, { expanded = false }, modifier = Modifier.heightIn(max = 360.dp)) {
            options.forEach { (id, text) -> DropdownMenuItem(text = { Text(text) }, onClick = { expanded = false; choose(id) }) }
        }
    }
}
