package ru.kmvms.mobile.widget

import android.appwidget.AppWidgetManager
import android.content.ComponentName
import android.content.Context
import org.json.JSONObject
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.*
import java.util.UUID

internal data class AlertWidgetBinding(val profileId: String, val username: String) {
    init { require(profileId.matches(Regex("[A-Za-z0-9_-]{1,128}")) && username.length in 1..256) }
    fun json() = JSONObject().put("profile", profileId).put("username", username)
    fun matches(profile: ServerProfile) = profile.id == profileId && profile.username == username
    companion object {
        fun from(value: JSONObject) = AlertWidgetBinding(value.getString("profile"), value.getString("username"))
    }
}

/** Widget storage contains connection identity only, never a token or password. */
internal class AlertWidgetStore(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences("alert_widgets", Context.MODE_PRIVATE)
    fun binding(id: Int): AlertWidgetBinding? = preferences.getString("widget.$id", null)?.let {
        runCatching { AlertWidgetBinding.from(JSONObject(it)) }.getOrNull()
    }
    fun bind(id: Int, binding: AlertWidgetBinding) {
        require(id > 0)
        preferences.edit().putString("widget.$id", binding.json().toString()).apply()
    }
    fun remove(id: Int) { preferences.edit().remove("widget.$id").apply() }
    fun pending(binding: AlertWidgetBinding, now: Long = System.currentTimeMillis(), variant: WidgetVariant = WidgetVariant.COMPACT): String {
        val edit = preferences.edit()
        // Lazy housekeeping, never a background worker or timer.
        preferences.all.filterKeys { it.startsWith("pin.") }.forEach { (key, stored) ->
            val created = runCatching { JSONObject(stored as String).getLong("created") }.getOrDefault(0)
            if (now - created !in 0..86_400_000) edit.remove(key)
        }
        val nonce = UUID.randomUUID().toString()
        edit.putString("pin.$nonce", JSONObject().put("binding", binding.json()).put("created", now).put("variant", variant.name).toString()).apply()
        return nonce
    }
    fun consume(nonce: String, now: Long = System.currentTimeMillis(), variant: WidgetVariant? = null): AlertWidgetBinding? = synchronized(lock) {
        if (!nonce.matches(Regex("[a-f0-9-]{36}"))) return@synchronized null
        val stored = preferences.getString("pin.$nonce", null) ?: return@synchronized null
        preferences.edit().remove("pin.$nonce").apply()
        runCatching { JSONObject(stored).let {
            require(now - it.getLong("created") in 0..86_400_000)
            require(variant == null || it.optString("variant", WidgetVariant.COMPACT.name) == variant.name)
            AlertWidgetBinding.from(it.getJSONObject("binding"))
        } }.getOrNull()
    }
    companion object { private val lock = Any() }
}

internal enum class WidgetVariant(val provider: Class<out AlertWidgetProvider>) {
    COMPACT(AlertWidgetProvider::class.java), COUNT(AlertCountWidgetProvider::class.java)
}

internal fun widgetVariant(context: Context, id: Int): WidgetVariant? = if (id <= 0) null else
    AppWidgetManager.getInstance(context).getAppWidgetInfo(id)?.provider?.let { provider ->
        WidgetVariant.entries.find { provider == ComponentName(context, it.provider) }
    }

internal fun ownedAlertWidget(context: Context, id: Int) = widgetVariant(context, id) != null
internal fun widgetIds(context: Context, variant: WidgetVariant) = AppWidgetManager.getInstance(context)
    .getAppWidgetIds(ComponentName(context, variant.provider))
internal fun hasCountWidgets(context: Context, profileId: String): Boolean {
    val profile = ProfileVault(context).profiles().find { it.id == profileId } ?: return false
    val store = AlertWidgetStore(context)
    return widgetIds(context, WidgetVariant.COUNT).any { store.binding(it)?.matches(profile) == true }
}

internal fun alertWidgetClient(context: Context, binding: AlertWidgetBinding): NasApi {
    val vault = ProfileVault(context.applicationContext)
    val profile = vault.profiles().firstOrNull(binding::matches) ?: throw NasException(ApiProblem.SAVED_SIGN_IN)
    val saved = vault.signIn(profile.id) ?: throw NasException(ApiProblem.AUTHENTICATION)
    return NasApi(ServerAddress.parse(profile.address)).apply {
        useSession(saved.access)
        sessionUser(profile.username)
        allowSessionRenewal({ vault.profiles().firstOrNull(binding::matches)?.let { vault.signIn(it.id) } },
            { expected, next -> vault.profiles().any(binding::matches) && vault.renewAccess(profile.id, expected, next) })
    }
}
