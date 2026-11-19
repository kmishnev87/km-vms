package ru.kmvms.mobile.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.Image
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.graphics.Brush
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.testTagsAsResourceId
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.media3.common.util.UnstableApi
import ru.kmvms.mobile.BuildConfig
import ru.kmvms.mobile.AppLocales
import ru.kmvms.mobile.MobileState
import ru.kmvms.mobile.MobileViewModel
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.ApiProblem
import ru.kmvms.mobile.data.ServerProfile
import ru.kmvms.mobile.ui.live.LiveScreen
import ru.kmvms.mobile.ui.archive.RecordingsScreen
import ru.kmvms.mobile.ui.chronology.ChronologyScreen
import ru.kmvms.mobile.ui.events.EventsScreen
import ru.kmvms.mobile.ui.events.PushSetupOffer
import ru.kmvms.mobile.push.PushStore
import ru.kmvms.mobile.events.MobileVideoRequest
import ru.kmvms.mobile.events.EventsApi
import ru.kmvms.mobile.events.CameraEvent
import ru.kmvms.mobile.events.EventVideoNavigation
import ru.kmvms.mobile.core.EventAction
import ru.kmvms.mobile.push.NotificationNavigation
import kotlinx.coroutines.CancellationException
import ru.kmvms.mobile.ui.camera.CameraManagementScreen
import ru.kmvms.mobile.ui.camera.LabeledChoice
import ru.kmvms.mobile.ui.server.StorageScreen
import ru.kmvms.mobile.ui.server.SystemScreen
import ru.kmvms.mobile.ui.server.ServerSettingsScreen
import ru.kmvms.mobile.ui.recording.RecordingCenterScreen
import kotlinx.coroutines.launch

enum class Section(val label: Int, val image: Int?, val permission: String?) {
    HOME(R.string.home, null, null), LIVE(R.string.live, R.drawable.kmvms_live, "view_live"),
    RECORDINGS(R.string.recordings, R.drawable.kmvms_recordings, "view_recordings"),
    CAMERAS(R.string.cameras, R.drawable.kmvms_camera, "manage_cameras"),
    EVENTS(R.string.events, R.drawable.kmvms_events, "view_live"), CHRONOLOGY(R.string.chronology, R.drawable.kmvms_chronology, "view_timeline"),
    STORAGE(R.string.storage, R.drawable.kmvms_storage, "manage_settings"),
    RECORDING_CENTER(R.string.rec_title, R.drawable.kmvms_ai_recording_v2, "manage_cameras"),
    SYSTEM(R.string.system_status, R.drawable.kmvms_system_status, "run_diagnostics"),
    SETTINGS(R.string.settings, R.drawable.kmvms_settings, null),
    SERVER_SETTINGS(R.string.server_settings, R.drawable.kmvms_settings, "manage_settings"),
}

@OptIn(ExperimentalMaterial3Api::class)
@UnstableApi
@Composable fun MobileApp(model: MobileViewModel = viewModel(), updates: ru.kmvms.mobile.update.AppUpdates = viewModel()) {
    val state by model.state.collectAsStateWithLifecycle()
    var section by rememberSaveable(state.active?.id) { mutableStateOf(Section.HOME) }
    var signOutConfirmation by remember { mutableStateOf(false) }
    var removeConfirmation by remember { mutableStateOf<ServerProfile?>(null) }
    var immersive by remember { mutableStateOf(false) }
    var videoRequest by rememberSaveable(state.active?.id, stateSaver = videoRequestSaver) { mutableStateOf<MobileVideoRequest?>(null) }
    var focusedEvent by remember(state.active?.id, state.user?.id) { mutableStateOf<Pair<CameraEvent, java.time.ZoneId>?>(null) }
    val snackbars = remember { SnackbarHostState() }
    val uiScope = rememberCoroutineScope()
    val screenState = rememberSaveableStateHolder()
    LaunchedEffect(section, state.active?.id) {
        if (section !in setOf(Section.LIVE, Section.CHRONOLOGY)) videoRequest = null
        // A viewing session ends on navigation away, not on rotation/fullscreen.
        if (section != Section.LIVE) screenState.removeState("${state.active?.id ?: "connection"}:${Section.LIVE.name}")
    }
    val configuration=LocalConfiguration.current
    val compactNavigation=configuration.screenWidthDp>configuration.screenHeightDp&&configuration.screenHeightDp<500
    val navigationVisible=!state.showConnection&&state.user!=null&&!immersive
    val problemText = state.problem?.let { stringResource(problemResource(it)) }
    val notification by NotificationNavigation.pending.collectAsStateWithLifecycle()
    val pushContext = LocalContext.current
    var pushSetupVisible by rememberSaveable(state.active?.id, state.user?.id) {
        mutableStateOf(state.user?.can("view_live") == true && state.active?.let { PushStore(pushContext).shouldOfferSetup(it.id) } == true)
    }
    val pushEligible = !state.showConnection && state.user?.can("view_live") == true && !immersive && notification == null
    LaunchedEffect(state.active?.id, state.user?.id, pushEligible) {
        if (!pushEligible) pushSetupVisible = false
        else state.active?.let { if (PushStore(pushContext).shouldOfferSetup(it.id)) pushSetupVisible = true }
    }
    LaunchedEffect(notification?.key) {
        notification?.let { pending ->
            val profile = state.profiles.find { it.id == pending.profileId }
            if (profile == null) NotificationNavigation.consume(pending.key)
            else if (state.active?.id != profile.id || state.user == null) model.select(profile)
        }
    }
    val eventUnavailable = stringResource(R.string.events_video_unavailable)
    val eventFallback = stringResource(R.string.events_live_fallback)
    LaunchedEffect(notification?.key, state.active?.id, state.user?.id) {
        val pending = notification ?: return@LaunchedEffect
        if (state.active?.id != pending.profileId || state.user == null || state.showConnection) return@LaunchedEffect
        if (pending.eventId == null) {
            if (state.user?.can("view_live") == true) {
                videoRequest = null
                section = if (pending.action == EventAction.LIVE) Section.LIVE else Section.EVENTS
            }
            NotificationNavigation.consume(pending.key)
            return@LaunchedEffect
        }
        val api = model.api() ?: return@LaunchedEffect
        try {
            val service = EventsApi(api)
            val (event, zone) = service.single(pending.eventId)
            val action = if (state.user?.can("view_timeline") == true) pending.action else EventAction.LIVE
            val result = EventVideoNavigation(api).open(event, action, zone, fromPush = true)
            val request = result.request
            if (request != null) {
                videoRequest = request
                section = if (request.archiveTime == null) Section.LIVE else Section.CHRONOLOGY
                runCatching { service.read(setOf(event.id)) }.onFailure { if (it is CancellationException) throw it }
                if (result.liveFallback) snackbars.showSnackbar(eventFallback)
            } else {
                section = Section.EVENTS
                if (event.ai != null) focusedEvent = event to zone else snackbars.showSnackbar(eventUnavailable)
            }
        } catch (cancelled: CancellationException) { throw cancelled }
        catch (_: Exception) { section = Section.EVENTS; snackbars.showSnackbar(eventUnavailable) }
        finally { NotificationNavigation.consume(pending.key) }
    }
    LaunchedEffect(problemText) { if (problemText != null) { snackbars.showSnackbar(problemText); model.clearProblem() } }
    // The foundation build only exposes complete screens. Further stages add destinations here.
    val implemented = setOf(Section.HOME, Section.LIVE, Section.RECORDINGS, Section.CHRONOLOGY, Section.EVENTS, Section.CAMERAS, Section.STORAGE, Section.RECORDING_CENTER, Section.SYSTEM, Section.SETTINGS, Section.SERVER_SETTINGS)
    val allowed = Section.entries.filter { it in implemented && (it.permission == null || state.user?.can(it.permission) == true) }
    LaunchedEffect(allowed, state.user?.id) { if (state.user != null && section !in allowed) section=Section.HOME }
    BackHandler(enabled = state.showConnection && state.profiles.isNotEmpty()) { model.cancelConnection() }
    BackHandler(enabled = !state.showConnection && section != Section.HOME) { section = Section.HOME }
    KMVMSTheme {
        Scaffold(modifier = Modifier.semantics { testTagsAsResourceId = true }, containerColor = PageBackground,
            snackbarHost = { SnackbarHost(snackbars) }, bottomBar = {
                if(navigationVisible&&compactNavigation)Surface(color=Color.White,modifier=Modifier.fillMaxWidth().navigationBarsPadding()){
                    Row(Modifier.fillMaxWidth().height(56.dp)){
                        primaryNavigation.filter{it in allowed}.forEach{target->
                            Column(Modifier.weight(1f).fillMaxHeight().background(if(section==target)SoftAccent else Color.White)
                                .testTag("nav-${target.name}").clickable{section=target}.semantics(mergeDescendants=true){role=Role.Tab;selected=section==target},
                                horizontalAlignment=Alignment.CenterHorizontally,verticalArrangement=Arrangement.Center){
                                SectionIcon(target,Modifier.size(24.dp), navigation = true)
                                Text(stringResource(target.label),fontSize=10.sp,maxLines=1)
                            }
                        }
                    }
                }
                else if (navigationVisible) NavigationBar(containerColor = Color.White, tonalElevation = 0.dp) {
                    primaryNavigation.filter { it in allowed }.forEach { target ->
                        NavigationBarItem(selected = section == target, onClick = { section = target },
                            modifier = Modifier.testTag("nav-${target.name}"),
                            icon = { SectionIcon(target, Modifier.size(28.dp), navigation = true) }, label = { Text(stringResource(target.label), fontSize = 11.sp, maxLines = 1) },
                            colors = NavigationBarItemDefaults.colors(indicatorColor = SoftAccent))
                    }
                }
            }, contentWindowInsets = if (immersive) WindowInsets(0, 0, 0, 0) else ScaffoldDefaults.contentWindowInsets) { padding ->
            Box(Modifier.fillMaxSize().padding(padding)) {
                screenState.SaveableStateProvider("${state.active?.id ?: "connection"}:${section.name}") {
                when {
                    state.showConnection -> ConnectionScreen(state, model::connect, model::cancelConnection)
                    state.user == null -> Column(Modifier.fillMaxSize()) {
                        Box(Modifier.fillMaxWidth().padding(16.dp)) { HomeBrandHeader(state,model::select,model::openConnection) }
                        Box(Modifier.weight(1f).fillMaxWidth(),contentAlignment=Alignment.Center) {
                            if(state.loading)CircularProgressIndicator()
                            else Column(horizontalAlignment=Alignment.CenterHorizontally,verticalArrangement=Arrangement.spacedBy(12.dp)) {
                                Text(stringResource(R.string.server_unavailable),color=Muted)
                                TextButton(onClick={state.active?.let(model::select)},modifier=Modifier.testTag("connection-retry")){Text(stringResource(R.string.retry))}
                            }
                        }
                    }
                    section == Section.HOME -> HomeScreen(allowed.filter { it != Section.HOME }, { section = it },state,model::select,model::openConnection)
                    section == Section.LIVE -> model.api()?.let { api ->
                        key(state.active?.id, videoRequest?.key) { LiveScreen(api, state.cameras, state.ptz,
                            onMessage = { text -> uiScope.launch { snackbars.showSnackbar(text) } }, onImmersive = { immersive = it }, initial = videoRequest,
                            profileId = state.active!!.id, mayTalk = state.user?.can("talk_cameras") == true, onExit = {
                                val destination = if (videoRequest?.returnToEvents == true) Section.EVENTS else Section.CAMERAS
                                videoRequest = null; section = destination
                            }) }
                    }
                    section == Section.RECORDINGS -> model.api()?.let { api -> RecordingsScreen(api,
                        state.user?.can("export_recordings") == true, state.user?.can("delete_recordings") == true, { immersive=it },
                        { text -> uiScope.launch { snackbars.showSnackbar(text) } }) }
                    section == Section.CHRONOLOGY -> model.api()?.let { api -> ChronologyScreen(api, state.cameras, { immersive = it },
                        { text -> uiScope.launch { snackbars.showSnackbar(text) } }, initial = videoRequest) }
                    section == Section.EVENTS -> model.api()?.let { api -> EventsScreen(api, state.user?.can("manage_cameras") == true,
                        state.user?.can("view_timeline") == true, { request -> videoRequest = request; section = if (request.archiveTime == null) Section.LIVE else Section.CHRONOLOGY },
                        { text -> uiScope.launch { snackbars.showSnackbar(text) } }, profileId = state.active?.id,
                        canClip = state.user?.can("export_recordings") == true, focusedEvent = focusedEvent, onFocusHandled = { focusedEvent = null }) }
                    section == Section.CAMERAS && Section.CAMERAS in allowed -> model.api()?.let { api -> CameraManagementScreen(api,state,model,
                        { text -> uiScope.launch { snackbars.showSnackbar(text) } },
                        openLive = if (Section.LIVE in allowed) { camera ->
                            videoRequest = MobileVideoRequest(camera.id, camera.name, singleCamera = true); section = Section.LIVE
                        } else null) }
                    section == Section.STORAGE -> model.api()?.let{api->StorageScreen(api,state.active!!.id,state.user?.can("run_diagnostics")==true){text->uiScope.launch{snackbars.showSnackbar(text)}}}
                    section == Section.RECORDING_CENTER && Section.RECORDING_CENTER in allowed -> model.api()?.let { api ->
                        key(api, state.user?.id) {
                            RecordingCenterScreen(api, state.user?.can("manage_settings") == true) { text ->
                                uiScope.launch { snackbars.showSnackbar(text) }
                            }
                        }
                    }
                    section == Section.SYSTEM -> model.api()?.let{api->SystemScreen(api){text->uiScope.launch{snackbars.showSnackbar(text)}}}
                    section == Section.SERVER_SETTINGS -> model.api()?.let{api->state.user?.let{actor->
                        key(api,actor.id) { ServerSettingsScreen(api,state.active!!.id,actor,
                            {if(model.api()===api)model.signOut()},{section=Section.SETTINGS}){text->uiScope.launch{snackbars.showSnackbar(text)}} }
                    }}
                    section == Section.SETTINGS -> SettingsScreen(state, model::openConnection,
                        { model.select(it) }, { removeConfirmation = it }, { signOutConfirmation = true }, {section=Section.SERVER_SETTINGS}, updates)
                    else -> Unit
                }
                }
            }
        }
        if (pushSetupVisible && pushEligible) state.active?.let { profile -> model.api()?.let { api ->
            key(profile.id, state.user?.id) {
                PushSetupOffer(api, profile, { text -> uiScope.launch { snackbars.showSnackbar(text) } },
                    close = { pushSetupVisible = false })
            }
        } }
        AppUpdateOffer(updates, visible = !immersive && !state.showConnection && !pushSetupVisible)
        if (signOutConfirmation) AlertDialog(onDismissRequest = { signOutConfirmation = false },
            text = { Text(stringResource(R.string.sign_out_confirmation)) },
            confirmButton = { TextButton(onClick = { signOutConfirmation = false; model.signOut() }) { Text(stringResource(R.string.sign_out)) } },
            dismissButton = { TextButton(onClick = { signOutConfirmation = false }) { Text(stringResource(R.string.cancel)) } })
        removeConfirmation?.let { profile -> AlertDialog(onDismissRequest = { removeConfirmation = null },
            text = { Text(stringResource(R.string.remove_connection_confirmation)) },
            confirmButton = { TextButton(onClick = { removeConfirmation = null; model.removeProfile(profile) }) { Text(stringResource(R.string.remove)) } },
            dismissButton = { TextButton(onClick = { removeConfirmation = null }) { Text(stringResource(R.string.cancel)) } }) }
    }
}

@Composable private fun ConnectionScreen(state: MobileState, connect: (String, String, String, String) -> Unit, cancel: () -> Unit) {
    val editing = state.active.takeIf { state.user == null }
    var name by rememberSaveable(editing?.id) { mutableStateOf(editing?.name.orEmpty()) }
    var address by rememberSaveable(editing?.id) { mutableStateOf(editing?.address.orEmpty()) }
    var user by rememberSaveable(editing?.id) { mutableStateOf(editing?.username.orEmpty()) }
    var password by remember(editing?.id) { mutableStateOf("") }
    Column(Modifier.fillMaxSize().imePadding().verticalScroll(rememberScrollState()).padding(24.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("KM VMS", fontSize = 28.sp, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
            if (state.profiles.isNotEmpty()) IconButton(onClick = cancel) { Icon(Icons.Default.Close, stringResource(R.string.cancel)) }
        }
        Text(stringResource(R.string.connect_server), style = MaterialTheme.typography.headlineSmall)
        OutlinedTextField(name, { name = it }, label = { Text(stringResource(R.string.server_name)) }, singleLine = true,
            modifier = Modifier.fillMaxWidth().testTag("server-name"), enabled = !state.connecting)
        OutlinedTextField(address, { address = it }, label = { Text(stringResource(R.string.server_address)) },
            placeholder = { Text("192.168.1.10:8088") }, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri), singleLine = true,
            modifier = Modifier.fillMaxWidth().testTag("server-address"), enabled = !state.connecting)
        OutlinedTextField(user, { user = it }, label = { Text(stringResource(R.string.username)) }, singleLine = true,
            modifier = Modifier.fillMaxWidth().testTag("server-user"), enabled = !state.connecting)
        OutlinedTextField(password, { password = it }, label = { Text(stringResource(R.string.password)) }, singleLine = true,
            visualTransformation = PasswordVisualTransformation(), keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
            modifier = Modifier.fillMaxWidth().testTag("server-password"), enabled = !state.connecting)
        // A fixed slot keeps the submit button stable as the URL is edited.
        Box(Modifier.fillMaxWidth().height(64.dp)) {
            if (runCatching { ru.kmvms.mobile.core.ServerAddress.parse(address).origin.scheme == "http" }.getOrDefault(false))
                Text(stringResource(R.string.http_warning), fontSize = 12.sp, color = Muted)
        }
        Button(onClick = { connect(name, address, user, password) }, modifier = Modifier.fillMaxWidth().height(50.dp).testTag("connect"),
            enabled = !state.connecting && name.isNotBlank() && address.isNotBlank() && user.isNotBlank() && password.isNotEmpty(), shape = RoundedCornerShape(12.dp)) {
            Text(stringResource(if (state.connecting) R.string.connecting else R.string.connect))
        }
    }
}

@Composable private fun HomeScreen(sections: List<Section>, select: (Section) -> Unit,
                                 state: MobileState, selectServer:(ServerProfile)->Unit, addServer:()->Unit) {
    BoxWithConstraints(Modifier.fillMaxSize()) {
    val visibleSections = dashboardSections.filter { it in sections }
    val columns = ((maxWidth.value - 20) / 172).toInt().coerceAtLeast(1)
    val rows = ((visibleSections.size + columns - 1) / columns).coerceAtLeast(1)
    val tileHeight = ((maxHeight - 108.dp - 12.dp * rows) / rows).coerceIn(128.dp, 146.dp)
    LazyVerticalGrid(columns = GridCells.Adaptive(160.dp), modifier = Modifier.fillMaxSize(), contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        item(span = { GridItemSpan(maxLineSpan) }) {
            HomeBrandHeader(state, selectServer, addServer,
                listOf(Section.RECORDING_CENTER, Section.SYSTEM).filter { it in sections }, select)
        }
        items(visibleSections) { section ->
            Surface(onClick = { select(section) }, shape = RoundedCornerShape(14.dp), color = Color.White,
                modifier = Modifier.testTag("home-${section.name}"),
                border = androidx.compose.foundation.BorderStroke(1.dp, Outline)) {
                Box(Modifier.fillMaxWidth().height(tileHeight)) {
                    DashboardBackground(section, Modifier.matchParentSize())
                    Column(Modifier.fillMaxSize().padding(16.dp), verticalArrangement = Arrangement.SpaceBetween) {
                        SectionIcon(section, Modifier.size(64.dp))
                        Text(stringResource(section.label), fontWeight = FontWeight.SemiBold, fontSize = 16.sp, maxLines = 2)
                    }
                }
            }
        }
    }
    }
}

@Composable private fun HomeBrandHeader(state:MobileState,selectServer:(ServerProfile)->Unit,addServer:()->Unit,
                                       shortcuts: List<Section> = emptyList(), openSection: (Section) -> Unit = {}) {
    var serverMenu by remember{mutableStateOf(false)}
    Box(Modifier.fillMaxWidth().height(76.dp).testTag("home-brand-header")) {
                Image(painterResource(R.drawable.kmvms_home_header),null,Modifier.matchParentSize(),contentScale=ContentScale.Crop,alignment=Alignment.CenterEnd)
                Box(Modifier.matchParentSize().background(Brush.horizontalGradient(listOf(PageBackground,PageBackground.copy(alpha=.8f),Color.Transparent))))
                Row(Modifier.fillMaxSize().padding(horizontal=8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f), verticalArrangement=Arrangement.Center) {
                        Text("KM VMS",fontSize=24.sp,fontWeight=FontWeight.Medium,color=Ink)
                        Box {
                            TextButton(onClick={serverMenu=true},modifier=Modifier.height(36.dp).testTag("server-switch"),
                                contentPadding=PaddingValues(0.dp),colors=ButtonDefaults.textButtonColors(contentColor=Ink)) {
                                Text(state.active?.name.orEmpty(),fontSize=15.sp,fontWeight=FontWeight.Normal,maxLines=1,overflow=TextOverflow.Ellipsis,
                                    modifier=Modifier.weight(1f, fill=false).widthIn(max=260.dp))
                                Text(" ▾",fontSize=14.sp,color=Muted)
                            }
                            DropdownMenu(serverMenu,{serverMenu=false}) {
                                state.profiles.forEach{profile->DropdownMenuItem(text={Text(profile.name)},onClick={serverMenu=false;selectServer(profile)})}
                                DropdownMenuItem(text={Text(stringResource(R.string.add_server))},leadingIcon={Icon(Icons.Default.Add,null)},onClick={serverMenu=false;addServer()})
                            }
                        }
                    }
                    shortcuts.forEach { target ->
                        val label = stringResource(target.label)
                        IconButton(onClick = { openSection(target) }, modifier = Modifier.size(44.dp)
                            .testTag("home-shortcut-${target.name}").semantics { contentDescription = label }) {
                            SectionIcon(target, Modifier.size(29.dp))
                        }
                    }
                }
    }
}

@Composable private fun SettingsScreen(state: MobileState, add: () -> Unit, select: (ServerProfile) -> Unit,
                                      remove: (ServerProfile) -> Unit, signOut: () -> Unit,serverSettings:()->Unit,
                                      updates: ru.kmvms.mobile.update.AppUpdates) {
    val context=LocalContext.current
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(stringResource(R.string.application), style = MaterialTheme.typography.titleLarge)
        val languages=listOf("" to stringResource(R.string.system_language),"en" to "English","ru" to "Русский","zh-CN" to "中文")
        LabeledChoice(stringResource(R.string.app_language),languages.find{it.first==AppLocales.selected(context)}?.second.orEmpty(),languages,true,"app-language"){AppLocales.set(context,it)}
        ru.kmvms.mobile.widget.AlertWidgetSetupButton(state.profiles, state.active?.id)
        Surface(shape = RoundedCornerShape(14.dp), color = Color.White) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(stringResource(R.string.servers), fontWeight = FontWeight.SemiBold)
                state.profiles.forEach { profile -> Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f).clickable { select(profile) }.padding(vertical = 8.dp)) {
                        Text(profile.name, fontWeight = if (state.active?.id == profile.id) FontWeight.Bold else FontWeight.Normal)
                        Text(profile.address, fontSize = 12.sp, color = Muted, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    }
                    IconButton(onClick = { remove(profile) }, modifier = Modifier.testTag("remove-server-${profile.id}")) { Icon(Icons.Default.Close, stringResource(R.string.remove_server)) }
                } }
                TextButton(onClick = add) { Icon(Icons.Default.Add, null); Spacer(Modifier.width(8.dp)); Text(stringResource(R.string.add_server)) }
            }
        }
        if(state.user?.can("manage_settings")==true)OutlinedButton(onClick=serverSettings,modifier=Modifier.fillMaxWidth().height(48.dp).testTag("open-nas-settings")){
            Icon(Icons.Default.Settings,null);Spacer(Modifier.width(8.dp));Text(stringResource(R.string.server_settings))
        }
        AppUpdatePanel(updates)
        ApkDiagnosticsPanel()
        if (state.user != null) Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
            OutlinedButton(onClick = signOut, modifier = Modifier.height(44.dp).testTag("sign-out")) { Text(stringResource(R.string.sign_out)) }
        }
    }
}

@Composable private fun SectionIcon(section: Section, modifier: Modifier, navigation: Boolean = false) {
    val artwork = if (navigation) menuArtwork(section) else section.image
    if (artwork != null) DashboardImage(artwork, modifier)
    else Icon(if (section == Section.HOME) Icons.Default.Home else Icons.Default.Notifications, contentDescription = null, modifier = modifier, tint = Accent)
}

private fun problemResource(problem: ApiProblem) = when (problem) {
    ApiProblem.ADDRESS -> R.string.invalid_server_address
    ApiProblem.AUTHENTICATION -> R.string.authentication_failed
    ApiProblem.FORBIDDEN -> R.string.access_denied
    ApiProblem.RATE_LIMIT -> R.string.too_many_attempts
    ApiProblem.CONNECTION -> R.string.server_unavailable
    ApiProblem.SERVER -> R.string.server_error
    ApiProblem.SAVED_SIGN_IN -> R.string.saved_login_unavailable
}
