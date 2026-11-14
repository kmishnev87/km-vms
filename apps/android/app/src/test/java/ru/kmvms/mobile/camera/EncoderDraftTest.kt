package ru.kmvms.mobile.camera

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class EncoderDraftTest {
    private fun contract()=JSONObject("""{
        "config":{"codec":"H264","width":1920,"height":1080,"fps":25,"bitrate":4096,"quality":6},
        "supported":{
            "codec":{"writable":true,"options":["H264","H265"]},
            "resolution":{"writable":true,"options":["1920x1080","1280x720"]},
            "fps":{"writable":true,"number_type":"integer","range":{"min":1,"max":30}},
            "bitrate":{"writable":true,"range":{"min":256,"max":8192}},
            "quality":{"writable":false}
        }
    }""")
    @Test fun readingAConfigurationDoesNotInventAChangeFromWidthAndHeight(){
        val draft=EncoderDraft.from(contract())
        assertEquals("1920x1080",draft["resolution"]);assertEquals(0,draft.diff().length());assertTrue(draft.valid())
    }
    @Test fun unsupportedFieldsNeverEnterTheCameraRequest(){
        val draft=EncoderDraft.from(contract()).change("quality","9").change("bitrate","2048")
        assertTrue(draft.valid());assertFalse(draft.diff().has("quality"));assertEquals(2048.0,draft.diff().getDouble("bitrate"),0.0)
    }
    @Test fun numericLimitsAndIntegerFpsAreEnforced(){
        val draft=EncoderDraft.from(contract())
        assertFalse(draft.change("fps","25.5").valid());assertFalse(draft.change("bitrate","9000").valid())
        assertFalse(draft.change("fps","NaN").valid());assertFalse(draft.change("fps","").valid())
        assertTrue(draft.change("fps","30").valid())
    }
    @Test fun codecChangeUsesTheCamerasOwnResolutionAndFpsOptions(){
        val value=contract().put("options_by_codec",JSONObject("""{"H265":{"fields":{
            "codec":{"writable":true,"options":["H264","H265"]},
            "resolution":{"writable":true,"options":["1280x720"]},
            "fps":{"writable":true,"number_type":"integer","range":{"min":1,"max":20}},
            "bitrate":{"writable":true,"range":{"min":256,"max":2048}}
        }}}"""))
        val changed=EncoderDraft.from(value).change("codec","H265")
        assertEquals("1280x720",changed["resolution"]);assertEquals("20",changed["fps"])
        assertTrue(changed.valid());assertEquals(2048.0,changed.diff().getDouble("bitrate"),0.0)
    }
    @Test(expected=IllegalArgumentException::class)fun unknownFieldsCannotBeAdded(){
        EncoderDraft.from(contract()).change("host","unrelated")
    }
}
