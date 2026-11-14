package ru.kmvms.mobile.media

import android.net.Uri
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.TransferListener
import androidx.media3.datasource.okhttp.OkHttpDataSource
import kotlinx.coroutines.runBlocking
import ru.kmvms.mobile.archive.ArchiveApi
import ru.kmvms.mobile.archive.Recording
import ru.kmvms.mobile.archive.encode
import ru.kmvms.mobile.data.NasApi
import java.io.IOException

/** A fresh scoped credential per range open; no expiring URL is saved in app state. */
@UnstableApi
class RecordingDataSource(private val api: NasApi, private val recording: Recording,
                          private val delegate: DataSource) : DataSource {
    private var stable: Uri? = null
    override fun open(dataSpec: DataSpec): Long {
        val expected = api.address.endpoint("api/recordings/stream?${recording.query()}").toString()
        if (dataSpec.uri.toString() != expected) throw IOException("Recording identity changed")
        stable = dataSpec.uri
        val authority = try { runBlocking { ArchiveApi(api).authorize(recording) } }
            catch (_: Exception) { throw IOException("Recording access could not be confirmed") }
        val actual = expected + "&media_token=" + encode(authority.token)
        return delegate.open(dataSpec.buildUpon().setUri(actual).build())
    }
    override fun read(buffer: ByteArray, offset: Int, length: Int) = delegate.read(buffer, offset, length)
    override fun getUri(): Uri? = stable
    override fun getResponseHeaders(): Map<String, List<String>> = delegate.responseHeaders
    override fun close() = delegate.close()
    override fun addTransferListener(transferListener: TransferListener) = delegate.addTransferListener(transferListener)
    class Factory(private val api: NasApi, private val recording: Recording) : DataSource.Factory {
        override fun createDataSource() = RecordingDataSource(api, recording, OkHttpDataSource.Factory(api.mediaClient).createDataSource())
    }
}
