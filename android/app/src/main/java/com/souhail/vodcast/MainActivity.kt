package com.souhail.vodcast

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import com.souhail.vodcast.data.Settings
import com.souhail.vodcast.ui.VodApp
import com.souhail.vodcast.ui.VodTheme
import com.souhail.vodcast.util.AppLog

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AppLog.init(applicationContext)
        val settings = Settings(applicationContext)
        setContent {
            VodTheme { VodApp(settings) }
        }
    }
}
