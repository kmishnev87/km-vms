package ru.kmvms.mobile.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import okhttp3.Call
import okhttp3.Callback
import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import ru.kmvms.mobile.core.ServerAddress
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resumeWithException
import ru.kmvms.mobile.diagnostics.*

private class InstallationCookies : CookieJar {
    private val cookies = mutableListOf<Cookie>()
    @Synchronized override fun saveFromResponse(url: HttpUrl, cookies: List<Cookie>) {
        cookies.forEach { value ->
            this.cookies.removeAll { it.name == value.name && it.domain == value.domain && it.path == value.path }
            if (value.expiresAt > System.currentTimeMillis()) this.cookies.add(value)
        }
    }
    @Synchronized override fun loadForRequest(url: HttpUrl): List<Cookie> {
        cookies.removeAll { it.expiresAt <= System.currentTimeMillis() }
        return cookies.filter { it.matches(url) }
    }
    @Synchronized fun clear() = cookies.clear()
}

/** One instance per saved NAS profile: cookies and credentials are never shared. */
class NasApi(val address: ServerAddress) {
    private val cookies = InstallationCookies()
    val mediaClient = OkHttpClient.Builder().cookieJar(cookies).followRedirects(false).followSslRedirects(false)
        .connectTimeout(10, TimeUnit.SECONDS).readTimeout(20, TimeUnit.SECONDS).callTimeout(30, TimeUnit.SECONDS).build()
    internal val longPollClient = mediaClient.newBuilder().readTimeout(35, TimeUnit.SECONDS).callTimeout(40, TimeUnit.SECONDS).build()
    // Only the notification JPEG uses this bound; the ordinary history asset
    // timeout and authentication path are unchanged.
    private val quickFrameClient = mediaClient.newBuilder().callTimeout(3200, TimeUnit.MILLISECONDS).build()
    @Volatile private var access: String? = null
    // Opaque, in-memory boundary for scoped media leases; never expose the bearer.
    @Volatile private var mediaAuthority: Any? = null
    private class Renewal(val load: () -> SavedSignIn?, val persist: (String, String) -> Boolean)
    @Volatile private var renewal: Renewal? = null
    @Volatile private var renewalRejected = false
    private val renewalLock = Mutex()
    fun useSession(value: String) {
        mediaAuthority = null
        access = value; renewalRejected = false
        mediaAuthority = Any()
    }
    internal fun mediaAuthorizationScope(): Any? = mediaAuthority
    fun allowSessionRenewal(load: () -> SavedSignIn?, persist: (String, String) -> Boolean) {
        renewal = Renewal(load, persist); renewalRejected = false
    }
    fun disableSessionRenewal() { mediaAuthority = null; renewal = null; renewalRejected = true }
    suspend fun login(username: String, password: String): String {
        val body = JSONObject().put("username", username).put("password", password).put("stay_signed_in", false)
        val result = request("api/auth/login", "POST", body, authenticated = false) as JSONObject
        val value = result.getString("access_token")
        sessionUsername = username
        useSession(value)
        return value
    }
    suspend fun me(): NasUser {
        val value = request("api/auth/me") as JSONObject
        val permissions = value.getJSONArray("permissions")
        return NasUser(value.getLong("id"), value.optString("full_name").ifBlank { value.getString("username") }, value.getString("role"),
            (0 until permissions.length()).map { permissions.getString(it) }.toSet())
    }
    suspend fun cameras(management: Boolean): List<Camera> {
        val array = request(if (management) "api/cameras" else "api/viewer/cameras") as JSONArray
        if (management) {
            // Management responses carry retention; viewer responses carry safe stream dimensions.
            val playable = request("api/viewer/cameras") as JSONArray
            val playbackById = (0 until playable.length()).map { playable.getJSONObject(it) }.associateBy { it.getLong("id") }
            for (index in 0 until array.length()) {
                val item = array.getJSONObject(index)
                playbackById[item.getLong("id")]?.let { viewer ->
                    for (key in listOf("rtsp_main_url", "rtsp_sub_url", "stream_dimensions", "ai_overlay")) {
                        if (viewer.has(key)) item.put(key, viewer.get(key))
                    }
                }
            }
        }
        return (0 until array.length()).map { Camera.fromJson(array.getJSONObject(it)) }
    }
    suspend fun setRecording(cameraId: Long, enabled: Boolean) {
        require(cameraId > 0)
        request("api/cameras/$cameraId/${if (enabled) "enable" else "disable"}", "POST", JSONObject())
    }
    suspend fun logout() {
        disableSessionRenewal()
        try { request("api/auth/logout", "POST", JSONObject(), authenticated = false) }
        finally { mediaAuthority = null; access = null; cookies.clear() }
    }
    suspend fun json(path: String, method: String = "GET", body: JSONObject? = null): Any? = request(path, method, body)
    suspend fun cameraPreview(cameraId: Long, mayRenew: Boolean = true): ByteArray {
        require(cameraId > 0)
        return getImage("api/previews/camera-previews/$cameraId.jpg", 4L * 1024 * 1024, true, mayRenew)
    }
    internal suspend fun eventSnapshot(id: String, quick: Boolean = false): ByteArray {
        require(id.matches(Regex("[a-fA-F0-9-]{36}")))
        return getImage("api/camera-events/$id/snapshot", 128L * 1024, false, quick = quick)
    }
    private suspend fun getImage(path: String, maxBytes: Long, allowPng: Boolean, mayRenew: Boolean = true,
                                 quick: Boolean = false): ByteArray = withContext(Dispatchers.IO) {
        val ownAccess = access ?: throw NasException(ApiProblem.AUTHENTICATION)
        val request = Request.Builder().url(address.endpoint(path).toASCIIString())
            .header("Accept", "image/jpeg").header("Authorization", "Bearer $ownAccess").build()
        try {
            execute(request, quickFrame = quick).use { response ->
                if (response.code == 401 && mayRenew && renewal != null) {
                    response.close(); renewSession(ownAccess)
                    return@withContext getImage(path, maxBytes, allowPng, false, quick)
                }
                if (!response.isSuccessful) throw NasException(when (response.code) {
                    401 -> ApiProblem.AUTHENTICATION; 403 -> ApiProblem.FORBIDDEN; else -> ApiProblem.SERVER
                }, response.code)
                require(response.body?.contentType()?.let { it.type == "image" && (it.subtype == "jpeg" || allowPng && it.subtype == "png") } == true)
                val source = response.body?.source() ?: throw NasException(ApiProblem.SERVER)
                require(!source.request(maxBytes + 1))
                source.readByteArray()
            }
        } catch (error: IOException) { throw NasException(ApiProblem.CONNECTION) }
    }
    suspend fun longPoll(path: String): Any? = request(path, longPolling = true)
    suspend fun generateClip(id: String): Any? {
        require(id.matches(Regex("[a-fA-F0-9-]{1,64}")))
        return request("api/archive/exports/$id/generate", "POST", JSONObject(), longOperation = true)
    }
    suspend fun prepareClipManifest(id: String): Any? {
        require(id.matches(Regex("[a-fA-F0-9-]{1,64}")))
        return request("api/archive/exports/$id/manifest", "POST", JSONObject(), longOperation = true)
    }
    private suspend fun request(path: String, method: String = "GET", body: JSONObject? = null,
                                authenticated: Boolean = true, mayRenew: Boolean = true, longOperation: Boolean = false,
                                longPolling: Boolean = false): Any? = withContext(Dispatchers.IO) {
        val url = try { address.endpoint(path).toASCIIString() } catch (_: IllegalArgumentException) { throw NasException(ApiProblem.ADDRESS) }
        val builder = Request.Builder().url(url).header("Accept", "application/json").header("User-Agent", "KM-VMS-Android/0.1")
        val ownAccess = access
        if (authenticated) builder.header("Authorization", "Bearer ${ownAccess ?: throw NasException(ApiProblem.AUTHENTICATION)}")
        val payload = if (method in setOf("POST", "PUT", "PATCH")) (body ?: JSONObject()).toString().toRequestBody("application/json".toMediaType()) else null
        builder.method(method, payload)
        try {
            execute(builder.build(), longOperation, longPolling).use { response ->
                if (response.code == 401 && authenticated && mayRenew && renewal != null) {
                    response.close()
                    renewSession(ownAccess ?: throw NasException(ApiProblem.AUTHENTICATION))
                    return@withContext request(path, method, body, authenticated, mayRenew = false, longOperation = longOperation, longPolling = longPolling)
                }
                if (!response.isSuccessful) {
                    // Expose allowlisted reason codes, never arbitrary server error bodies.
                    val talk = path.startsWith("api/talk/")
                    val recording = path.startsWith("api/ai-recording/")
                    val publicReason = if (talk || recording) runCatching {
                        val source = response.body?.source() ?: return@runCatching null
                        if (source.request(8193)) return@runCatching null
                        val error = JSONObject(source.readUtf8())
                        if (recording) error.optJSONObject("detail")?.optString("code")?.takeIf { it in setOf(
                            "recording_policy_changed", "emergency_setting_changed", "recording_link_source_unavailable",
                            "recording_link_cycle", "camera_event_reception_required", "ai_frame_busy",
                            "ai_frame_reference_required", "ai_frame_reference_changed", "ai_record_rule_required",
                        ) } else error.optString("detail").takeIf { it in setOf(
                            "talk_authentication_failed", "talk_access_denied", "talk_credentials_required",
                            "camera_unavailable", "camera_configuration_changed", "camera_busy", "server_busy",
                            "discovery_busy", "camera_timeout", "camera_rejected_call", "authentication_unsupported",
                            "backchannel_unsupported", "invalid_camera_response",
                        ) }
                    }.getOrNull() else null
                    throw NasException(when (response.code) {
                        401 -> ApiProblem.AUTHENTICATION; 403 -> ApiProblem.FORBIDDEN; 429 -> ApiProblem.RATE_LIMIT; else -> ApiProblem.SERVER
                    }, response.code, publicReason)
                }
                if (response.code == 204) return@withContext null
                val source = response.body?.source() ?: throw NasException(ApiProblem.SERVER)
                if (source.request(2L * 1024 * 1024 + 1)) throw NasException(ApiProblem.SERVER)
                val text = source.readUtf8().trim()
                when { text.startsWith('[') -> JSONArray(text); text.startsWith('{') -> JSONObject(text); else -> throw NasException(ApiProblem.SERVER) }
            }
        } catch (error: NasException) { diagnosticFailure(path, error.httpStatus ?: 0); throw error }
        catch (_: IOException) { diagnosticFailure(path, 0); throw NasException(ApiProblem.CONNECTION) }
        catch (_: org.json.JSONException) { diagnosticFailure(path, -1); throw NasException(ApiProblem.SERVER) }
    }

    private fun diagnosticFailure(path: String, status: Int) {
        val area = when {
            path.startsWith("api/auth/") -> DiagnosticArea.AUTH
            path.startsWith("api/mobile/push/") -> DiagnosticArea.PUSH
            path.startsWith("api/camera-events") -> DiagnosticArea.EVENTS
            path.startsWith("api/recordings") || path.startsWith("api/archive/") || path.startsWith("api/chronology") -> DiagnosticArea.ARCHIVE
            path.startsWith("api/live/") -> DiagnosticArea.LIVE
            path.startsWith("api/cameras") -> DiagnosticArea.CAMERAS
            path.startsWith("api/settings") || path.startsWith("api/system") -> DiagnosticArea.SETTINGS
            else -> DiagnosticArea.OTHER
        }
        AppDiagnostics.record(DiagnosticCode.API_FAILED, area, number = status)
    }

    private suspend fun renewSession(expected: String) = renewalLock.withLock {
        if (access != expected) return@withLock
        val configuration = renewal ?: throw NasException(ApiProblem.AUTHENTICATION)
        if (renewalRejected) throw NasException(ApiProblem.AUTHENTICATION)
        val saved = configuration.load() ?: throw NasException(ApiProblem.AUTHENTICATION)
        // Kept on this installation's client. No callback may change selected NAS.
        val username = sessionUsername ?: throw NasException(ApiProblem.AUTHENTICATION)
        try {
            val token = login(username, saved.password)
            if (renewal !== configuration || !configuration.persist(saved.access, token)) {
                mediaAuthority = null; access = null; throw NasException(ApiProblem.AUTHENTICATION)
            }
        } catch (error: NasException) {
            if (error.problem == ApiProblem.AUTHENTICATION) { mediaAuthority = null; renewalRejected = true }
            throw error
        }
    }
    @Volatile private var sessionUsername: String? = null
    fun sessionUser(username: String) { sessionUsername = username }

    private suspend fun execute(request: Request, longOperation: Boolean = false, longPolling: Boolean = false,
                                quickFrame: Boolean = false): Response = suspendCancellableCoroutine { continuation ->
        val client = if (quickFrame) quickFrameClient
            else if (longOperation) mediaClient.newBuilder().callTimeout(20, TimeUnit.MINUTES).readTimeout(20, TimeUnit.MINUTES).build()
            else if (longPolling) longPollClient else mediaClient
        val call = client.newCall(request)
        continuation.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (continuation.isActive) continuation.resumeWithException(e)
            }
            override fun onResponse(call: Call, response: Response) {
                continuation.resume(response) { _, cancelledResponse, _ -> cancelledResponse.close() }
            }
        })
    }
}
