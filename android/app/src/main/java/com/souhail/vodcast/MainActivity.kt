package com.souhail.vodcast

import android.os.Bundle
import androidx.activity.compose.setContent
import androidx.fragment.app.FragmentActivity
import com.souhail.vodcast.data.Settings
import com.souhail.vodcast.ui.VodApp
import com.souhail.vodcast.ui.VodTheme
import com.souhail.vodcast.util.AppLog

class MainActivity : FragmentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AppLog.init(applicationContext)
        val settings = Settings(applicationContext)
        setContent {
            VodTheme { VodApp(settings) }
        }
    }
}
