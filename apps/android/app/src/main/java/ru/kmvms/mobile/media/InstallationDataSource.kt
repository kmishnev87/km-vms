package ru.kmvms.mobile.media

import android.net.Uri
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.TransferListener
import androidx.media3.datasource.okhttp.OkHttpDataSource
import ru.kmvms.mobile.data.NasApi
import java.io.IOException
import java.net.URI

/** Even a malicious playlist cannot send NAS cookies or requests to another host. */
@UnstableApi
class InstallationDataSource(private val api: NasApi, private val delegate: DataSource) : DataSource {
    override fun open(dataSpec: DataSpec): Long {
        val requested = try { URI(dataSpec.uri.toString()) } catch (_: Exception) { throw IOException("Invalid media address") }
        val base = api.address.base
        fun port(uri: URI) = if (uri.port >= 0) uri.port else if (uri.scheme == "https") 443 else 80
        if (requested.scheme != base.scheme || !requested.host.equals(base.host, true) || port(requested) != port(base) ||
            requested.rawUserInfo != null || requested.rawFragment != null || requested.normalize().path != requested.path ||
            !requested.path.startsWith(base.path.trimEnd('/') + "/api/live/")) throw IOException("Media origin is not this installation")
        return delegate.open(dataSpec)
    }
    override fun read(buffer: ByteArray, offset: Int, length: Int) = delegate.read(buffer, offset, length)
    override fun getUri(): Uri? = delegate.uri
    override fun getResponseHeaders(): Map<String, List<String>> = delegate.responseHeaders
    override fun close() = delegate.close()
    override fun addTransferListener(transferListener: TransferListener) = delegate.addTransferListener(transferListener)

    class Factory(private val api: NasApi) : DataSource.Factory {
        override fun createDataSource(): DataSource = InstallationDataSource(api,
            OkHttpDataSource.Factory(api.mediaClient).setUserAgent("KM-VMS-Android/0.1").createDataSource())
    }
}
