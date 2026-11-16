package ru.kmvms.mobile.diagnostics

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

class DiagnosticJournalTest {
    @get:Rule val temporary = TemporaryFolder()
    @Test fun boundsDiskAndSnapshotAndSurvivesRecreation() {
        var now = 1794800000000L
        val directory = temporary.newFolder()
        val journal = DiagnosticJournal(directory, maxBytes = 4096, clock = { now })
        repeat(150) { journal.append(DiagnosticEntry(now++, DiagnosticCode.PUSH_RECEIVED, DiagnosticArea.PUSH, number = it)) }
        assertTrue(directory.listFiles()!!.sumOf { it.length() } <= 4096)
        val saved = DiagnosticJournal(directory, maxBytes = 4096, clock = { now }).snapshot(4)
        assertEquals(4, saved.size)
        assertEquals(listOf(146, 147, 148, 149), saved.map { it.getInt("number") })
        assertFalse(directory.listFiles()!!.any { it.extension == "zip" })
    }
    @Test fun expiresPrivateJournalAndIgnoresMalformedLinesAndUnknownFields() {
        var now = 1794800000000L
        val directory = temporary.newFolder()
        val journal = DiagnosticJournal(directory, clock = { now })
        journal.append(DiagnosticEntry(now, DiagnosticCode.APP_STARTED))
        directory.listFiles()!!.single().appendText("broken\n" + JSONObject().put("time", now).put("code", "APP_STARTED").put("area", "APP")
            .put("password", "must-not-be-exported").toString() + "\n")
        assertFalse(journal.snapshot().toString().contains("must-not-be-exported"))
        now += 8L * 86400000
        assertTrue(journal.snapshot().isEmpty())
        assertTrue(directory.listFiles()!!.isEmpty())
    }
    @Test fun exportsOnlyTypedIdentityNotArbitraryStrings() {
        val json = DiagnosticEntry(1794800000000, DiagnosticCode.PUSH_PARSE_FAILED, DiagnosticArea.PUSH,
            "https://user:secret@example.test/", "private-token", -1).json().toString()
        assertFalse(json.contains("secret")); assertFalse(json.contains("private-token")); assertFalse(json.contains("camera"))
        assertEquals(16, DiagnosticEntry.profileKey("test-profile").length)
        assertEquals(DiagnosticEntry.profileKey("test-profile"), DiagnosticEntry.profileKey("test-profile"))
    }
}
