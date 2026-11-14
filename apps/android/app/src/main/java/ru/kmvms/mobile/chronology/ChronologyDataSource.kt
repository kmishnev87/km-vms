package ru.kmvms.mobile.chronology

import android.net.Uri
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.TransferListener
import androidx.media3.datasource.okhttp.OkHttpDataSource
import kotlinx.coroutines.runBlocking
import ru.kmvms.mobile.archive.encode
import java.io.IOException

/** Stable archive identity with a fresh, scoped media credential per range open. */
@UnstableApi
class ChronologyDataSource(
    private val api: ChronologyApi,
    private val source: TimelineSource,
    private val delegate: DataSource,
) : DataSource {
    private var stable: Uri? = null
    override fun open(dataSpec: DataSpec): Long {
        val expected = api.api.address.endpoint("api/chronology/file?${source.query()}").toString()
        if (dataSpec.uri.toString() != expected) throw IOException("Archive identity changed")
        stable = dataSpec.uri
        val lease = try { runBlocking { api.authorize(source) } }
            catch (_: Exception) { throw IOException("Archive access could not be confirmed") }
        return delegate.open(dataSpec.buildUpon().setUri(expected + "&media_token=" + encode(lease.token)).build())
    }
    override fun read(buffer: ByteArray, offset: Int, length: Int) = delegate.read(buffer, offset, length)
    override fun getUri(): Uri? = stable
    override fun getResponseHeaders(): Map<String, List<String>> = delegate.responseHeaders
    override fun close() = delegate.close()
    override fun addTransferListener(transferListener: TransferListener) = delegate.addTransferListener(transferListener)

    class Factory(private val api: ChronologyApi, private val source: TimelineSource) : DataSource.Factory {
        override fun createDataSource() = ChronologyDataSource(api, source,
            OkHttpDataSource.Factory(api.api.mediaClient).createDataSource())
    }
}
