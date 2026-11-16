package ru.kmvms.mobile.core

/** null means the default (all cameras); an explicit empty selection stays empty. */
object LiveCameraOrder {
    fun resolve(saved: List<Long>?, available: List<Long>): List<Long> {
        val allowed = available.filter { it > 0 }.distinct()
        return saved?.filter { it in allowed }?.distinct() ?: allowed
    }

    fun toggle(ids: List<Long>, id: Long): List<Long> = when {
        id <= 0 -> ids
        id in ids -> ids.filterNot { it == id }
        else -> ids + id
    }

    fun move(ids: List<Long>, id: Long, destination: Int): List<Long> {
        val index = ids.indexOf(id)
        if (index < 0 || destination !in ids.indices || index == destination) return ids
        return ids.toMutableList().apply { removeAt(index); add(destination, id) }
    }

    fun viewing(saved: List<Long>?, available: List<Long>, single: Long?): List<Long> =
        if (single != null) available.filter { it == single }.take(1) else resolve(saved, available)
}
