package ru.kmvms.mobile.archive

import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.os.Environment

/** Android owns the long download; NAS credentials never enter DownloadManager. */
suspend fun downloadClip(context: Context, api: ArchiveApi, clip: Clip): Long {
    if (!clip.canDownload) api.manifest(clip.id)
    val value = api.downloadToken(clip.id)
    val url = api.api.address.endpoint("api/archive/exports/${clip.id}/file?media_token=${encode(value.getString("media_token"))}")
    return enqueueArchiveDownload(context, url.toString(), value.getString("filename"))
}

suspend fun downloadRecording(context: Context, api: ArchiveApi, recording: Recording): Long {
    require(recording.canDownload)
    val lease = api.authorizeDownload(recording)
    val url = api.api.address.endpoint("api/recordings/download?${recording.query()}&media_token=${encode(lease.token)}")
    return enqueueArchiveDownload(context, url.toString(), recording.filename)
}

private fun enqueueArchiveDownload(context: Context, url: String, filename: String): Long {
    require(filename.isNotBlank() && filename !in setOf(".", "..") &&
        '/' !in filename && '\\' !in filename && filename.none { it.code < 32 })
    val request = DownloadManager.Request(Uri.parse(url)).setTitle(filename).setDescription("KM VMS")
        .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
        .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, filename)
        .setAllowedOverMetered(true).setAllowedOverRoaming(false)
    return context.getSystemService(DownloadManager::class.java).enqueue(request)
}
