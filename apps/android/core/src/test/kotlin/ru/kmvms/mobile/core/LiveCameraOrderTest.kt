package ru.kmvms.mobile.core

import org.junit.Assert.*
import org.junit.Test

class LiveCameraOrderTest {
    @Test fun defaultShowsAllButAnExplicitEmptySetStaysEmpty() {
        assertEquals(listOf(3L, 1L, 2L), LiveCameraOrder.resolve(null, listOf(3, 1, 2)))
        assertEquals(emptyList<Long>(), LiveCameraOrder.resolve(emptyList(), listOf(3, 1, 2)))
    }
    @Test fun selectionOrderIsStableAndMissingCamerasAreNotReplaced() {
        assertEquals(listOf(2L, 1L), LiveCameraOrder.resolve(listOf(2, 4, 1, 2), listOf(1, 2, 3)))
        assertEquals(emptyList<Long>(), LiveCameraOrder.resolve(listOf(4), listOf(1, 2, 3)))
    }
    @Test fun deselectRenumbersAndSelectingAgainAppends() {
        val removed = LiveCameraOrder.toggle(listOf(2, 3, 1), 3)
        assertEquals(listOf(2L, 1L), removed)
        assertEquals(listOf(2L, 1L, 3L), LiveCameraOrder.toggle(removed, 3))
    }
    @Test fun moveKeepsEveryCameraExactlyOnce() {
        assertEquals(listOf(3L, 1L, 2L), LiveCameraOrder.move(listOf(1, 2, 3), 3, 0))
        assertEquals(listOf(2L, 3L, 1L), LiveCameraOrder.move(listOf(1, 2, 3), 1, 2))
        assertEquals(listOf(1L, 2L), LiveCameraOrder.move(listOf(1, 2), 9, 0))
        assertEquals(listOf(1L, 2L), LiveCameraOrder.move(listOf(1, 2), 1, -1))
    }
    @Test fun previewLaunchNeverIncludesTheNormalSet() {
        val normal = listOf(3L, 1L)
        assertEquals(listOf(2L), LiveCameraOrder.viewing(normal, listOf(1, 2, 3), 2))
        assertEquals(normal, LiveCameraOrder.viewing(normal, listOf(1, 2, 3), null))
        assertEquals(emptyList<Long>(), LiveCameraOrder.viewing(normal, listOf(1, 3), 2))
    }
}
