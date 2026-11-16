package ru.kmvms.mobile.widget

import android.appwidget.AppWidgetManager
import android.content.Context
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.*
import androidx.core.app.NotificationManagerCompat
import ru.kmvms.mobile.AppLocales
import ru.kmvms.mobile.data.ApiProblem
import ru.kmvms.mobile.data.NasException
import ru.kmvms.mobile.data.ProfileVault
import ru.kmvms.mobile.events.EventsApi
import ru.kmvms.mobile.push.PushStatus
import ru.kmvms.mobile.push.PushStore
import ru.kmvms.mobile.ui.KMVMSTheme

class AlertWidgetActivity : ComponentActivity() {
    private var controls: AlertWidgetControls? = null
    override fun attachBaseContext(newBase: Context) = super.attachBaseContext(AppLocales.wrap(newBase))
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val id = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID)
        if (!ownedAlertWidget(this, id)) { finish(); return }
        enableEdgeToEdge()
        val binding = AlertWidgetStore(this).binding(id)
        val profile = binding?.let { ProfileVault(this).profiles().firstOrNull(it::matches) }
        val failure = try {
            if (binding == null || profile == null) throw NasException(ApiProblem.SAVED_SIGN_IN)
            controls = AlertWidgetControls(NasAlertChoices(EventsApi(alertWidgetClient(this, binding))))
            null
        } catch (error: NasException) { error.problem }
        catch (_: Exception) { ApiProblem.SAVED_SIGN_IN }
        val phoneReady = profile != null && NotificationManagerCompat.from(this).areNotificationsEnabled() &&
            PushStore(this).binding(profile.id)?.let { it.enabled && it.status == PushStatus.READY } == true
        setContent {
            KMVMSTheme {
                val controller = controls
                val state = controller?.state?.collectAsState()?.value ?: AlertWidgetState(loading = false, problem = failure)
                AlertWidgetPopup(profile?.name.orEmpty(), state, phoneReady,
                    { if (controller == null) recreate() else controller.refresh() },
                    { controller?.master(it) }, { camera, checked -> controller?.camera(camera, checked) }, ::finish)
            }
        }
        controls?.refresh()
    }
    override fun onDestroy() { controls?.close(); controls = null; super.onDestroy() }
}

/** Standard launcher configuration is supported as a fallback to in-app pinning. */
class AlertWidgetConfigureActivity : ComponentActivity() {
    override fun attachBaseContext(newBase: Context) = super.attachBaseContext(AppLocales.wrap(newBase))
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setResult(RESULT_CANCELED)
        val id = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID)
        if (intent.action != AppWidgetManager.ACTION_APPWIDGET_CONFIGURE || !ownedAlertWidget(this, id)) { finish(); return }
        enableEdgeToEdge()
        val vault = ProfileVault(this)
        setContent { KMVMSTheme {
            AlertWidgetProfileDialog(vault.profiles(), vault.selectedId(), { profile ->
                if (runCatching { vault.signIn(profile.id) }.getOrNull() == null) false else {
                    AlertWidgetStore(this).bind(id, AlertWidgetBinding(profile.id, profile.username))
                    AlertWidgetProvider.render(this, id)
                    setResult(RESULT_OK, Intent().putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id))
                    finish(); true
                }
            }, ::finish)
        } }
    }
}
