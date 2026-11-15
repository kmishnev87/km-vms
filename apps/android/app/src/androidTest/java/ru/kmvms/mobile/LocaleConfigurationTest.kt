package ru.kmvms.mobile

import android.app.UiAutomation
import android.content.res.Configuration
import android.os.SystemClock
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

/** Exercise the pre-13 context path on a real Android resource manager, irrespective of test OS. */
@RunWith(AndroidJUnit4::class)
class LocaleConfigurationTest {
    @Test fun explicitLanguagesFollowRotationWithoutFreezingScreenDimensions() {
        val instrumentation=InstrumentationRegistry.getInstrumentation()
        val context=instrumentation.targetContext
        val automation=instrumentation.uiAutomation
        fun awaitOrientation(wanted:Int) {
            val deadline=SystemClock.elapsedRealtime()+5000
            while(context.resources.configuration.orientation!=wanted&&SystemClock.elapsedRealtime()<deadline)SystemClock.sleep(50)
            assertEquals(wanted,context.resources.configuration.orientation)
        }
        try {
            assertTrue(automation.setRotation(UiAutomation.ROTATION_FREEZE_0))
            awaitOrientation(Configuration.ORIENTATION_PORTRAIT)
            val localized=listOf("en","ru","zh-CN").associateWith{AppLocales.localizedContext(context,it)}
            val frozen=context.createConfigurationContext(Configuration(context.resources.configuration))
            assertTrue(automation.setRotation(UiAutomation.ROTATION_FREEZE_90))
            awaitOrientation(Configuration.ORIENTATION_LANDSCAPE)
            assertEquals(Configuration.ORIENTATION_PORTRAIT,frozen.resources.configuration.orientation)
            for((language,wrapped)in localized) {
                val actual=wrapped.resources.configuration
                assertEquals(language,actual.locales.toLanguageTags())
                assertEquals(Configuration.ORIENTATION_LANDSCAPE,actual.orientation)
                assertEquals(context.resources.configuration.screenWidthDp,actual.screenWidthDp)
                assertEquals(context.resources.configuration.screenHeightDp,actual.screenHeightDp)
            }
            assertTrue(automation.setRotation(UiAutomation.ROTATION_FREEZE_0))
            awaitOrientation(Configuration.ORIENTATION_PORTRAIT)
            localized.values.forEach{assertEquals(Configuration.ORIENTATION_PORTRAIT,it.resources.configuration.orientation)}
        } finally { automation.setRotation(UiAutomation.ROTATION_UNFREEZE) }
    }
}
