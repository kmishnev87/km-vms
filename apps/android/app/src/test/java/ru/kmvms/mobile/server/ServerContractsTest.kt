package ru.kmvms.mobile.server

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class ServerContractsTest {
    @Test fun theModePatchDoesNotAccidentallyChangeRecordingSettings() {
        val baseline=ServerSettings("en","UTC","mkv","auto",false,false,"")
        val changed=baseline.copy(onlineMode="fast").diff(baseline,false)
        assertEquals(setOf("online_mode"),changed.keys().asSequence().toSet())
        assertEquals("fast",changed.getString("online_mode"))
        assertEquals("stable",ServerSettings.from(JSONObject("""{"language":"en","timezone":"UTC","recording_format":"mkv"}""")).onlineMode)
    }
    private fun baseline()=ServerSettings("ru","Etc/GMT-5","mkv","auto",false,true,"fixture-terms")
    @Test fun changingLanguageDoesNotResubmitOtherServerSettings(){
        val current=baseline()
        val body=current.copy(language="en").diff(current,false)
        assertEquals(setOf("language"),body.keys().asSequence().toSet())
    }
    @Test fun formatChangeWaitsForSegmentBoundaryAndKeepsExpectedSourceFormat(){
        val current=baseline();val body=current.copy(format="mp4").diff(current,false)
        assertEquals("after_segment",body.getString("recording_format_apply_mode"))
        assertEquals("mkv",body.getString("expected_recording_format"));assertFalse(body.has("hardware_preferred_backend"))
    }
    @Test(expected=IllegalArgumentException::class)fun automaticDeletionCannotBeEnabledWithoutExplicitAgreement(){
        val current=baseline();current.copy(autoCleanup=true).diff(current,false)
    }
    @Test fun automaticDeletionAgreementUsesTheCurrentNasTermsAndDisableNeedsNoAgreement(){
        val current=baseline();val enabled=current.copy(autoCleanup=true).diff(current,true)
        assertEquals("fixture-terms",enabled.getJSONObject("auto_free_space_acknowledgement").getString("terms_version"))
        val disabled=current.diff(current.copy(autoCleanup=true),false)
        assertFalse(disabled.getBoolean("auto_free_space_cleanup_enabled"));assertFalse(disabled.has("auto_free_space_acknowledgement"))
    }
    @Test fun existingUnknownTimezoneDoesNotBlockAnUnrelatedLanguageChange(){
        val current=baseline().copy(timezone="Future/Timezone")
        assertEquals(setOf("language"),current.copy(language="en").diff(current,false).keys().asSequence().toSet())
    }
    @Test fun storageUsesNasCapacityOnceAndPreservesDeletedCameraName(){
        val snapshot=StorageView.from(JSONObject("""{"available":true,"capacity":{"total_bytes":1000,"free_bytes":400},
            "owned_archive":{"kmvms_owned_archive_bytes":200},"camera_usage":[{"camera_id":5,"camera_name":"Old garden","camera_deleted":true,"size_bytes":200,"recording_count":2}],
            "archive_roots":[{"id":"a","is_active":true,"read_access_state":"available","size_bytes":100},{"id":"b","is_active":false,"read_access_state":"available","size_bytes":100}]}"""))
        assertEquals(1000L,snapshot.total);assertEquals(400L,snapshot.free);assertEquals(200L,snapshot.archiveBytes)
        assertEquals("Old garden",snapshot.cameras.single().name);assertTrue(snapshot.cameras.single().deleted)
    }
    @Test fun missingCapacityIsNotInventedAsZeroFreeSpace(){
        val snapshot=StorageView.from(JSONObject("""{"available":true,"capacity":{"total_bytes":null,"free_bytes":null}}"""))
        assertNull(snapshot.total);assertNull(snapshot.free)
    }
    @Test fun systemSeverityAndCountsComeFromNasNotFromAFrontEndGuess(){
        val view=SystemView.from(JSONObject("""{"severity":"error","summary":{"problem_count":1,"warning_count":0,"unknown_count":0},"domains":{"storage":{"severity":"error","reason_codes":["storage_unavailable"],"items":[]}}}"""))
        assertEquals("error",view.severity);assertEquals(1,view.problems);assertEquals(listOf("storage_unavailable"),view.domains.single().reasons)
    }
    @Test fun noLiveViewsIsAnIdleStateNotAFailedOrUnperformedHealthCheck(){
        val view=SystemView.from(JSONObject("""{"severity":"ok","summary":{},"domains":{"live":{"severity":"unknown","items":[]}}}"""))
        assertEquals("ok",view.severity);assertTrue(view.domains.single().idle)
        val active=SystemView.from(JSONObject("""{"severity":"ok","summary":{},"domains":{"live":{"severity":"ok","items":[{"severity":"ok"}]}}}"""))
        assertFalse(active.domains.single().idle)
    }
}
