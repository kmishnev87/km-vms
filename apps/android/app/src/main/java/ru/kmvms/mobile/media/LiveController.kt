package ru.kmvms.mobile.media

import android.content.Context
import androidx.media3.common.util.UnstableApi
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import ru.kmvms.mobile.core.CameraPlayback
import ru.kmvms.mobile.core.Stream
import ru.kmvms.mobile.data.Camera
import ru.kmvms.mobile.data.NasApi

@UnstableApi
class LiveController(private val context: Context, val api: NasApi) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val mutable = MutableStateFlow<Map<Long, LivePlaybackSession>>(emptyMap())
    val sessions = mutable.asStateFlow()
    private val selections = MutableStateFlow<Map<Long, CameraPlayback>>(emptyMap())
    val playback = selections.asStateFlow()
    private var cameras = emptyMap<Long, Camera>()
    private var visible = emptySet<Long>()
    private var foreground = true
    private var focused: Long? = null
    private var closed = false

    fun cameras(values: List<Camera>) {
        cameras = values.associateBy { it.id }
        selections.update { old -> cameras.mapValues { (id, camera) -> old[id] ?: CameraPlayback(id, if (camera.hasSub) Stream.SUB else Stream.MAIN) } }
        reconcile()
    }
    fun visible(ids: Set<Long>) { visible = ids; reconcile() }
    fun foreground(active: Boolean) { foreground = active; reconcile() }
    fun focus(id: Long?) {
        if (id == null) focused?.let { previous ->
            selections.update { old -> old[previous]?.let { value -> old + (previous to value.copy(
                stream = if (cameras[previous]?.hasSub == true) Stream.SUB else Stream.MAIN,
                sound = false, ptz = false, talk = false, talkDuck = false, soundBeforeCall = null)) } ?: old }
        }
        focused = id; reconcile()
    }
    fun selectStream(id: Long, stream: Stream) = change(id) { it.selectStream(stream) }
    fun sound(id: Long) = change(id) { it.copy(sound = !it.sound) }
    fun talk(id: Long, enabled: Boolean, duck: Boolean = false) = change(id) { it.setTalk(enabled, duck) }
    fun ptz(id: Long, enabled: Boolean) = change(id) { it.setPtz(enabled) }
    private fun change(id: Long, change: (CameraPlayback) -> CameraPlayback) {
        selections.update { old -> old[id]?.let { old + (id to change(it)) } ?: old }
        selections.value[id]?.let { mutable.value[id]?.configure(it) }
    }
    private fun reconcile() {
        if (closed) return
        val wanted = if (foreground) (focused?.let { setOf(it) } ?: visible).intersect(cameras.keys) else emptySet()
        val kept = mutable.value.toMutableMap()
        (kept.keys - wanted).forEach { kept.remove(it)?.close() }
        wanted.forEach { id ->
            val selection = selections.value[id] ?: return@forEach
            val session = kept[id] ?: LivePlaybackSession(context, api, selection, scope).also { kept[id] = it }
            session.configure(selection)
        }
        mutable.value = kept
    }
    fun close() {
        if (closed) return
        closed = true; mutable.value.values.forEach { it.close() }; mutable.value = emptyMap()
        // Session finally blocks retain a bounded, non-cancellable release of their OWN lease.
        scope.cancel()
    }
}
