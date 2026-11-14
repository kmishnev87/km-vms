package ru.kmvms.mobile.archive

import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.os.Environment

/** Android owns the long download; NAS credentials never enter DownloadManager. */
suspend fun downloadClip(context: Context, api: ArchiveApi, clip: Clip): Long {
    if (!clip.canDownload) api.manifest(clip.id)
    val value = api.downloadToken(clip.id)
    val filename = value.getString("filename")
    require(filename.isNotBlank() && '/' !in filename && '\\' !in filename && filename.none { it.code < 32 })
    val url = api.api.address.endpoint("api/archive/exports/${clip.id}/file?media_token=${encode(value.getString("media_token"))}")
    val request = DownloadManager.Request(Uri.parse(url.toString())).setTitle(filename).setDescription("KM VMS")
        .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
        .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, filename)
        .setAllowedOverMetered(true).setAllowedOverRoaming(false)
    return context.getSystemService(DownloadManager::class.java).enqueue(request)
}
