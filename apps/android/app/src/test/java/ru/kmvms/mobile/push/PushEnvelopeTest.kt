package ru.kmvms.mobile.push

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class PushEnvelopeTest {
    private val profile = "51605a9f-bd9f-4cad-bdb0-133d48d68615"
    private val event = "aa14fbdf-a2b5-4fd8-9ab7-f1f3d718b4e1"
    private fun body() = JSONObject().put("v", 1).put("type", "camera_event").put("event_id", event)
        .put("camera_id", 1).put("camera_name", "Gate\ncontrol").put("kind", "person").put("occurred_at", "2026-11-14T10:30:00Z")
    @Test fun decryptableEnvelopeIsOnlyTextAndTypedIdentity() {
        val parsed = PushEnvelope.parse(body().toString().toByteArray())
        assertEquals(event, parsed.eventId)
        assertEquals("Gatecontrol", parsed.cameraName)
        assertEquals(1L, parsed.cameraId)
        assertFalse(parsed.test)
    }
    @Test fun rejectsOversizedMalformedAndCommandPayloads() {
        for (bytes in listOf(ByteArray(3001), body().put("v", 2).toString().toByteArray(),
            body().put("event_id", "http://not-an-event").toString().toByteArray(),
            body().put("type", "ptz_command").toString().toByteArray())) {
            assertTrue(runCatching { PushEnvelope.parse(bytes) }.isFailure)
        }
    }
    @Test fun aiConditionIsOptionalAndRecognizesZoneExitWithoutChangingOldMessages() {
        assertNull(PushEnvelope.parse(body().toString().toByteArray()).aiCondition)
        assertEquals(ru.kmvms.mobile.events.AiEventCondition.EXIT,
            PushEnvelope.parse(body().put("ai_condition", "exit").toString().toByteArray()).aiCondition)
        assertNull(PushEnvelope.parse(body().put("ai_condition", "unknown").toString().toByteArray()).aiCondition)
    }
    @Test fun parsesRealNasUtcOffsetAndZIdenticallyIncludingFractionAndOtherOffsets() {
        for (value in listOf("2026-11-14T10:30:00Z", "2026-11-14T10:30:00+00:00", "2026-11-14T15:30:00+05:00")) {
            assertEquals(java.time.Instant.parse("2026-11-14T10:30:00Z"), PushEnvelope.parse(body().put("occurred_at", value).toString().toByteArray()).occurred)
        }
        assertEquals(123456000, PushEnvelope.parse(body().put("occurred_at", "2026-11-14T10:30:00.123456+00:00").toString().toByteArray()).occurred.nano)
        for (value in listOf("2026-11-14T10:30:00", "not-a-time", "2026-99-14T10:30:00Z"))
            assertTrue(runCatching { PushEnvelope.parse(body().put("occurred_at", value).toString().toByteArray()) }.isFailure)
    }
    @Test fun explicitNotificationRoutesCannotBecomeExternalAddresses() {
        val parsed = NotificationNavigation.parse("kmvms://notification/$profile/$event/archive")
        assertEquals(profile, parsed?.profileId)
        assertEquals(event, parsed?.eventId)
        assertEquals(ru.kmvms.mobile.core.EventAction.OPEN, NotificationNavigation.parse("kmvms://notification/$profile/$event/open")?.action)
        for (value in listOf("https://example.com/$profile/$event/live", "kmvms://notification/$profile/$event/ptz",
            "kmvms://notification/$profile/$event/live?url=https://example.com", "kmvms://notification/unknown/$event/live")) {
            assertNull(NotificationNavigation.parse(value))
        }
    }
}
