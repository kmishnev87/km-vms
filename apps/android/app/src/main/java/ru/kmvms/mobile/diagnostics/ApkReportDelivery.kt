package ru.kmvms.mobile.diagnostics

import kotlinx.coroutines.suspendCancellableCoroutine
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okio.BufferedSink
import org.json.JSONObject
import java.io.IOException
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/** Immutable memory-only attempt: retry after a lost response reuses id/body. */
class ApkReportAttempt(val id: String, val version: String, val comment: String, internal val bytes: ByteArray) {
    override fun toString() = "ApkReportAttempt([private])"
    companion object {
        fun create(version: String, comment: String, diagnostics: JSONObject): ApkReportAttempt {
            require(comment.length <= 4000)
            val id = UUID.randomUUID().toString()
            val body = JSONObject().put("schema", 1).put("source", "android_apk").put("report_id", id)
                .put("version", version).put("comment", comment).put("diagnostics", diagnostics).toString().toByteArray(Charsets.UTF_8)
            require(body.size <= 1024 * 1024)
            return ApkReportAttempt(id, version, comment, body)
        }
    }
}
class ApkReportException(val status: Int = 0) : Exception("apk_report_delivery_failed")

class ApkReportDelivery(private val client: OkHttpClient = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
    .retryOnConnectionFailure(false).connectTimeout(10, TimeUnit.SECONDS).readTimeout(95, TimeUnit.SECONDS)
    .writeTimeout(45, TimeUnit.SECONDS).callTimeout(100, TimeUnit.SECONDS).build()) {
    companion object { const val DESTINATION = "https://xn--80asbicph.xn--p1ai/km-vms-support/v1/reports" }
    suspend fun send(attempt: ApkReportAttempt, progress: (Long, Long) -> Unit = { _, _ -> }): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(attempt.bytes).joinToString("") { "%02x".format(it) }
        val body = object : RequestBody() {
            override fun contentType() = "application/json; charset=utf-8".toMediaType()
            override fun contentLength() = attempt.bytes.size.toLong()
            override fun writeTo(sink: BufferedSink) {
                var position = 0
                while (position < attempt.bytes.size) {
                    val size = minOf(8192, attempt.bytes.size - position)
                    sink.write(attempt.bytes, position, size); position += size
                    progress(position.toLong(), contentLength())
                }
            }
        }
        val request = Request.Builder().url(DESTINATION).header("Accept", "application/json")
            .header("X-KM-Report-Id", attempt.id).header("X-KM-Report-Source", "android_apk")
            .header("X-KM-Report-Version", attempt.version).header("X-KM-Report-Fingerprint", digest)
            .header("User-Agent", "KM-VMS-Android/${attempt.version} SupportDelivery").post(body).build()
        return suspendCancellableCoroutine { continuation ->
            val call = client.newCall(request)
            continuation.invokeOnCancellation { call.cancel() }
            call.enqueue(object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    if (continuation.isActive) continuation.resumeWithException(ApkReportException())
                }
                override fun onResponse(call: Call, response: Response) {
                    val result = runCatching { response.use {
                        if (it.code !in setOf(200, 201)) throw ApkReportException(it.code)
                        val source = it.body?.source() ?: throw ApkReportException()
                        if (source.request(8193)) throw ApkReportException()
                        val receipt = JSONObject(source.readUtf8())
                        if (!receipt.optBoolean("accepted") || receipt.optString("report_id") != attempt.id) throw ApkReportException()
                        attempt.id
                    } }
                    if (continuation.isActive) result.fold({ continuation.resume(it) }, { continuation.resumeWithException(if (it is ApkReportException) it else ApkReportException()) })
                }
            })
        }
    }
}
