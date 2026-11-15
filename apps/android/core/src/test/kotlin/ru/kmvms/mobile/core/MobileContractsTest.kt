package ru.kmvms.mobile.core

import org.junit.Assert.*
import org.junit.Test
import java.time.Instant
import java.time.LocalDate

class MobileContractsTest {
    @Test fun serverAddressPreservesInstallationPrefix() {
        val address = ServerAddress.parse("https://nas.example:8443/vms/")
        assertEquals("https://nas.example:8443/vms/api/auth/me", address.endpoint("api/auth/me").toString())
        assertEquals("https://nas.example:8443", address.origin.toString())
    }
    @Test fun omittedSchemeUsesHttps() { assertEquals("https://nas.example/", ServerAddress.parse("nas.example").toString()) }
    @Test fun bareNasAddressDoesNotRequireTypingHttp() {
        assertEquals("http://192.168.31.41:8088/", ServerAddress.parse(" 192.168.31.41:8088 ").toString())
        assertEquals("http://192.168.31.41/", ServerAddress.parse("192.168.31.41").toString())
        assertEquals("http://nas.example:8088/vms/", ServerAddress.parse("nas.example:8088/vms").toString())
        assertEquals("http://[::1]:8088/", ServerAddress.parse("[::1]:8088").toString())
        assertEquals("https://192.168.31.41:8443/", ServerAddress.parse("https://192.168.31.41:8443").toString())
    }
    @Test fun internationalizedNasHostnamesUseIdnaWithoutLosingThePrefix() {
        val address = ServerAddress.parse("https://кмпрайм.рф:8443/vms/")
        val host = java.net.IDN.toASCII("кмпрайм.рф")
        assertEquals("https://$host:8443/vms/api/auth/me", address.endpoint("api/auth/me").toString())
        assertThrows(IllegalArgumentException::class.java) { ServerAddress.parse("https://user:secret@кмпрайм.рф") }
        assertThrows(IllegalArgumentException::class.java) { ServerAddress.parse("https://кмпрайм.рф/%2e%2e/") }
    }
    @Test fun explicitLanHttpWorks() { assertEquals("http://192.168.31.41:8088/", ServerAddress.parse("http://192.168.31.41:8088").toString()) }
    @Test fun explicitHttpAlsoSupportsOtherInstallationsAndPrefixes() {
        val address = ServerAddress.parse("http://nas.example:8088/vms/")
        assertEquals("http://nas.example:8088/vms/api/auth/me", address.endpoint("api/auth/me").toString())
        assertEquals("http://nas.example:8088", address.origin.toString())
    }
    @Test fun httpsInstallationNeverFallsBackToHttp() {
        val address = ServerAddress.parse("https://nas.example:8443/vms/")
        assertEquals("https", address.endpoint("api/auth/login").scheme)
        assertThrows(IllegalArgumentException::class.java) { address.endpoint("http://nas.example:8443/api/auth/login") }
    }
    @Test fun rejectsCredentialsAndUnsupportedSchemes() {
        listOf("https://user:password@nas.example", "file:///data", "https://nas.example/?token=x", "https://nas.example/#x", "http://nas.example:99999").forEach {
            assertThrows(IllegalArgumentException::class.java) { ServerAddress.parse(it) }
        }
    }
    @Test fun endpointsCannotChangeServer() {
        val address = ServerAddress.parse("https://nas.example/")
        listOf("https://other.example/api", "//other.example/api", "../api").forEach {
            assertThrows(IllegalArgumentException::class.java) { address.endpoint(it) }
        }
    }
    @Test fun queryNamesWithDotsAreNotMistakenForPathTraversal() {
        val address = ServerAddress.parse("https://nas.example/vms/")
        assertEquals("https://nas.example/vms/api/recordings?camera=Garden...", address.endpoint("api/recordings?camera=Garden...").toString())
        listOf("api/%2e%2e/auth/me", "api/../auth/me", "api/auth/me#fragment").forEach {
            assertThrows(IllegalArgumentException::class.java) { address.endpoint(it) }
        }
    }
    @Test fun ptzChangesOnlySelectedCameraTransport() {
        val first = CameraPlayback(1, sound = true)
        val second = CameraPlayback(2)
        val control = first.setPtz(true)
        assertEquals(LiveTransport.WEBSOCKET_FMP4, control.transport)
        assertEquals(LiveTransport.HLS, second.transport)
        assertTrue(control.sound)
        assertEquals(Stream.SUB, control.stream)
        assertEquals(LiveTransport.HLS, control.setPtz(false).transport)
    }
    @Test fun streamChangeKeepsSoundAndPtz() {
        val changed = CameraPlayback(1, sound = true, ptz = true).selectStream(Stream.MAIN)
        assertTrue(changed.sound)
        assertTrue(changed.ptz)
        assertEquals(LiveTransport.WEBSOCKET_FMP4, changed.transport)
    }
    @Test fun partiallyVisibleFourthCameraRemainsActive() {
        val tiles = (0L..4L).map { VisibleTile(it + 1, (it * 200).toInt(), 190) }
        assertEquals(setOf(1L, 2L, 3L, 4L), visibleCameraIds(tiles, 0, 700, true))
        assertEquals(setOf(2L, 3L, 4L), visibleCameraIds(tiles, 200, 700, true))
        assertEquals(emptySet<Long>(), visibleCameraIds(tiles, 0, 700, false))
    }
    @Test fun dailyChecksAreNotRepeatedOnServerSwitchOrClockRollback() {
        val today = LocalDate.of(2026, 11, 14)
        assertTrue(shouldCheckForUpdate(today, null))
        assertFalse(shouldCheckForUpdate(today, today))
        assertFalse(shouldCheckForUpdate(today.minusDays(1), today))
        assertTrue(shouldCheckForUpdate(today.plusDays(1), today))
        assertTrue(shouldCheckForUpdate(today, today, manual = true))
    }
    private val now = Instant.parse("2026-11-14T10:00:00Z")
    @Test fun explicitLiveNeverShowsArchiveEvenForOldEvent() {
        assertEquals(EventDestination.Live, eventDestination(EventAction.LIVE, now.minusSeconds(10800), now, ArchiveAvailability.READY))
    }
    @Test fun oldEventOpensAvailableArchive() {
        assertEquals(EventDestination.Recording, eventDestination(EventAction.OPEN, now.minusSeconds(10800), now, ArchiveAvailability.READY))
    }
    @Test fun unfinishedRecordingFallsBackWithExplanation() {
        assertEquals(EventDestination.LiveWhileRecording, eventDestination(EventAction.RECORDING, now, now, ArchiveAvailability.WRITING))
    }
    @Test fun absentOldArchiveIsNotPretendedToBeLiveEvent() {
        assertEquals(EventDestination.ArchiveUnavailable, eventDestination(EventAction.OPEN, now.minusSeconds(10800), now, ArchiveAvailability.MISSING))
    }
    @Test fun recentNotificationOpensLiveAndUnknownArchiveRequiresCheck() {
        assertEquals(EventDestination.Live, eventDestination(EventAction.OPEN, now.minusSeconds(20), now, ArchiveAvailability.UNKNOWN))
        assertEquals(EventDestination.CheckArchive, eventDestination(EventAction.OPEN, now.minusSeconds(10800), now, ArchiveAvailability.UNKNOWN))
    }
}
