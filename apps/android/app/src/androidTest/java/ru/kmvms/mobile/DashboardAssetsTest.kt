package ru.kmvms.mobile

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import androidx.test.platform.app.InstrumentationRegistry
import com.caverock.androidsvg.SVG
import org.junit.Assert.*
import org.junit.Test

class DashboardAssetsTest {
    @Test fun eventActionsMatchVisibleInkNotOnlyDrawableBoxes() {
        val context=InstrumentationRegistry.getInstrumentation().targetContext
        fun bounds(resource:Int):List<Int> {
            val bitmap=Bitmap.createBitmap(240,240,Bitmap.Config.ARGB_8888)
            try {
                val drawable=context.getDrawable(resource)!!
                drawable.setBounds(0,0,240,240);drawable.draw(Canvas(bitmap))
                var left=240;var top=240;var right=-1;var bottom=-1
                for(y in 0 until 240)for(x in 0 until 240)if(Color.alpha(bitmap.getPixel(x,y))>=64){
                    left=minOf(left,x);right=maxOf(right,x);top=minOf(top,y);bottom=maxOf(bottom,y)
                }
                return listOf(left,top,right,bottom)
            } finally { bitmap.recycle() }
        }
        val live=bounds(R.drawable.ic_event_live);val archive=bounds(R.drawable.ic_event_archive)
        assertTrue(kotlin.math.abs((live[3]-live[1])-(archive[3]-archive[1]))<=2)
        assertTrue(kotlin.math.abs((live[1]+live[3])-(archive[1]+archive[3]))<=2)
        assertTrue(kotlin.math.abs((live[0]+live[2])-(archive[0]+archive[2]))<=3)
    }
    @Test fun originalSvgBackgroundsRenderLocallyAndLargeImagesKeepTheirDimensions() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        for (resource in listOf(R.raw.dashboard_live, R.raw.dashboard_camera, R.raw.dashboard_recordings,
            R.raw.dashboard_chronology, R.raw.dashboard_storage, R.raw.dashboard_settings, R.raw.dashboard_system_status, R.raw.dashboard_events)) {
            val svg = SVG.getFromResource(context, resource)
            val width = if (resource == R.raw.dashboard_storage) 420 else 720
            val height = if (resource == R.raw.dashboard_storage) 250 else 260
            assertEquals(width.toFloat(), svg.documentWidth, 0f)
            assertEquals(height.toFloat(), svg.documentHeight, 0f)
            val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
            try {
                svg.renderToCanvas(Canvas(bitmap))
                val pixels = IntArray(width * height)
                bitmap.getPixels(pixels, 0, width, 0, 0, width, height)
                assertTrue(pixels.any { Color.alpha(it) > 0 })
            } finally { bitmap.recycle() }
        }
        val options = BitmapFactory.Options().apply { inJustDecodeBounds = true; inScaled = false }
        for (resource in listOf(R.drawable.kmvms_camera, R.drawable.kmvms_live, R.drawable.kmvms_recordings,
            R.drawable.kmvms_chronology, R.drawable.kmvms_settings, R.drawable.kmvms_system_status)) {
            BitmapFactory.decodeResource(context.resources, resource, options)
            assertEquals(512, options.outWidth)
            assertEquals(512, options.outHeight)
        }
        for (resource in listOf(R.drawable.kmvms_menu_camera, R.drawable.kmvms_menu_live, R.drawable.kmvms_menu_recordings, R.drawable.kmvms_menu_events, R.drawable.kmvms_menu_home)) {
            BitmapFactory.decodeResource(context.resources, resource, options)
            assertEquals(128, options.outWidth)
            assertEquals(128, options.outHeight)
        }
    }

    @Test fun approvedNavigationArtKeepsRealTransparencyAndConsistentVisibleSize() {
        val resources = InstrumentationRegistry.getInstrumentation().targetContext.resources
        val options = BitmapFactory.Options().apply { inScaled = false }
        for ((resource, dimension) in listOf(R.drawable.kmvms_events to 1254, R.drawable.kmvms_menu_events to 128, R.drawable.kmvms_menu_home to 128)) {
            val bitmap = BitmapFactory.decodeResource(resources, resource, options)
            try {
                assertEquals(dimension, bitmap.width)
                assertEquals(dimension, bitmap.height)
                assertTrue(bitmap.hasAlpha())
                for ((x, y) in listOf(0 to 0, (dimension - 1) to 0, 0 to (dimension - 1), (dimension - 1) to (dimension - 1))) {
                    assertEquals(0, Color.alpha(bitmap.getPixel(x, y)))
                }
                assertTrue(Color.alpha(bitmap.getPixel(dimension / 2, dimension / 2)) > 0)
                if (resource == R.drawable.kmvms_menu_events) {
                    val visibleRows = (0 until dimension).filter { y ->
                        (0 until dimension).any { x -> Color.alpha(bitmap.getPixel(x, y)) >= 32 }
                    }
                    // Same ~100px optical height as the existing 128px UI icons.
                    assertTrue(visibleRows.last() - visibleRows.first() + 1 in 98..102)
                }
                if (resource == R.drawable.kmvms_menu_home) {
                    val visibleColumns = (0 until dimension).filter { x ->
                        (0 until dimension).any { y -> Color.alpha(bitmap.getPixel(x, y)) >= 32 }
                    }
                    assertTrue(visibleColumns.last() - visibleColumns.first() + 1 in 98..103)
                }
            } finally { bitmap.recycle() }
        }
    }
}
