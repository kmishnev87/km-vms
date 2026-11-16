package ru.kmvms.mobile.server

import kotlinx.coroutines.test.runTest
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.*
import org.junit.Test
import ru.kmvms.mobile.core.ServerAddress
import ru.kmvms.mobile.data.*

class UsersApiTest {
    private val owner=NasUser(1,"Owner","owner",setOf("manage_users"))
    private val admin=NasUser(2,"Admin","admin",setOf("manage_users"))
    private val users=listOf(ManagedUser(1,"owner","","owner",true),ManagedUser(2,"admin","","admin",true),ManagedUser(3,"viewer","","viewer",true))
    @Test fun viewerAndOperatorCannotManageUsersEvenWithForgedPermissionInLocalState() {
        listOf("viewer","operator").forEach { role ->
            val actor=NasUser(4,"User",role,setOf("manage_users"))
            assertFalse(UserManagementPolicy.allowed(actor));assertTrue(UserManagementPolicy.createRoles(actor).isEmpty())
            assertFalse(UserManagementPolicy.canEdit(actor,users[2]));assertFalse(UserManagementPolicy.canDelete(actor,users[2],users))
        }
        assertFalse(UserManagementPolicy.allowed(owner.copy(permissions=emptySet())))
    }
    @Test fun sameRoleAndSelfProtectionAsTheWebClient() {
        assertEquals(listOf("admin","operator","viewer"),UserManagementPolicy.createRoles(owner))
        assertEquals(listOf("operator","viewer"),UserManagementPolicy.createRoles(admin))
        assertEquals(listOf("owner"),UserManagementPolicy.roles(owner,users[0]))
        assertFalse(UserManagementPolicy.canDelete(owner,users[0],users))
        assertFalse(UserManagementPolicy.canDeactivate(admin,users[1]))
        assertFalse(UserManagementPolicy.canEdit(admin,users[0]))
        assertFalse(UserManagementPolicy.canEdit(admin,users[1].copy(id=9)))
        assertTrue(UserManagementPolicy.canDelete(admin,users[2],users))
        assertFalse(UserManagementPolicy.canDelete(owner,users[1],users.drop(1)))
    }
    @Test fun passwordsAreOptionalOnlyForEditsAndOwnChangeNeedsCurrentPassword() {
        assertFalse(UserMutation("new-user","","viewer",true).valid(owner,null))
        assertTrue(UserMutation("new-user","","viewer",true,"test-pass-123").valid(owner,null))
        assertFalse(UserMutation("new-user","","admin",true,"test-pass-123").valid(admin,null))
        assertTrue(UserMutation("viewer","","viewer",true).valid(owner,users[2]))
        assertFalse(UserMutation("owner","","owner",true,"test-pass-123").valid(owner,users[0]))
        assertTrue(UserMutation("owner","","owner",true,"test-pass-123","old-fixture").valid(owner,users[0]))
        assertFalse(UserMutation("owner","","owner",false).valid(owner,users[0]))
        assertFalse(UserMutation("bad/name","","viewer",true,"test-pass-123").valid(owner,null))
        assertFalse(UserMutation("x".repeat(65),"","viewer",true,"test-pass-123").valid(owner,null))
        assertFalse(UserMutation("valid","","viewer",true,"hidden-fixture").toString().contains("hidden-fixture"))
    }
    @Test fun officialExistingEndpointsAndSessionAreUsedForCreateEditAndDelete()=runTest {
        MockWebServer().use { server ->
            val api=NasApi(ServerAddress.parse(server.url("/").toString())).apply{useSession("fixture-session")}
            val service=UsersApi(api,owner)
            val body="""{"id":3,"username":"viewer","display_name":"Front gate","role":"viewer","is_active":true}"""
            server.enqueue(MockResponse().setBody(body))
            service.save(null,UserMutation("viewer","Front gate","viewer",true,"fixture-pass-123"))
            server.takeRequest().let{assertEquals("/api/users",it.path);assertEquals("POST",it.method);assertEquals("Bearer fixture-session",it.getHeader("Authorization"))}
            server.enqueue(MockResponse().setBody(body))
            service.save(users[2],UserMutation("viewer","Front gate","viewer",true))
            server.takeRequest().let{assertEquals("/api/users/3",it.path);assertEquals("PATCH",it.method);assertFalse(it.body.readUtf8().contains("password"))}
            server.enqueue(MockResponse().setBody(body));service.delete(users[2],users)
            server.takeRequest().let{assertEquals("/api/users/3",it.path);assertEquals("DELETE",it.method)}
        }
    }
    @Test fun revokedAccessIsNotConvertedIntoClientSideSuccess()=runTest {
        MockWebServer().use { server ->
            val api=NasApi(ServerAddress.parse(server.url("/").toString())).apply{useSession("fixture-session")}
            server.enqueue(MockResponse().setResponseCode(403).setBody("{}"))
            try{UsersApi(api,owner).list();fail("must reject")}catch(error:NasException){assertEquals(ApiProblem.FORBIDDEN,error.problem)}
            val viewer=NasUser(4,"Viewer","viewer",emptySet())
            try{UsersApi(api,viewer).list();fail("must reject")}catch(error:NasException){assertEquals(ApiProblem.FORBIDDEN,error.problem)}
            assertEquals(1,server.requestCount)
        }
    }
}
