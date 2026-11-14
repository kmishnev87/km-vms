package ru.kmvms.mobile.server

import android.content.Context
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import org.json.JSONObject
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.data.NasException
import java.util.UUID

data class NasUpdateCandidate(val version: String, val commit: String) {
    init { require(version.matches(Regex("[A-Za-z0-9][A-Za-z0-9._+-]{0,79}")) && commit.matches(Regex("[a-fA-F0-9]{40}"))) }
}
data class NasUpdateAttempt(val id: String, val candidate: NasUpdateCandidate) {
    fun json() = JSONObject().put("submission_id", id).put("expected_manifest_version", candidate.version)
        .put("expected_manifest_commit", candidate.commit).put("confirm", true)
    companion object {
        fun parse(value: String?): NasUpdateAttempt? = runCatching {
            val body = JSONObject(value ?: "")
            val id = body.getString("submission_id").also { require(UUID.fromString(it).toString() == it) }
            NasUpdateAttempt(id, NasUpdateCandidate(body.getString("expected_manifest_version"), body.getString("expected_manifest_commit")))
        }.getOrNull()
    }
}
data class NasUpdateCheck(val installed: String?, val status: String, val candidate: NasUpdateCandidate?) {
    companion object {
        fun from(body: JSONObject): NasUpdateCheck {
            val trusted = body.optJSONObject("trusted_apply_candidate")
            val source = if (trusted?.optBoolean("can_apply_from_ui") == true) trusted else body
            val candidate = if (source.optBoolean("can_apply_from_ui")) runCatching {
                val latest = source.getJSONObject("latest")
                NasUpdateCandidate(latest.getString("version"), latest.getString("commit"))
            }.getOrNull() else null
            return NasUpdateCheck(body.optJSONObject("installed_release")?.text("version"), body.text("status") ?: "unknown", candidate)
        }
    }
}
data class NasUpdateProgress(val submission: String?, val status: String, val percent: Int?, val elapsed: Long?,
                             val step: String?, val verified: Boolean, val installedCommit: String?, val canCancel: Boolean) {
    val terminal get() = status in setOf("completed", "failed", "failed_rolled_back", "cancelled", "blocked")
    val visible get() = status !in setOf("idle", "unknown", "not_configured")
    fun completed(attempt: NasUpdateAttempt?) = status == "completed" && verified &&
        (attempt == null || installedCommit.equals(attempt.candidate.commit, true))
    companion object {
        fun from(value: JSONObject): NasUpdateProgress {
            val body = value.optJSONObject("apply_status") ?: value
            val status = body.text("status") ?: "unknown"
            return NasUpdateProgress(body.text("submission_id"), status,
                if (body.has("progress_percent") && !body.isNull("progress_percent")) body.optInt("progress_percent").takeIf { it in 0..100 } else null,
                body.number("elapsed_seconds"), body.text("current_step"), body.optBoolean("commit_verified"),
                body.text("installed_commit"), body.optBoolean("can_cancel") && status == "queued")
        }
    }
}
interface NasUpdateAttempts { fun load(): NasUpdateAttempt?; fun save(value: NasUpdateAttempt?) }
private class SavedNasUpdateAttempts(context: Context, private val namespace: String): NasUpdateAttempts {
    private val preferences = context.getSharedPreferences("nas_update_receipts", Context.MODE_PRIVATE)
    override fun load() = NasUpdateAttempt.parse(preferences.getString(namespace, null))
    override fun save(value: NasUpdateAttempt?) { preferences.edit().putString(namespace, value?.json()?.toString()).apply() }
}
data class NasUpdatesState(val check: NasUpdateCheck? = null, val progress: NasUpdateProgress? = null,
    val pending: NasUpdateAttempt? = null, val loading: Boolean = false, val checking: Boolean = false,
    val posting: Boolean = false, val watching: Boolean = false, val reconnecting: Boolean = false, val failures: Int = 0)

class NasUpdatesApi(private val api: NasApi) {
    suspend fun check(manual: Boolean) = NasUpdateCheck.from(api.json("api/system/update/${if (manual) "check" else "status"}", if (manual) "POST" else "GET") as JSONObject)
    suspend fun progress() = NasUpdateProgress.from(api.json("api/system/update/apply/status") as JSONObject)
    suspend fun apply(attempt: NasUpdateAttempt) = NasUpdateProgress.from(api.json("api/system/update/apply", "POST", attempt.json()) as JSONObject)
    suspend fun cancel() = NasUpdateProgress.from(api.json("api/system/update/apply/cancel", "POST", JSONObject()) as JSONObject)
}

/** Same NAS updater as web. Poll only the open, foreground operation; never an idle health check. */
class NasUpdatesController(private val api: NasUpdatesApi, private val store: NasUpdateAttempts) {
    constructor(context: Context, api: NasUpdatesApi, namespace: String): this(api, SavedNasUpdateAttempts(context, namespace))
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val mutable = MutableStateFlow(NasUpdatesState(pending = store.load()))
    val state = mutable.asStateFlow()
    private var active = false
    private var watch: Job? = null
    init { refresh() }
    fun foreground(value: Boolean) {
        active = value
        if (value) { refresh(); startWatching() } else { watch?.cancel(); watch = null; mutable.update { it.copy(watching=false) } }
    }
    fun refresh(manual: Boolean = false) {
        if (mutable.value.loading || mutable.value.posting) return
        mutable.update { it.copy(loading=true, checking=manual) }
        scope.launch {
            try {
                val check = api.check(manual)
                mutable.update { it.copy(check=check) }
                accept(api.progress())
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { mutable.update { it.copy(failures=it.failures+1) } }
            finally { mutable.update { it.copy(loading=false, checking=false) }; startWatching() }
        }
    }
    fun start(candidate: NasUpdateCandidate) {
        if (mutable.value.pending != null || mutable.value.posting || candidate != mutable.value.check?.candidate) return
        val attempt = NasUpdateAttempt(UUID.randomUUID().toString(), candidate)
        store.save(attempt); mutable.update { it.copy(pending=attempt, progress=null) }
        submit(attempt)
    }
    fun retry() { mutable.value.pending?.let(::submit) }
    private fun submit(attempt: NasUpdateAttempt) {
        if (mutable.value.posting) return
        watch?.cancel(); watch=null
        mutable.update { it.copy(posting=true) }
        scope.launch {
            var rejected=false
            try { accept(api.apply(attempt)) }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                if ((error as? NasException)?.httpStatus in setOf(400,401,403,404,409,422)) {
                    rejected=true
                    store.save(null); mutable.update { it.copy(pending=null, failures=it.failures+1) }
                } else mutable.update { it.copy(reconnecting=true) } // Keep the receipt on a lost reply.
            } finally { mutable.update { it.copy(posting=false) }; if(rejected)refresh() else startWatching() }
        }
    }
    fun cancel() {
        if (mutable.value.posting || mutable.value.progress?.canCancel != true) return
        mutable.update { it.copy(posting=true) }
        scope.launch {
            try { accept(api.cancel()) }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { mutable.update { it.copy(failures=it.failures+1) } }
            finally { mutable.update { it.copy(posting=false) }; refresh() }
        }
    }
    private fun accept(progress: NasUpdateProgress) {
        val pending = mutable.value.pending
        if (pending != null && progress.submission != pending.id) {
            mutable.update { it.copy(reconnecting=true) }; return
        }
        mutable.update { it.copy(progress=progress, reconnecting=false) }
        if (pending != null && progress.terminal) {
            // Terminal failure is not success. Completion additionally needs the installed commit proof.
            if (progress.status != "completed" || progress.completed(pending)) {
                store.save(null); mutable.update { it.copy(pending=null) }
                scope.launch { try { val check=api.check(false); mutable.update { it.copy(check=check) } } catch (_: Exception) { } }
            }
        }
    }
    private fun startWatching() {
        val value=mutable.value
        val running=value.pending!=null || value.progress?.let { !it.terminal && it.status !in setOf("idle","not_configured","unknown") }==true
        if (!active || !running || value.posting || watch?.isActive==true) return
        watch=scope.launch {
            mutable.update { it.copy(watching=true) }
            val deadline=System.nanoTime()+15L*60*1_000_000_000
            var failures=0
            try { while (isActive && active && System.nanoTime()<deadline) {
                delay(if(failures==0)2500L else (2500L shl failures.coerceAtMost(3)))
                try { accept(api.progress()); failures=0 }
                catch(cancelled:CancellationException){throw cancelled}
                catch(_:Exception){failures++;mutable.update{it.copy(reconnecting=true)};if(failures>=10)break}
                val current=mutable.value
                if(current.pending==null&&current.progress?.terminal==true)break
                if(current.progress?.status=="completed")break // Unverified terminal must remain visible, not spin forever.
            } } finally { mutable.update{it.copy(watching=false)} }
        }
    }
    fun close(){scope.cancel()}
}
