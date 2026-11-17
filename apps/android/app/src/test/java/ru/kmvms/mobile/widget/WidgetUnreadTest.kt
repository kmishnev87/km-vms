package ru.kmvms.mobile.widget

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.util.UUID

class WidgetUnreadTest {
    @Test fun acceptsZeroAndActualLargeCountsWithoutInventingAnUnreadPushCounter() {
        for (count in listOf(0, 1, 999, 100000)) {
            val value = WidgetUnread(UUID.randomUUID().toString(), 19, count)
            assertEquals(value, WidgetUnread.parse(value.json().toString().toByteArray()))
        }
    }
    @Test fun rejectsInvalidNegativeOversizeAndNonBadgeMessages() {
        val valid = WidgetUnread(UUID.randomUUID().toString(), 2, 7).json()
        for (invalid in listOf(valid.toString().replace("unread_badge", "camera_event"),
            JSONObject(valid.toString()).put("count", -1).toString(),
            JSONObject(valid.toString()).put("count", 2147483648L).toString(),
            JSONObject(valid.toString()).put("epoch", "not-an-epoch").toString(),
            JSONObject(valid.toString()).put("revision", 0).toString(), "{}", "x".repeat(513))) {
            assertNull(WidgetUnread.parse(invalid.toByteArray()))
        }
    }
}
