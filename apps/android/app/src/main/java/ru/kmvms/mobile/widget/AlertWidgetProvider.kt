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
import ru.kmvms.mobile.MainActivity

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
            WidgetVariant.entries.forEach { variant -> widgetIds(context, variant).forEach { render(context, it) } }
        }
        internal fun render(context: Context, id: Int) {
            val variant = widgetVariant(context, id) ?: return
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
            val views = RemoteViews(context.packageName, if (variant == WidgetVariant.COUNT) R.layout.alert_widget_count else R.layout.alert_widget).apply {
                setTextViewText(R.id.alert_widget_title, localized.getString(R.string.widget_alerts))
                setTextViewText(R.id.alert_widget_server, profile?.name ?: localized.getString(R.string.widget_configure))
                setContentDescription(R.id.alert_widget_root, localized.getString(R.string.widget_open_for, profile?.name.orEmpty()))
                setOnClickPendingIntent(R.id.alert_widget_root, click)
                if (variant == WidgetVariant.COUNT) {
                    val signedIn = profile != null && runCatching { ProfileVault(context).signIn(profile.id) }.getOrNull() != null
                    val value = profile?.takeIf { signedIn }?.let { WidgetUnreadStore(context).get(it)?.count }
                    setTextViewText(R.id.alert_widget_count, value?.toString() ?: "—")
                    setContentDescription(R.id.alert_widget_count, if (value == null) localized.getString(R.string.widget_count_unavailable)
                        else localized.getString(R.string.widget_unread_count, value))
                    val events = Intent(context, MainActivity::class.java).setAction(OPEN_EVENTS)
                        .setData(Uri.parse("kmvmswidget://events/$id")).putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
                    setOnClickPendingIntent(R.id.alert_widget_count, if (configured)
                        PendingIntent.getActivity(context, id, events, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE) else click)
                }
            }
            AppWidgetManager.getInstance(context).updateAppWidget(id, views)
        }
        internal const val OPEN_EVENTS = "ru.kmvms.mobile.WIDGET_EVENTS"
        internal fun openEvents(context: Context, intent: Intent?): Boolean {
            if (intent?.action != OPEN_EVENTS || intent.data?.scheme != "kmvmswidget" || intent.data?.host != "events") return false
            val id = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, -1)
            if (widgetVariant(context, id) != WidgetVariant.COUNT || intent.data?.path != "/$id") return false
            val binding = AlertWidgetStore(context).binding(id) ?: return false
            if (ProfileVault(context).profiles().none(binding::matches)) return false
            ru.kmvms.mobile.push.NotificationNavigation.events(binding.profileId)
            return true
        }
    }
}

internal object AlertWidgetPin {
    fun supported(context: Context) = AppWidgetManager.getInstance(context).isRequestPinAppWidgetSupported
    fun request(context: Context, profileId: String, variant: WidgetVariant = WidgetVariant.COMPACT): Boolean {
        val vault = ProfileVault(context)
        val profile = vault.profiles().firstOrNull { it.id == profileId } ?: return false
        if (vault.signIn(profileId) == null) return false
        val manager = AppWidgetManager.getInstance(context)
        if (!manager.isRequestPinAppWidgetSupported) return false
        val nonce = AlertWidgetStore(context).pending(AlertWidgetBinding(profile.id, profile.username), variant = variant)
        val callback = PendingIntent.getBroadcast(context, 0,
            Intent(context, AlertWidgetPinnedReceiver::class.java).setData(Uri.parse("kmvmswidget://pin/$nonce")),
            // Explicit private receiver + one-use nonce. The launcher must fill in the allocated widget ID.
            PendingIntent.FLAG_ONE_SHOT or PendingIntent.FLAG_MUTABLE)
        return manager.requestPinAppWidget(ComponentName(context, variant.provider), null, callback)
    }
}

class AlertWidgetPinnedReceiver : android.content.BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.data?.scheme != "kmvmswidget" || intent.data?.host != "pin") return
        val id = intent.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, AppWidgetManager.INVALID_APPWIDGET_ID)
        val variant = widgetVariant(context, id) ?: return
        val binding = AlertWidgetStore(context).consume(intent.data?.lastPathSegment.orEmpty(), variant = variant) ?: return
        if (!ProfileVault(context).profiles().any(binding::matches)) return
        AlertWidgetStore(context).bind(id, binding)
        AlertWidgetProvider.render(context, id)
        if (variant == WidgetVariant.COUNT) WidgetUnreadSync.enqueue(context, binding.profileId)
    }
}

/** Separate provider keeps both sizes selectable and preserves existing widgets. */
class AlertCountWidgetProvider : AlertWidgetProvider()
