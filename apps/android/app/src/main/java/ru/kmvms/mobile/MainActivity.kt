package ru.kmvms.mobile

import android.os.Bundle
import android.content.Context
import android.content.Intent
import ru.kmvms.mobile.push.NotificationNavigation
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import ru.kmvms.mobile.ui.MobileApp

@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
class MainActivity : ComponentActivity() {
    private val mobile: MobileViewModel by viewModels()
    private val updates: ru.kmvms.mobile.update.AppUpdates by viewModels()
    override fun attachBaseContext(newBase:Context) { super.attachBaseContext(AppLocales.wrap(newBase)) }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        NotificationNavigation.accept(intent)
        ru.kmvms.mobile.widget.AlertWidgetProvider.openDestination(this, intent)
        setContent { MobileApp(model = mobile, updates = updates) }
    }
    override fun onResume() {
        super.onResume()
        updates.foreground()
        mobile.checkPushRegistration()
        ru.kmvms.mobile.widget.WidgetUnreadSync.opened(this)
    }
    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        NotificationNavigation.accept(intent)
        ru.kmvms.mobile.widget.AlertWidgetProvider.openDestination(this, intent)
    }
}
