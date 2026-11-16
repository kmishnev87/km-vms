package ru.kmvms.mobile.diagnostics

import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.security.MessageDigest

class ApkReportDeliveryTest {
    @Test fun postsJsonOnlyAndRetryReusesExactPayloadWithoutNasCredentials() = runBlocking {
        val attempt = ApkReportAttempt.create("0.8.4-development", "Notification missing", JSONObject().put("sdk", 31))
        MockWebServer().use { server ->
            val client = OkHttpClient.Builder().followRedirects(false).retryOnConnectionFailure(false).addInterceptor { chain ->
                assertEquals(ApkReportDelivery.DESTINATION, chain.request().url.toString())
                chain.proceed(chain.request().newBuilder().url(server.url("/v1/reports")).build())
            }.build()
            val service = ApkReportDelivery(client)
            server.enqueue(MockResponse().setResponseCode(503))
            assertTrue(runCatching { service.send(attempt) }.isFailure)
            server.enqueue(MockResponse().setResponseCode(201).setBody("{\"accepted\":true,\"report_id\":\"${attempt.id}\"}"))
            assertEquals(attempt.id, service.send(attempt))
            val first = server.takeRequest(); val second = server.takeRequest()
            assertEquals(first.body.clone().readUtf8(), second.body.clone().readUtf8())
            assertEquals(first.getHeader("X-KM-Report-Id"), second.getHeader("X-KM-Report-Id"))
            assertTrue(second.getHeader("Content-Type")!!.startsWith("application/json"))
            assertNull(second.getHeader("Authorization")); assertNull(second.getHeader("Cookie"))
            assertEquals("android_apk", second.getHeader("X-KM-Report-Source"))
            val bytes = second.body.readByteArray()
            val expected = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
            assertEquals(expected, second.getHeader("X-KM-Report-Fingerprint"))
            assertEquals("Notification missing", JSONObject(bytes.toString(Charsets.UTF_8)).getString("comment"))
        }
    }
    @Test fun rejectsWrongReceiptAndRedirectAndOversizedResponse() = runBlocking {
        val attempt = ApkReportAttempt.create("0.8.4-development", "", JSONObject())
        MockWebServer().use { server ->
            val client = OkHttpClient.Builder().followRedirects(false).retryOnConnectionFailure(false)
                .addInterceptor { it.proceed(it.request().newBuilder().url(server.url("/")).build()) }.build()
            for (reply in listOf(MockResponse().setBody("{\"accepted\":true,\"report_id\":\"different\"}"),
                MockResponse().setResponseCode(302).setHeader("Location", "http://example.test/"), MockResponse().setBody("x".repeat(8193)))) {
                server.enqueue(reply)
                assertTrue(runCatching { ApkReportDelivery(client).send(attempt) }.isFailure)
            }
        }
    }
}
