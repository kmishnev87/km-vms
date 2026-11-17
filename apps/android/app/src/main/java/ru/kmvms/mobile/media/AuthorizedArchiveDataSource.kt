package ru.kmvms.mobile.media

import android.net.Uri
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.HttpDataSource
import androidx.media3.datasource.TransferListener
import ru.kmvms.mobile.archive.encode
import java.io.IOException

/** Stable, token-free archive identity with a bounded retry for an expired lease. */
@UnstableApi
internal class AuthorizedArchiveDataSource(
    private val expected: String,
    private val delegate: DataSource,
    private val access: ArchiveMediaAccess,
) : DataSource {
    private var stable: Uri? = null

    override fun open(dataSpec: DataSpec): Long {
        if (dataSpec.uri.toString() != expected) throw IOException("Archive identity changed")
        stable = dataSpec.uri
        var lease = access.lease()
        for (attempt in 0..1) {
            try {
                val actual = expected + "&media_token=" + encode(lease.token)
                return delegate.open(dataSpec.buildUpon().setUri(actual).build())
            } catch (error: HttpDataSource.InvalidResponseCodeException) {
                if (error.responseCode in setOf(401, 403)) access.invalidate(lease)
                // A forbidden resource is not an expired token. No loop or
                // downgrade of permission checks; leave network/Range errors alone.
                if (error.responseCode != 401 || attempt != 0) throw error
                try { delegate.close() } catch (_: IOException) { }
                lease = access.lease()
            }
        }
        throw IOException("Archive access could not be confirmed")
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int) = delegate.read(buffer, offset, length)
    override fun getUri(): Uri? = stable
    override fun getResponseHeaders(): Map<String, List<String>> = delegate.responseHeaders
    override fun close() = delegate.close()
    override fun addTransferListener(transferListener: TransferListener) = delegate.addTransferListener(transferListener)
}
