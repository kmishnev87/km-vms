package ru.kmvms.mobile.core

import org.junit.Assert.*
import org.junit.Test
import java.io.IOException
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

class LiveBytePipeTest {
    @Test fun partialReadsKeepBytesInOrder() {
        val pipe = LiveBytePipe()
        pipe.offer(byteArrayOf(1, 2, 3)); pipe.offer(byteArrayOf(4))
        val bytes = ByteArray(4)
        assertEquals(2, pipe.read(bytes, 0, 2)); assertEquals(1, pipe.read(bytes, 2, 2)); assertEquals(1, pipe.read(bytes, 3, 1))
        assertArrayEquals(byteArrayOf(1, 2, 3, 4), bytes)
    }
    @Test fun overflowFailsInsteadOfDroppingInterdependentMp4Fragments() {
        val pipe = LiveBytePipe(capacity = 3)
        assertTrue(pipe.offer(byteArrayOf(1, 2))); assertFalse(pipe.offer(byteArrayOf(3, 4)))
        try { pipe.read(ByteArray(3), 0, 3); fail("must reconnect") } catch (_: IOException) { }
    }
    @Test fun ageIsBoundedUsingMonotonicTime() {
        var clock = 0L
        val pipe = LiveBytePipe(maximumAgeNanos = 100, clock = { clock })
        pipe.offer(byteArrayOf(1)); clock = 101
        try { pipe.read(ByteArray(1), 0, 1); fail("must not show stale PTZ video") } catch (_: IOException) { }
    }
    @Test fun closingWakesBlockedLoaderWithoutWaitingForNetworkTimeout() {
        val executor = Executors.newSingleThreadExecutor()
        try {
            val pipe = LiveBytePipe()
            val waiting = executor.submit<Int> { pipe.read(ByteArray(8), 0, 8) }
            pipe.close()
            assertEquals(-1, waiting.get(1, TimeUnit.SECONDS).toInt())
        } finally { executor.shutdownNow() }
    }
    @Test fun failureReleasesBufferedMediaAndWakesReader() {
        val pipe = LiveBytePipe()
        pipe.offer(byteArrayOf(1)); pipe.fail(IOException("fixture"))
        try { pipe.read(ByteArray(1), 0, 1); fail("must not replay old media") } catch (_: IOException) { }
    }
}
