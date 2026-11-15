package ru.kmvms.mobile.update

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import kotlin.coroutines.coroutineContext

/** A separate public client. Never uses NAS cookies, Authorization, or its TLS configuration. */
class OfficialAndroidSource internal constructor(
    private val client: OkHttpClient = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
        .connectTimeout(15, TimeUnit.SECONDS).readTimeout(30, TimeUnit.SECONDS).callTimeout(10, TimeUnit.MINUTES).build()
) {

    suspend fun latest(): AndroidRelease? = withContext(Dispatchers.IO) {
        client.newCall(Request.Builder().url(AndroidRelease.METADATA).header("Accept", "application/json")
            .header("User-Agent", "KM-VMS-Android").build()).execute().use { response ->
            if (response.code == 404) return@withContext null
            if (!response.isSuccessful) throw IOException("android_update_check_failed")
            val source = response.body?.source() ?: throw IOException("android_update_empty")
            require(!source.request(64 * 1024L + 1))
            AndroidRelease.from(JSONObject(source.readUtf8()))
        }
    }

    suspend fun download(release: AndroidRelease, destination: File, progress: (Long) -> Unit) = withContext(Dispatchers.IO) {
        val partial = File(destination.parentFile, "update.part")
        require(partial.parentFile == destination.parentFile)
        partial.delete()
        try {
            openArtifact(release.download.toHttpUrl()).use { response ->
                if (!response.isSuccessful) throw IOException("android_update_download_failed")
                val length = response.body?.contentLength() ?: -1
                require(length < 0 || length == release.bytes)
                val hash = MessageDigest.getInstance("SHA-256")
                var count = 0L
                val buffer = ByteArray(64 * 1024)
                val input = response.body?.byteStream() ?: throw IOException("android_update_empty")
                input.use { stream -> partial.outputStream().use { out ->
                    while (true) {
                        coroutineContext.ensureActive()
                        val read = stream.read(buffer)
                        if (read == -1) break
                        count += read
                        require(count <= release.bytes)
                        hash.update(buffer, 0, read)
                        out.write(buffer, 0, read)
                        progress(count)
                    }
                } }
                require(count == release.bytes && hash.digest().hex() == release.sha256)
            }
            require(partial.renameTo(destination))
        } finally { partial.delete() }
    }

    private fun openArtifact(start: HttpUrl): Response {
        var url = start
        repeat(6) {
            require(trustedArtifactUrl(url))
            val response = client.newCall(Request.Builder().url(url).header("User-Agent", "KM-VMS-Android").build()).execute()
            if (response.code !in setOf(301, 302, 303, 307, 308)) return response
            val next = response.header("Location")?.let(url::resolve)
            response.close()
            url = next ?: throw IOException("android_update_redirect_invalid")
        }
        throw IOException("android_update_redirect_limit")
    }
}

internal fun trustedArtifactUrl(url: HttpUrl): Boolean = url.isHttps && url.port == 443 &&
    url.username.isEmpty() && url.password.isEmpty() && url.fragment == null && when (url.host) {
        "github.com" -> url.encodedPath.startsWith("/kmishnev87/km-vms/releases/download/")
        "release-assets.githubusercontent.com", "objects.githubusercontent.com" -> true
        else -> false
    }

internal fun ByteArray.hex() = joinToString("") { "%02x".format(it) }
