package ru.kmvms.mobile.core

import org.junit.Assert.*
import org.junit.Test

class TimelineViewportTest {
    private val day = 86_400_000L
    @Test fun zoomKeepsSelectedTimeAndPanContinuesBeyondBothDayBoundaries() {
        val view=TimelineViewport.around(0,day,43_200_000,600_000)
        assertEquals(.5f,view.fraction(43_200_000),.001f)
        assertEquals(43_200_000,view.time(.5f))
        assertEquals(view.start-day,view.shifted(-day).start)
        assertEquals(view.end+day,view.shifted(day).end)
        assertTrue(view.shifted(-day).time(.5f)<0)
        assertTrue(view.shifted(day).time(.5f)>day)
    }
    @Test fun aShortPaintedBarCannotSeekIntoAnAdjacentGap() {
        val view=TimelineViewport.around(0,day,43_200_000,day)
        val from=43_200_000L
        val x=view.fraction(from)*300+1.5f
        assertEquals(from+1499,view.hit(x/300,300f,listOf(from to from+1500)))
    }
    @Test fun zoomHasReadableTicksAndMinuteAccuracyInsteadOfAWholeDayPerScreen() {
        val view=TimelineViewport.around(0,day,43_200_000,600_000)
        assertTrue(view.time(.51f)-view.time(.5f)<10_000)
        assertTrue(view.ticks(5).size in 4..6)
        assertTrue(view.ticks(5).zipWithNext().all { it.second-it.first==120_000L })
    }
    @Test fun aShortDayDoesNotShortenOrClampAContinuousTwentyFourHourView() {
        val end=23*3_600_000L
        val view=TimelineViewport.around(0,end,end,day)
        assertEquals(day,view.span)
        assertEquals(end,view.time(.5f))
        assertEquals(view.end-1,view.time(1f))
        assertEquals(view.start,view.time(-1f))
    }
}
