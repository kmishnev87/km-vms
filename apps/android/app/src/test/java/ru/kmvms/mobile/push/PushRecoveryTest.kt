package ru.kmvms.mobile.push

import org.junit.Assert.assertEquals
import org.junit.Test

class PushRecoveryTest {
    @Test fun discardedRouteCanBeReuploadedButExpiredRouteNeedsTheService() {
        assertEquals(PushRecovery.REUPLOAD, PushRecovery.forServer(4, 4, false, false, "push_endpoint_invalid"))
        assertEquals(PushRecovery.NEEDS_SERVICE, PushRecovery.forServer(4, 4, false, false, "push_registration_expired"))
    }

    @Test fun staleOrDeliberatelyDisabledRegistrationIsNotRestored() {
        assertEquals(PushRecovery.NONE, PushRecovery.forServer(4, 5, false, false, "push_endpoint_invalid"))
        assertEquals(PushRecovery.NONE, PushRecovery.forServer(4, 4, false, false, "disabled"))
        assertEquals(PushRecovery.NONE, PushRecovery.forServer(4, 4, false, true, "push_endpoint_invalid"))
        assertEquals(PushRecovery.NONE, PushRecovery.forServer(4, 4, true, false, "push_endpoint_invalid"))
    }
}
