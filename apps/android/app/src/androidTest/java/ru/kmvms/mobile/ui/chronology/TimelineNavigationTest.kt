package ru.kmvms.mobile.ui.chronology

import androidx.compose.runtime.*
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.chronology.ChronologyState
import ru.kmvms.mobile.ui.KMVMSTheme
import java.time.Instant
import java.time.ZoneId

@RunWith(AndroidJUnit4::class)
class TimelineNavigationTest {
    @get:Rule val compose=createComposeRule()
    @Test fun minusShortensTheIntervalPlusLengthensItAndTheLabelIsCentred() {
        val position=Instant.parse("2026-11-15T10:00:00Z").toEpochMilli()
        var window:Pair<Long,Long>?=null
        compose.setContent { KMVMSTheme {
            TimelineRuler(ChronologyState(position),emptyList(),{}, {from,to->window=from to to})
        } }
        fun duration()=compose.runOnIdle { window!!.second-window!!.first }
        val before=duration()
        compose.onNodeWithTag("timeline-interval-decrease").performClick()
        assertTrue(duration()<before)
        compose.onNodeWithTag("timeline-interval-increase").performClick()
        assertEquals(before,duration())
        val minus=compose.onNodeWithTag("timeline-interval-decrease").getUnclippedBoundsInRoot()
        val plus=compose.onNodeWithTag("timeline-interval-increase").getUnclippedBoundsInRoot()
        val label=compose.onNodeWithTag("timeline-interval").getUnclippedBoundsInRoot()
        assertEquals((minus.left.value+minus.right.value+plus.left.value+plus.right.value)/4,
            (label.left.value+label.right.value)/2,.5f)
        repeat(3) { compose.onNodeWithTag("timeline-interval-increase").performClick() }
        compose.onNodeWithTag("timeline-interval-increase").assertIsNotEnabled()
        compose.onNodeWithTag("timeline-interval-decrease").assertIsEnabled()
        repeat(6) { compose.onNodeWithTag("timeline-interval-decrease").performClick() }
        compose.onNodeWithTag("timeline-interval-decrease").assertIsNotEnabled()
        compose.onNodeWithTag("timeline-interval-increase").assertIsEnabled()
    }
    @Test fun rulerSwipesThroughMidnightInBothDirectionsAndRequestsTheVisibleWindow() {
        val zone=ZoneId.of("Asia/Yekaterinburg")
        val first=Instant.parse("2026-11-14T19:05:00Z").toEpochMilli()
        var state by mutableStateOf(ChronologyState(first,zone=zone,day=Instant.ofEpochMilli(first).atZone(zone).toLocalDate()))
        var window:Pair<Long,Long>?=null
        compose.setContent { KMVMSTheme {
            TimelineRuler(state,emptyList(),{time->state=state.copy(position=time,day=Instant.ofEpochMilli(time).atZone(zone).toLocalDate())},
                {from,to->window=from to to})
        } }
        compose.onNodeWithTag("timeline-ranges").performTouchInput {
            swipe(Offset(width*.2f,height*.6f),Offset(width*.8f,height*.6f),500)
        }
        compose.runOnIdle {
            assertTrue(state.position<first-60_000)
            assertEquals("2026-11-14",state.day.toString())
            assertTrue(window!!.first<state.position&&window!!.second>state.position)
        }
        repeat(2) {compose.onNodeWithTag("timeline-ranges").performTouchInput {
            swipe(Offset(width*.8f,height*.6f),Offset(width*.2f,height*.6f),500)
        }}
        compose.runOnIdle {assertEquals("2026-11-15",state.day.toString());assertTrue(state.position>first)}
    }
}
