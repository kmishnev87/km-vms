package ru.kmvms.mobile.chronology

import java.time.Instant
import java.time.ZoneId

/** Fetch only the calendar days visible in the ruler, including both sides of midnight. */
internal fun timelineRangeWindow(from:Long,to:Long,zone:ZoneId):Pair<Instant,Instant> {
    require(to>from && to-from<=7*86_400_000L)
    val first=Instant.ofEpochMilli(from).atZone(zone).toLocalDate()
    val last=Instant.ofEpochMilli(to-1).atZone(zone).toLocalDate()
    return first.atStartOfDay(zone).toInstant() to last.plusDays(1).atStartOfDay(zone).toInstant()
}
