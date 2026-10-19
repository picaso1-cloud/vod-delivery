package com.souhail.vodcast.cast

import android.content.Context
import com.google.android.gms.cast.CastMediaControlIntent
import com.google.android.gms.cast.MediaInfo
import com.google.android.gms.cast.MediaMetadata
import com.google.android.gms.cast.framework.CastContext
import com.google.android.gms.cast.framework.CastOptions
import com.google.android.gms.cast.framework.OptionsProvider
import com.google.android.gms.cast.framework.SessionProvider
import com.souhail.vodcast.util.AppLog

class CastOptionsProvider : OptionsProvider {
    override fun getCastOptions(context: Context): CastOptions =
        CastOptions.Builder()
            .setReceiverApplicationId(CastMediaControlIntent.DEFAULT_MEDIA_RECEIVER_APPLICATION_ID)
            .build()

    override fun getAdditionalSessionProviders(context: Context): List<SessionProvider>? = null
}

object CastCaster {
    fun load(context: Context, url: String, title: String, contentType: String = "video/mp4"): String {
        val castContext = CastContext.getSharedInstance(context)
        val session = castContext.sessionManager.currentCastSession
        if (session == null) {
            AppLog.w("CAST", "load without a session")
            return "No Cast session. Tap the Cast button and pick a TV first."
        }
        val client = session.remoteMediaClient ?: return "Cast session has no media client."
        val metadata = MediaMetadata(MediaMetadata.MEDIA_TYPE_MOVIE).apply {
            putString(MediaMetadata.KEY_TITLE, title)
        }
        val info = MediaInfo.Builder(url)
            .setStreamType(MediaInfo.STREAM_TYPE_BUFFERED)
            .setContentType(contentType)
            .setMetadata(metadata)
            .build()
        val name = session.castDevice?.friendlyName ?: "Cast device"
        AppLog.action("cast_load", "device=$name type=$contentType url=${AppLog.redact(url)}")
        client.load(info)
        return "Casting to $name"
    }

    fun stop(context: Context) {
        AppLog.action("cast_stop")
        CastContext.getSharedInstance(context).sessionManager.currentCastSession
            ?.remoteMediaClient
            ?.stop()
    }
}
