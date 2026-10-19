package com.souhail.vodcast.data

import android.content.Context

class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("vodcast", Context.MODE_PRIVATE)

    var lanUrl: String
        get() = prefs.getString("lan_url", prefs.getString("base_url", "")) ?: ""
        set(value) = prefs.edit().putString("lan_url", value.trim()).apply()

    var publicUrl: String
        get() = prefs.getString("public_url", "") ?: ""
        set(value) = prefs.edit().putString("public_url", value.trim()).apply()

    var baseUrl: String
        get() = prefs.getString("base_url", "") ?: ""
        set(value) = prefs.edit().putString("base_url", value.trim()).apply()

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(value) = prefs.edit().putString("token", value.trim()).apply()

    var usingLan: Boolean
        get() = prefs.getBoolean("using_lan", false)
        set(value) = prefs.edit().putBoolean("using_lan", value).apply()

    var lastTv: String
        get() = prefs.getString("last_tv", "") ?: ""
        set(value) = prefs.edit().putString("last_tv", value).apply()

    val configured: Boolean get() = baseUrl.isNotBlank() && token.isNotBlank()
}
