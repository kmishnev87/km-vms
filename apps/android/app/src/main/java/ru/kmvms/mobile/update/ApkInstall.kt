package ru.kmvms.mobile.update

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import java.io.File
import java.security.MessageDigest

/** The APK must match this installed app, not just an editable hash in public metadata. */
@Suppress("DEPRECATION")
fun verifyApk(context: Context, file: File, release: AndroidRelease) {
    require(context.packageName == AndroidRelease.PACKAGE)
    val manager = context.packageManager
    val flags = if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES
    val candidate = manager.getPackageArchiveInfo(file.absolutePath, flags) ?: error("android_update_apk_invalid")
    val installed = manager.getPackageInfo(context.packageName, flags)
    require(candidate.packageName == context.packageName)
    val candidateVersion = if (Build.VERSION.SDK_INT >= 28) candidate.longVersionCode else candidate.versionCode.toLong()
    val installedVersion = if (Build.VERSION.SDK_INT >= 28) installed.longVersionCode else installed.versionCode.toLong()
    require(candidateVersion == release.code && candidateVersion > installedVersion && candidate.versionName == release.version)
    fun certificates(info: android.content.pm.PackageInfo): Set<String> {
        val signatures = if (Build.VERSION.SDK_INT >= 28) info.signingInfo?.apkContentsSigners else info.signatures
        return signatures.orEmpty().map { MessageDigest.getInstance("SHA-256").digest(it.toByteArray()).hex() }.toSet()
    }
    val expected = certificates(installed)
    require(expected.size == 1 && certificates(candidate) == expected && release.certificate in expected)
    require(candidate.applicationInfo?.minSdkVersion?.let { it <= Build.VERSION.SDK_INT } == true)
}

fun launchApkInstaller(context: Context, file: File): Boolean {
    if (!context.packageManager.canRequestPackageInstalls()) {
        context.startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${context.packageName}"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        return false
    }
    val uri = FileProvider.getUriForFile(context, "${context.packageName}.updates", file)
    context.startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK))
    return true
}
