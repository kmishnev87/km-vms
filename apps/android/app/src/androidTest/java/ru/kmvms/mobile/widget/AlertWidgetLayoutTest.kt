package ru.kmvms.mobile.widget

import android.view.View
import android.widget.RemoteViews
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.R

@RunWith(AndroidJUnit4::class)
class AlertWidgetLayoutTest {
    @Test fun largeLauncherCellDoesNotStretchTheVisibleWidgetCard() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.runOnMainSync {
            val context = instrumentation.targetContext
            val density = context.resources.displayMetrics.density
            val root = RemoteViews(context.packageName, R.layout.alert_widget).apply(context, null)
            val width = (240 * density).toInt()
            val height = (140 * density).toInt()
            root.measure(View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.EXACTLY), View.MeasureSpec.makeMeasureSpec(height, View.MeasureSpec.EXACTLY))
            root.layout(0, 0, width, height)
            val card = root.findViewById<View>(R.id.alert_widget_card)
            assertNull(root.background)
            assertNotNull(card.background)
            assertTrue(card.height <= 64 * density)
            assertTrue(kotlin.math.abs(card.top - (height-card.height)/2) <= 1)
            assertEquals(width, card.width)
        }
    }
}
