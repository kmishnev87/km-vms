package ru.kmvms.mobile.media

import android.net.Uri
import android.util.Log
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.BaseDataSource
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONObject
import ru.kmvms.mobile.core.LiveBytePipe
import ru.kmvms.mobile.core.Stream
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.BuildConfig
import java.io.IOException
import java.util.concurrent.TimeUnit

internal class LiveTransportIncompatibleException : IOException("Live source codec is incompatible")

/** NAS-native fMP4 over WS, fed directly to Media3, not through a WebView. */
@UnstableApi
class WebSocketDataSource(private val api: NasApi, private val cameraId: Long, private val stream: Stream,
                          private val audioAvailable: (Boolean) -> Unit) : BaseDataSource(true) {
    private val pipe = LiveBytePipe()
    @Volatile private var socket: WebSocket? = null
    @Volatile private var closed = false
    private var opened = false
    private var uri: Uri? = null
    override fun open(dataSpec: DataSpec): Long {
        if (dataSpec.position != 0L) throw IOException("Live transport is not seekable")
        transferInitializing(dataSpec)
        uri = dataSpec.uri
        val endpoint = api.address.endpoint("api/live/$cameraId/${stream.apiKey}/mse").toString()
        if (dataSpec.uri.toString() != endpoint) throw IOException("Invalid live transport endpoint")
        val request = Request.Builder().url(endpoint).header("Origin", api.address.origin.toASCIIString()).build()
        var initialized = false
        socket = api.mediaClient.newBuilder().readTimeout(0, TimeUnit.SECONDS).callTimeout(0, TimeUnit.SECONDS).build()
            .newWebSocket(request, object : WebSocketListener() {
                override fun onMessage(webSocket: WebSocket, text: String) {
                    if (closed) return
                    try {
                        if (text.length > 8192) throw IOException("Invalid live control packet")
                        val value = JSONObject(text)
                        when (value.optString("type")) {
                            "init" -> {
                                if (!value.optString("mime").startsWith("video/mp4")) throw IOException("Unsupported live container")
                                initialized = true; audioAvailable(value.optBoolean("audio_available"))
                            }
                            "error" -> {
                                if (value.optString("code") == "mse_codec_unsupported") throw LiveTransportIncompatibleException()
                                val code = value.optString("code").takeIf { it in setOf("mse_access_denied", "mse_stream_failed", "mse_slow_viewer", "mse_stream_closed") } ?: "unknown"
                                debug("server-error=$code")
                                throw IOException("Live source unavailable")
                            }
                            else -> throw IOException("Invalid live control packet")
                        }
                    } catch (error: Exception) {
                        pipe.fail(if (error is LiveTransportIncompatibleException) error else IOException("Live source unavailable"))
                        webSocket.cancel()
                    }
                }
                override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                    if (!initialized || !pipe.offer(bytes.toByteArray())) {
                        debug("pipe-rejected=${pipe.rejectionReason}")
                        pipe.fail(IOException("Live consumer must reconnect")); webSocket.cancel()
                    }
                }
                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    debug("connection-failed=${t.javaClass.simpleName}")
                    response?.close()
                    if (!closed) pipe.fail(IOException("Live connection interrupted"))
                }
                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                    debug("closed-code=$code")
                    if (!closed) pipe.fail(IOException("Live connection closed"))
                }
                override fun onClosing(webSocket: WebSocket, code: Int, reason: String) { webSocket.close(code, null) }
            })
        if (closed) socket?.cancel()
        opened = true; transferStarted(dataSpec)
        return C.LENGTH_UNSET.toLong()
    }
    fun refresh(token: String): Boolean {
        val sent = !closed && socket?.send(JSONObject().put("type", "refresh").put("media_token", token).toString()) == true
        debug("refresh-queued=$sent")
        return sent
    }
    private fun debug(message: String) { if (BuildConfig.DEBUG) Log.i("KMVMSVideo", "camera=$cameraId ws-$message") }
    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        val count = try { pipe.read(buffer, offset, length) } catch (error: IOException) { debug("pipe-read-failed=${pipe.rejectionReason}"); throw error }
        if (count > 0) bytesTransferred(count)
        return count
    }
    override fun getUri(): Uri? = uri
    fun queuedAgeMillis() = pipe.queuedAgeMillis()
    override fun close() {
        closed = true; pipe.close(); socket?.cancel(); socket = null
        if (opened) { opened = false; transferEnded() }
    }
    class Factory(private val api: NasApi, private val cameraId: Long, private val stream: Stream,
                  private val audio: (Boolean) -> Unit) : DataSource.Factory {
        @Volatile private var current: WebSocketDataSource? = null
        override fun createDataSource(): DataSource = WebSocketDataSource(api, cameraId, stream, audio).also { current = it }
        fun refresh(token: String) = current?.refresh(token) ?: true // Cookie jar covers a socket still opening.
        fun queuedAgeMillis() = current?.queuedAgeMillis() ?: 0
    }
}
