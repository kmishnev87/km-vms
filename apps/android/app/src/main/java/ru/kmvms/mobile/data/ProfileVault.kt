package ru.kmvms.mobile.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Public connection metadata is separate from each encrypted sign-in. Backups are disabled. */
class ProfileVault(context: Context) {
    private val preferences = context.getSharedPreferences("nas_profiles", Context.MODE_PRIVATE)
    private val alias = "kmvms.profile.signin.v1"
    fun profiles(): List<ServerProfile> {
        val stored = JSONArray(preferences.getString("profiles", "[]"))
        return (0 until stored.length()).map { index -> stored.getJSONObject(index).let {
            ServerProfile(it.getString("id"), it.getString("name"), it.getString("address"), it.getString("username"))
        } }
    }
    fun selectedId(): String? = preferences.getString("selected", null)
    internal fun pushState(id: String): JSONObject? {
        val encoded = preferences.getString("push.$id", null) ?: return null
        return runCatching { JSONObject(decrypt(encoded, "push:$id")) }.getOrNull()
    }
    internal fun putPushState(id: String, value: JSONObject) {
        preferences.edit().putString("push.$id", encrypt(value.toString(), "push:$id")).apply()
    }
    internal fun pushSetupAnswered(id: String) = preferences.getBoolean("push_setup.$id", false)
    internal fun answerPushSetup(id: String) { preferences.edit().putBoolean("push_setup.$id", true).apply() }
    fun select(id: String) { preferences.edit().putString("selected", id).apply() }
    fun save(profile: ServerProfile, password: String, access: String) = synchronized(transaction) {
        val profiles = profiles().filterNot { it.id == profile.id } + profile
        val array = JSONArray()
        profiles.forEach { array.put(JSONObject().put("id", it.id).put("name", it.name).put("address", it.address).put("username", it.username)) }
        val encrypted = encrypt(JSONObject().put("password", password).put("access", access).toString(), profile.id)
        preferences.edit().putString("profiles", array.toString()).putString("signin.${profile.id}", encrypted)
            .putString("selected", profile.id).apply()
    }
    fun signIn(id: String): SavedSignIn? {
        val encoded = preferences.getString("signin.$id", null) ?: return null
        return try {
            val plain = JSONObject(decrypt(encoded, id))
            SavedSignIn(plain.getString("password"), plain.getString("access"))
        } catch (_: Exception) { throw NasException(ApiProblem.SAVED_SIGN_IN) }
    }
    fun forgetSignIn(id: String) = synchronized(transaction) { preferences.edit().remove("signin.$id").apply() }
    fun renewAccess(id: String, expected: String, access: String): Boolean = synchronized(transaction) {
        val saved = signIn(id) ?: return@synchronized false
        if (saved.access != expected) return@synchronized false
        val encrypted = encrypt(JSONObject().put("password", saved.password).put("access", access).toString(), id)
        // Renewing an inactive server never switches the selected NAS or revives a signed-out profile.
        preferences.edit().putString("signin.$id", encrypted).apply()
        true
    }
    fun remove(id: String) = synchronized(transaction) {
        val remaining = profiles().filterNot { it.id == id }
        val array = JSONArray()
        remaining.forEach { array.put(JSONObject().put("id", it.id).put("name", it.name).put("address", it.address).put("username", it.username)) }
        val edit = preferences.edit().putString("profiles", array.toString()).remove("signin.$id").remove("push.$id").remove("push_setup.$id")
        if (selectedId() == id) edit.putString("selected", remaining.firstOrNull()?.id)
        edit.apply()
    }
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
            generateKey()
        }
    }
    private fun encrypt(plain: String, profileId: String): String {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()); updateAAD(profileId.toByteArray(Charsets.UTF_8)) }
        return Base64.encodeToString(cipher.iv + cipher.doFinal(plain.toByteArray(Charsets.UTF_8)), Base64.NO_WRAP)
    }
    private fun decrypt(encoded: String, profileId: String): String {
        val bytes = Base64.decode(encoded, Base64.NO_WRAP)
        require(bytes.size > 28)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply {
            init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, bytes.copyOfRange(0, 12)))
            updateAAD(profileId.toByteArray(Charsets.UTF_8))
        }
        return cipher.doFinal(bytes.copyOfRange(12, bytes.size)).toString(Charsets.UTF_8)
    }
    companion object { private val transaction = Any() }
}
