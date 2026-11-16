package ru.kmvms.mobile.ui.server

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Close
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.*
import ru.kmvms.mobile.server.*
import ru.kmvms.mobile.ui.Muted
import ru.kmvms.mobile.ui.camera.LabeledChoice

@Composable internal fun UsersScreen(api: NasApi, actor: NasUser, close: () -> Unit,
    credentialsChanged: () -> Unit, message: (String) -> Unit) {
    if (!UserManagementPolicy.allowed(actor)) {
        Text(stringResource(R.string.access_denied), modifier = Modifier.padding(16.dp)); return
    }
    val service = remember(api, actor) { UsersApi(api, actor) }
    var users by remember(service) { mutableStateOf(emptyList<ManagedUser>()) }
    var loading by remember(service) { mutableStateOf(true) }
    var busy by remember(service) { mutableStateOf(false) }
    var revision by remember(service) { mutableIntStateOf(0) }
    var editing by remember(service) { mutableStateOf(false) }
    var target by remember(service) { mutableStateOf<ManagedUser?>(null) }
    var deleting by remember(service) { mutableStateOf<ManagedUser?>(null) }
    var error by remember(service) { mutableStateOf<Int?>(null) }
    val scope = rememberCoroutineScope()
    val changedNotice = stringResource(R.string.users_credentials_changed)
    LaunchedEffect(service, revision) {
        loading = true
        try { users = service.list(); error = null }
        catch (cancelled: CancellationException) { throw cancelled }
        catch (failure: Exception) { error = userError(failure) }
        finally { loading = false }
    }
    BackHandler(!busy) { if (editing) editing = false else close() }
    if (editing) {
        UserEditor(actor, target, busy, error, { if (!busy) { editing = false; error = null } }) { mutation ->
            if (busy) return@UserEditor
            busy = true; error = null
            scope.launch {
                try {
                    val original = target
                    service.save(original, mutation)
                    editing = false; revision++
                    if (original?.id == actor.id && (mutation.username.trim() != original.username || mutation.password.isNotEmpty())) {
                        message(changedNotice); credentialsChanged()
                    }
                } catch (cancelled: CancellationException) { throw cancelled }
                catch (failure: Exception) { error = userError(failure) }
                finally { busy = false }
            }
        }
        return
    }
    Column(Modifier.fillMaxSize()) {
        Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(stringResource(R.string.users_and_roles), style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
            IconButton(onClick = { target = null; error = null; editing = true }, enabled = !loading && !busy,
                modifier = Modifier.testTag("user-add")) { Icon(Icons.Default.Add, stringResource(R.string.user_add)) }
            IconButton(onClick = close, enabled = !busy) { Icon(Icons.Default.Close, stringResource(R.string.close)) }
        }
        Box(Modifier.fillMaxWidth().height(6.dp)) { if (loading || busy) LinearProgressIndicator(Modifier.fillMaxWidth()) }
        error?.let { Text(stringResource(it), color = MaterialTheme.colorScheme.error, modifier = Modifier.padding(16.dp)) }
        LazyColumn(Modifier.fillMaxSize().testTag("users-list"), contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            if (error != null) item { OutlinedButton(onClick = { revision++ }, enabled = !loading && !busy) { Text(stringResource(R.string.retry)) } }
            items(users, key = { it.id }) { user -> Panel {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(user.displayName.ifBlank { user.username }, maxLines = 1, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.titleMedium)
                        if (user.displayName.isNotBlank()) Text(user.username, color = Muted, style = MaterialTheme.typography.bodySmall)
                        Text(stringResource(roleLabel(user.role)) + " · " + stringResource(if (user.active) R.string.user_active else R.string.user_inactive),
                            color = Muted, style = MaterialTheme.typography.bodySmall)
                    }
                    IconButton(onClick = { target = user; error = null; editing = true },
                        enabled = !busy && UserManagementPolicy.canEdit(actor, user), modifier = Modifier.testTag("user-edit-${user.id}")) {
                        Icon(painterResource(R.drawable.ic_camera_edit), stringResource(R.string.user_edit), tint = Muted, modifier = Modifier.size(21.dp))
                    }
                    IconButton(onClick = { deleting = user }, enabled = !busy && UserManagementPolicy.canDelete(actor, user, users),
                        modifier = Modifier.testTag("user-delete-${user.id}")) {
                        Icon(painterResource(R.drawable.ic_camera_trash), stringResource(R.string.remove), tint = MaterialTheme.colorScheme.error, modifier = Modifier.size(21.dp))
                    }
                }
            } }
        }
    }
    deleting?.let { user -> AlertDialog(onDismissRequest = { if (!busy) deleting = null },
        title = { Text(stringResource(R.string.user_delete_title)) }, text = { Text(stringResource(R.string.user_delete_confirmation, user.username)) },
        confirmButton = { TextButton(onClick = {
            if (busy) return@TextButton
            busy = true
            scope.launch {
                try { service.delete(user, users); deleting = null; revision++ }
                catch (cancelled: CancellationException) { throw cancelled }
                catch (failure: Exception) { error = userError(failure); deleting = null }
                finally { busy = false }
            }
        }, enabled = !busy, modifier = Modifier.testTag("user-delete-confirm")) { Text(stringResource(R.string.remove)) } },
        dismissButton = { TextButton(onClick = { deleting = null }, enabled = !busy) { Text(stringResource(R.string.cancel)) } }) }
}

@Composable internal fun UserEditor(actor: NasUser, baseline: ManagedUser?, busy: Boolean, error: Int?, close: () -> Unit,
    save: (UserMutation) -> Unit) {
    var username by remember(baseline?.id) { mutableStateOf(baseline?.username.orEmpty()) }
    var name by remember(baseline?.id) { mutableStateOf(baseline?.displayName.orEmpty()) }
    var role by remember(baseline?.id) { mutableStateOf(baseline?.role ?: UserManagementPolicy.createRoles(actor).lastOrNull().orEmpty()) }
    var active by remember(baseline?.id) { mutableStateOf(baseline?.active ?: true) }
    // Passwords are deliberately not saveable and never enter the diagnostic journal.
    var password by remember(baseline?.id) { mutableStateOf("") }
    var confirmation by remember(baseline?.id) { mutableStateOf("") }
    var currentPassword by remember(baseline?.id) { mutableStateOf("") }
    val mutation = UserMutation(username, name, role, active, password, currentPassword)
    val valid = mutation.valid(actor, baseline) && password == confirmation
    Column(Modifier.fillMaxSize().imePadding()) {
        Row(Modifier.fillMaxWidth().padding(start = 16.dp, end = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(stringResource(if (baseline == null) R.string.user_add else R.string.user_edit), style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
            IconButton(onClick = close, enabled = !busy) { Icon(Icons.Default.Close, stringResource(R.string.close)) }
        }
        Column(Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            OutlinedTextField(username, { username = it.take(64) }, label = { Text(stringResource(R.string.username)) }, enabled = !busy,
                singleLine = true, supportingText = { Text(stringResource(R.string.user_username_hint)) }, modifier = Modifier.fillMaxWidth().testTag("user-username"))
            OutlinedTextField(name, { name = it.take(255) }, label = { Text(stringResource(R.string.user_display_name)) }, enabled = !busy,
                singleLine = true, modifier = Modifier.fillMaxWidth().testTag("user-display-name"))
            val options = UserManagementPolicy.roles(actor, baseline).map { it to stringResource(roleLabel(it)) }
            LabeledChoice(stringResource(R.string.user_role), stringResource(roleLabel(role)), options, !busy && options.size > 1, "user-role") { role = it }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(stringResource(R.string.user_active), modifier = Modifier.weight(1f))
                Switch(active, { active = it }, enabled = !busy && (baseline == null || UserManagementPolicy.canDeactivate(actor, baseline)), modifier = Modifier.testTag("user-active"))
            }
            OutlinedTextField(password, { password = it.take(256) }, label = { Text(stringResource(if (baseline == null) R.string.password else R.string.password_unchanged)) },
                supportingText = { Text(stringResource(R.string.user_password_hint)) }, visualTransformation = PasswordVisualTransformation(), singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password), enabled = !busy, modifier = Modifier.fillMaxWidth().testTag("user-password"))
            if (baseline == null || password.isNotEmpty()) {
                OutlinedTextField(confirmation, { confirmation = it.take(256) }, label = { Text(stringResource(R.string.user_confirm_password)) },
                    visualTransformation = PasswordVisualTransformation(), keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password), singleLine = true, enabled = !busy,
                    isError = confirmation.isNotEmpty() && confirmation != password, modifier = Modifier.fillMaxWidth().testTag("user-confirm-password"))
            }
            if (baseline?.id == actor.id && password.isNotEmpty()) OutlinedTextField(currentPassword, { currentPassword = it.take(256) },
                label = { Text(stringResource(R.string.user_current_password)) }, visualTransformation = PasswordVisualTransformation(), keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                singleLine = true, enabled = !busy, modifier = Modifier.fillMaxWidth().testTag("user-current-password"))
            error?.let { Text(stringResource(it), color = MaterialTheme.colorScheme.error) }
            if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            FilledTonalButton(onClick = { if (valid && !busy) save(mutation) }, enabled = valid && !busy,
                modifier = Modifier.fillMaxWidth().height(44.dp).testTag("user-save")) { Text(stringResource(R.string.save)) }
        }
    }
}

internal fun roleLabel(role: String) = when(role) { "owner" -> R.string.user_role_owner; "admin" -> R.string.user_role_admin;
    "operator" -> R.string.user_role_operator; else -> R.string.user_role_viewer }
private fun userError(failure: Exception) = when ((failure as? NasException)?.httpStatus) {
    401 -> R.string.authentication_failed; 403 -> R.string.access_denied; 409 -> R.string.user_duplicate;
    400, 422 -> R.string.user_change_rejected; else -> R.string.server_error
}
