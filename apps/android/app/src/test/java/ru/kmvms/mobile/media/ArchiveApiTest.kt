package ru.kmvms.mobile.media

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.archive.*
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.NasApi
import java.time.*

class ArchiveApiTest {
    @Test fun bulkDeletionUsesTheSameExactPerFileApiAndKeepsDeniedFilesForRetry()=runBlocking {
        MockWebServer().use { server ->
            server.enqueue(MockResponse().setBody("""{"ok":true}"""))
            server.enqueue(MockResponse().setResponseCode(403).setBody("{}"))
            server.enqueue(MockResponse().setBody("""{"ok":true}"""))
            val rows=listOf("root-a", "root-b").map { root ->
                RecordingDeleteRequest(Recording(12,root,"$root:12",5,"Garden","same.mkv","today",null,null,null,"1 MB",true))
            }
            val service=client(server)
            val first=deleteSelectedRecordings(rows,service::deleteRecording)
            assertEquals(listOf(rows[0]),first.removed);assertEquals(listOf(rows[1]),first.failed)
            val second=deleteSelectedRecordings(first.failed,service::deleteRecording)
            assertTrue(second.failed.isEmpty())
            val requests=(1..3).map { server.takeRequest() }
            assertTrue(requests.all { it.method=="DELETE" })
            assertEquals(listOf("root-a:12","root-b:12","root-b:12"),requests.map { it.requestUrl!!.queryParameter("recording_ref") })
            assertEquals(requests[1].requestUrl!!.queryParameter("operation_id"),requests[2].requestUrl!!.queryParameter("operation_id"))
        }
    }
    @Test fun deletingRecordingUsesExactReferenceAndStableOperationNotFilename()=runBlocking {
        MockWebServer().use{server->
            server.enqueue(MockResponse().setBody("""{"ok":true}"""))
            val item=Recording(12,"root-a","root-a:12",5,"Garden","same.mkv","today",null,null,null,"1 MB",false)
            val operation="8bb88b98-00c3-42d1-9c0f-2739f8b31c99"
            client(server).deleteRecording(item,operation)
            val request=server.takeRequest();val url=request.requestUrl!!
            assertEquals("DELETE",request.method);assertEquals("root-a:12",url.queryParameter("recording_ref"))
            assertEquals(operation,url.queryParameter("operation_id"));assertNull(url.queryParameter("path"))
            assertNull(url.queryParameter("segment_id"))
        }
    }
    @Test fun deletingLegacyRecordingStillIncludesArchiveRootIdentity()=runBlocking {
        MockWebServer().use{server->
            server.enqueue(MockResponse().setBody("""{"ok":true}"""))
            client(server).deleteRecording(Recording(12,"root-b",null,5,"Garden","same.mkv","today",null,null,null,"1 MB",true),
                "8bb88b98-00c3-42d1-9c0f-2739f8b31c99")
            val url=server.takeRequest().requestUrl!!
            assertEquals("12",url.queryParameter("segment_id"));assertEquals("root-b",url.queryParameter("archive_root_id"))
        }
    }
    @Test fun groupedBrowserKeepsEqualNamesSeparateAndSortsDeletedLast()=runBlocking{
        MockWebServer().use{server->
            server.enqueue(MockResponse().setBody("""{"items":["Yard"],"export_items":[],"camera_items":[{"id":"1","name":"Yard","deleted":true,"value":"camera:1"},{"id":"2","name":"Yard","deleted":false,"value":"camera:2"}]}"""))
            val options=client(server).options()
            assertEquals(listOf(2L,1L),options.cameras.map{it.id})
            assertTrue(options.cameras.all{it.filterById})
            assertEquals(listOf("camera:2","camera:1"),options.cameras.map{it.value})
        }
    }
    @Test fun identitySelectionIsNotANameFilter()=runBlocking{
        MockWebServer().use{server->
            server.enqueue(MockResponse().setBody("""{"items":[],"total":0,"pagination":{"has_more":false},"timezone":{"id":"UTC"}}"""))
            client(server).list("Yard",cameraId=12)
            val url=server.takeRequest().requestUrl!!
            assertEquals("12",url.queryParameter("camera_id"));assertNull(url.queryParameter("camera"))
        }
    }
    @Test fun oldNasNamesStayLiteralIncludingNamesThatLookLikeTokens()=runBlocking{
        MockWebServer().use{server->
            server.enqueue(MockResponse().setBody("""{"items":["camera:12"],"export_items":[]}"""))
            val item=client(server).options().cameras.single()
            assertEquals("camera:12",item.value);assertFalse(item.filterById)
        }
    }
    private fun client(server: MockWebServer)=ArchiveApi(NasApi(ServerAddress.parse(server.url("/").toString())).apply{useSession("fixture")})
    @Test fun cameraNameAndDateCannotBecomeExtraQueryParameters()=runBlocking{
        MockWebServer().use{server->
            server.enqueue(MockResponse().setBody("""{"items":[],"total":0,"pagination":{"has_more":false},"timezone":{"id":"Etc/GMT-5"}}"""))
            val result=client(server).list("Garden... & ?camera=Other",LocalDate.of(2026,11,14))
            val url=server.takeRequest().requestUrl!!
            assertEquals("Garden... & ?camera=Other",url.queryParameter("camera"));assertEquals("2026-11-14",url.queryParameter("date"))
            assertEquals(ZoneId.of("Etc/GMT-5"),result.zone)
        }
    }
    @Test fun deletedCameraRecordingsRemainExportable()=runBlocking{
        MockWebServer().use{server->
            server.enqueue(MockResponse().setBody("""{"items":["Old camera"],"export_items":[{"id":"12","name":"Old camera","deleted":true}]}"""))
            val options=client(server).options()
            assertTrue(options.exportable.single().deleted);assertEquals(12L,options.exportable.single().id)
        }
    }
    @Test fun playbackAuthorityUsesStableReferenceNotDisplayNameOrFilename()=runBlocking{
        MockWebServer().use{server->
            server.enqueue(MockResponse().setBody("""{"media_token":"fixture-only-token","expires_in":300}"""))
            val recording=Recording(12,"root-a","root-a:12",5,"Garden","same-name.mkv","today",null,null,"video/x-matroska","1 MB",true)
            val lease=client(server).authorize(recording)
            val request=server.takeRequest();val body=JSONObject(request.body.readUtf8())
            assertEquals("/api/recordings/media-token",request.path);assertEquals("root-a:12",body.getString("recording_ref"))
            assertFalse(body.has("path"));assertFalse(recording.toString().contains(lease.token))
        }
    }
    @Test fun clipCreationKeepsExplicitNasOffset()=runBlocking{
        MockWebServer().use{server->
            server.enqueue(MockResponse().setBody("""{"id":"abcdef12","status":"queued","camera":{"label":"Garden"}}"""))
            val start=OffsetDateTime.parse("2026-11-14T12:00:00+05:00")
            client(server).createClip(12,"Fixture",start,start.plusMinutes(1))
            val body=JSONObject(server.takeRequest().body.readUtf8())
            assertEquals(start,OffsetDateTime.parse(body.getString("start_ts")))
            assertEquals(12L,body.getLong("camera_id"))
        }
    }
    @Test fun deletingClipCannotTargetRecordings()=runBlocking{
        MockWebServer().use{server->
            server.enqueue(MockResponse().setBody("""{"deleted":true}"""))
            client(server).deleteClip("abcdef12")
            val request=server.takeRequest()
            assertEquals("DELETE",request.method);assertEquals("/api/archive/exports/abcdef12?confirm=true",request.path)
        }
    }
    @Test fun productLocalNaiveAndExplicitOffsetReferToTheSameInstant(){
        val zone=ZoneId.of("Etc/GMT-5")
        assertEquals(Instant.parse("2026-11-14T07:52:23Z"),parseTime("2026-11-14T12:52:23",zone))
        assertEquals(parseTime("2026-11-14T12:52:23",zone),parseTime("2026-11-14T12:52:23+05:00",ZoneOffset.UTC))
        assertEquals("UTC+05:00",zoneLabel(zone))
    }
    @Test fun missingSourceRangeHasAnExplicitSafeExplanation(){
        val clip=Clip.fromJson(JSONObject("""{"id":"abcdef12","status":"failed","error_code":"source_gap_detected","error_message":"private-server-path"}"""))
        assertEquals("source_gap_detected",clip.errorCode)
        assertFalse(clip.toString().contains("private-server-path"))
    }
    @Test fun sameSegmentNumberInDifferentRootsDoesNotShareUiIdentity(){
        val first=Recording(12,"root-a",null,5,"Garden","same.mkv","today",null,null,null,"1 MB",true)
        val second=first.copy(root="root-b")
        assertNotEquals(first.identityKey,second.identityKey)
        assertEquals("canonical-reference",first.copy(reference="canonical-reference").identityKey)
    }
    @Test fun failedClipRemainsVisibleAndDeletedJobsDoNotReappear()=runBlocking{
        MockWebServer().use{server->
            server.enqueue(MockResponse().setBody("""{"items":[{"id":"abc1","status":"failed"},{"id":"abc2","status":"deleted"},{"id":"abc3","status":"expired"}],"has_more":false}"""))
            assertEquals(listOf("abc1"),client(server).clips().first.map{it.id})
        }
    }
}
