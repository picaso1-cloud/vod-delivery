package com.souhail.vodcast.cast

import android.content.Context
import com.google.android.gms.cast.CastMediaControlIntent
import com.google.android.gms.cast.MediaInfo
import com.google.android.gms.cast.MediaMetadata
import com.google.android.gms.cast.framework.CastContext
import com.google.android.gms.cast.framework.CastOptions
import com.google.android.gms.cast.framework.OptionsProvider
import com.google.android.gms.cast.framework.SessionProvider

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
            ?: return "No Cast session. Tap the Cast button and pick a TV first."
        val client = session.remoteMediaClient ?: return "Cast session has no media client."
        val metadata = MediaMetadata(MediaMetadata.MEDIA_TYPE_MOVIE).apply {
            putString(MediaMetadata.KEY_TITLE, title)
        }
        val info = MediaInfo.Builder(url)
            .setStreamType(MediaInfo.STREAM_TYPE_BUFFERED)
            .setContentType(contentType)
            .setMetadata(metadata)
            .build()
        client.load(info)
        return "Casting to ${session.castDevice?.friendlyName ?: "Cast device"}"
    }

    fun stop(context: Context) {
        CastContext.getSharedInstance(context).sessionManager.currentCastSession
            ?.remoteMediaClient
            ?.stop()
    }
}
