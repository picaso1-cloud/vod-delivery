package com.souhail.vodcast.player

import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.ui.Modifier
import androidx.compose.ui.viewinterop.AndroidView
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.util.EventLogger
import androidx.media3.ui.PlayerView

class PlayerActivity : ComponentActivity() {
    private var player: ExoPlayer? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val url = intent.getStringExtra("url")
        if (url.isNullOrBlank()) {
            finish()
            return
        }
        val exo = ExoPlayer.Builder(this).build()
        player = exo
        exo.addAnalyticsListener(EventLogger())
        exo.addListener(object : Player.Listener {
            override fun onPlaybackStateChanged(state: Int) {
                Log.i("VodPlayer", "state=$state")
            }

            override fun onIsPlayingChanged(isPlaying: Boolean) {
                Log.i("VodPlayer", "isPlaying=$isPlaying")
            }

            override fun onPlayerError(error: PlaybackException) {
                Log.e("VodPlayer", "player error: ${error.errorCodeName}", error)
            }
        })
        exo.setMediaItem(MediaItem.fromUri(url))
        exo.prepare()
        exo.playWhenReady = true
        setContent {
            AndroidView(
                modifier = Modifier.fillMaxSize(),
                factory = { context -> PlayerView(context).apply { player = exo } },
            )
        }
        intent.getStringExtra("title")?.takeIf { it.isNotBlank() }?.let { setTitle(it) }
    }

    override fun onDestroy() {
        player?.release()
        player = null
        super.onDestroy()
    }
}
