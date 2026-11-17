package ru.kmvms.mobile.media

import android.net.Uri
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.HttpDataSource
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import okio.Buffer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.archive.Recording
import ru.kmvms.mobile.chronology.ChronologyApi
import ru.kmvms.mobile.chronology.ChronologyDataSource
import ru.kmvms.mobile.chronology.TimelineSource
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.util.Collections
import java.util.concurrent.atomic.AtomicInteger

/** Real HTTP Range/Media3 adapters with synthetic bytes, no footage or cameras. */
@UnstableApi
@RunWith(AndroidJUnit4::class)
class ArchiveRangeAccessTest {
    private class Fixture(val timeline: Boolean) : java.io.Closeable {
        val server = MockWebServer()
        val grants = AtomicInteger()
        val ranges = Collections.synchronizedList(mutableListOf<RecordedRequest>())
        var response: (Int, RecordedRequest) -> Int = { _, _ -> 206 }
        val bytes = ByteArray(1024) { (it % 251).toByte() }
        val api: NasApi
        init {
            server.dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    if (request.requestUrl!!.encodedPath.endsWith("/media-token")) {
                        assertEquals("POST", request.method)
                        assertTrue(request.getHeader("Authorization")!!.startsWith("Bearer fixture"))
                        if (!timeline) assertEquals("stream", JSONObject(request.body.readUtf8()).getString("action"))
                        return MockResponse().setBody("""{"media_token":"fixture-lease-${grants.incrementAndGet()}","expires_in":300}""")
                    }
                    ranges += request
                    val status = response(ranges.size, request)
                    if (status != 206) return MockResponse().setResponseCode(status)
                    val bounds = Regex("bytes=(\\d+)-(\\d+)").matchEntire(request.getHeader("Range").orEmpty())!!
                    val start = bounds.groupValues[1].toInt(); val end = bounds.groupValues[2].toInt()
                    return MockResponse().setResponseCode(206).setHeader("Content-Type", "application/octet-stream")
                        .setHeader("Accept-Ranges", "bytes").setHeader("Content-Range", "bytes $start-$end/${bytes.size}")
                        .setBody(Buffer().write(bytes.copyOfRange(start, end + 1)))
                }
            }
            server.start()
            api = NasApi(ServerAddress.parse(server.url("/").toString())).apply { useSession("fixture-first") }
        }
        fun factory(id: Long = 9): Pair<DataSource.Factory, String> {
            return if (timeline) {
                val source = TimelineSource(3, id, "root-a", null, 0, 60_000, "video/mp4")
                ChronologyDataSource.Factory(ChronologyApi(api), source) to
                    api.address.endpoint("api/chronology/file?${source.query()}").toString()
            } else {
                val recording = Recording(id, "root-a", null, 3, "Fixture", "fixture.mp4", "", null, null,
                    "video/mp4", "1 KiB", true)
                RecordingDataSource.Factory(api, recording) to
                    api.address.endpoint("api/recordings/stream?${recording.query()}").toString()
            }
        }
        override fun close() = server.close()
    }

    private fun read(source: DataSource, url: String, start: Int = 0, length: Int = 16): ByteArray {
        try {
            source.open(DataSpec.Builder().setUri(Uri.parse(url)).setPosition(start.toLong()).setLength(length.toLong()).build())
            assertEquals(url, source.uri.toString())
            assertFalse(source.uri.toString().contains("media_token"))
            val result = ByteArrayOutputStream(); val buffer = ByteArray(32)
            while (true) {
                val count = source.read(buffer, 0, buffer.size)
                if (count == -1) break
                result.write(buffer, 0, count)
            }
            return result.toByteArray()
        } finally { source.close() }
    }

    @Test fun bothAdaptersReuseOneLeaseAcrossRangeReopensAndSiblingReaders() {
        for (timeline in listOf(false, true)) Fixture(timeline).use { fixture ->
            val (factory, url) = fixture.factory()
            val first = factory.createDataSource(); val sibling = factory.createDataSource()
            assertArrayEquals(fixture.bytes.copyOfRange(0, 16), read(first, url))
            assertArrayEquals(fixture.bytes.copyOfRange(80, 96), read(sibling, url, 80))
            assertArrayEquals(fixture.bytes.copyOfRange(7, 12), read(first, url, 7, 5))
            assertEquals(1, fixture.grants.get()); assertEquals(3, fixture.ranges.size)
            assertEquals(listOf("bytes=0-15", "bytes=80-95", "bytes=7-11"), fixture.ranges.map { it.getHeader("Range") })
        }
    }

    @Test fun differentFilesHaveDifferentLeasesAndChangedIdentityMakesNoRequest() {
        for (timeline in listOf(false, true)) Fixture(timeline).use { fixture ->
            val (first, url) = fixture.factory(); val (other, otherUrl) = fixture.factory(10)
            read(first.createDataSource(), url); read(other.createDataSource(), otherUrl)
            assertEquals(2, fixture.grants.get())
            assertNotEquals(fixture.ranges[0].requestUrl!!.queryParameter("media_token"),
                fixture.ranges[1].requestUrl!!.queryParameter("media_token"))
            assertTrue(runCatching { read(first.createDataSource(), otherUrl) }.exceptionOrNull() is IOException)
            assertEquals(2, fixture.grants.get()); assertEquals(2, fixture.ranges.size)
        }
    }

    @Test fun expiredLeaseIsRefreshedOnceWithoutLosingTheRangePosition() {
        for (timeline in listOf(false, true)) Fixture(timeline).use { fixture ->
            fixture.response = { index, _ -> if (index == 1) 401 else 206 }
            val (factory, url) = fixture.factory()
            assertArrayEquals(fixture.bytes.copyOfRange(80, 96), read(factory.createDataSource(), url, 80))
            assertEquals(2, fixture.grants.get()); assertEquals(2, fixture.ranges.size)
            assertTrue(fixture.ranges.all { it.getHeader("Range") == "bytes=80-95" })
            read(factory.createDataSource(), url)
            assertEquals(2, fixture.grants.get())
        }
    }

    @Test fun aSecondUnauthorizedResponseStopsWithoutAnInfiniteRetry() {
        for (timeline in listOf(false, true)) Fixture(timeline).use { fixture ->
            fixture.response = { _, _ -> 401 }
            val (factory, url) = fixture.factory()
            val failure = runCatching { read(factory.createDataSource(), url) }.exceptionOrNull()
            assertEquals(401, (failure as HttpDataSource.InvalidResponseCodeException).responseCode)
            assertEquals(2, fixture.grants.get()); assertEquals(2, fixture.ranges.size)
        }
    }

    @Test fun forbiddenDoesNotRetryAndOtherHttpFailuresDoNotDiscardTheLease() {
        for (timeline in listOf(false, true)) Fixture(timeline).use { fixture ->
            val (factory, url) = fixture.factory()
            fixture.response = { _, _ -> 503 }
            assertTrue(runCatching { read(factory.createDataSource(), url) }.isFailure)
            fixture.response = { _, _ -> 206 }
            read(factory.createDataSource(), url)
            assertEquals(1, fixture.grants.get())
            fixture.response = { _, _ -> 403 }
            val failure = runCatching { read(factory.createDataSource(), url) }.exceptionOrNull()
            assertEquals(403, (failure as HttpDataSource.InvalidResponseCodeException).responseCode)
            assertEquals(1, fixture.grants.get()); assertEquals(3, fixture.ranges.size)
            fixture.response = { _, _ -> 206 }
            read(factory.createDataSource(), url)
            assertEquals(2, fixture.grants.get())
        }
    }

    @Test fun signingOutAndChangingAccountsNeverReuseThePreviousLease() {
        for (timeline in listOf(false, true)) Fixture(timeline).use { fixture ->
            val (factory, url) = fixture.factory()
            read(factory.createDataSource(), url)
            fixture.api.useSession("fixture-second")
            read(factory.createDataSource(), url)
            assertEquals(2, fixture.grants.get())
            fixture.api.disableSessionRenewal()
            assertTrue(runCatching { read(factory.createDataSource(), url) }.exceptionOrNull() is IOException)
            assertEquals(2, fixture.grants.get()); assertEquals(2, fixture.ranges.size)
        }
    }
}
