package com.souhail.vodcast

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import com.souhail.vodcast.data.Settings
import com.souhail.vodcast.ui.VodApp
import com.souhail.vodcast.util.AppLog

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AppLog.init(applicationContext)
        val settings = Settings(applicationContext)
        setContent {
            MaterialTheme(colorScheme = darkColorScheme()) {
                VodApp(settings)
            }
        }
    }
}
