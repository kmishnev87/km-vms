package ru.kmvms.mobile.push

internal enum class PushRecovery {
    NONE, REUPLOAD, NEEDS_SERVICE;

    companion object {
        fun forServer(localRevision: Int, remoteRevision: Int, push: Boolean, badge: Boolean, status: String): PushRecovery {
            if (localRevision != remoteRevision || push || badge) return NONE
            return when (status) {
                // Older NAS versions could mistakenly discard a route on DNS failure.
                "push_endpoint_invalid" -> REUPLOAD
                "push_registration_expired", "push_registration_invalid", "push_service_rejected" -> NEEDS_SERVICE
                else -> NONE  // Do not undo a normal explicit disable.
            }
        }
    }
}
