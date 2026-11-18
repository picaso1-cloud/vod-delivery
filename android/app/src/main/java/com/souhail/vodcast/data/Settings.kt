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

    var sessionToken: String
        get() = prefs.getString("session_token", "") ?: ""
        set(value) = prefs.edit().putString("session_token", value).apply()

    var sessionTitle: String
        get() = prefs.getString("session_title", "") ?: ""
        set(value) = prefs.edit().putString("session_title", value).apply()

    var sessionMode: String
        get() = prefs.getString("session_mode", "") ?: ""
        set(value) = prefs.edit().putString("session_mode", value).apply()

    var sessionTv: String
        get() = prefs.getString("session_tv", "") ?: ""
        set(value) = prefs.edit().putString("session_tv", value).apply()

    var lastUpdateCheck: Long
        get() = prefs.getLong("update_check", 0L)
        set(value) = prefs.edit().putLong("update_check", value).apply()

    var skippedVersion: Int
        get() = prefs.getInt("update_skip", 0)
        set(value) = prefs.edit().putInt("update_skip", value).apply()

    fun clearSession() {
        prefs.edit()
            .remove("session_token")
            .remove("session_title")
            .remove("session_mode")
            .remove("session_tv")
            .apply()
    }

    val configured: Boolean get() = baseUrl.isNotBlank() && token.isNotBlank()
}
