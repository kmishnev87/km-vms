package ru.kmvms.mobile.archive

import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import java.time.OffsetDateTime

data class ClipsState(val items: List<Clip> = emptyList(), val more: Boolean = false, val loading: Boolean = false,
                      val working: Boolean = false, val maxDuration: Long? = null, val failures: Int = 0)

class ClipsController(private val api: ArchiveApi) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val mutable = MutableStateFlow(ClipsState())
    val state = mutable.asStateFlow()
    private var foreground = true
    private var generating: String? = null
    fun foreground(active: Boolean) { foreground = active }
    init {
        refresh()
        scope.launch { try { val duration = api.limits(); mutable.update { it.copy(maxDuration = duration) } }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { error() } }
        scope.launch { while (isActive) {
            delay(5000)
            if (foreground) generating?.let { id ->
                try { val updated = api.clip(id); mutable.update { it.copy(items = it.items.map { clip -> if (clip.id == id) updated else clip }) } }
                catch (cancelled: CancellationException) { throw cancelled }
                catch (_: Exception) { /* The long operation owns the final error; do not spam periodic toasts. */ }
            }
        } }
    }
    fun refresh(append: Boolean = false) {
        if (mutable.value.loading) return
        mutable.update { it.copy(loading = true) }
        val offset = if (append) mutable.value.items.size else 0
        scope.launch {
            try { val (items,more) = api.clips(offset)
                mutable.update { it.copy(items = if (append) (it.items+items).distinctBy { clip -> clip.id } else items, more = more) } }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { error() }
            finally { mutable.update { it.copy(loading = false) } }
        }
    }
    fun create(camera: Long, title: String, start: OffsetDateTime, end: OffsetDateTime) = operation {
        val clip = api.createClip(camera,title,start,end)
        generating = clip.id
        mutable.update { it.copy(items = listOf(clip)+it.items) }
        val generated = api.generate(clip.id)
        if (generated.status == "done" && !generated.canDownload) api.manifest(clip.id)
    }
    fun generate(clip: Clip) = operation {
        generating = clip.id
        val generated = api.generate(clip.id)
        if (generated.status == "done" && !generated.canDownload) api.manifest(clip.id)
    }
    fun delete(clip: Clip) = operation { api.deleteClip(clip.id) }
    private fun operation(block: suspend () -> Unit) {
        if (mutable.value.working) return
        mutable.update { it.copy(working = true) }
        scope.launch {
            try { block() }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { error() }
            finally { generating = null; mutable.update { it.copy(working = false) }; refresh() }
        }
    }
    private fun error() { mutable.update { it.copy(failures = it.failures+1) } }
    fun close() { scope.cancel() }
}
