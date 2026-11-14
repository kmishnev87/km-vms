package ru.kmvms.mobile.core

/** Process-local attention; never communicates a global mute to a NAS. */
class LiveAttention {
    private val owners = mutableMapOf<Any, Pair<String, Set<Long>>>()
    @Synchronized fun update(owner: Any, profile: String, playingVisible: Set<Long>) {
        if (playingVisible.isEmpty()) owners.remove(owner)
        else owners[owner] = profile to playingVisible.toSet()
    }
    @Synchronized fun clear(owner: Any) { owners.remove(owner) }
    @Synchronized fun watching(profile: String, camera: Long?) = camera != null &&
        owners.values.any { it.first == profile && camera in it.second }
}
