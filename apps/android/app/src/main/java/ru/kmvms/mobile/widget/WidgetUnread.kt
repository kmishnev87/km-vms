package ru.kmvms.mobile.widget

import android.content.Context
import org.json.JSONObject
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.ProfileVault
import ru.kmvms.mobile.data.ServerProfile
import java.util.UUID

internal data class WidgetUnread(val epoch: String, val revision: Long, val count: Int) {
    fun json() = JSONObject().put("v", 1).put("type", "unread_badge").put("epoch", epoch).put("revision", revision).put("count", count)
    companion object {
        fun parse(value: JSONObject): WidgetUnread {
            require(value.getInt("v") == 1 && value.getString("type") == "unread_badge")
            val epoch = value.getString("epoch").also { require(UUID.fromString(it).toString() == it) }
            val revision = value.getLong("revision").also { require(it >= 1) }
            val count = value.getLong("count").also { require(it in 0..Int.MAX_VALUE.toLong()) }
            return WidgetUnread(epoch, revision, count.toInt())
        }
        fun parse(bytes: ByteArray): WidgetUnread? = runCatching {
            require(bytes.size in 1..512)
            parse(JSONObject(bytes.toString(Charsets.UTF_8)))
        }.getOrNull()
    }
}

/** No credentials. A cache belongs to one saved NAS address AND account. */
internal class WidgetUnreadStore(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences("widget_unread", Context.MODE_PRIVATE)
    private fun identity(profile: ServerProfile) = ServerAddress.parse(profile.address).endpoint("").toString() + "\n" + profile.username
    private fun saved(profile: ServerProfile): JSONObject? = preferences.getString(profile.id, null)?.let { raw ->
        runCatching { JSONObject(raw).takeIf { it.getString("identity") == identity(profile) } }.getOrNull()
    }
    fun get(profile: ServerProfile): WidgetUnread? = synchronized(lock) {
        saved(profile)?.let { runCatching { WidgetUnread.parse(it.getJSONObject("snapshot")) }.getOrNull() }
    }
    fun clear(profileId: String) = synchronized(lock) { preferences.edit().remove(profileId).apply() }
    /** Only an authenticated NAS response may establish/change a database epoch. */
    fun install(profile: ServerProfile, value: WidgetUnread): Boolean = synchronized(lock) {
        val old = get(profile)
        if (old?.epoch == value.epoch && old.revision > value.revision) return@synchronized false
        preferences.edit().putString(profile.id, JSONObject().put("identity", identity(profile))
            .put("snapshot", value.json()).toString()).apply()
        true
    }
    fun receive(profile: ServerProfile, value: WidgetUnread): Boolean = synchronized(lock) {
        val old = get(profile) ?: return@synchronized false
        if (old.epoch != value.epoch || old.revision >= value.revision) return@synchronized false
        install(profile, value)
    }
    companion object { private val lock = Any() }
}

/** The silent packet never enters Android's notification manager. */
internal fun receiveWidgetUnread(context: Context, profile: ServerProfile, value: WidgetUnread): Boolean {
    if (!hasCountWidgets(context, profile.id) || ProfileVault(context).signIn(profile.id) == null) return false
    val accepted = WidgetUnreadStore(context).receive(profile, value)
    if (accepted) AlertWidgetProvider.refresh(context)
    return accepted
}
