package ru.kmvms.mobile.widget

import android.graphics.Matrix
import android.graphics.Rect
import android.graphics.drawable.BitmapDrawable
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
import android.widget.ImageView
import android.widget.FrameLayout
import java.util.Locale

@RunWith(AndroidJUnit4::class)
class AlertWidgetLayoutTest {
    // Exclude the transparent edges of the PNGs and the font's unused line height.
    private fun visibleArtworkHeight(image: ImageView): Float {
        val bitmap = (image.drawable as BitmapDrawable).bitmap
        var top = bitmap.height
        var bottom = -1
        for (y in 0 until bitmap.height) {
            if ((0 until bitmap.width).any { x -> bitmap.getPixel(x, y) ushr 24 > 64 }) {
                top = minOf(top, y)
                bottom = y
            }
        }
        val transform = FloatArray(9)
        image.imageMatrix.getValues(transform)
        return (bottom - top + 1) * (image.drawable.intrinsicHeight.toFloat() / bitmap.height) * transform[Matrix.MSCALE_Y]
    }

    @Test fun actualNumeralHeightBalancesBothIconsWithoutDisplacingThePlus() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.runOnMainSync {
            val base = instrumentation.targetContext
            for (scale in listOf(1f, 2f)) for (width in listOf(110, 126, 150, 180)) for (number in listOf("0", "17", "99")) {
                val context = base.createConfigurationContext(Configuration(base.resources.configuration).apply { fontScale = scale })
                val density = context.resources.displayMetrics.density
                val root = RemoteViews(context.packageName, R.layout.alert_widget_count).apply {
                    setTextViewText(R.id.alert_widget_count, number)
                    setViewVisibility(R.id.alert_widget_count_plus, if (number == "99") View.VISIBLE else View.INVISIBLE)
                }.apply(context, null)
                val w = (width * density).toInt()
                val h = (90 * density).toInt()
                root.measure(View.MeasureSpec.makeMeasureSpec(w, View.MeasureSpec.EXACTLY), View.MeasureSpec.makeMeasureSpec(h, View.MeasureSpec.EXACTLY))
                root.layout(0, 0, w, h)
                val count = root.findViewById<TextView>(R.id.alert_widget_count)
                val suffix = root.findViewById<TextView>(R.id.alert_widget_count_plus)
                val bell = visibleArtworkHeight(root.findViewById<ImageView>(R.id.alert_widget_bell))
                val live = visibleArtworkHeight(root.findViewById<FrameLayout>(R.id.alert_widget_live).getChildAt(0) as ImageView)
                val bounds = Rect().apply { count.paint.getTextBounds(number, 0, number.length, this) }
                val detail = "$number scale=$scale width=$width: glyph=${bounds.height() / density}dp, bell=${bell / density}dp, live=${live / density}dp"
                assertTrue(detail, bounds.height() >= minOf(bell, live) - 6f * density)
                assertTrue(detail, bounds.height() <= maxOf(bell, live) + 6f * density)
                assertEquals(detail, 34f * density, count.textSize, 1f)
                assertEquals(detail, 1f, count.textScaleX, 0.001f)
                assertTrue(detail, count.paint.measureText(number) <= count.width + .5f)
                assertEquals(detail, 0, count.layout.getEllipsisCount(0))
                assertEquals(detail, count.right, suffix.left)
            }
        }
    }

    @Test fun nasNameIsCenteredBelowThreeSeparateZonesAtMinimumWidthInAllLanguages() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.runOnMainSync {
            val base = instrumentation.targetContext
            for (language in listOf("ru", "en", "zh-CN")) for (scale in listOf(1f, 1.3f, 1.6f, 2f)) {
                val context = base.createConfigurationContext(Configuration(base.resources.configuration).apply {
                    setLocale(Locale.forLanguageTag(language)); fontScale = scale
                })
                val density = context.resources.displayMetrics.density
                for (width in listOf(110, 126, 180)) {
                    val root = RemoteViews(context.packageName, R.layout.alert_widget_count).apply {
                        setTextViewText(R.id.alert_widget_server, "Dacha")
                    }.apply(context, null)
                    root.measure(View.MeasureSpec.makeMeasureSpec((width * density).toInt(), View.MeasureSpec.EXACTLY),
                        View.MeasureSpec.makeMeasureSpec((100 * density).toInt(), View.MeasureSpec.EXACTLY))
                    root.layout(0, 0, root.measuredWidth, root.measuredHeight)
                    val server = root.findViewById<TextView>(R.id.alert_widget_server)
                    val settings = root.findViewById<View>(R.id.alert_widget_settings)
                    val live = root.findViewById<View>(R.id.alert_widget_live)
                    val count = root.findViewById<View>(R.id.alert_widget_count_area)
                    val detail = "$language scale=$scale width=$width"
                    assertEquals(detail, "Dacha", server.text.toString())
                    assertEquals(detail, android.view.Gravity.CENTER, server.gravity)
                    assertEquals(detail, 0, server.layout.getEllipsisCount(0))
                    assertEquals(detail, (settings.parent as View).bottom, server.top)
                    assertEquals(detail, settings.right, live.left)
                    assertEquals(detail, live.right, count.left)
                    assertEquals(detail, settings.height, live.height)
                    assertEquals(detail, live.height, count.height)
                }
            }
        }
    }
    @Test fun largeLauncherCellDoesNotStretchTheVisibleWidgetCard() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.runOnMainSync {
            val context = instrumentation.targetContext
            val density = context.resources.displayMetrics.density
            val root = RemoteViews(context.packageName, R.layout.alert_widget_count).apply(context, null)
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
    @Test fun counterHasAQuietFullNumberAtMinimumWidth() {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        instrumentation.runOnMainSync {
            val context = instrumentation.targetContext
            val density = context.resources.displayMetrics.density
            for (number in listOf("0", "17", "99")) {
                val root = RemoteViews(context.packageName, R.layout.alert_widget_count).apply {
                    setTextViewText(R.id.alert_widget_count, number)
                }.apply(context, null)
                root.measure(View.MeasureSpec.makeMeasureSpec((110 * density).toInt(), View.MeasureSpec.EXACTLY),
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
                root.measure(View.MeasureSpec.makeMeasureSpec((110 * density).toInt(), View.MeasureSpec.EXACTLY),
                    View.MeasureSpec.makeMeasureSpec((90 * density).toInt(), View.MeasureSpec.EXACTLY))
                root.layout(0, 0, root.measuredWidth, root.measuredHeight)
                assertEquals(left, count.left)
                assertEquals(right, count.right)
                assertEquals("+", suffix.text.toString())
            }
        }
    }
}
