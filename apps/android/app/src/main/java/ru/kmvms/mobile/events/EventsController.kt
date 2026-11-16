package ru.kmvms.mobile.events

import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import ru.kmvms.mobile.data.NasException

data class EventsState(
    val filter: EventFilter = EventFilter(), val page: EventPage? = null,
    val selected: Set<String> = emptySet(), val cameras: List<EventCamera> = emptyList(),
    val preferences: EventPreferences? = null, val loading: Boolean = false,
    val busy: Boolean = false, val failures: Int = 0,
    val selection: EventSelection? = null,
    val settingsLoading: Boolean = false,
) {
    val selectedCount get() = selection?.selectedCount ?: selected.size
    fun isSelected(id: String) = selection?.contains(id) ?: (id in selected)
    fun canSelect(id: String) = selection?.let { id in it.visibleIds } ?: (selected.size < 200 || id in selected)
}

/** Long-poll exists only while this native screen is in the foreground. */
class EventsController(private val api: EventsApi) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val mutable = MutableStateFlow(EventsState())
    val state = mutable.asStateFlow()
    private var loading: Job? = null
    private var watch: Job? = null
    private var settingsJob: Job? = null
    private var settingsGeneration = 0L
    private var generation = 0L
    private var selectionGeneration = 0L
    private var active = false
    init {
        refresh()
        refreshSettings()
    }

    fun refreshSettings() {
        settingsJob?.cancel()
        val own = ++settingsGeneration
        mutable.update { it.copy(settingsLoading = true) }
        settingsJob = scope.launch {
            try {
                val cameras = api.cameras()
                val preferences = api.preferences()
                if (own == settingsGeneration) mutable.update { it.copy(cameras = cameras, preferences = preferences) }
            }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { if (own == settingsGeneration) failure() }
            finally { if (own == settingsGeneration) mutable.update { it.copy(settingsLoading = false) } }
        }
    }

    fun filter(value: EventFilter) {
        selectionGeneration++
        mutable.update { it.copy(filter = value, selected = emptySet(), selection = null) }
        refresh(0)
    }
    fun refresh(offset: Int = mutable.value.page?.offset ?: 0) {
        loading?.cancel()
        val own = ++generation
        val filter = mutable.value.filter
        val selection = mutable.value.selection
        mutable.update { it.copy(loading = true) }
        loading = scope.launch {
            try {
                val page = api.page(filter, offset, selection)
                if (own == generation) {
                    mutable.update { it.copy(page = page, selection = it.selection?.let { selected ->
                        if(page.selectionId==selected.id)selected.copy(visibleIds=page.visibleIds) else selected }) }
                    if (offset > 0 && page.items.isEmpty()) refresh(((page.total - 1).coerceAtLeast(0) / 30) * 30)
                }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { if (own == generation) {
                if(selection!=null && (error as? NasException)?.httpStatus==409)clearSelection()
                failure()
            } }
            finally { if (own == generation) mutable.update { it.copy(loading = false) } }
        }
    }
    fun select(id: String, selected: Boolean) {
        mutable.update { state ->
            val snapshot=state.selection
            if(snapshot!=null) {
                if(id !in snapshot.visibleIds)state
                else snapshot.copy(excluded=if(selected)snapshot.excluded-id else snapshot.excluded+id).let { next ->
                    state.copy(selection=next.takeIf { it.selectedCount>0 },selected=emptySet()) }
            } else state.copy(selected = if (selected) (state.selected + id).take(200).toSet() else state.selected - id)
        }
    }
    fun selectPage(selected: Boolean) {
        selectionGeneration++
        val ids = mutable.value.page?.items.orEmpty().map { it.id }.toSet()
        mutable.update { it.copy(selection=null,selected = if (selected) (it.selected + ids).take(200).toSet() else it.selected - ids) }
    }
    fun clearSelection() { selectionGeneration++; mutable.update { it.copy(selected=emptySet(),selection=null) } }
    fun selectFiltered() {
        val value=mutable.value
        if(value.busy||value.loading||value.page==null)return
        val own=++selectionGeneration
        mutable.update { it.copy(busy=true) }
        scope.launch {
            try {
                val selected=api.selectFiltered(value.filter,value.page)
                if(own==selectionGeneration){mutable.update { it.copy(selection=selected,selected=emptySet()) };refresh()}
            } catch(cancelled:CancellationException){throw cancelled}
            catch(_:Exception){if(own==selectionGeneration)failure()}
            finally{mutable.update { it.copy(busy=false) }}
        }
    }
    fun deletion(): EventDeletion? {
        val value = mutable.value
        value.selection?.let { return EventDeletion(count=it.selectedCount,snapshot=it.copy(excluded=it.excluded.toSet())) }
        if (value.selected.isNotEmpty()) return EventDeletion(ids = value.selected.toSet(), count = value.selected.size)
        val page = value.page ?: return null
        return page.cursor?.takeIf { page.historyCount > 0 }?.let { EventDeletion(through = it, count = page.historyCount) }
    }
    fun read() {
        val value = mutable.value
        if(value.selection!=null){action { api.readSelection(value.selection) };return}
        val ids = value.selected.toSet()
        val through = value.page?.cursor
        if (ids.isEmpty() && through == null) return
        action { if (ids.isNotEmpty()) api.read(ids) else api.readAll(through!!) }
    }
    fun delete(target: EventDeletion) = action { api.delete(target) }
    fun save(preferences: EventPreferences) = action {
        val saved = api.save(preferences)
        mutable.update { it.copy(preferences = saved) }
    }
    fun receiving(cameraId: Long, enabled: Boolean) = action {
        api.receiving(cameraId, enabled)
        val cameras = api.cameras()
        mutable.update { it.copy(cameras = cameras) }
    }
    private fun action(block: suspend () -> Unit) {
        if (mutable.value.busy) return
        mutable.update { it.copy(busy = true) }
        scope.launch {
            try { block(); clearSelection(); refresh() }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { failure() }
            finally { mutable.update { it.copy(busy = false) } }
        }
    }
    fun foreground(value: Boolean) {
        active = value
        watch?.cancel(); watch = null
        if (!value) { loading?.cancel(); mutable.update { it.copy(loading = false) }; return }
        refresh()
        watch = scope.launch {
            var cursor: String? = null
            while (isActive && active) {
                try {
                    val changes = api.updates(cursor)
                    cursor = changes.optString("cursor").takeUnless { it.isBlank() || it == "null" }
                    changes.optJSONObject("preferences")?.let { value ->
                        if (!mutable.value.busy) mutable.update { it.copy(preferences = EventPreferences.from(value)) }
                    }
                    if (changes.optBoolean("changed") || (changes.optJSONArray("items")?.length() ?: 0) > 0) refresh()
                    // A broken server without a cursor must not become a hot loop.
                    if (cursor == null) delay(30_000)
                } catch (cancelled: CancellationException) { throw cancelled }
                catch (_: Exception) { delay(30_000) }
            }
        }
    }
    private fun failure() { mutable.update { it.copy(failures = it.failures + 1) } }
    fun close() { scope.cancel() }
}
