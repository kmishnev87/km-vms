package ru.kmvms.mobile.data

import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.launch
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.async
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.RecordedRequest
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.core.ServerAddress

class NasApiTest {
    @Test fun foregroundEventLongPollHasABoundedDeadlineBeyondTheNasWait() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"items":[],"cursor":"fixture"}"""))
            val api = client(server).apply { useSession("fixture") }
            assertEquals(35_000, api.longPollClient.readTimeoutMillis)
            assertEquals(40_000, api.longPollClient.callTimeoutMillis)
            assertFalse(api.longPollClient.followRedirects)
            api.longPoll("api/camera-events/updates?wait=25")
            assertEquals("/api/camera-events/updates?wait=25", server.takeRequest().path)
        }
    }
    private fun client(server: MockWebServer) = NasApi(ServerAddress.parse(server.url("/").toString()))
    private fun meResponse() = MockResponse().setBody("""{"id":1,"username":"fixture","full_name":"Fixture","role":"viewer","permissions":["view_live"]}""")
    @Test fun shortJsonResponseAndBearerLoginWork() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"access_token":"fixture-session"}"""))
            server.enqueue(meResponse())
            val api = client(server)
            api.login("fixture", "fixture-only-password")
            assertTrue(api.me().can("view_live"))
            val login = server.takeRequest()
            assertNull(login.getHeader("Authorization"))
            assertEquals("fixture", JSONObject(login.body.readUtf8()).getString("username"))
            assertEquals("Bearer fixture-session", server.takeRequest().getHeader("Authorization"))
        }
    }
    @Test fun profilesOnTheSameHostNeverShareCookies() = runBlocking {
        MockWebServer().use { first -> MockWebServer().use { second ->
            first.enqueue(MockResponse().setHeader("Set-Cookie", "fixture_browser=first; Path=/; HttpOnly").setBody("""{"access_token":"first-session"}"""))
            second.enqueue(MockResponse().setBody("""{"access_token":"second-session"}"""))
            first.enqueue(meResponse()); second.enqueue(meResponse())
            val apiA = client(first); val apiB = client(second)
            apiA.login("fixture", "dummy"); apiB.login("fixture", "dummy")
            apiA.me(); apiB.me()
            first.takeRequest(); second.takeRequest()
            assertEquals("fixture_browser=first", first.takeRequest().getHeader("Cookie"))
            assertNull(second.takeRequest().getHeader("Cookie"))
        } }
    }
    @Test fun redirectsDoNotForwardSignInToAnotherOrigin() = runBlocking {
        MockWebServer().use { server -> MockWebServer().use { other ->
            server.enqueue(MockResponse().setResponseCode(307).setHeader("Location", other.url("/unexpected")))
            val problem = try { client(server).login("fixture", "dummy"); null } catch (error: NasException) { error }
            assertEquals(ApiProblem.SERVER, problem?.problem)
            assertEquals(0, other.requestCount)
        } }
    }
    @Test fun serverErrorBodyIsNotExposedByExceptions() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setResponseCode(500).setBody("do-not-display-this-sensitive-fixture"))
            val problem = try { client(server).login("fixture", "dummy"); null } catch (error: NasException) { error }
            assertEquals(ApiProblem.SERVER, problem?.problem)
            assertFalse(problem.toString().contains("do-not-display"))
        }
    }
    @Test fun forbiddenIsNotMistakenForWrongPassword() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setResponseCode(403))
            val api = client(server).apply { useSession("fixture") }
            val problem = try { api.me(); null } catch (error: NasException) { error }
            assertEquals(ApiProblem.FORBIDDEN, problem?.problem)
        }
    }
    @Test fun logoutClearsOnlyItsOwnCookieAuthority() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setHeader("Set-Cookie", "fixture_browser=first; Path=/; HttpOnly").setBody("""{"access_token":"fixture"}"""))
            server.enqueue(MockResponse().setResponseCode(204)); server.enqueue(meResponse())
            val api = client(server)
            api.login("fixture", "dummy"); api.logout(); api.useSession("renewed-fixture"); api.me()
            server.takeRequest(); server.takeRequest()
            assertNull(server.takeRequest().getHeader("Cookie"))
        }
    }
    @Test fun cancelledScreenCancelsItsOutstandingHttpCall() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
            val api = client(server).apply { useSession("fixture") }
            val request = launch { api.me() }
            delay(150)
            request.cancelAndJoin()
            assertTrue(request.isCancelled)
            repeat(20) { if (api.mediaClient.dispatcher.runningCallsCount() != 0) delay(25) }
            assertEquals(0, api.mediaClient.dispatcher.runningCallsCount())
        }
    }
    @Test fun expiredSessionRenewsOnceAndUsesTheNewBearer() = runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setResponseCode(401))
            server.enqueue(MockResponse().setBody("""{"access_token":"new-session"}"""))
            server.enqueue(meResponse())
            val api = client(server).apply { useSession("old-session"); sessionUser("fixture") }
            var persisted = false
            api.allowSessionRenewal({ SavedSignIn("dummy", "old-session") }, { old, next -> persisted = old == "old-session" && next == "new-session"; persisted })
            assertTrue(api.me().can("view_live")); assertTrue(persisted)
            assertEquals("Bearer old-session", server.takeRequest().getHeader("Authorization"))
            assertEquals("/api/auth/login", server.takeRequest().path)
            assertEquals("Bearer new-session", server.takeRequest().getHeader("Authorization"))
        }
    }
    @Test fun concurrentExpiredRequestsShareOnePasswordRenewal() = runBlocking {
        MockWebServer().use { server ->
            val logins = AtomicInteger()
            server.dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    if (request.path == "/api/auth/login") { logins.incrementAndGet(); Thread.sleep(100); return MockResponse().setBody("""{"access_token":"new"}""") }
                    return if (request.getHeader("Authorization") == "Bearer new") meResponse() else MockResponse().setResponseCode(401)
                }
            }
            val api = client(server).apply { useSession("old"); sessionUser("fixture") }
            api.allowSessionRenewal({ SavedSignIn("dummy", "old") }, { _, _ -> true })
            val first = async { api.me() }; val second = async { api.me() }
            assertEquals(first.await(), second.await()); assertEquals(1, logins.get())
        }
    }
    @Test fun signedOutProfileCannotBeRevivedByLateRenewal() = runBlocking {
        MockWebServer().use { server ->
            val started = CountDownLatch(1); val release = CountDownLatch(1)
            server.dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    if (request.path == "/api/auth/login") {
                        started.countDown(); release.await(3, TimeUnit.SECONDS)
                        return MockResponse().setBody("""{"access_token":"late"}""")
                    }
                    return MockResponse().setResponseCode(401)
                }
            }
            val api = client(server).apply { useSession("old"); sessionUser("fixture") }
            var persisted = false
            api.allowSessionRenewal({ SavedSignIn("dummy", "old") }, { _, _ -> persisted = true; true })
            val pending = async { try { api.me(); null } catch (error: NasException) { error.problem } }
            while (started.count > 0) delay(20)
            api.disableSessionRenewal(); release.countDown()
            assertEquals(ApiProblem.AUTHENTICATION, pending.await()); assertFalse(persisted)
        }
    }
    @Test fun invalidSavedPasswordIsNotRetriedByEveryTile() = runBlocking {
        MockWebServer().use { server ->
            val logins = AtomicInteger()
            server.dispatcher = object : Dispatcher() {
                override fun dispatch(request: RecordedRequest): MockResponse {
                    if (request.path == "/api/auth/login") logins.incrementAndGet()
                    return MockResponse().setResponseCode(401)
                }
            }
            val api = client(server).apply { useSession("old"); sessionUser("fixture") }
            api.allowSessionRenewal({ SavedSignIn("dummy", "old") }, { _, _ -> true })
            repeat(3) { try { api.me(); fail("access should be rejected") } catch (error: NasException) { assertEquals(ApiProblem.AUTHENTICATION, error.problem) } }
            assertEquals(1, logins.get())
        }
    }
}
