package ru.kmvms.mobile.media

import org.junit.Assert.*
import org.junit.Test
import java.io.IOException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

class ArchiveMediaAccessTest {
    @Test fun repeatedRangesReuseOnlyTheSameUnexpiredLeaseWithAnExpiryReserve() {
        var now = 0L
        var calls = 0
        val scope = Any()
        val access = ArchiveMediaAccess({ scope }, { MediaLease("fixture-${++calls}", 300) }, { now })
        val first = access.lease()
        now = 254_999_000_000
        assertSame(first, access.lease())
        assertEquals(1, calls)
        now = 255_000_000_000
        assertNotSame(first, access.lease())
        assertEquals(2, calls)
    }

    @Test fun timeSpentAuthorizingIsIncludedInTheReuseDeadline() {
        var now = 0L
        var calls = 0
        val scope = Any()
        val access = ArchiveMediaAccess({ scope }, {
            calls++; now += 10_000_000_000; MediaLease("fixture", 300)
        }, { now })
        val first = access.lease()
        now = 254_999_000_000
        assertSame(first, access.lease())
        now = 255_000_000_000
        assertNotSame(first, access.lease())
        assertEquals(2, calls)
    }

    @Test fun sourceFactoriesNeverShareTheirAuthorizationEvenInTheSameSession() {
        val scope = Any()
        var calls = 0
        fun source() = ArchiveMediaAccess({ scope }, { MediaLease("fixture-${++calls}", 300) })
        val recording = source(); val chronology = source(); val anotherFile = source()
        assertNotSame(recording.lease(), chronology.lease())
        assertNotSame(recording.lease(), anotherFile.lease())
        assertEquals(3, calls)
    }

    @Test fun aChangedAccountCannotReuseThePreviousAccountsLease() {
        var scope: Any? = Any()
        var calls = 0
        val access = ArchiveMediaAccess({ scope }, { MediaLease("fixture-${++calls}", 300) })
        val old = access.lease()
        scope = Any()
        assertNotSame(old, access.lease())
        assertEquals(2, calls)
        scope = null
        assertTrue(runCatching { access.lease() }.exceptionOrNull() is IOException)
        assertEquals(2, calls)
    }

    @Test fun sessionRenewalDuringAuthorizationMustBeConfirmedInTheNewSession() {
        var scope: Any? = Any()
        var calls = 0
        val access = ArchiveMediaAccess({ scope }, {
            if (++calls == 1) scope = Any()
            MediaLease("fixture-$calls", 300)
        })
        val lease = access.lease()
        assertEquals(2, calls)
        assertSame(lease, access.lease())
    }

    @Test fun signingOutDuringAuthorizationRejectsTheLateCredential() {
        var scope: Any? = Any()
        var calls = 0
        val access = ArchiveMediaAccess({ scope }, {
            calls++; scope = null; MediaLease("fixture", 300)
        })
        assertTrue(runCatching { access.lease() }.exceptionOrNull() is IOException)
        assertEquals(1, calls)
    }

    @Test fun repeatedSessionChangesCannotCreateAnAuthorizationLoop() {
        var scope: Any? = Any()
        var calls = 0
        val access = ArchiveMediaAccess({ scope }, {
            calls++; scope = Any(); MediaLease("fixture", 300)
        })
        assertTrue(runCatching { access.lease() }.exceptionOrNull() is IOException)
        assertEquals(2, calls)
    }

    @Test fun aLateFailureDoesNotInvalidateAnotherReadersFreshCredential() {
        val scope = Any()
        var calls = 0
        val access = ArchiveMediaAccess({ scope }, { MediaLease("fixture-${++calls}", 300) })
        val first = access.lease()
        access.invalidate(first)
        val second = access.lease()
        access.invalidate(first)
        assertSame(second, access.lease())
        assertEquals(2, calls)
    }

    @Test fun concurrentReadersShareOneAuthorizationRequest() {
        val scope = Any()
        val calls = AtomicInteger()
        val started = CountDownLatch(1); val release = CountDownLatch(1)
        val access = ArchiveMediaAccess({ scope }, {
            calls.incrementAndGet(); started.countDown()
            check(release.await(3, TimeUnit.SECONDS))
            MediaLease("fixture", 300)
        })
        val executor = Executors.newFixedThreadPool(5)
        try {
            val readers = (1..5).map { executor.submit<MediaLease> { access.lease() } }
            assertTrue(started.await(3, TimeUnit.SECONDS))
            release.countDown()
            val leases = readers.map { it.get(3, TimeUnit.SECONDS) }
            assertTrue(leases.all { it === leases.first() })
            assertEquals(1, calls.get())
        } finally { release.countDown(); executor.shutdownNow() }
    }

    @Test fun authorizationFailuresContainNoTokenOrServerResponseAndAreNotCached() {
        var calls = 0
        val scope = Any()
        val access = ArchiveMediaAccess({ scope }, {
            calls++; throw IllegalStateException("sensitive-fixture-response")
        })
        repeat(2) {
            val failure = runCatching { access.lease() }.exceptionOrNull()
            assertTrue(failure is IOException)
            assertFalse(failure.toString().contains("sensitive-fixture"))
            assertNull(failure?.cause)
        }
        assertEquals(2, calls)
    }
}
