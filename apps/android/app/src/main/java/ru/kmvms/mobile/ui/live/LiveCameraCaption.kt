package ru.kmvms.mobile.ui.live

import androidx.compose.foundation.layout.*
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shadow
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import ru.kmvms.mobile.R

@Composable internal fun LiveCameraCaption(name: String, hlsFallback: Boolean, modifier: Modifier = Modifier) {
    val shadow = Shadow(Color.Black.copy(alpha = .85f), Offset(0f, 1f), 3f)
    Column(modifier.padding(horizontal = 10.dp).widthIn(max = 180.dp)) {
        Text(name, color = Color.White.copy(alpha = .82f), maxLines = 2, overflow = TextOverflow.Ellipsis,
            style = TextStyle(fontSize = 12.sp, shadow = shadow))
        if (hlsFallback) Text(stringResource(R.string.live_hls_fallback), color = Color.White.copy(alpha = .75f),
            style = TextStyle(fontSize = 11.sp, lineHeight = 14.sp, shadow = shadow), modifier = Modifier.testTag("live-hls-fallback"))
    }
}
