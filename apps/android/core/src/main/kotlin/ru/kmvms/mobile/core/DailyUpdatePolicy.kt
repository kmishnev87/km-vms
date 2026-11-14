package ru.kmvms.mobile.core

import java.time.LocalDate

/** The caller persists the attempt before starting IO, including offline attempts. */
fun shouldCheckForUpdate(today: LocalDate, lastAttempt: LocalDate?, manual: Boolean = false): Boolean =
    manual || lastAttempt == null || today.isAfter(lastAttempt)
