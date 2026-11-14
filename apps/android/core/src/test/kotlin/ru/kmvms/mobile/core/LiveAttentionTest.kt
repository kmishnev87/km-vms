package ru.kmvms.mobile.core

import org.junit.Assert.*
import org.junit.Test

class LiveAttentionTest {
    @Test fun exactProfileAndLensOnly() {
        val state = LiveAttention()
        state.update("view", "home", setOf(3))
        assertTrue(state.watching("home", 3))
        assertFalse(state.watching("dacha", 3))
        assertFalse(state.watching("home", 4))
        assertFalse(state.watching("home", null))
    }
    @Test fun invisibleBufferingAndBackgroundClearAttention() {
        val state = LiveAttention()
        state.update("view", "home", setOf(3, 4))
        state.update("view", "home", setOf(4))
        assertFalse(state.watching("home", 3))
        assertTrue(state.watching("home", 4))
        state.clear("view")
        assertFalse(state.watching("home", 4))
    }
    @Test fun ownersDoNotClearOneAnotherAndInputCannotChangeStoredAttention() {
        val state = LiveAttention()
        val ids = mutableSetOf(3L)
        state.update("first", "home", ids)
        ids.clear()
        state.update("second", "home", setOf(4))
        state.clear("second")
        assertTrue(state.watching("home", 3))
        assertFalse(state.watching("home", 4))
    }
}
