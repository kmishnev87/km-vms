package ru.kmvms.mobile.widget

import android.view.View
import android.widget.RemoteViews
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.R
import android.widget.TextView

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
    @Test fun compactHasNoCounterAndCountWidgetHasAQuietFullNumberAtMinimumWidth() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.runOnMainSync {
            val context = instrumentation.targetContext
            val density = context.resources.displayMetrics.density
            val compact = RemoteViews(context.packageName, R.layout.alert_widget).apply(context, null)
            assertNull(compact.findViewById<View>(R.id.alert_widget_count))
            for (number in listOf("0", "17", "100000")) {
                val root = RemoteViews(context.packageName, R.layout.alert_widget_count).apply {
                    setTextViewText(R.id.alert_widget_count, number)
                }.apply(context, null)
                root.measure(View.MeasureSpec.makeMeasureSpec((180 * density).toInt(), View.MeasureSpec.EXACTLY),
                    View.MeasureSpec.makeMeasureSpec((90 * density).toInt(), View.MeasureSpec.EXACTLY))
                root.layout(0, 0, root.measuredWidth, root.measuredHeight)
                val count = root.findViewById<TextView>(R.id.alert_widget_count)
                assertEquals(number, count.text.toString())
                assertEquals(android.graphics.Color.parseColor("#475569"), count.currentTextColor)
                assertNull(count.background)
                assertTrue(count.right <= root.width && count.width >= 44 * density)
                assertTrue(count.paint.measureText(number) <= count.width - count.paddingLeft - count.paddingRight)
            }
        }
    }
}
