package ru.kmvms.mobile.ui

import androidx.annotation.DrawableRes
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.FilterQuality
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.graphics.drawscope.drawIntoCanvas
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.imageResource
import com.caverock.androidsvg.SVG
import ru.kmvms.mobile.R
import kotlin.math.max

/** The original bundled SVGs, not screenshots, web views or network images. */
@Composable internal fun DashboardBackground(section: Section, modifier: Modifier) {
    val resource = when (section) {
        Section.LIVE -> R.raw.dashboard_live
        Section.RECORDINGS -> R.raw.dashboard_recordings
        Section.CAMERAS -> R.raw.dashboard_camera
        Section.EVENTS -> R.raw.dashboard_events
        Section.CHRONOLOGY -> R.raw.dashboard_chronology
        Section.STORAGE -> R.raw.dashboard_storage
        Section.SYSTEM -> R.raw.dashboard_system_status
        Section.SETTINGS, Section.SERVER_SETTINGS -> R.raw.dashboard_settings
        else -> return
    }
    val context = LocalContext.current
    val picture = remember(resource, context.resources) { SVG.getFromResource(context, resource).renderToPicture() }
    Canvas(modifier) {
        // Exactly the web card's right-center / cover rule; never distort the SVG.
        val factor = max(size.width / picture.width, size.height / picture.height)
        drawIntoCanvas { canvas ->
            val native = canvas.nativeCanvas
            val saved = native.save()
            try {
                native.translate(size.width - picture.width * factor, (size.height - picture.height * factor) / 2)
                native.scale(factor, factor)
                native.drawPicture(picture)
            } finally { native.restoreToCount(saved) }
        }
    }
}

@Composable internal fun DashboardImage(@DrawableRes resource: Int, modifier: Modifier) {
    val image = ImageBitmap.imageResource(resource)
    // Keep the original full-resolution PNG. Hint GPU minification to use mipmaps
    // instead of throwing away source pixels with a single bilinear sample.
    remember(image) { image.asAndroidBitmap().setHasMipMap(true) }
    Image(bitmap = image, contentDescription = null, modifier = modifier, filterQuality = FilterQuality.Medium)
}

internal val primaryNavigation = listOf(Section.HOME, Section.RECORDINGS, Section.LIVE, Section.CAMERAS, Section.EVENTS)

internal val dashboardSections = listOf(Section.CAMERAS, Section.RECORDINGS, Section.CHRONOLOGY, Section.LIVE,
    Section.SYSTEM, Section.EVENTS, Section.STORAGE, Section.SETTINGS)

internal fun menuArtwork(section: Section): Int? = when (section) {
    Section.HOME -> R.drawable.kmvms_menu_home
    Section.LIVE -> R.drawable.kmvms_menu_live
    Section.RECORDINGS -> R.drawable.kmvms_menu_recordings
    Section.CAMERAS -> R.drawable.kmvms_menu_camera
    Section.EVENTS -> R.drawable.kmvms_menu_events
    else -> section.image
}
