package ru.kmvms.mobile.core

import org.junit.Assert.*
import org.junit.Test

class OnlineModeTest {
    @Test fun existingAndUnknownServersKeepStableDefault() {
        listOf(null,"", "future", "FAST", "stable").forEach { assertEquals(OnlineMode.STABLE, OnlineMode.from(it)) }
        assertEquals(OnlineMode.FAST, OnlineMode.from("fast"))
    }
    @Test fun stableRetainsThePtzOnlyFastRoute() {
        assertEquals(LiveTransport.HLS, onlineTransport(OnlineMode.STABLE, false))
        assertEquals(LiveTransport.WEBSOCKET_FMP4, onlineTransport(OnlineMode.STABLE, true))
    }
    @Test fun fastUsesTheSocketWithOrWithoutPtzAndFallbackNeverChangesTheNasPreference() {
        for (ptz in listOf(false,true)) {
            assertEquals(LiveTransport.WEBSOCKET_FMP4, onlineTransport(OnlineMode.FAST, ptz))
            assertEquals(LiveTransport.HLS, onlineTransport(OnlineMode.FAST, ptz, incompatible=true))
        }
        assertEquals(LiveTransport.HLS, onlineTransport(OnlineMode.STABLE,true,incompatible=true))
    }
}
