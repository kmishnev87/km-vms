package ru.kmvms.mobile.media

import android.content.Context
import org.json.JSONArray

/** Non-sensitive display preference, isolated by the saved NAS/account profile. */
class LiveViewPreferences(context: Context) {
    private val preferences = context.getSharedPreferences("nas_profiles", Context.MODE_PRIVATE)

    fun load(profileId: String): List<Long>? {
        val encoded = preferences.getString("live_order.$profileId", null) ?: return null
        return runCatching {
            val array = JSONArray(encoded)
            List(array.length()) { array.getLong(it) }.filter { it > 0 }.distinct()
        }.getOrNull()
    }

    fun save(profileId: String, ids: List<Long>) {
        preferences.edit().putString("live_order.$profileId", JSONArray(ids.filter { it > 0 }.distinct()).toString()).apply()
    }
}
