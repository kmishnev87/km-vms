package ru.kmvms.mobile.core

import java.io.IOException
import org.junit.Assert.*
import org.junit.Test

class LiveFirstFailureTest {
    @Test fun cancellingTheSocketCannotOverwriteItsOriginalCompatibilityError() {
        val pipe=LiveBytePipe()
        val cause=IOException("synthetic compatibility failure")
        pipe.fail(cause)
        pipe.fail(IOException("secondary cancellation"))
        try { pipe.read(ByteArray(1),0,1);fail("original failure expected") }
        catch(error:IOException) { assertSame(cause,error) }
    }
}
