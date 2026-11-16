package ru.kmvms.mobile.widget

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.*
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.data.*
import ru.kmvms.mobile.events.*

@OptIn(ExperimentalCoroutinesApi::class)
class AlertWidgetControlsTest {
    private val baseline = EventPreferences(cameraIds = setOf(1, 2), kinds = setOf(CameraEventKind.PERSON),
        hoursEnabled = true, perCameraHours = true, cameraSchedules = mapOf(1L to EventHours(1140, 420)))
    private inner class Fake : AlertChoices {
        var snapshot = AlertWidgetSnapshot(baseline, listOf(EventCamera(1, "Home", true)), "GMT+05:00")
        var loads = 0; var changes = 0
        var pending: CompletableDeferred<EventPreferences>? = null
        var error: NasException? = null
        override suspend fun load(): AlertWidgetSnapshot { loads++; return snapshot }
        override suspend fun master(enabled: Boolean): EventPreferences = change(snapshot.preferences.copy(notifications = enabled))
        override suspend fun camera(id: Long, enabled: Boolean): EventPreferences = change(snapshot.preferences.copy(
            cameraIds = if (enabled) snapshot.preferences.cameraIds.orEmpty() + id else snapshot.preferences.cameraIds.orEmpty() - id))
        private suspend fun change(next: EventPreferences): EventPreferences {
            changes++; error?.let { throw it }
            val confirmed = pending?.await() ?: next
            snapshot = snapshot.copy(preferences = confirmed)
            return confirmed
        }
    }
    @Test fun choiceChangesOnlyAfterNasConfirmsAndDoubleTapCannotRace() = runTest {
        val api = Fake(); val controls = AlertWidgetControls(api, backgroundScope)
        controls.refresh(); runCurrent()
        api.pending = CompletableDeferred()
        controls.camera(1, false); runCurrent()
        assertTrue(controls.state.value.busy)
        assertEquals(baseline, controls.state.value.snapshot!!.preferences)
        controls.master(false); runCurrent(); assertEquals(1, api.changes)
        api.pending!!.complete(baseline.copy(cameraIds = setOf(2))); runCurrent()
        assertFalse(controls.state.value.busy)
        assertEquals(baseline.copy(cameraIds = setOf(2)), controls.state.value.snapshot!!.preferences)
        controls.close()
    }
    @Test fun connectionFailureKeepsOldChoiceAndRefreshReadsAuthoritativeState() = runTest {
        val api = Fake(); val controls = AlertWidgetControls(api, backgroundScope)
        controls.refresh(); runCurrent()
        api.error = NasException(ApiProblem.CONNECTION)
        controls.master(false); runCurrent()
        assertEquals(baseline, controls.state.value.snapshot!!.preferences)
        assertEquals(ApiProblem.CONNECTION, controls.state.value.problem)
        assertFalse(controls.state.value.busy)
        api.snapshot = api.snapshot.copy(preferences = baseline.copy(notifications = false))
        controls.refresh(); runCurrent()
        assertFalse(controls.state.value.snapshot!!.preferences.notifications)
        assertNull(controls.state.value.problem)
        controls.close()
    }
    @Test fun closedPopupDoesNotPollAndPendingWorkIsCancelled() = runTest {
        val api = Fake(); val controls = AlertWidgetControls(api, backgroundScope)
        controls.refresh(); runCurrent(); advanceTimeBy(3_600_000); runCurrent()
        assertEquals(1, api.loads)
        api.pending = CompletableDeferred()
        controls.master(false); runCurrent(); controls.close()
        api.pending!!.complete(baseline.copy(notifications = false)); runCurrent()
        assertEquals(baseline, controls.state.value.snapshot!!.preferences)
    }
    @Test fun oldNasCannotPretendAWidgetChoiceWasApplied() = runTest {
        val api = Fake(); val controls = AlertWidgetControls(api, backgroundScope)
        controls.refresh(); runCurrent(); api.error = NasException(ApiProblem.SERVER, 405)
        controls.master(false); runCurrent()
        assertTrue(controls.state.value.updateRequired)
        assertEquals(baseline, controls.state.value.snapshot!!.preferences)
        controls.camera(1, false); runCurrent(); assertEquals(1, api.changes)
        controls.close()
    }
    @Test fun widgetBindingNeverUsesTheCurrentlySelectedDifferentNasOrUser() {
        val profile = ServerProfile("nas-one", "Home", "http://192.0.2.1:8088", "owner")
        val binding = AlertWidgetBinding(profile.id, profile.username)
        assertEquals(binding, AlertWidgetBinding.from(binding.json()))
        assertTrue(binding.matches(profile))
        assertFalse(binding.matches(profile.copy(id = "nas-two")))
        assertFalse(binding.matches(profile.copy(username = "viewer")))
        assertEquals(setOf("profile", "username"), binding.json().keys().asSequence().toSet())
    }
}
