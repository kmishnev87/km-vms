package ru.kmvms.mobile.archive

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test

class RecordingDeletionTest {
    private fun request(id:Long)=RecordingDeleteRequest(Recording(id,"root",null,1,"Camera","$id.mp4","now",null,null,"video/mp4","1 MB",true))
    @Test fun onlyTheFrozenSelectionIsDeletedAndPartialFailuresKeepTheSameReceipts()=runBlocking {
        val requests=listOf(request(1),request(2),request(3))
        val calls=mutableListOf<Pair<Long,String>>()
        val first=deleteSelectedRecordings(requests) { row,id -> calls+=row.id to id; if(row.id==2L)error("connection lost") }
        assertEquals(listOf(1L,3L),first.removed.map { it.recording.id })
        assertEquals(listOf(requests[1]),first.failed)
        val retry=deleteSelectedRecordings(first.failed) { row,id -> calls+=row.id to id }
        assertTrue(retry.failed.isEmpty())
        assertEquals(listOf(1L,2L,3L,2L),calls.map { it.first })
        assertEquals(calls[1].second,calls[3].second)
    }
    @Test fun cancellationDoesNotContinueDeletingOtherFiles()=runBlocking {
        val calls=mutableListOf<Long>()
        try {
            deleteSelectedRecordings(listOf(request(1),request(2))) { row,_ -> calls+=row.id;throw CancellationException() }
            fail("Cancellation must propagate")
        } catch(_:CancellationException) { assertEquals(listOf(1L),calls) }
    }
}
