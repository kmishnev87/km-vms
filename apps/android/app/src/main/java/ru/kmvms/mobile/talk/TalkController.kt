package ru.kmvms.mobile.talk

import android.annotation.SuppressLint
import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.AudioRecord
import android.os.SystemClock
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.data.NasException
import ru.kmvms.mobile.diagnostics.DiagnosticEntry
import java.util.concurrent.TimeUnit

data class TalkCapability(val status: String = "loading", val reason: String = "", val fullDuplex: Boolean = false,
                          val credentials: Boolean = false, val saved: Boolean = false,
                          val detectedDuplex: String = "unknown", val preference: String = "auto") {
    val available get() = status in setOf("supported", "needs_credentials")
}
enum class CallPhase { IDLE, CONNECTING, ACTIVE, FAILED }
data class CallState(val phase: CallPhase = CallPhase.IDLE, val microphone: Boolean = false, val reason: String = "")
data class CredentialState(val saving: Boolean = false, val reason: String = "")

/** Per-open-camera controller. No background service, file recording or persistent audio queue. */
class TalkController(context: Context, private val api: NasApi, private val cameraId: Long) {
    private val context = context.applicationContext
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val modes = this.context.getSharedPreferences("camera_talk_modes", Context.MODE_PRIVATE)
    private val modeKey = DiagnosticEntry.profileKey(api.address.base.toASCIIString() + ":" + cameraId)
    private val mutable = MutableStateFlow(CallState())
    val state = mutable.asStateFlow()
    private val capabilityState = MutableStateFlow(TalkCapability())
    val capability = capabilityState.asStateFlow()
    private val credentialState = MutableStateFlow(CredentialState())
    val credential = credentialState.asStateFlow()
    @Volatile private var epoch = 0
    @Volatile private var socket: WebSocket? = null
    private var sessionId: String? = null
    private var connection: Job? = null
    private var heartbeat: Job? = null
    private var microphoneJob: Job? = null
    private var capture: NativeMicrophone? = null
    private val audio = context.getSystemService(AudioManager::class.java)
    private var previousMode: Int? = null
    private var focus: AudioFocusRequest? = null
    private var closed = false
    private var lastReply = 0L

    init { reload() }

    fun reload() {
        scope.launch {
            capabilityState.value = TalkCapability()
            capabilityState.value = try {
                val value = api.json("api/talk/cameras/$cameraId") as JSONObject
                val detected = value.optString("duplex", "unknown")
                val preference = modes.getString(modeKey, "auto") ?: "auto"
                TalkCapability(value.optString("status"), value.optString("reason"), isDuplex(detected, preference),
                    value.optBoolean("credential_required"), value.optBoolean("credential_saved"), detected, preference)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { TalkCapability("unsupported", "camera_unavailable") }
        }
    }

    fun chooseMode(value: String) {
        if (value !in setOf("auto", "full", "push_to_talk")) return
        val capability = capabilityState.value
        if (capability.detectedDuplex == "push_to_talk" && value == "full") return
        if (capability.preference == value) return
        end() // A new mode is applied only to a new, explicitly started call.
        modes.edit().putString(modeKey, value).apply()
        capabilityState.value = capability.copy(fullDuplex = isDuplex(capability.detectedDuplex, value), preference = value)
    }

    private fun isDuplex(detected: String, preference: String) =
        detected != "push_to_talk" && preference != "push_to_talk" && (detected == "full" || preference == "full")

    fun saveCredential(password: String?, after: () -> Unit) {
        if (closed || credentialState.value.saving) return
        credentialState.value = CredentialState(saving = true)
        scope.launch {
            try {
                api.json("api/talk/cameras/$cameraId/credentials", "PUT", JSONObject().put("password", password ?: JSONObject.NULL))
                capabilityState.value = capabilityState.value.copy(status = "supported", saved = password != null)
                credentialState.value = CredentialState()
                if (!closed) after()
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                credentialState.value = CredentialState(reason = (error as? NasException)?.publicReason ?: "credential_save_failed")
            }
        }
    }

    fun editCredential() { if (!credentialState.value.saving) credentialState.value = CredentialState() }

    fun start() {
        if (closed || mutable.value.phase in setOf(CallPhase.CONNECTING, CallPhase.ACTIVE)) return
        val own = ++epoch
        mutable.value = CallState(CallPhase.CONNECTING)
        connection = scope.launch {
            try {
                val response = api.json("api/talk/sessions", "POST", JSONObject().put("camera_id", cameraId)) as JSONObject
                val id = response.getString("session_id")
                require(id.matches(Regex("[A-Za-z0-9_-]{16,64}")))
                if (own != epoch) { release(id); return@launch }
                sessionId = id
                // Construct our own relative endpoint; never follow a remote URL returned by the server.
                val endpoint = api.address.endpoint("api/talk/sessions/$id/audio").toASCIIString()
                val request = Request.Builder().url(endpoint).header("Authorization", "Bearer " + response.getString("media_token")).build()
                val client = api.mediaClient.newBuilder().readTimeout(0, TimeUnit.MILLISECONDS).callTimeout(0, TimeUnit.MILLISECONDS).build()
                socket = client.newWebSocket(request, object : WebSocketListener() {
                    override fun onMessage(webSocket: WebSocket, text: String) {
                        scope.launch {
                            if (own != epoch) return@launch
                            if (text.length > 2048) { fail("invalid_camera_response"); return@launch }
                            val value = runCatching { JSONObject(text) }.getOrNull() ?: return@launch fail("invalid_camera_response")
                            lastReply = SystemClock.elapsedRealtime()
                            when (value.optString("type")) {
                                "ready" -> {
                                    mutable.value = CallState(CallPhase.ACTIVE)
                                    heartbeat = keepAlive(id, own)
                                    if (capabilityState.value.fullDuplex) microphone(true)
                                }
                                "error" -> fail(value.optString("reason").takeIf { it.matches(Regex("[a-z_]{1,64}")) } ?: "call_failed")
                            }
                        }
                    }
                    override fun onMessage(webSocket: WebSocket, bytes: ByteString) { scope.launch { if (own == epoch) fail("invalid_camera_response") } }
                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                        scope.launch { if (own == epoch) fail("connection_lost") }
                    }
                    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                        scope.launch { if (own == epoch) fail("call_ended") }
                    }
                })
                delay(12_000)
                if (own == epoch && mutable.value.phase == CallPhase.CONNECTING) fail("connection_timeout")
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { if (own == epoch) fail((error as? NasException)?.publicReason ?: "call_failed") }
        }
    }

    private fun keepAlive(id: String, own: Int) = scope.launch {
        var seconds = 0
        try {
            while (own == epoch) {
                delay(1000)
                if (SystemClock.elapsedRealtime() - lastReply > 6000) { fail("connection_lost"); break }
                if (socket?.send("{\"type\":\"keepalive\"}") != true) { fail("connection_lost"); break }
                if (++seconds % 60 == 0) {
                    val token = api.json("api/talk/sessions/$id/token", "POST", JSONObject()) as JSONObject
                    socket?.send(JSONObject().put("type", "authorization").put("media_token", token.getString("media_token")).toString())
                }
            }
        } catch (cancelled: CancellationException) { throw cancelled }
        catch (_: Exception) { if (own == epoch) fail("connection_lost") }
    }

    @SuppressLint("MissingPermission")
    fun microphone(enabled: Boolean) {
        if (mutable.value.phase != CallPhase.ACTIVE || enabled == mutable.value.microphone) return
        if (!enabled) { mutable.value = mutable.value.copy(microphone = false); stopMicrophone(); return }
        try {
            if (previousMode == null) {
                previousMode = audio.mode
                val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                    .setAudioAttributes(AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                    .setOnAudioFocusChangeListener { change -> if (change < 0) scope.launch { end() } }.build()
                if (audio.requestAudioFocus(request) != AudioManager.AUDIOFOCUS_REQUEST_GRANTED) throw IllegalStateException()
                focus = request
                audio.mode = AudioManager.MODE_IN_COMMUNICATION
            }
            mutable.value = mutable.value.copy(microphone = true)
            val own = epoch
            val source = NativeMicrophone().also { capture = it }
            microphoneJob = scope.launch(Dispatchers.IO) {
                try {
                    val recorder = source.open()
                    val frame = ByteArray(640)
                    while (isActive && own == epoch) {
                        var used = 0
                        while (used < frame.size && isActive) {
                            val count = recorder.read(frame, used, frame.size - used, AudioRecord.READ_BLOCKING)
                            check(count > 0); used += count
                        }
                        ensureActive()
                        val target = socket ?: throw IllegalStateException()
                        check(target.queueSize() <= 6400 && target.send(frame.toByteString()))
                    }
                } catch (cancelled: CancellationException) { throw cancelled }
                catch (_: Exception) { withContext(Dispatchers.Main) { if (own == epoch && capture === source && mutable.value.microphone) fail("microphone_failed") } }
                finally { source.close() }
            }
        } catch (_: Exception) { fail("microphone_failed") }
    }

    private fun stopMicrophone() {
        microphoneJob?.cancel(); microphoneJob = null
        val own = capture; capture = null
        own?.stopSending()
        if (own != null) scope.launch(NonCancellable + Dispatchers.IO) { own.close() }
    }

    private fun fail(reason: String) {
        if (capabilityState.value.credentials && reason in setOf("talk_authentication_failed", "talk_credentials_required")) {
            capabilityState.value = capabilityState.value.copy(status = "needs_credentials", saved = false)
        }
        end()
        mutable.value = CallState(CallPhase.FAILED, reason = reason)
    }

    fun end() {
        ++epoch
        connection?.cancel(); connection = null
        heartbeat?.cancel(); heartbeat = null
        mutable.value = CallState()
        stopMicrophone()
        socket?.cancel(); socket = null
        previousMode?.let { runCatching { audio.mode = it } }; previousMode = null
        focus?.let { runCatching { audio.abandonAudioFocusRequest(it) } }; focus = null
        val id = sessionId; sessionId = null
        if (id != null) scope.launch(start = CoroutineStart.UNDISPATCHED) { release(id) }
    }

    private suspend fun release(id: String) = withContext(NonCancellable) {
        withTimeoutOrNull(5000) { runCatching { api.json("api/talk/sessions/$id", "DELETE") } }
        Unit
    }

    fun close() { if (!closed) { closed = true; end(); scope.cancel() } }
}
