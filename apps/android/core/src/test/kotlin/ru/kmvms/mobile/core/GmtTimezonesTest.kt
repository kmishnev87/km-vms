package ru.kmvms.mobile.core

import org.junit.Assert.*
import org.junit.Test
import java.time.Instant
import java.time.ZoneId

class GmtTimezonesTest {
    @Test fun sameTwentySevenHourOffsetsAsWeb() {
        assertEquals(27,gmtTimezones.size)
        assertEquals("GMT-12:00",gmtTimezones.first().label)
        assertEquals("GMT+14:00",gmtTimezones.last().label)
        assertEquals("UTC",gmtTimezones[12].id)
        val now=Instant.parse("2026-11-14T00:00:00Z")
        gmtTimezones.forEachIndexed{index,zone->assertEquals((index-12)*3600,ZoneId.of(zone.id).rules.getOffset(now).totalSeconds)}
    }
    @Test fun savedRegionalTimezonesAreDisplayedWithoutLosingTheirMeaning() {
        assertEquals("GMT+05:00",gmtTimezoneLabel("Asia/Yekaterinburg"))
        assertEquals("GMT+05:30",gmtTimezoneLabel("Asia/Kolkata"))
    }
}
