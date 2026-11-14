package ru.kmvms.mobile.update

import okhttp3.HttpUrl.Companion.toHttpUrl
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class AndroidReleaseTest {
    private fun metadata() = JSONObject().put("format", 1).put("published", true).put("package_name", AndroidRelease.PACKAGE)
        .put("version_name", "0.8.1").put("version_code", 801).put("release_tag", "v0.9.16").put("size_bytes", 1234)
        .put("sha256", "ab".repeat(32)).put("certificate_sha256", "cd".repeat(32)).put("min_android_sdk", 26)
    @Test fun independentVersionAndFixedOfficialUrl() {
        val release = AndroidRelease.from(metadata())!!
        assertEquals(801, release.code.toInt())
        assertEquals("https://github.com/kmishnev87/km-vms/releases/download/v0.9.16/km-vms-android-0.8.1.apk", release.download)
        assertEquals(release, AndroidRelease.from(release.json()))
    }
    @Test fun unpublishedIsNotReportedAsCurrent() { assertNull(AndroidRelease.from(metadata().put("published", false))) }
    @Test fun hostileOrMalformedMetadataRejected() {
        for ((key, value) in listOf("package_name" to "someone.else", "version_name" to "../../bad", "release_tag" to "v1/../../bad",
            "sha256" to "", "certificate_sha256" to "not-a-certificate", "version_code" to 0, "size_bytes" to 300L * 1024 * 1024, "min_android_sdk" to 25)) {
            assertTrue(key, runCatching { AndroidRelease.from(metadata().put(key, value)) }.isFailure)
        }
    }
    @Test fun redirectsStayOnOfficialEncryptedArtifactHosts() {
        for (url in listOf("https://github.com/kmishnev87/km-vms/releases/download/v1.0.0/app.apk",
            "https://release-assets.githubusercontent.com/github-production-release-asset/1/file?sig=example"))
            assertTrue(trustedArtifactUrl(url.toHttpUrl()))
        for (url in listOf("http://github.com/kmishnev87/km-vms/releases/download/v1/app.apk", "https://evil.example/app.apk",
            "https://github.com/other/repo/releases/download/v1/app.apk", "https://user@github.com/kmishnev87/km-vms/releases/download/v1/app.apk",
            "https://github.com:8443/kmishnev87/km-vms/releases/download/v1/app.apk", "https://github.com.evil.example/app.apk"))
            assertFalse(trustedArtifactUrl(url.toHttpUrl()))
    }
}
