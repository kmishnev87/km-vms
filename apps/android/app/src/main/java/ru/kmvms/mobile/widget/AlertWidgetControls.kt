package ru.kmvms.mobile.widget

import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import ru.kmvms.mobile.data.ApiProblem
import ru.kmvms.mobile.data.NasException
import ru.kmvms.mobile.events.*

data class AlertWidgetSnapshot(val preferences: EventPreferences, val cameras: List<EventCamera>, val timezone: String)
data class AlertWidgetState(val snapshot: AlertWidgetSnapshot? = null, val loading: Boolean = true,
    val busy: Boolean = false, val problem: ApiProblem? = null, val updateRequired: Boolean = false)

internal interface AlertChoices {
    suspend fun load(): AlertWidgetSnapshot
    suspend fun master(enabled: Boolean): EventPreferences
    suspend fun camera(id: Long, enabled: Boolean): EventPreferences
}

internal class NasAlertChoices(private val api: EventsApi) : AlertChoices {
    override suspend fun load(): AlertWidgetSnapshot {
        val cameras = api.cameras()
        // One immediate snapshot, not a long poll or a recurring task.
        val result = api.updates(null)
        return AlertWidgetSnapshot(EventPreferences.from(result.getJSONObject("preferences")), cameras, result.getString("timezone"))
    }
    override suspend fun master(enabled: Boolean) = api.chooseNotifications(enabled)
    override suspend fun camera(id: Long, enabled: Boolean) = api.chooseCamera(id, enabled)
}

/** Owns only a visible popup. Failed requests never turn a pending choice into success. */
internal class AlertWidgetControls(private val choices: AlertChoices,
    private val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)) {
    private val mutable = MutableStateFlow(AlertWidgetState())
    val state = mutable.asStateFlow()
    private var request: Job? = null
    fun refresh() {
        if (mutable.value.busy) return
        request?.cancel()
        mutable.update { it.copy(loading = true, problem = null, updateRequired = false) }
        request = scope.launch {
            try { mutable.value = AlertWidgetState(snapshot = choices.load(), loading = false) }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { failed(error); mutable.update { it.copy(loading = false) } }
        }
    }
    fun master(enabled: Boolean) = choose { choices.master(enabled) }
    fun camera(id: Long, enabled: Boolean) = choose { choices.camera(id, enabled) }
    private fun choose(change: suspend () -> EventPreferences) {
        if (mutable.value.busy || mutable.value.loading || mutable.value.snapshot == null || mutable.value.updateRequired) return
        mutable.update { it.copy(busy = true, problem = null) }
        request = scope.launch {
            try {
                val confirmed = change()
                mutable.update { it.copy(snapshot = it.snapshot?.copy(preferences = confirmed)) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { failed(error) }
            finally { mutable.update { it.copy(busy = false) } }
        }
    }
    private fun failed(error: Exception) {
        val failure = error as? NasException
        mutable.update { it.copy(problem = failure?.problem ?: ApiProblem.SERVER,
            updateRequired = failure?.httpStatus in setOf(404, 405)) }
    }
    fun close() = scope.cancel()
}
