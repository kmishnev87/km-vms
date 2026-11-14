package ru.kmvms.mobile.core

import java.time.Instant
import java.time.ZoneId
import java.util.Locale

data class GmtTimezone(val id:String,val label:String)

val gmtTimezones = (-12..14).map { offset ->
    val id = if(offset==0) "UTC" else "Etc/GMT${if(offset>0)"-"else"+"}${kotlin.math.abs(offset)}"
    GmtTimezone(id,String.format(Locale.ROOT,"GMT%+03d:00",offset))
}

/** Display an existing IANA setting as GMT without silently replacing it on save. */
fun gmtTimezoneLabel(id:String, now:Instant=Instant.now()):String = runCatching {
    val totalMinutes=ZoneId.of(id).rules.getOffset(now).totalSeconds/60
    val sign=if(totalMinutes<0)"-"else"+"
    String.format(Locale.ROOT,"GMT%s%02d:%02d",sign,kotlin.math.abs(totalMinutes)/60,kotlin.math.abs(totalMinutes)%60)
}.getOrDefault("GMT+00:00")
