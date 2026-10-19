package com.souhail.vodcast.data

import android.content.Context

class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("vodcast", Context.MODE_PRIVATE)

    var baseUrl: String
        get() = prefs.getString("base_url", "") ?: ""
        set(value) = prefs.edit().putString("base_url", value.trim()).apply()

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(value) = prefs.edit().putString("token", value.trim()).apply()

    var lastTv: String
        get() = prefs.getString("last_tv", "") ?: ""
        set(value) = prefs.edit().putString("last_tv", value).apply()

    val configured: Boolean get() = baseUrl.isNotBlank() && token.isNotBlank()
}
