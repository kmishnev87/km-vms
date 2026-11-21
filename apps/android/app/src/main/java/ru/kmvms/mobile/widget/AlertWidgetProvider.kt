package ru.kmvms.mobile.widget

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.widget.RemoteViews
import ru.kmvms.mobile.AppLocales
import ru.kmvms.mobile.R
import ru.kmvms.mobile.data.ProfileVault
import ru.kmvms.mobile.MainActivity
import ru.kmvms.mobile.push.NotificationNavigation

/** No network, boot work, camera streams or periodic refresh in a home-screen widget. */
open class AlertWidgetProvider : AppWidgetProvider() {
    override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) = ids.forEach { render(context, it) }
    override fun onDeleted(context: Context, ids: IntArray) {
        val store = AlertWidgetStore(context)
        val profiles = ids.map { store.binding(it)?.profileId }.filterNotNull().distinct()
        ids.forEach { store.remove(it) }
        profiles.forEach { WidgetUnreadSync.enqueue(context, it) }
    }
    override fun onReceive(context: Context, intent: Intent) {
        super.onReceive(context, intent)
        if (intent.action == Intent.ACTION_LOCALE_CHANGED) refresh(context)
    }
    companion object {
        fun refresh(context: Context) {
            widgetIds(context).forEach { render(context, it) }
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
            val views = RemoteViews(context.packageName, R.layout.alert_widget_count).apply {
                setTextViewText(R.id.alert_widget_server, profile?.name ?: localized.getString(R.string.widget_configure))
                setContentDescription(R.id.alert_widget_settings, localized.getString(R.string.widget_open_for, profile?.name.orEmpty()))
                setOnClickPendingIntent(R.id.alert_widget_settings, click)
                val signedIn = profile != null && runCatching { ProfileVault(context).signIn(profile.id) }.getOrNull() != null
                val value = profile?.takeIf { signedIn }?.let { WidgetUnreadStore(context).get(it)?.count }
                setTextViewText(R.id.alert_widget_count, value?.coerceAtMost(99)?.toString() ?: "—")
                setViewVisibility(R.id.alert_widget_count_plus, if (value != null && value > 99) android.view.View.VISIBLE else android.view.View.INVISIBLE)
                setContentDescription(R.id.alert_widget_count_area, if (value == null) localized.getString(R.string.widget_count_unavailable)
                    else localized.getString(R.string.widget_unread_count, value))
                setContentDescription(R.id.alert_widget_live, localized.getString(R.string.live))
                setOnClickPendingIntent(R.id.alert_widget_live, if (configured) destinationClick(context, id, OPEN_LIVE, "live") else click)
                setOnClickPendingIntent(R.id.alert_widget_count_area, if (configured) destinationClick(context, id, OPEN_EVENTS, "events") else click)
            }
            AppWidgetManager.getInstance(context).updateAppWidget(id, views)
        }
        internal const val OPEN_EVENTS = "ru.kmvms.mobile.WIDGET_EVENTS"
        internal const val OPEN_LIVE = "ru.kmvms.mobile.WIDGET_LIVE"

        private fun destinationClick(context: Context, id: Int, action: String, host: String): PendingIntent {
            val intent = Intent(context, MainActivity::class.java).setAction(action)
                .setData(Uri.parse("kmvmswidget://$host/$id"))
                .putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            return PendingIntent.getActivity(context, id, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        }

        internal fun openDestination(context: Context, intent: Intent?): Boolean {
            intent ?: return false
            val host = when (intent.action) {
                OPEN_EVENTS -> "events"
                OPEN_LIVE -> "live"
                else -> return false
            }
            val data = intent.data ?: return false
            if (data.scheme != "kmvmswidget" || data.host != host || data.query != null || data.fragment != null) return false
            val id = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, -1)
            if (!ownedAlertWidget(context, id) || data.path != "/$id") return false
            val binding = AlertWidgetStore(context).binding(id) ?: return false
            if (ProfileVault(context).profiles().none(binding::matches)) return false
            if (host == "live") NotificationNavigation.live(binding.profileId) else NotificationNavigation.events(binding.profileId)
            return true
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
        return manager.requestPinAppWidget(alertWidgetComponent(context), null, callback)
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
        WidgetUnreadSync.enqueue(context, binding.profileId)
    }
}

/** Keep the original counter provider ID so an already pinned counter keeps its profile. */
class AlertCountWidgetProvider : AlertWidgetProvider()
