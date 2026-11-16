package ru.kmvms.mobile.ui.archive

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import ru.kmvms.mobile.R
import ru.kmvms.mobile.ui.Muted

/** Shares the 16dp inset and 64dp action column with the swipe rows below. */
@Composable internal fun RecordingsSelectionBar(total: Int, zone: String, selectedCount: Int,
    loading: Boolean, canDelete: Boolean, deleting: Boolean, deleteSelected: () -> Unit) {
    Row(Modifier.fillMaxWidth().height(40.dp).padding(horizontal = 16.dp).testTag("recordings-selection-bar"),
        verticalAlignment = Alignment.CenterVertically) {
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.Center) {
            if (loading) LinearProgressIndicator(Modifier.fillMaxWidth())
            else Text(stringResource(R.string.archive_count, total, zone), color = Muted, style = MaterialTheme.typography.labelSmall)
            if (canDelete && selectedCount > 0) Text(stringResource(R.string.archive_selected, selectedCount),
                color = Muted, style = MaterialTheme.typography.labelSmall, modifier = Modifier.testTag("recordings-selected-count"))
        }
        if (canDelete && selectedCount > 1) IconButton(onClick = deleteSelected, enabled = !deleting && !loading,
            modifier = Modifier.width(64.dp).fillMaxHeight().testTag("recordings-delete-selected")) {
            Icon(painterResource(R.drawable.ic_camera_trash), stringResource(R.string.recordings_delete_selected),
                tint = Color(0xFFD85C63), modifier = Modifier.size(21.dp))
        }
    }
}
