package ru.kmvms.mobile.core

import org.junit.Assert.*
import org.junit.Test

class CallAudioPolicyTest {
    @Test fun callTemporarilyEnablesListeningAndRestoresBothPreviousChoices() {
        for (previous in listOf(false, true)) {
            val initial = CameraPlayback(7, sound = previous, ptz = true)
            val call = initial.setTalk(true)
            assertTrue(call.sound)
            assertTrue(call.audible)
            assertEquals(previous, call.soundBeforeCall)
            assertEquals(initial, call.setTalk(false))
            assertEquals(initial, call.copy(sound = false).setTalk(false))
            assertEquals(initial, call.copy(sound = true).setTalk(false))
        }
    }

    @Test fun microphoneUpdatesDoNotUndoTheCallListeningChoice() {
        var call = CameraPlayback(7, sound = false).setTalk(true).copy(sound = false)
        repeat(3) {
            call = call.setTalk(true, duck = true)
            assertFalse(call.sound)
            assertFalse(call.audible)
            call = call.setTalk(true, duck = false)
            assertFalse(call.audible)
            assertEquals(false, call.soundBeforeCall)
        }
        call = call.copy(sound = true)
        assertFalse(call.setTalk(true, duck = true).audible)
        assertTrue(call.setTalk(true, duck = false).audible)
    }

    @Test fun streamAndPtzChangesPreserveCallMuteAndTheOriginalPreference() {
        val initial = CameraPlayback(7, sound = true)
        val call = initial.setTalk(true).copy(sound = false).selectStream(Stream.MAIN).setPtz(true)
        assertFalse(call.audible)
        assertEquals(true, call.soundBeforeCall)
        val ended = call.setTalk(false)
        assertTrue(ended.sound)
        assertTrue(ended.ptz)
        assertEquals(Stream.MAIN, ended.stream)
        assertNull(ended.soundBeforeCall)
    }

    @Test fun idleEndIsIdempotentAndCannotUnmuteAfterAnAlreadyEndedCall() {
        for (previous in listOf(false, true)) {
            val initial = CameraPlayback(7, sound = previous)
            assertEquals(initial, initial.setTalk(false))
            val ended = initial.setTalk(true).setTalk(false)
            val manuallyMuted = ended.copy(sound = false)
            assertEquals(manuallyMuted, manuallyMuted.setTalk(false))
        }
    }
}
