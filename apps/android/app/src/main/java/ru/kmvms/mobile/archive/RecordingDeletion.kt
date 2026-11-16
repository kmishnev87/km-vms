package ru.kmvms.mobile.archive

import kotlinx.coroutines.CancellationException
import java.util.UUID

data class RecordingDeleteRequest(val recording: Recording, val operationId: String = UUID.randomUUID().toString())
data class RecordingDeleteResult(val removed: List<RecordingDeleteRequest>, val failed: List<RecordingDeleteRequest>)

/** A frozen, explicit selection. Each file retains its own idempotency receipt on retry. */
suspend fun deleteSelectedRecordings(requests: List<RecordingDeleteRequest>,
    delete: suspend (Recording, String) -> Unit): RecordingDeleteResult {
    require(requests.map { it.recording.identityKey }.distinct().size == requests.size)
    val removed = mutableListOf<RecordingDeleteRequest>()
    val failed = mutableListOf<RecordingDeleteRequest>()
    for (request in requests) {
        try { delete(request.recording, request.operationId); removed += request }
        catch (cancelled: CancellationException) { throw cancelled }
        catch (_: Exception) { failed += request }
    }
    return RecordingDeleteResult(removed, failed)
}
