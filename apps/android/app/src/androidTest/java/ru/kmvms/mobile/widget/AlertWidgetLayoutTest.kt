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
import android.widget.ImageView
import java.util.Locale

@RunWith(AndroidJUnit4::class)
class AlertWidgetLayoutTest {
    @Test fun fullTitleFitsBothMinimumWidthsAndLargerSystemFontsInAllLanguages() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.runOnMainSync {
            val base = instrumentation.targetContext
            for (language in listOf("ru", "en", "zh-CN")) for (scale in listOf(1f, 1.3f, 1.6f, 2f)) {
                val context = base.createConfigurationContext(Configuration(base.resources.configuration).apply {
                    setLocale(Locale.forLanguageTag(language)); fontScale = scale
                })
                val density = context.resources.displayMetrics.density
                val widths = listOf(150, 180, 203).map { R.layout.alert_widget_count to it }
                val cases = if (scale <= 1.6f) widths + (R.layout.alert_widget to 110) else widths
                for ((layout, width) in cases) {
                    val root = LayoutInflater.from(context).inflate(layout, null)
                    root.measure(View.MeasureSpec.makeMeasureSpec((width * density).toInt(), View.MeasureSpec.EXACTLY),
                        View.MeasureSpec.makeMeasureSpec((100 * density).toInt(), View.MeasureSpec.EXACTLY))
                    root.layout(0, 0, root.measuredWidth, root.measuredHeight)
                    val title = root.findViewById<TextView>(R.id.alert_widget_title)
                    val detail = "$language scale=$scale width=$width title=${title.text}"
                    assertEquals(detail, context.getString(R.string.widget_alerts), title.text.toString())
                    assertTrue(detail, title.paint.measureText(title.text.toString()) <= title.width - title.paddingLeft - title.paddingRight + .5f)
                    assertEquals(detail, 0, title.layout.getEllipsisCount(0))
                    assertEquals(detail, title.text.length, title.layout.getLineEnd(0))
                    if (layout == R.layout.alert_widget_count) {
                        val settings = root.findViewById<View>(R.id.alert_widget_settings)
                        val bell = root.findViewById<ImageView>(R.id.alert_widget_bell)
                        val live = root.findViewById<ImageView>(R.id.alert_widget_live)
                        val count = root.findViewById<View>(R.id.alert_widget_count_area)
                        assertEquals(detail, settings.right, live.left)
                        assertEquals(detail, live.right, count.left)
                        assertEquals(detail, settings.height, live.height)
                        assertEquals(detail, live.height, count.height)
                        assertTrue(detail, kotlin.math.abs(bell.width - (live.width - live.paddingLeft - live.paddingRight)) <= 1)
                        assertTrue(detail, kotlin.math.abs(bell.height - (live.height - live.paddingTop - live.paddingBottom)) <= 1)
                    }
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
            assertNull(compact.findViewById<View>(R.id.alert_widget_live))
            for (number in listOf("0", "17", "99")) {
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
                val left = count.left
                val right = count.right
                assertTrue(count.paint.measureText(number) <= count.width - count.paddingLeft - count.paddingRight)
                val suffix = root.findViewById<TextView>(R.id.alert_widget_count_plus)
                suffix.visibility = View.VISIBLE
                root.measure(View.MeasureSpec.makeMeasureSpec((180 * density).toInt(), View.MeasureSpec.EXACTLY),
                    View.MeasureSpec.makeMeasureSpec((90 * density).toInt(), View.MeasureSpec.EXACTLY))
                root.layout(0, 0, root.measuredWidth, root.measuredHeight)
                assertEquals(left, count.left)
                assertEquals(right, count.right)
                assertEquals("+", suffix.text.toString())
            }
        }
    }
}
