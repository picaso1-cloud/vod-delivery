package com.souhail.vodcast.tv

import android.os.Bundle
import android.view.KeyEvent
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.foundation.focusable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material3.Surface
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.ui.text.style.TextOverflow
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.Player
import androidx.media3.common.TrackGroup
import androidx.media3.common.TrackSelectionOverride
import androidx.media3.common.Tracks
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.PlayerView
import com.souhail.vodcast.data.Api
import com.souhail.vodcast.data.Settings
import com.souhail.vodcast.ui.VodTheme
import com.souhail.vodcast.util.AppLog
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

class TvPlayerActivity : ComponentActivity() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private lateinit var settings: Settings
    private var api: Api? = null
    private var socket: PlayerSocket? = null
    private var exo: ExoPlayer? = null

    private var connection = mutableStateOf("starting")
    private var title = mutableStateOf("")
    private var playerName = mutableStateOf("")
    private var playing = mutableStateOf(false)
    private var configured = mutableStateOf(false)
    private var statusText = mutableStateOf("")
    private var mediaKey = mutableStateOf("")
    private var sessionToken = mutableStateOf("")
    private var overlayVisible = mutableStateOf(true)
    private var overlayTick = mutableIntStateOf(0)
    private var panelOpen = mutableStateOf(false)
    private var panelIndex = mutableIntStateOf(0)
    private var audioChoices = mutableStateOf<List<TrackChoice>>(emptyList())
    private var textChoices = mutableStateOf<List<TrackChoice>>(emptyList())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = Settings(applicationContext)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        intent.getStringExtra("server")?.let { settings.baseUrl = it.trim().trimEnd('/') }
        intent.getStringExtra("token")?.let { settings.token = it.trim() }
        intent.getStringExtra("name")?.let { settings.playerName = it.trim() }

        configured.value = settings.baseUrl.isNotBlank() && settings.token.isNotBlank()
        playerName.value = settings.playerName.ifBlank { "Android TV" }

        setContent {
            VodTheme {
                TvRoot()
            }
        }
        if (configured.value) connect()
    }

    private fun persist(server: String, token: String, name: String) {
        settings.baseUrl = server.trim().trimEnd('/')
        settings.token = token.trim()
        settings.playerName = name.trim().ifBlank { "Android TV" }
        playerName.value = settings.playerName
        api = Api(settings.baseUrl, settings.token)
        configured.value = true
        connect()
    }

    private fun connect() {
        if (!configured.value) return
        connection.value = "connecting"
        scope.launch {
            try {
                val client = Api(settings.baseUrl, settings.token)
                api = client
                val registration = client.registerPlayer(playerName.value)
                settings.playerId = registration.player_id
                AppLog.i("TVPLAYER", "registered ${registration.name} (${registration.player_id})")
                openSocket(registration.player_id)
            } catch (e: Exception) {
                connection.value = "error: ${e.message}"
                AppLog.w("TVPLAYER", "register failed: ${e.message}")
                delay(5000)
                connect()
            }
        }
    }

    private fun openSocket(playerId: String) {
        socket?.close()
        val base = settings.baseUrl.trimEnd('/')
            .replaceFirst("https://", "wss://")
            .replaceFirst("http://", "ws://")
        val url = "$base/api/app/player/ws?player_id=$playerId&token=" +
            URLEncoder.encode(settings.token, "UTF-8")
        socket = PlayerSocket(
            url = url,
            onOpen = { runOnUiThread { connection.value = "online" } },
            onClosed = {
                runOnUiThread {
                    connection.value = "offline"
                    if (playing.value) stopPlayback()
                }
                scope.launch {
                    delay(4000)
                    connect()
                }
            },
            onMessage = { message -> runOnUiThread { handleCommand(message) } },
        ).also { it.start() }
    }

    private fun handleCommand(message: JSONObject) {
        when (message.optString("type")) {
            "play" -> {
                val url = message.optString("url")
                if (url.isBlank()) return
                title.value = message.optString("title")
                mediaKey.value = message.optString("media_key")
                sessionToken.value = message.optString("token")
                startPlayback(url, message.optDouble("position", 0.0))
            }
            "control" -> when (message.optString("action")) {
                "pause" -> exo?.pause()
                "resume" -> exo?.play()
                "seek" -> exo?.seekTo((message.optDouble("value", 0.0) * 1000).toLong())
                "stop" -> stopPlayback()
            }
        }
    }

    private fun startPlayback(url: String, positionSeconds: Double) {
        try {
            startPlaybackInner(url, positionSeconds)
        } catch (e: Exception) {
            connection.value = "playback error: ${e.message}"
            AppLog.e("TVPLAYER", "playback start failed", e)
        }
    }

    private fun startPlaybackInner(url: String, positionSeconds: Double) {
        stopPlayback(silent = true)
        val loadControl = DefaultLoadControl.Builder()
            .setBufferDurationsMs(15_000, 40_000, 1_500, 3_000)
            .build()
        val player = ExoPlayer.Builder(this).setLoadControl(loadControl).build()
        exo = player
        player.addListener(object : Player.Listener {
            override fun onPlaybackStateChanged(state: Int) {
                if (state == Player.STATE_ENDED) {
                    sendStatus("stopped")
                    stopPlayback(silent = true)
                }
            }

            override fun onTracksChanged(tracks: Tracks) {
                rebuildTracks(tracks)
            }

            override fun onPlayerError(error: androidx.media3.common.PlaybackException) {
                AppLog.w("TVPLAYER", "player error: ${error.errorCodeName} ${error.message}")
                connection.value = "player error: ${error.errorCodeName}"
                sendStatus("error")
            }
        })
        player.setMediaItem(MediaItem.Builder().setUri(url).build())
        player.prepare()
        if (positionSeconds > 0) player.seekTo((positionSeconds * 1000).toLong())
        player.playWhenReady = true
        playing.value = true
        connection.value = "playing"
        AppLog.i("TVPLAYER", "play ${title.value.take(40)} at ${positionSeconds.toInt()}s")
        statusLoop()
    }

    private var statusJob: Job? = null

    private fun statusLoop() {
        statusJob?.cancel()
        statusJob = scope.launch {
            while (playing.value) {
                delay(2000)
                val player = exo ?: break
                val state = when {
                    player.isPlaying -> "playing"
                    player.playbackState == Player.STATE_BUFFERING -> "buffering"
                    player.playbackState == Player.STATE_ENDED -> "stopped"
                    else -> "paused"
                }
                sendStatus(state)
            }
        }
    }

    private fun sendStatus(state: String) {
        val player = exo ?: return
        val payload = JSONObject()
            .put("type", "status")
            .put("state", state)
            .put("position", player.currentPosition / 1000.0)
            .put("duration", player.duration.coerceAtLeast(0) / 1000.0)
            .put("media_key", mediaKey.value)
            .put("token", sessionToken.value)
            .put("title", title.value)
        socket?.send(payload)
    }

    private fun rebuildTracks(tracks: Tracks) {
        val audio = mutableListOf<TrackChoice>()
        val text = mutableListOf<TrackChoice>()
        for (group in tracks.groups) {
            if (group.type != C.TRACK_TYPE_AUDIO && group.type != C.TRACK_TYPE_TEXT) continue
            for (index in 0 until group.length) {
                val format = group.getTrackFormat(index)
                val label = buildString {
                    append((format.language ?: "und").uppercase())
                    if (!format.label.isNullOrBlank()) append(" · ${format.label}")
                    if (group.type == C.TRACK_TYPE_AUDIO && format.channelCount > 2) {
                        append(" · ${format.channelCount}ch")
                    }
                }
                val choice = TrackChoice(group, index, label, group.type)
                if (group.type == C.TRACK_TYPE_AUDIO) audio += choice else text += choice
            }
        }
        audioChoices.value = audio
        textChoices.value = text
    }

    private fun trackChoices(): List<TrackChoice> =
        audioChoices.value + TrackChoice(null, -1, "Subtitles off", C.TRACK_TYPE_TEXT) + textChoices.value

    private fun applyTrack(choice: TrackChoice) {
        val player = exo ?: return
        val builder = player.trackSelectionParameters.buildUpon()
        if (choice.group == null) {
            builder.setTrackTypeDisabled(C.TRACK_TYPE_TEXT, true)
        } else {
            val type = choice.group.type
            if (type == C.TRACK_TYPE_TEXT) builder.setTrackTypeDisabled(C.TRACK_TYPE_TEXT, false)
            builder.clearOverridesOfType(type)
            builder.addOverride(TrackSelectionOverride(choice.group.mediaTrackGroup, choice.trackIndex))
        }
        player.trackSelectionParameters = builder.build()
        AppLog.i("TVPLAYER", "track selected: ${choice.label}")
    }

    private fun showOverlay() {
        overlayVisible.value = true
        overlayTick.intValue += 1
    }

    private fun stopPlayback(silent: Boolean = false) {
        if (exo != null) {
            if (!silent) sendStatus("stopped")
            exo?.release()
            exo = null
        }
        statusJob?.cancel()
        statusJob = null
        playing.value = false
        panelOpen.value = false
        audioChoices.value = emptyList()
        textChoices.value = emptyList()
        if (!silent) {
            title.value = ""
            mediaKey.value = ""
            sessionToken.value = ""
            connection.value = "online"
        }
    }

    private fun toggle() {
        val player = exo ?: return
        if (player.isPlaying) player.pause() else player.play()
    }

    private fun seekBy(seconds: Long) {
        val player = exo ?: return
        val target = (player.currentPosition + seconds * 1000).coerceAtLeast(0)
        player.seekTo(target)
    }

    @Composable
    private fun TvRoot() {
        val focusRequester = remember { FocusRequester() }
        LaunchedEffect(Unit) { focusRequester.requestFocus() }
        Box(
            Modifier
                .fillMaxSize()
                .background(Color(0xFF08080B))
                .focusRequester(focusRequester)
                .focusable()
                .onPreviewKeyEvent { event ->
                    if (event.type != KeyEventType.KeyDown) return@onPreviewKeyEvent false
                    if (panelOpen.value) {
                        val last = trackChoices().lastIndex
                        return@onPreviewKeyEvent when (event.key) {
                            Key.DirectionDown -> {
                                panelIndex.intValue = (panelIndex.intValue + 1).coerceAtMost(last)
                                showOverlay(); true
                            }
                            Key.DirectionUp -> {
                                panelIndex.intValue = (panelIndex.intValue - 1).coerceAtLeast(0)
                                showOverlay(); true
                            }
                            Key.DirectionCenter -> {
                                trackChoices().getOrNull(panelIndex.intValue)?.let { applyTrack(it) }
                                panelOpen.value = false
                                showOverlay(); true
                            }
                            Key.Back -> { panelOpen.value = false; showOverlay(); true }
                            else -> false
                        }
                    }
                    when (event.key) {
                        Key.DirectionCenter -> { toggle(); showOverlay(); true }
                        Key.DirectionLeft -> { seekBy(-30); showOverlay(); true }
                        Key.DirectionRight -> { seekBy(30); showOverlay(); true }
                        Key.DirectionDown -> {
                            if (playing.value) {
                                panelOpen.value = true
                                panelIndex.intValue = 0
                                showOverlay()
                                true
                            } else false
                        }
                        Key.Back -> {
                            if (playing.value) {
                                stopPlayback()
                                true
                            } else {
                                false
                            }
                        }
                        else -> false
                    }
                },
        ) {
            if (!configured.value) {
                TvSetup()
            } else if (playing.value) {
                TvPlaying()
            } else {
                TvWaiting()
            }
        }
    }

    @Composable
    private fun TvSetup() {
        var server by remember { mutableStateOf(settings.baseUrl) }
        var token by remember { mutableStateOf(settings.token) }
        var name by remember { mutableStateOf(settings.playerName.ifBlank { "Android TV" }) }
        Column(
            Modifier.fillMaxSize().padding(64.dp),
            verticalArrangement = Arrangement.Center,
        ) {
            Text("VOD Cast — TV player", style = MaterialTheme.typography.headlineMedium)
            Text(
                "This screen only plays what you pick on the phone.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(24.dp))
            OutlinedTextField(
                value = server, onValueChange = { server = it },
                label = { Text("Server URL") }, singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(12.dp))
            OutlinedTextField(
                value = token, onValueChange = { token = it },
                label = { Text("App token") }, singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(12.dp))
            OutlinedTextField(
                value = name, onValueChange = { name = it },
                label = { Text("This TV's name") }, singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(24.dp))
            Button(
                onClick = { persist(server, token, name) },
                enabled = server.isNotBlank() && token.isNotBlank(),
            ) { Text("Connect this TV") }
        }
    }

    @Composable
    private fun TvWaiting() {
        Column(
            Modifier.fillMaxSize(),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text("VOD Cast", style = MaterialTheme.typography.displayMedium, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(8.dp))
            Text(
                playerName.value,
                style = MaterialTheme.typography.titleLarge,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(40.dp))
            Text(
                when (connection.value) {
                    "online" -> "Waiting for a title from your phone…"
                    "connecting", "starting" -> "Connecting to the server…"
                    else -> connection.value
                },
                style = MaterialTheme.typography.bodyLarge,
            )
            Spacer(Modifier.height(16.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    Modifier
                        .width(10.dp).height(10.dp)
                        .background(
                            if (connection.value == "online") Color(0xFF4CD964) else Color(0xFFE6A23C),
                            RoundedCornerShape(50),
                        )
                )
                Spacer(Modifier.width(8.dp))
                Text(connection.value, style = MaterialTheme.typography.bodySmall)
            }
        }
    }

    @Composable
    private fun TvPlaying() {
        Box(Modifier.fillMaxSize().background(Color.Black)) {
            AndroidView(
                modifier = Modifier.fillMaxSize(),
                factory = { context ->
                    PlayerView(context).apply {
                        useController = false
                        player = exo
                        isFocusable = false
                        isFocusableInTouchMode = false
                    }
                },
                update = { view -> view.player = exo },
            )
            LaunchedEffect(overlayTick.intValue, panelOpen.value) {
                overlayVisible.value = true
                if (panelOpen.value) return@LaunchedEffect
                delay(4000)
                overlayVisible.value = false
            }
            if (overlayVisible.value) {
                Column(
                    Modifier.align(Alignment.TopStart).padding(24.dp),
                ) {
                    Text(
                        title.value,
                        style = MaterialTheme.typography.titleMedium,
                        color = Color.White,
                    )
                    Text(
                        "OK play/pause · ◀ ▶ ±30s · ▼ tracks · Back stop",
                        style = MaterialTheme.typography.bodySmall,
                        color = Color(0xB3FFFFFF),
                    )
                }
            }
            if (panelOpen.value) TrackPanel()
        }
    }

    @Composable
    private fun BoxScope.TrackPanel() {
        val choices = trackChoices()
        Surface(
            color = Color(0xF2101014),
            shape = RoundedCornerShape(14.dp),
            modifier = Modifier
                .align(Alignment.CenterEnd)
                .padding(end = 32.dp)
                .widthIn(max = 560.dp),
        ) {
            Column(Modifier.padding(vertical = 12.dp)) {
                Text(
                    "Tracks",
                    style = MaterialTheme.typography.titleMedium,
                    color = Color.White,
                    modifier = Modifier.padding(horizontal = 20.dp, vertical = 6.dp),
                )
                LazyColumn {
                    itemsIndexed(choices) { index, choice ->
                        val selected = if (choice.group == null) {
                            textChoices.value.none { it.group?.isTrackSelected(it.trackIndex) == true }
                        } else {
                            choice.group.isTrackSelected(choice.trackIndex)
                        }
                        Row(
                            Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 12.dp, vertical = 3.dp)
                                .background(
                                    if (index == panelIndex.intValue) Color(0xFF2A2A33) else Color.Transparent,
                                    RoundedCornerShape(8.dp),
                                )
                                .clickable {
                                    panelIndex.intValue = index
                                    applyTrack(choice)
                                    panelOpen.value = false
                                    showOverlay()
                                }
                                .padding(horizontal = 12.dp, vertical = 10.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Text(
                                if (selected) "●" else "○",
                                color = if (selected) Color(0xFF4CD964) else Color(0x66FFFFFF),
                                modifier = Modifier.width(24.dp),
                            )
                            Text(
                                choice.label,
                                color = Color.White,
                                style = MaterialTheme.typography.bodyMedium,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }
                }
            }
        }
    }

    override fun onDestroy() {
        socket?.close()
        stopPlayback(silent = true)
        scope.cancel()
        super.onDestroy()
    }
}

data class TrackChoice(
    val group: Tracks.Group?,
    val trackIndex: Int,
    val label: String,
    val type: Int,
)

private class PlayerSocket(
    private val url: String,
    private val onOpen: () -> Unit,
    private val onClosed: () -> Unit,
    private val onMessage: (JSONObject) -> Unit,
) {
    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .build()
    private var webSocket: WebSocket? = null

    fun start() {
        val request = Request.Builder().url(url).build()
        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                onOpen()
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                runCatching { onMessage(JSONObject(text)) }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                onClosed()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                onClosed()
            }
        })
    }

    fun send(payload: JSONObject) {
        webSocket?.send(payload.toString())
    }

    fun close() {
        webSocket?.close(1000, "bye")
        webSocket = null
    }
}
