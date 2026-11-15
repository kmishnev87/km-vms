package ru.kmvms.mobile

import android.app.Activity
import android.app.LocaleManager
import android.content.Context
import android.content.ContextWrapper
import android.content.res.Configuration
import android.os.Build
import android.os.LocaleList

/** Phone language is independent of every connected NAS's language. */
object AppLocales {
    private val supported=setOf("","en","ru","zh-CN")
    fun selected(context:Context):String {
        val value=if(Build.VERSION.SDK_INT>=33)context.getSystemService(LocaleManager::class.java).applicationLocales.toLanguageTags()
            else context.getSharedPreferences("application",Context.MODE_PRIVATE).getString("language","").orEmpty()
        return value.takeIf{it in supported}.orEmpty()
    }
    fun wrap(context:Context):Context {
        if(Build.VERSION.SDK_INT>=33)return context
        val language=selected(context)
        if(language.isEmpty())return context
        return localizedContext(context,language)
    }
    // Override language only. Copying the whole configuration freezes orientation,
    // screen size and density in this context on Android versions before 13.
    internal fun localizedContext(context:Context,language:String):Context {
        require(language in supported && language.isNotEmpty())
        val configuration=Configuration().apply{setLocales(LocaleList.forLanguageTags(language))}
        return context.createConfigurationContext(configuration)
    }
    fun set(context:Context,language:String) {
        require(language in supported)
        context.getSharedPreferences("application",Context.MODE_PRIVATE).edit().putString("language",language).apply()
        if(Build.VERSION.SDK_INT>=33)context.getSystemService(LocaleManager::class.java).applicationLocales=LocaleList.forLanguageTags(language)
        else activity(context)?.recreate()
    }
    private fun activity(context:Context):Activity?=when(context){is Activity->context;is ContextWrapper->activity(context.baseContext);else->null}
}
