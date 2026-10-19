package com.souhail.vodcast.player

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.ui.Modifier
import androidx.compose.ui.viewinterop.AndroidView
import androidx.media3.common.MediaItem
import androidx.media3.exoplayer.ExoPlayer
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
