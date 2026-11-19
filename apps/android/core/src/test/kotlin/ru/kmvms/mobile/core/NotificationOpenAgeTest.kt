package ru.kmvms.mobile.core

import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Test

class NotificationOpenAgeTest {
    @Test fun automaticTapChangesAtExactlyOneHourButExplicitActionsDoNot() {
        val now = Instant.parse("2026-11-19T13:00:00Z")
        assertEquals(EventAction.LIVE, notificationEventAction(EventAction.OPEN, now.minusSeconds(3599), now))
        assertEquals(EventAction.RECORDING, notificationEventAction(EventAction.OPEN, now.minusSeconds(3600), now))
        assertEquals(EventAction.LIVE, notificationEventAction(EventAction.LIVE, now.minusSeconds(7200), now))
        assertEquals(EventAction.RECORDING, notificationEventAction(EventAction.RECORDING, now, now))
    }
}
