package ru.kmvms.mobile.server

import org.json.JSONArray
import org.json.JSONObject
import ru.kmvms.mobile.data.ApiProblem
import ru.kmvms.mobile.data.NasApi
import ru.kmvms.mobile.data.NasException
import ru.kmvms.mobile.data.NasUser

data class ManagedUser(val id: Long, val username: String, val displayName: String, val role: String, val active: Boolean) {
    companion object { fun from(value: JSONObject) = ManagedUser(value.getLong("id"), value.getString("username"),
        value.optString("display_name"), value.getString("role"), value.getBoolean("is_active")) }
}

/** Mirrors web restrictions for presentation; the existing NAS endpoints remain authoritative. */
object UserManagementPolicy {
    fun allowed(actor: NasUser) = actor.can("manage_users") && actor.role in setOf("owner", "admin")
    fun createRoles(actor: NasUser): List<String> = if (!allowed(actor)) emptyList() else when (actor.role) {
        "owner" -> listOf("admin", "operator", "viewer")
        "admin" -> listOf("operator", "viewer")
        else -> emptyList()
    }
    fun canEdit(actor: NasUser, target: ManagedUser): Boolean = allowed(actor) && when {
        target.id == actor.id -> true
        target.role == "owner" -> false
        actor.role == "owner" -> true
        else -> target.role in setOf("operator", "viewer")
    }
    fun roles(actor: NasUser, target: ManagedUser?) = when {
        target == null -> createRoles(actor)
        !canEdit(actor, target) -> emptyList()
        target.id == actor.id -> listOf(target.role)
        else -> createRoles(actor)
    }
    fun canDeactivate(actor: NasUser, target: ManagedUser) = canEdit(actor, target) && target.id != actor.id && target.role != "owner"
    fun canDelete(actor: NasUser, target: ManagedUser, users: List<ManagedUser>) =
        canDeactivate(actor, target) && users.any { it.id != target.id && it.active && it.role in setOf("owner", "admin") }
}

/** Not a data class: generated toString/copy must never expose passwords. */
class UserMutation(val username: String, val displayName: String, val role: String, val active: Boolean,
                   val password: String = "", val currentPassword: String = "") {
    override fun toString() = "UserMutation([redacted])"
    fun valid(actor: NasUser, baseline: ManagedUser?): Boolean =
        UserManagementPolicy.allowed(actor) && (baseline == null || UserManagementPolicy.canEdit(actor, baseline)) &&
        username.trim().matches(Regex("[A-Za-z0-9_.-]{2,64}")) && displayName.trim().length <= 255 &&
        role in UserManagementPolicy.roles(actor, baseline) &&
        (baseline != null && password.isEmpty() || password.length in 8..256) &&
        (baseline == null || UserManagementPolicy.canDeactivate(actor, baseline) || active == baseline.active) &&
        (baseline?.id != actor.id || password.isEmpty() || currentPassword.isNotEmpty()) && currentPassword.length <= 256
    fun body() = JSONObject().put("username", username.trim()).put("display_name", displayName.trim())
        .put("role", role).put("is_active", active).apply {
            if (password.isNotEmpty()) put("password", password)
            if (currentPassword.isNotEmpty()) put("current_password", currentPassword)
        }
}

class UsersApi(private val api: NasApi, private val actor: NasUser) {
    private fun checkAccess() { if (!UserManagementPolicy.allowed(actor)) throw NasException(ApiProblem.FORBIDDEN) }
    suspend fun list(): List<ManagedUser> {
        checkAccess()
        val values = api.json("api/users") as JSONArray
        return (0 until values.length()).map { ManagedUser.from(values.getJSONObject(it)) }
            .sortedWith(compareBy<ManagedUser> { when(it.role) { "owner" -> 0; "admin" -> 1; "operator" -> 2; else -> 3 } }.thenBy { it.username.lowercase() })
    }
    suspend fun save(baseline: ManagedUser?, mutation: UserMutation): ManagedUser {
        checkAccess(); require(mutation.valid(actor, baseline))
        if (baseline != null) require(baseline.id > 0)
        return ManagedUser.from(api.json(if (baseline == null) "api/users" else "api/users/${baseline.id}",
            if (baseline == null) "POST" else "PATCH", mutation.body()) as JSONObject)
    }
    suspend fun delete(target: ManagedUser, users: List<ManagedUser>) {
        checkAccess()
        if (!UserManagementPolicy.canDelete(actor, target, users)) throw NasException(ApiProblem.FORBIDDEN)
        require(target.id > 0)
        api.json("api/users/${target.id}", "DELETE")
    }
}
