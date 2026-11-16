package ru.kmvms.mobile.ui

import android.text.format.Formatter
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp

/** A measured transfer, not an estimate of how long the whole update will take. */
@Composable internal fun MeasuredTransfer(current: Long, total: Long, tag: String) {
    if (total <= 0) return
    val received = current.coerceIn(0, total)
    val fraction = (received.toDouble() / total).toFloat().coerceIn(0f, 1f)
    val context = LocalContext.current
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text("${Formatter.formatShortFileSize(context, received)} / ${Formatter.formatShortFileSize(context, total)}",
            style = MaterialTheme.typography.bodySmall, color = Ink, modifier = Modifier.testTag("$tag-bytes"))
        Text("${(received * 100 / total).toInt()}%", style = MaterialTheme.typography.labelMedium,
            color = Ink, modifier = Modifier.testTag("$tag-percent"))
    }
    LinearProgressIndicator(progress = { fraction }, color = Accent, trackColor = Outline,
        modifier = Modifier.fillMaxWidth().height(6.dp).testTag("$tag-bar"), drawStopIndicator = {})
}
