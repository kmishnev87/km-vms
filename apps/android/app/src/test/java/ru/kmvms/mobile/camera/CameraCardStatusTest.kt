package ru.kmvms.mobile.camera

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.data.Camera

class CameraCardStatusTest {
    private fun camera(enabled:Boolean)=Camera.fromJson(JSONObject().put("id",1).put("name","Fixture").put("enabled",enabled))
    private fun summary()=JSONObject("""{"heartbeat":{"status":"current"},"camera_recording_states":[{"camera_id":1,"job_state":"recording","heartbeat_current":true,"confirmed_recording":true,"current_failure":false,"stale_current_segment":false}]}""")
    @Test fun enabledDoesNotMeanActuallyRecording() {
        assertEquals(CameraCardStatus.STOPPED,cameraCardStatus(camera(false),null))
        assertEquals(CameraCardStatus.UNKNOWN,cameraCardStatus(camera(true),null))
        assertEquals(CameraCardStatus.RECORDING,cameraCardStatus(camera(true),summary()))
    }
    @Test fun staleAndFailedRecordingsAreNeverGreen() {
        for(field in listOf("stale_current_segment","current_failure")){
            val value=summary();value.getJSONArray("camera_recording_states").getJSONObject(0).put(field,true)
            assertEquals(CameraCardStatus.STOPPED,cameraCardStatus(camera(true),value))
        }
        val value=summary();value.getJSONObject("heartbeat").put("status","stale_or_unavailable")
        assertEquals(CameraCardStatus.UNKNOWN,cameraCardStatus(camera(true),value))
    }
    @Test fun aStartingJobIsNotReportedAsConfirmedRecording() {
        val value=summary();value.getJSONArray("camera_recording_states").getJSONObject(0).put("confirmed_recording",false)
        assertEquals(CameraCardStatus.STARTING,cameraCardStatus(camera(true),value))
    }
}
