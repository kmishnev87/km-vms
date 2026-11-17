package ru.kmvms.mobile.media

import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.okhttp.OkHttpDataSource
import ru.kmvms.mobile.archive.ArchiveApi
import ru.kmvms.mobile.archive.Recording
import ru.kmvms.mobile.data.NasApi

/** Reuse the same source's unexpired scoped lease; never persist a tokenized URL. */
@UnstableApi
class RecordingDataSource private constructor(api: NasApi, recording: Recording,
    delegate: DataSource, access: ArchiveMediaAccess) : DataSource by AuthorizedArchiveDataSource(
        api.address.endpoint("api/recordings/stream?${recording.query()}").toString(), delegate, access) {
    constructor(api: NasApi, recording: Recording, delegate: DataSource) : this(api, recording, delegate,
        ArchiveMediaAccess(api::mediaAuthorizationScope, { ArchiveApi(api).authorize(recording) }))

    class Factory(private val api: NasApi, private val recording: Recording) : DataSource.Factory {
        private val access = ArchiveMediaAccess(api::mediaAuthorizationScope, { ArchiveApi(api).authorize(recording) })
        override fun createDataSource() = RecordingDataSource(api, recording,
            OkHttpDataSource.Factory(api.mediaClient).createDataSource(), access)
    }
}
