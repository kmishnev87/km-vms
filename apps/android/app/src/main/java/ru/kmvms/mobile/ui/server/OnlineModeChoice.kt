package ru.kmvms.mobile.ui.server

import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.launch
import ru.kmvms.mobile.R
import ru.kmvms.mobile.ui.Muted

/** Existing choice metrics, a concise selected-mode caption and detailed popup help. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable internal fun OnlineModeChoice(value: String, enabled: Boolean, choose: (String) -> Unit) {
    var open by remember { mutableStateOf(false) }
    val tooltip = rememberTooltipState(isPersistent = true)
    val scope = rememberCoroutineScope()
    val title = stringResource(R.string.online_mode)
    Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(5.dp)) {
            Image(painterResource(R.drawable.kmvms_menu_live), null, Modifier.size(16.dp))
            Text(title, style = MaterialTheme.typography.labelMedium, color = Muted)
            TooltipBox(positionProvider = TooltipDefaults.rememberPlainTooltipPositionProvider(), state = tooltip,
                tooltip = { PlainTooltip { Text(stringResource(R.string.online_mode_help)) } }) {
                TextButton(onClick = { scope.launch { if (tooltip.isVisible) tooltip.dismiss() else tooltip.show() } }, contentPadding = PaddingValues(0.dp),
                    modifier = Modifier.size(18.dp).testTag("nas-online-mode-help")) { Text("ⓘ", color = Muted) }
            }
        }
        Box(Modifier.fillMaxWidth()) {
            OutlinedButton(onClick = { open = true }, enabled = enabled,
                modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp).testTag("nas-online-mode"),
                shape = RoundedCornerShape(10.dp), contentPadding = PaddingValues(horizontal = 14.dp, vertical = 10.dp)) {
                Text(stringResource(if (value == "fast") R.string.online_mode_fast else R.string.online_mode_stable),
                    modifier = Modifier.weight(1f), color = if (enabled) MaterialTheme.colorScheme.onSurface else Muted)
                Text("▾", color = Muted, modifier = Modifier.padding(start = 8.dp))
            }
            DropdownMenu(open, { open = false }) {
                listOf("fast" to R.string.online_mode_fast, "stable" to R.string.online_mode_stable).forEach { (mode, label) ->
                    DropdownMenuItem(text = { Text(stringResource(label)) }, onClick = { open = false; choose(mode) })
                }
            }
        }
        Text(stringResource(if (value == "fast") R.string.online_mode_fast_summary else R.string.online_mode_stable_summary),
            style = MaterialTheme.typography.bodySmall, color = Muted, modifier = Modifier.testTag("nas-online-mode-summary"))
    }
}
