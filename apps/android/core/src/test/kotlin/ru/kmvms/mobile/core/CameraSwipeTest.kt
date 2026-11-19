package ru.kmvms.mobile.core

import org.junit.Assert.*
import org.junit.Test

class CameraSwipeTest {
    @Test fun cyclesInTheActualSelectedOrderInBothDirections() {
        val ids = listOf(9L, 2L, 7L)
        assertEquals(2L, adjacentCamera(ids, 9, CameraStep.NEXT))
        assertEquals(9L, adjacentCamera(ids, 7, CameraStep.NEXT))
        assertEquals(7L, adjacentCamera(ids, 9, CameraStep.PREVIOUS))
        assertEquals(9L, adjacentCamera(ids, 2, CameraStep.PREVIOUS))
    }
    @Test fun neverAddsAHiddenCameraOrReloadsTheOnlyCamera() {
        assertNull(adjacentCamera(emptyList(), 1, CameraStep.NEXT))
        assertNull(adjacentCamera(listOf(1), 1, CameraStep.NEXT))
        assertNull(adjacentCamera(listOf(1, 2), 3, CameraStep.NEXT))
        assertEquals(2L, adjacentCamera(listOf(-1, 1, 1, 2), 1, CameraStep.NEXT))
    }
    @Test fun horizontalSwipeIsDeliberateAndItsDirectionIsPhysicalNotLocaleDependent() {
        assertEquals(CameraStep.NEXT, cameraSwipeStep(-100f, 5f, 48f))
        assertEquals(CameraStep.PREVIOUS, cameraSwipeStep(100f, -5f, 48f))
        assertNull(cameraSwipeStep(20f, 0f, 48f))
        assertNull(cameraSwipeStep(10f, 100f, 48f))
        assertNull(cameraSwipeStep(100f, 100f, 48f))
    }
}
