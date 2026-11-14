package ru.kmvms.mobile.core

import org.junit.Assert.*
import org.junit.Test

class FrameGeometryTest {
    @Test fun blackBarsCannotSendPointCommand() {
        val frame = fittedFrame(1000.0, 1000.0, 2.0)
        assertNull(frame.point(500.0, 100.0)); assertEquals(.5, frame.point(500.0, 500.0)!!.second, .0001)
    }
    @Test fun digitalPanZoomIsAccountedForBeforeSendingCameraCoordinates() {
        val frame = fittedFrame(1000.0, 500.0, 2.0, 2.0, 100.0, 0.0)
        assertEquals(.45, frame.point(500.0, 250.0)!!.first, .0001)
    }
    @Test fun reversedDragIsNormalizedAndClippedToTheRealFrame() {
        val frame = fittedFrame(1000.0, 1000.0, 2.0)
        val box = frame.selection(800.0, 650.0, -100.0, 100.0)!!
        assertEquals(0.0, box.left, .0001); assertEquals(0.0, box.top, .0001)
        assertEquals(.8, box.width, .0001); assertEquals(.8, box.height, .0001)
        assertNull(frame.selection(100.0, 50.0, 800.0, 600.0))
    }
}
