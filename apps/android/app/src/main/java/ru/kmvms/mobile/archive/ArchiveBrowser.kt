package ru.kmvms.mobile.archive

import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZoneOffset

data class ArchiveBrowserState(val cameras: List<ArchiveCamera> = emptyList(), val exportable: List<ArchiveCamera> = emptyList(),
                               val items: List<Recording> = emptyList(), val total: Int = 0, val more: Boolean = false,
                               val loading: Boolean = false, val failed: Boolean = false, val zone: ZoneId = ZoneOffset.UTC)

class ArchiveBrowser(private val api: ArchiveApi) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val mutable = MutableStateFlow(ArchiveBrowserState())
    val state = mutable.asStateFlow()
    private var request: Job? = null
    private var generation = 0
    private var camera: String? = null
    private var day: LocalDate? = null
    private val optionsLock = Mutex()
    private var optionsLoaded = false
    private suspend fun ensureOptions() = optionsLock.withLock {
        if (!optionsLoaded) {
            val options = api.options()
            mutable.update { it.copy(cameras = options.cameras, exportable = options.exportable) }
            optionsLoaded = true
        }
    }
    init { scope.launch {
        try { ensureOptions() }
        catch (cancelled: CancellationException) { throw cancelled }
        catch (_: Exception) { mutable.update { it.copy(failed = true) } }
    } }
    fun search(camera: String?, day: LocalDate?) {
        this.camera = camera; this.day = day; load(append = false)
    }
    fun more() { if (!mutable.value.loading && mutable.value.more) load(append = true) }
    fun refresh() = load(append = false)
    private fun load(append: Boolean) {
        request?.cancel(); val own = ++generation
        val offset = if (append) mutable.value.items.size else 0
        mutable.update { it.copy(loading = true, failed = false) }
        request = scope.launch {
            try {
                // A restored selection can arrive before the new browser's catalog.
                // Never reinterpret its stable ID token as a legacy camera name.
                ensureOptions()
                val selected = mutable.value.cameras.find { it.value == camera }
                val page = api.list(selected?.name ?: camera, day, offset, selected?.takeIf { it.filterById }?.id)
                if (own == generation) mutable.update { it.copy(items = if (append) (it.items+page.items).distinctBy { row -> row.identityKey } else page.items,
                    total = page.total, more = page.more, zone = page.zone, loading = false) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { if (own == generation) mutable.update { it.copy(loading = false, failed = true) } }
        }
    }
    fun close() { scope.cancel() }
    internal suspend fun awaitClosed() { scope.coroutineContext[Job]?.join() }
}
