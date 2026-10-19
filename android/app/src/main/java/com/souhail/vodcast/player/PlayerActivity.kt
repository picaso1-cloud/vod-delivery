package com.souhail.vodcast.player

import android.app.PictureInPictureParams
import android.os.Build
import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.TrackSelectionOverride
import androidx.media3.common.Tracks
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.util.EventLogger
import androidx.media3.ui.PlayerView
import com.souhail.vodcast.data.Api
import com.souhail.vodcast.data.Settings
import com.souhail.vodcast.util.AppLog
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

class PlayerActivity : ComponentActivity() {
    private var player: ExoPlayer? = null
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var sleepJob: Job? = null
    private var api: Api? = null
    private var mediaKey: String? = null
    private var startOffset: Double = 0.0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val url = intent.getStringExtra("url")
        if (url.isNullOrBlank()) {
            finish()
            return
        }
        mediaKey = intent.getStringExtra("media_key")
        startOffset = intent.getDoubleExtra("start_offset", 0.0)
        val seekTo = intent.getDoubleExtra("seek_to", 0.0)
        val settings = Settings(applicationContext)
        if (settings.configured) api = Api(settings.baseUrl, settings.token)

        AppLog.action("player_open", "url=${AppLog.redact(url)} offset=$startOffset seek=$seekTo")
        val exo = ExoPlayer.Builder(this).build()
        player = exo
        exo.addAnalyticsListener(EventLogger())
        exo.addListener(object : Player.Listener {
            override fun onPlaybackStateChanged(state: Int) {
                Log.i("VodPlayer", "state=$state")
                AppLog.i("PLAYER", "state=$state ${stateName(state)}")
                if (state == Player.STATE_ENDED) saveNow()
            }

            override fun onIsPlayingChanged(isPlaying: Boolean) {
                Log.i("VodPlayer", "isPlaying=$isPlaying")
                AppLog.i("PLAYER", "isPlaying=$isPlaying")
                if (!isPlaying) saveNow()
            }

            override fun onPlayerError(error: PlaybackException) {
                Log.e("VodPlayer", "player error: ${error.errorCodeName}", error)
                AppLog.e("PLAYER", "error ${error.errorCodeName}", error)
            }
        })
        exo.setMediaItem(MediaItem.fromUri(url))
        exo.prepare()
        if (seekTo > 1.0) exo.seekTo((seekTo * 1000).toLong())
        exo.playWhenReady = true
        startProgressSaver()
        intent.getStringExtra("title")?.takeIf { it.isNotBlank() }?.let { setTitle(it) }

        setContent {
            var speedLabel by remember { mutableStateOf("1x") }
            var sleepLabel by remember { mutableStateOf("Sleep") }
            var showTracks by remember { mutableStateOf(false) }
            Box(Modifier.fillMaxSize()) {
                AndroidView(
                    modifier = Modifier.fillMaxSize(),
                    factory = { context -> PlayerView(context).apply { player = exo } },
                )
                Row(
                    modifier = Modifier.align(Alignment.TopEnd).statusBarsPadding().padding(8.dp),
                    horizontalArrangement = Arrangement.spacedBy(2.dp),
                ) {
                    TextButton(onClick = {
                        val speeds = listOf(0.75f, 1f, 1.25f, 1.5f, 2f)
                        val current = exo.playbackParameters.speed
                        val idx = speeds.indexOf(current).let { if (it < 0) 1 else it }
                        val next = speeds[(idx + 1) % speeds.size]
                        exo.setPlaybackSpeed(next)
                        speedLabel = if (next == next.toInt().toFloat()) "${next.toInt()}x" else "${next}x"
                        AppLog.action("speed", "$next")
                    }) { Text(speedLabel, color = Color.White) }
                    TextButton(onClick = {
                        sleepJob?.cancel()
                        sleepJob = scope.launch {
                            delay(30 * 60 * 1000L)
                            player?.pause()
                            AppLog.action("sleep_timer", "fired")
                        }
                        sleepLabel = "30m"
                        AppLog.action("sleep_timer", "30m")
                    }) { Text(sleepLabel, color = Color.White) }
                    TextButton(onClick = { showTracks = true }) { Text("Tracks", color = Color.White) }
                    TextButton(onClick = {
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                            enterPictureInPictureMode(PictureInPictureParams.Builder().build())
                        }
                    }) { Text("PiP", color = Color.White) }
                }
            }
            if (showTracks) {
                TrackDialog(exo) { showTracks = false }
            }
        }
    }

    private fun startProgressSaver() {
        val key = mediaKey ?: return
        val client = api ?: return
        scope.launch {
            while (isActive) {
                delay(10_000)
                val exo = player ?: break
                if (exo.isPlaying && exo.duration > 0) {
                    runCatching {
                        client.saveProgress(
                            key,
                            startOffset + exo.currentPosition / 1000.0,
                            startOffset + exo.duration / 1000.0,
                        )
                    }
                }
            }
        }
    }

    private fun saveNow() {
        val exo = player ?: return
        val key = mediaKey ?: return
        val client = api ?: return
        if (exo.duration <= 0) return
        val position = startOffset + exo.currentPosition / 1000.0
        val duration = startOffset + exo.duration / 1000.0
        CoroutineScope(Dispatchers.IO).launch {
            runCatching { client.saveProgress(key, position, duration) }
        }
    }

    override fun onPause() {
        saveNow()
        super.onPause()
    }

    override fun onDestroy() {
        saveNow()
        sleepJob?.cancel()
        scope.cancel()
        player?.release()
        player = null
        super.onDestroy()
    }
}

@Composable
private fun TrackDialog(exo: ExoPlayer, onDismiss: () -> Unit) {
    val options = mutableListOf<Triple<String, Tracks.Group, Int>>()
    exo.currentTracks.groups.forEach { group ->
        val type = when (group.type) {
            C.TRACK_TYPE_AUDIO -> "Audio"
            C.TRACK_TYPE_TEXT -> "Subtitle"
            else -> null
        }
        if (type != null) {
            for (i in 0 until group.length) {
                val format = group.getTrackFormat(i)
                val label = format.label ?: format.language ?: "track ${i + 1}"
                options.add(Triple("$type: $label", group, i))
            }
        }
    }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Tracks") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                options.forEach { (label, group, index) ->
                    TextButton(onClick = {
                        val builder = exo.trackSelectionParameters.buildUpon()
                        builder.setOverrideForType(TrackSelectionOverride(group.mediaTrackGroup, index))
                        exo.trackSelectionParameters = builder.build()
                        AppLog.action("track", label)
                        onDismiss()
                    }) { Text(label) }
                }
                TextButton(onClick = {
                    exo.trackSelectionParameters = exo.trackSelectionParameters.buildUpon()
                        .setTrackTypeDisabled(C.TRACK_TYPE_TEXT, true)
                        .build()
                    AppLog.action("track", "subtitles off")
                    onDismiss()
                }) { Text("Subtitles off") }
            }
        },
        confirmButton = { TextButton(onClick = onDismiss) { Text("Done") } },
    )
}

private fun stateName(state: Int): String = when (state) {
    Player.STATE_IDLE -> "IDLE"
    Player.STATE_BUFFERING -> "BUFFERING"
    Player.STATE_READY -> "READY"
    Player.STATE_ENDED -> "ENDED"
    else -> "UNKNOWN"
}
