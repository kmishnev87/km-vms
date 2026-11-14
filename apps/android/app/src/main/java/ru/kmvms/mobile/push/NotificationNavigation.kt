package ru.kmvms.mobile.push

import android.content.Intent
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import ru.kmvms.mobile.core.EventAction
import java.net.URI
import java.util.UUID

data class NotificationOpen(val profileId: String, val eventId: String, val action: EventAction, val key: String = UUID.randomUUID().toString())

object NotificationNavigation {
    private val mutable = MutableStateFlow<NotificationOpen?>(null)
    val pending = mutable.asStateFlow()
    fun accept(intent: Intent?) { parse(intent?.dataString)?.let { mutable.value = it } }
    fun consume(key: String) { if (mutable.value?.key == key) mutable.value = null }
    fun parse(value: String?): NotificationOpen? = runCatching {
        val uri = URI(value ?: return null)
        require(uri.scheme == "kmvms" && uri.rawAuthority == "notification" && uri.query == null && uri.fragment == null)
        val parts = uri.path.split('/').filter(String::isNotBlank)
        require(parts.size == 3)
        parts.take(2).forEach { require(it.length == 36 && UUID.fromString(it).toString() == it.lowercase()) }
        NotificationOpen(parts[0], parts[1], when (parts[2]) { "open" -> EventAction.OPEN; "live" -> EventAction.LIVE; "archive" -> EventAction.RECORDING; else -> error("action") })
    }.getOrNull()
}
