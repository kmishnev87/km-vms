package ru.kmvms.mobile.update

import kotlinx.coroutines.runBlocking
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okio.*
import org.junit.Assert.*
import org.junit.Test
import java.nio.file.Files
import java.security.MessageDigest

/** Slow synthetic network data at the fixed official URL; no real APK installation. */
class OfficialDownloadProgressTest {
    @Test fun aSlowTransferReportsIntermediateBytesAndOnlyPublishesTheVerifiedCompleteFile() = runBlocking {
        val content=ByteArray(1024*1024){(it%251).toByte()}
        val updates=mutableListOf<Long>()
        val directory=Files.createTempDirectory("kmvms-apk-progress-").toFile()
        val destination=directory.resolve("verified.apk")
        try {
            val release=release(content)
            source(content).download(release,destination){updates.add(it)}
            assertTrue(updates.size>10)
            assertTrue(updates.zipWithNext().all{(left,right)->right>left})
            assertTrue(updates.any{it>0&&it<content.size/2})
            assertEquals(content.size.toLong(),updates.last())
            assertArrayEquals(content,destination.readBytes())
            assertFalse(directory.resolve("update.part").exists())
        } finally { destination.delete();directory.resolve("update.part").delete();directory.delete() }
    }

    @Test fun corruptAndTruncatedDownloadsNeverBecomeInstallable() = runBlocking {
        val expected=ByteArray(100_000){(it%251).toByte()}
        val directory=Files.createTempDirectory("kmvms-apk-invalid-").toFile()
        try {
            for(content in listOf(ByteArray(expected.size),expected.copyOf(expected.size/2))) {
                val destination=directory.resolve("verified.apk")
                assertTrue(runCatching{source(content).download(release(expected),destination){}}.isFailure)
                assertFalse(destination.exists());assertFalse(directory.resolve("update.part").exists())
            }
        } finally { directory.listFiles()?.forEach{it.delete()};directory.delete() }
    }

    private fun release(bytes:ByteArray)=AndroidRelease("0.8.3",803,"v0.9.19",bytes.size.toLong(),
        MessageDigest.getInstance("SHA-256").digest(bytes).hex(),"cd".repeat(32),26)

    private fun source(bytes:ByteArray):OfficialAndroidSource {
        val client=OkHttpClient.Builder().addInterceptor{chain->
            assertTrue(trustedArtifactUrl(chain.request().url))
            val body=object:ResponseBody(){
                override fun contentType()="application/vnd.android.package-archive".toMediaTypeOrNull()
                override fun contentLength()=-1L
                override fun source():BufferedSource=object:ForwardingSource(Buffer().write(bytes)){
                    override fun read(sink:Buffer,byteCount:Long):Long {
                        Thread.sleep(4)
                        return super.read(sink,minOf(byteCount,8192))
                    }
                }.buffer()
            }
            Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1).code(200).message("OK").body(body).build()
        }.build()
        return OfficialAndroidSource(client)
    }
}
