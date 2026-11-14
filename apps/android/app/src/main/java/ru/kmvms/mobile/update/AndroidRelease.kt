package ru.kmvms.mobile.update

import org.json.JSONObject

/** Public product metadata; no NAS address, account, token, or private signing material. */
data class AndroidRelease(
    val version: String,
    val code: Long,
    val tag: String,
    val bytes: Long,
    val sha256: String,
    val certificate: String,
    val minSdk: Int,
) {
    val filename get() = "km-vms-android-$version.apk"
    val download get() = "https://github.com/kmishnev87/km-vms/releases/download/$tag/$filename"
    fun json() = JSONObject().put("format", 1).put("published", true).put("package_name", PACKAGE)
        .put("version_name", version).put("version_code", code).put("release_tag", tag)
        .put("size_bytes", bytes).put("sha256", sha256).put("certificate_sha256", certificate)
        .put("min_android_sdk", minSdk)

    companion object {
        const val PACKAGE = "ru.kmvms.mobile"
        const val METADATA = "https://raw.githubusercontent.com/kmishnev87/km-vms/main/apps/android/release.json"
        private val versionPattern = Regex("[0-9]{1,5}\\.[0-9]{1,5}\\.[0-9]{1,5}")
        private val hashPattern = Regex("[a-f0-9]{64}")
        fun from(value: JSONObject): AndroidRelease? {
            require(value.getInt("format") == 1 && value.getString("package_name") == PACKAGE)
            if (!value.getBoolean("published")) return null
            val version = value.getString("version_name").also { require(versionPattern.matches(it)) }
            val tag = value.getString("release_tag").also {
                require(it.matches(Regex("(?:android-)?v[0-9]{1,5}\\.[0-9]{1,5}\\.[0-9]{1,5}")))
            }
            val code = value.getLong("version_code").also { require(it in 1..2_100_000_000L) }
            val bytes = value.getLong("size_bytes").also { require(it in 1..256L * 1024 * 1024) }
            val sha = value.getString("sha256").also { require(hashPattern.matches(it)) }
            val certificate = value.getString("certificate_sha256").also { require(hashPattern.matches(it)) }
            val minSdk = value.getInt("min_android_sdk").also { require(it in 26..100) }
            return AndroidRelease(version, code, tag, bytes, sha, certificate, minSdk)
        }
    }
}
