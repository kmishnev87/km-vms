package ru.kmvms.mobile.chronology

import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.okhttp.OkHttpDataSource
import ru.kmvms.mobile.media.ArchiveMediaAccess
import ru.kmvms.mobile.media.AuthorizedArchiveDataSource

/** Stable archive identity; range opens share only this source's unexpired lease. */
@UnstableApi
class ChronologyDataSource private constructor(api: ChronologyApi, source: TimelineSource,
    delegate: DataSource, access: ArchiveMediaAccess) : DataSource by AuthorizedArchiveDataSource(
        api.api.address.endpoint("api/chronology/file?${source.query()}").toString(), delegate, access) {
    constructor(api: ChronologyApi, source: TimelineSource, delegate: DataSource) : this(api, source, delegate,
        ArchiveMediaAccess(api.api::mediaAuthorizationScope, { api.authorize(source) }))

    class Factory(private val api: ChronologyApi, private val source: TimelineSource) : DataSource.Factory {
        private val access = ArchiveMediaAccess(api.api::mediaAuthorizationScope, { api.authorize(source) })
        override fun createDataSource() = ChronologyDataSource(api, source,
            OkHttpDataSource.Factory(api.api.mediaClient).createDataSource(), access)
    }
}
