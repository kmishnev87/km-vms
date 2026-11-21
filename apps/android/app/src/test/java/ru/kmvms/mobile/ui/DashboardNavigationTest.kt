package ru.kmvms.mobile.ui

import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.R

class DashboardNavigationTest {
    @Test fun homeTilesUseTheLatestApprovedOrderIndependentlyOfTheBottomBar() {
        assertEquals(listOf(Section.CAMERAS, Section.RECORDINGS, Section.CHRONOLOGY, Section.LIVE,
            Section.RECORDING_CENTER, Section.EVENTS, Section.STORAGE, Section.SETTINGS), dashboardSections)
        assertEquals(8, dashboardSections.distinct().size)
        assertEquals(Section.LIVE, primaryNavigation[2])
        assertEquals("manage_cameras", Section.RECORDING_CENTER.permission)
        assertEquals("run_diagnostics", Section.SYSTEM.permission)
    }
    @Test fun liveIsTheMiddleOfTheApprovedFiveTabs() {
        assertEquals(listOf(Section.HOME, Section.RECORDINGS, Section.LIVE, Section.CAMERAS, Section.EVENTS), primaryNavigation)
        assertEquals(Section.LIVE, primaryNavigation[2])
    }
    @Test fun systemStatusBackReturnsToSettingsWithoutAddingADashboardTile() {
        assertFalse(Section.SYSTEM in dashboardSections)
        assertEquals(Section.SETTINGS, parentSection(Section.SYSTEM))
        assertEquals(Section.HOME, parentSection(Section.SETTINGS))
    }
    @Test fun menuUsesMatchedSmallArtworkAndDashboardKeepsOriginalSources() {
        assertEquals(R.drawable.kmvms_menu_home, menuArtwork(Section.HOME))
        assertNull(Section.HOME.image)
        assertEquals(R.drawable.kmvms_menu_recordings, menuArtwork(Section.RECORDINGS))
        assertEquals(R.drawable.kmvms_menu_live, menuArtwork(Section.LIVE))
        assertEquals(R.drawable.kmvms_menu_camera, menuArtwork(Section.CAMERAS))
        assertEquals(R.drawable.kmvms_menu_events, menuArtwork(Section.EVENTS))
        assertEquals(R.drawable.kmvms_recordings, Section.RECORDINGS.image)
        assertEquals(R.drawable.kmvms_live, Section.LIVE.image)
        assertEquals(R.drawable.kmvms_camera, Section.CAMERAS.image)
        assertEquals(R.drawable.kmvms_events, Section.EVENTS.image)
    }
}
