package ru.kmvms.mobile.ui.archive

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.runtime.CompositionLocalProvider
import android.content.res.Configuration
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import ru.kmvms.mobile.archive.Clip
import ru.kmvms.mobile.ui.KMVMSTheme
import java.util.Locale

@RunWith(AndroidJUnit4::class)
class ClipFactsTest {
    @get:Rule val compose=createComposeRule()
    private val clip=Clip("fixture","Clip","Camera","done",100,null,6442450944L,true,null,null,durationSeconds=3723.0)

    private fun facts(locale:String) {
        compose.setContent {
            val configuration=Configuration(LocalConfiguration.current).apply { setLocale(Locale.forLanguageTag(locale)) }
            val context=LocalContext.current.createConfigurationContext(configuration)
            CompositionLocalProvider(LocalContext provides context,LocalConfiguration provides configuration) {
                KMVMSTheme { Box(Modifier.width(288.dp)) { ClipFacts(clip) } }
            }
        }
        compose.onNodeWithTag("clip-size-fixture").assertIsDisplayed()
        compose.onNodeWithTag("clip-duration-fixture").assertTextContains("1:02:03",substring=true)
        compose.onNodeWithText("Ready").assertDoesNotExist()
        compose.onNodeWithText("Готов").assertDoesNotExist()
        compose.onNodeWithText("就绪").assertDoesNotExist()
        val parent=compose.onNodeWithTag("clip-facts-fixture").getUnclippedBoundsInRoot()
        for(tag in listOf("clip-size-fixture","clip-duration-fixture")) {
            val bounds=compose.onNodeWithTag(tag).getUnclippedBoundsInRoot()
            assertTrue(bounds.left>=parent.left&&bounds.right<=parent.right)
        }
    }
    @Test fun englishFactsFitSmallCard()=facts("en")
    @Test fun russianFactsFitSmallCard()=facts("ru")
    @Test fun chineseFactsFitSmallCard()=facts("zh-CN")
}
