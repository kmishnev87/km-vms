package ru.kmvms.mobile.chronology

import org.junit.Assert.*
import org.junit.Test
import java.time.Instant
import java.time.ZoneId

class TimelineRangeWindowTest {
    @Test fun aViewAcrossMidnightRequestsBothNasCalendarDays() {
        val zone=ZoneId.of("Asia/Yekaterinburg")
        val bounds=timelineRangeWindow(Instant.parse("2026-11-14T18:30:00Z").toEpochMilli(),
            Instant.parse("2026-11-14T20:30:00Z").toEpochMilli(),zone)
        assertEquals(Instant.parse("2026-11-13T19:00:00Z"),bounds.first)
        assertEquals(Instant.parse("2026-11-15T19:00:00Z"),bounds.second)
    }
    @Test fun panningInsideTheSameCalendarWindowDoesNotChangeTheRequest() {
        val zone=ZoneId.of("Asia/Yekaterinburg")
        val start=Instant.parse("2026-11-14T10:00:00Z").toEpochMilli()
        assertEquals(timelineRangeWindow(start,start+3_600_000,zone),timelineRangeWindow(start+600_000,start+4_200_000,zone))
    }
    @Test fun daylightSavingUsesRealDayBoundariesNotFixedTwentyFourHourOffsets() {
        val zone=ZoneId.of("Europe/Berlin")
        val bounds=timelineRangeWindow(Instant.parse("2026-03-29T10:00:00Z").toEpochMilli(),
            Instant.parse("2026-03-29T11:00:00Z").toEpochMilli(),zone)
        assertEquals(23*3_600L,java.time.Duration.between(bounds.first,bounds.second).seconds)
        val exact=timelineRangeWindow(bounds.first.toEpochMilli(),bounds.second.toEpochMilli(),zone)
        assertEquals(bounds,exact)
    }
}
