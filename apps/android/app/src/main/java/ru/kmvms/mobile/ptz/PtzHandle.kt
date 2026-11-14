package ru.kmvms.mobile.ptz

import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import ru.kmvms.mobile.core.PtzDispatcher
import ru.kmvms.mobile.data.NasApi

/** Its shutdown Stop survives UI disposal, but is bounded and never renews in background. */
class PtzHandle(api: NasApi, val cameraId: Long, val capability: PtzCapabilities) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val endpoint = PtzApi(api)
    private val mutableHeld = MutableStateFlow<String?>(null)
    private val mutableError = MutableStateFlow(0)
    private val mutablePresets = MutableStateFlow<List<PtzPreset>>(emptyList())
    private val mutableBusy = MutableStateFlow(false)
    val held = mutableHeld.asStateFlow()
    val errors = mutableError.asStateFlow()
    val presets = mutablePresets.asStateFlow()
    val busy = mutableBusy.asStateFlow()
    private val dispatcher = PtzDispatcher(scope, { endpoint.send(cameraId, capability.profile, it) },
        changed = { mutableHeld.value = it }, failed = { mutableError.value++ })
    private var closed = false
    init {
        if (capability.presets) scope.launch {
            try { mutablePresets.value = endpoint.presets(cameraId) }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { mutableError.value++ }
        }
    }
    fun press(action: String, direction: String, speed: Double) = !closed && dispatcher.press(action, direction, speed)
    fun release() { if (!closed) dispatcher.release() }
    fun stop(force: Boolean = false) { if (!closed) scope.launch { dispatcher.stop(force) } }
    fun preset(token: String) = discrete { dispatcher.preset(token) }
    fun point(x: Double, y: Double) = discrete { dispatcher.point(x, y) }
    fun area(left: Double, top: Double, right: Double, bottom: Double) = discrete { dispatcher.area(left, top, right, bottom) }
    private fun discrete(command: suspend () -> Boolean) {
        if (closed || mutableBusy.value) return
        mutableBusy.value = true
        scope.launch {
            try { if (!command()) mutableError.value++ }
            finally { mutableBusy.value = false }
        }
    }
    fun close() {
        if (closed) return
        closed = true
        scope.launch(NonCancellable) {
            try { withTimeoutOrNull(5000) { dispatcher.dispose() } }
            finally { scope.cancel() }
        }
    }
}
