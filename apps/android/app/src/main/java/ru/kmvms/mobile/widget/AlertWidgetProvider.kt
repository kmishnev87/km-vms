package ru.kmvms.mobile.widget

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.widget.RemoteViews
import ru.kmvms.mobile.AppLocales
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.ProfileVault

/** No network, boot work, camera streams or periodic refresh in a home-screen widget. */
class AlertWidgetProvider : AppWidgetProvider() {
    override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) = ids.forEach { render(context, it) }
    override fun onDeleted(context: Context, ids: IntArray) = ids.forEach { AlertWidgetStore(context).remove(it) }
    override fun onReceive(context: Context, intent: Intent) {
        super.onReceive(context, intent)
        if (intent.action == Intent.ACTION_LOCALE_CHANGED) refresh(context)
    }
    companion object {
        fun refresh(context: Context) {
            AppWidgetManager.getInstance(context).getAppWidgetIds(ComponentName(context, AlertWidgetProvider::class.java)).forEach { render(context, it) }
        }
        internal fun render(context: Context, id: Int) {
            if (!ownedAlertWidget(context, id)) return
            val language = AppLocales.selected(context)
            val localized = if (language.isNotEmpty()) AppLocales.localizedContext(context, language) else context
            val binding = AlertWidgetStore(context).binding(id)
            val profile = binding?.let { ProfileVault(context).profiles().firstOrNull(it::matches) }
            val configured = profile != null
            val target = if (configured) AlertWidgetActivity::class.java else AlertWidgetConfigureActivity::class.java
            val intent = Intent(context, target).setAction(if (configured) "ru.kmvms.mobile.OPEN_ALERT_WIDGET" else AppWidgetManager.ACTION_APPWIDGET_CONFIGURE)
                .setData(Uri.parse("kmvmswidget://open/$id"))
                .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            val click = PendingIntent.getActivity(context, id, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
            val views = RemoteViews(context.packageName, R.layout.alert_widget).apply {
                setTextViewText(R.id.alert_widget_title, localized.getString(R.string.widget_alerts))
                setTextViewText(R.id.alert_widget_server, profile?.name ?: localized.getString(R.string.widget_configure))
                setContentDescription(R.id.alert_widget_root, localized.getString(R.string.widget_open_for, profile?.name.orEmpty()))
                setOnClickPendingIntent(R.id.alert_widget_root, click)
            }
            AppWidgetManager.getInstance(context).updateAppWidget(id, views)
        }
    }
}

internal object AlertWidgetPin {
    fun supported(context: Context) = AppWidgetManager.getInstance(context).isRequestPinAppWidgetSupported
    fun request(context: Context, profileId: String): Boolean {
        val vault = ProfileVault(context)
        val profile = vault.profiles().firstOrNull { it.id == profileId } ?: return false
        if (vault.signIn(profileId) == null) return false
        val manager = AppWidgetManager.getInstance(context)
        if (!manager.isRequestPinAppWidgetSupported) return false
        val nonce = AlertWidgetStore(context).pending(AlertWidgetBinding(profile.id, profile.username))
        val callback = PendingIntent.getBroadcast(context, 0,
            Intent(context, AlertWidgetPinnedReceiver::class.java).setData(Uri.parse("kmvmswidget://pin/$nonce")),
            // Explicit private receiver + one-use nonce. The launcher must fill in the allocated widget ID.
            PendingIntent.FLAG_ONE_SHOT or PendingIntent.FLAG_MUTABLE)
        return manager.requestPinAppWidget(ComponentName(context, AlertWidgetProvider::class.java), null, callback)
    }
}

class AlertWidgetPinnedReceiver : android.content.BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.data?.scheme != "kmvmswidget" || intent.data?.host != "pin") return
        val id = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID)
        if (!ownedAlertWidget(context, id)) return
        val binding = AlertWidgetStore(context).consume(intent.data?.lastPathSegment.orEmpty()) ?: return
        if (!ProfileVault(context).profiles().any(binding::matches)) return
        AlertWidgetStore(context).bind(id, binding)
        AlertWidgetProvider.render(context, id)
    }
}
