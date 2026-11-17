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
import android.content.res.Configuration
import android.view.LayoutInflater
import java.util.Locale

@RunWith(AndroidJUnit4::class)
class AlertWidgetLayoutTest {
    @Test fun fullTitleFitsBothMinimumWidthsAndLargerSystemFontsInAllLanguages() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.runOnMainSync {
            val base = instrumentation.targetContext
            for (language in listOf("ru", "en", "zh-CN")) for (scale in listOf(1f, 1.3f, 1.6f)) {
                val context = base.createConfigurationContext(Configuration(base.resources.configuration).apply {
                    setLocale(Locale.forLanguageTag(language)); fontScale = scale
                })
                val density = context.resources.displayMetrics.density
                for ((layout, width) in listOf(R.layout.alert_widget to 110, R.layout.alert_widget_count to 180)) {
                    val root = LayoutInflater.from(context).inflate(layout, null)
                    root.measure(View.MeasureSpec.makeMeasureSpec((width * density).toInt(), View.MeasureSpec.EXACTLY),
                        View.MeasureSpec.makeMeasureSpec((100 * density).toInt(), View.MeasureSpec.EXACTLY))
                    root.layout(0, 0, root.measuredWidth, root.measuredHeight)
                    val title = root.findViewById<TextView>(R.id.alert_widget_title)
                    val detail = "$language scale=$scale width=$width title=${title.text}"
                    assertEquals(detail, context.getString(R.string.widget_alerts), title.text.toString())
                    assertTrue(detail, title.paint.measureText(title.text.toString()) <= title.width - title.paddingLeft - title.paddingRight + .5f)
                    assertEquals(detail, 0, title.layout.getEllipsisCount(0))
                }
            }
        }
    }
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
