package com.souhail.vodcast.ui

import android.content.Intent
import android.widget.Toast
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.mediarouter.app.MediaRouteButton
import com.google.android.gms.cast.framework.CastButtonFactory
import com.souhail.vodcast.cast.CastCaster
import com.souhail.vodcast.cast.DlnaCaster
import com.souhail.vodcast.cast.DlnaDevice
import com.souhail.vodcast.data.Api
import com.souhail.vodcast.data.CastMethod
import com.souhail.vodcast.data.EpisodesResponse
import com.souhail.vodcast.data.MediaGroup
import com.souhail.vodcast.data.SessionInfo
import com.souhail.vodcast.data.Settings
import com.souhail.vodcast.data.StreamInfo
import com.souhail.vodcast.data.TvInfo
import com.souhail.vodcast.player.PlayerActivity
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

sealed class Screen {
    data object Setup : Screen()
    data object Search : Screen()
    data class Detail(val group: MediaGroup) : Screen()
    data object Tvs : Screen()
    data object Remote : Screen()
}

private data class PendingPlay(val mediaKey: String, val title: String)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun VodApp(settings: Settings) {
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    var api by remember {
        mutableStateOf(if (settings.configured) Api(settings.baseUrl, settings.token) else null)
    }
    var screen by remember { mutableStateOf<Screen>(if (settings.configured) Screen.Search else Screen.Setup) }
    var selectedTv by remember { mutableStateOf<TvInfo?>(null) }
    var lastStream by remember { mutableStateOf<StreamInfo?>(null) }
    var lastDevice by remember { mutableStateOf<DlnaDevice?>(null) }
    var lastTitle by remember { mutableStateOf("") }
    var pendingPlay by remember { mutableStateOf<PendingPlay?>(null) }
    var toast by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(toast) {
        toast?.let {
            Toast.makeText(context, it, Toast.LENGTH_SHORT).show()
            toast = null
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(if (screen is Screen.Setup) "Connect to VOD" else "VOD Cast") },
                navigationIcon = {
                    if (screen !is Screen.Setup && screen !is Screen.Search) {
                        IconButton(onClick = { screen = Screen.Search }) {
                            Icon(Icons.Default.ArrowBack, contentDescription = "Back")
                        }
                    }
                },
                actions = {
                    AndroidView(factory = { ctx ->
                        MediaRouteButton(ctx).also { CastButtonFactory.setUpMediaRouteButton(ctx, it) }
                    })
                    if (screen !is Screen.Setup) {
                        TextButton(onClick = { screen = Screen.Tvs }) { Text("TVs") }
                    }
                    if (lastStream != null && screen !is Screen.Remote) {
                        TextButton(onClick = { screen = Screen.Remote }) { Text("Remote") }
                    }
                },
            )
        },
    ) { padding ->
        Box(Modifier.padding(padding).fillMaxSize()) {
            when (val current = screen) {
                Screen.Setup -> SetupScreen(
                    initialUrl = settings.baseUrl,
                    initialToken = settings.token,
                ) { url, token ->
                    scope.launch {
                        try {
                            val candidate = Api(url, token)
                            candidate.config()
                            settings.baseUrl = url
                            settings.token = token
                            api = candidate
                            screen = Screen.Search
                        } catch (e: Exception) {
                            toast = "Connection failed: ${e.message}"
                        }
                    }
                }

                Screen.Search -> api?.let { client ->
                    SearchScreen(
                        api = client,
                        selectedTv = selectedTv,
                        onOpen = { screen = Screen.Detail(it) },
                    )
                }

                is Screen.Detail -> api?.let { client ->
                    DetailScreen(
                        group = current.group,
                        api = client,
                        onPlay = { key, title -> pendingPlay = PendingPlay(key, title) },
                    )
                }

                Screen.Tvs -> api?.let { client ->
                    TvScreen(
                        api = client,
                        selected = selectedTv,
                        onSelect = { tv ->
                            selectedTv = tv
                            settings.lastTv = tv.name
                            screen = Screen.Search
                        },
                        notify = { toast = it },
                    )
                }

                Screen.Remote -> {
                    val stream = lastStream
                    if (stream == null) {
                        Text("Nothing playing", modifier = Modifier.padding(24.dp))
                    } else {
                        RemoteScreen(
                            api = api!!,
                            stream = stream,
                            title = lastTitle,
                            device = lastDevice,
                            onStop = {
                                lastStream = null
                                lastDevice = null
                                screen = Screen.Search
                            },
                            notify = { toast = it },
                        )
                    }
                }
            }
        }
    }

    pendingPlay?.let { play ->
        PlayDialog(
            api = api!!,
            pending = play,
            selectedTv = selectedTv,
            onPickTv = {
                pendingPlay = null
                screen = Screen.Tvs
            },
            onStart = { stream, device, title ->
                lastStream = stream
                lastDevice = device
                lastTitle = title
                pendingPlay = null
                screen = Screen.Remote
            },
            onDismiss = { pendingPlay = null },
            notify = { toast = it },
        )
    }
}

@Composable
private fun SetupScreen(initialUrl: String, initialToken: String, onSave: (String, String) -> Unit) {
    var url by remember { mutableStateOf(initialUrl.ifBlank { "http://192.168.1.240:8095" }) }
    var token by remember { mutableStateOf(initialToken) }
    Column(
        modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Server", style = MaterialTheme.typography.headlineSmall)
        Text(
            "LAN: http://<server-ip>:8095\nPublic: https://vod.allocamera.com\n\n" +
                "The token is the app_token setting on the server.",
            style = MaterialTheme.typography.bodyMedium,
        )
        OutlinedTextField(
            value = url,
            onValueChange = { url = it },
            label = { Text("Server URL") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = token,
            onValueChange = { token = it },
            label = { Text("App token") },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Button(
            onClick = { onSave(url.trim().trimEnd('/'), token.trim()) },
            enabled = url.isNotBlank() && token.isNotBlank(),
        ) { Text("Connect") }
    }
}

@Composable
private fun SearchScreen(api: Api, selectedTv: TvInfo?, onOpen: (MediaGroup) -> Unit) {
    val scope = rememberCoroutineScope()
    var query by remember { mutableStateOf("") }
    var series by remember { mutableStateOf(false) }
    var results by remember { mutableStateOf<List<MediaGroup>>(emptyList()) }
    var loading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    Column(Modifier.fillMaxSize().padding(16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(
                value = query,
                onValueChange = { query = it },
                label = { Text("Search movies / series") },
                singleLine = true,
                modifier = Modifier.weight(1f),
            )
            Spacer(Modifier.width(8.dp))
            Button(
                onClick = {
                    scope.launch {
                        loading = true
                        error = null
                        try {
                            results = api.search(query, if (series) "series" else "movie")
                        } catch (e: Exception) {
                            error = e.message
                        } finally {
                            loading = false
                        }
                    }
                },
                enabled = query.isNotBlank() && !loading,
            ) { Text("Search") }
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Checkbox(checked = series, onCheckedChange = { series = it })
            Text("Series")
            Spacer(Modifier.width(16.dp))
            Text(
                selectedTv?.let { "TV: ${it.name}" } ?: "No TV selected (open TVs)",
                style = MaterialTheme.typography.bodySmall,
            )
        }
        if (loading) {
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
        }
        error?.let { Text("Error: $it", color = MaterialTheme.colorScheme.error) }
        LazyColumn(Modifier.fillMaxSize()) {
            items(results) { group ->
                Card(
                    modifier = Modifier.fillMaxWidth().padding(vertical = 6.dp).clickable { onOpen(group) },
                ) {
                    ListItem(
                        headlineContent = { Text(group.title, fontWeight = FontWeight.SemiBold) },
                        supportingContent = {
                            val langs = group.variants.mapNotNull { it.lang }.distinct().joinToString("/")
                            Text(
                                listOfNotNull(group.year?.toString(), langs.ifBlank { null }, "%.2f".format(group.score))
                                    .joinToString(" · ")
                            )
                        },
                        trailingContent = { Text(group.kind) },
                    )
                }
            }
        }
    }
}

@Composable
private fun DetailScreen(group: MediaGroup, api: Api, onPlay: (String, String) -> Unit) {
    var episodes by remember { mutableStateOf<EpisodesResponse?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    val seriesId = group.variants.firstOrNull()?.media_key?.removePrefix("series:")?.toIntOrNull()

    LaunchedEffect(group) {
        if (group.kind == "series" && seriesId != null) {
            try {
                episodes = api.episodes(seriesId)
            } catch (e: Exception) {
                error = e.message
            }
        }
    }

    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp)) {
        Text(group.title, style = MaterialTheme.typography.headlineSmall)
        Text(
            listOfNotNull(group.year?.toString(), "score %.2f".format(group.score)).joinToString(" · "),
            style = MaterialTheme.typography.bodyMedium,
        )
        Spacer(Modifier.height(12.dp))
        if (group.kind == "series") {
            val data = episodes
            if (data == null && error == null) {
                CircularProgressIndicator()
            }
            data?.items?.forEach { (season, entries) ->
                Text(
                    "Season $season",
                    style = MaterialTheme.typography.titleMedium,
                    modifier = Modifier.padding(top = 12.dp),
                )
                entries.forEach { ep ->
                    ListItem(
                        headlineContent = { Text(ep.title ?: "Episode ${ep.episode}") },
                        supportingContent = { Text("E${ep.episode}" + (ep.ext?.let { " · $it" } ?: "")) },
                        modifier = Modifier.clickable {
                            onPlay(ep.media_key, "${group.title} S${season}E${ep.episode}")
                        },
                    )
                }
            }
        } else {
            group.variants.forEach { variant ->
                ListItem(
                    headlineContent = { Text(variant.lang ?: "Original") },
                    supportingContent = { Text(variant.ext ?: "") },
                    modifier = Modifier.clickable { onPlay(variant.media_key, group.title) },
                )
            }
        }
        error?.let { Text("Error: $it", color = MaterialTheme.colorScheme.error) }
    }
}

@Composable
private fun TvScreen(
    api: Api,
    selected: TvInfo?,
    onSelect: (TvInfo) -> Unit,
    notify: (String) -> Unit,
) {
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    var tvs by remember { mutableStateOf<List<TvInfo>>(emptyList()) }
    var devices by remember { mutableStateOf<List<DlnaDevice>>(emptyList()) }
    var methods by remember { mutableStateOf<Map<String, List<CastMethod>>>(emptyMap()) }
    var busy by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) {
        try {
            tvs = api.tvs()
        } catch (e: Exception) {
            notify(e.message ?: "failed to load TVs")
        }
    }

    Column(Modifier.fillMaxSize().padding(16.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = {
                scope.launch {
                    busy = true
                    try {
                        devices = DlnaCaster(context).discover()
                        if (devices.isEmpty()) notify("No DLNA renderers answered SSDP")
                    } catch (e: Exception) {
                        notify(e.message ?: "discovery failed")
                    } finally {
                        busy = false
                    }
                }
            }, enabled = !busy) { Text("Discover DLNA") }
            if (busy) CircularProgressIndicator(modifier = Modifier.height(24.dp))
        }
        Spacer(Modifier.height(8.dp))
        LazyColumn(Modifier.fillMaxSize()) {
            item { Text("Registered TVs", style = MaterialTheme.typography.titleMedium) }
            items(tvs) { tv ->
                ListItem(
                    headlineContent = { Text(tv.name, fontWeight = FontWeight.SemiBold) },
                    supportingContent = {
                        Text(listOfNotNull(tv.ip, tv.kind, tv.prefer_proxy.takeIf { it == 1 }?.let { "proxy" }).joinToString(" · "))
                    },
                    trailingContent = {
                        TextButton(onClick = {
                            scope.launch {
                                try {
                                    methods = methods + (tv.name to api.probe(tv.name).methods)
                                } catch (e: Exception) {
                                    notify(e.message ?: "probe failed")
                                }
                            }
                        }) { Text("Probe") }
                    },
                    modifier = Modifier.clickable { onSelect(tv) },
                )
                if (selected?.name == tv.name) {
                    Text("selected", color = MaterialTheme.colorScheme.primary, modifier = Modifier.padding(start = 16.dp))
                }
                methods[tv.name]?.let { list ->
                    Text(
                        list.joinToString("  ") { "${it.method}:${if (it.available) "+" else "-"}" },
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(start = 16.dp, bottom = 6.dp),
                    )
                }
                HorizontalDivider()
            }
            if (devices.isNotEmpty()) {
                item { Text("Discovered on this network", style = MaterialTheme.typography.titleMedium, modifier = Modifier.padding(top = 12.dp)) }
                items(devices) { device ->
                    ListItem(
                        headlineContent = { Text(device.name) },
                        supportingContent = { Text("${device.ip} · ${device.controlUrl}") },
                        modifier = Modifier.clickable {
                            onSelect(TvInfo(name = device.name, ip = device.ip, kind = "dlna"))
                        },
                    )
                }
            }
        }
    }
}

@Composable
private fun PlayDialog(
    api: Api,
    pending: PendingPlay,
    selectedTv: TvInfo?,
    onPickTv: () -> Unit,
    onStart: (StreamInfo, DlnaDevice?, String) -> Unit,
    onDismiss: () -> Unit,
    notify: (String) -> Unit,
) {
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    var target by remember { mutableStateOf("dlna") }
    var quality by remember { mutableStateOf("copy") }
    var usePublic by remember { mutableStateOf(false) }
    var prepared by remember { mutableStateOf<StreamInfo?>(null) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    fun prepare() {
        scope.launch {
            busy = true
            error = null
            try {
                prepared = api.stream(
                    mediaKey = pending.mediaKey,
                    mode = "cache",
                    quality = if (target == "cast") "copy" else quality,
                    prepare = true,
                    format = if (target == "cast") "mp4" else null,
                )
            } catch (e: Exception) {
                error = e.message
            } finally {
                busy = false
            }
        }
    }

    fun start() {
        val stream = prepared ?: return
        val url = if (usePublic) stream.public_url else stream.lan_url
        if (url.isNullOrBlank()) {
            error = "No ${if (usePublic) "public" else "LAN"} URL — pick a different option."
            return
        }
        when (target) {
            "player" -> {
                context.startActivity(
                    Intent(context, PlayerActivity::class.java)
                        .putExtra("url", url)
                        .putExtra("title", pending.title),
                )
                onStart(stream, null, pending.title)
            }
            "cast" -> {
                val message = runCatching {
                    CastCaster.load(context, url, pending.title, "video/mp4")
                }.getOrElse { "Cast failed: ${it.message}" }
                notify(message)
                onStart(stream, null, pending.title)
            }
            else -> {
                val tv = selectedTv
                if (tv == null) {
                    onPickTv()
                    return
                }
                scope.launch {
                    busy = true
                    try {
                        val caster = DlnaCaster(context)
                        val devices = caster.discover()
                        val device = devices.firstOrNull { it.ip == tv.ip }
                            ?: devices.firstOrNull { it.name.contains(tv.name, ignoreCase = true) }
                            ?: devices.firstOrNull()
                        if (device == null) {
                            error = "No DLNA renderer found for ${tv.name}"
                            return@launch
                        }
                        caster.cast(device, url, pending.title, stream.mime ?: "video/mpeg")
                        onStart(stream, device, pending.title)
                    } catch (e: Exception) {
                        error = e.message
                    } finally {
                        busy = false
                    }
                }
            }
        }
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(pending.title) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("Target", style = MaterialTheme.typography.labelLarge)
                SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                    listOf("dlna" to "DLNA TV", "player" to "Phone", "cast" to "Chromecast").forEachIndexed { index, (value, label) ->
                        SegmentedButton(
                            selected = target == value,
                            onClick = { target = value; prepared = null },
                            shape = SegmentedButtonDefaults.itemShape(index, 3),
                        ) { Text(label) }
                    }
                }
                Text(
                    selectedTv?.let { "TV: ${it.name} (${it.kind})" } ?: "No TV selected",
                    style = MaterialTheme.typography.bodySmall,
                )
                if (target == "dlna" && selectedTv == null) {
                    TextButton(onClick = onPickTv) { Text("Pick a TV") }
                }
                if (target != "cast") {
                    Text("Quality", style = MaterialTheme.typography.labelLarge)
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(checked = quality == "copy", onCheckedChange = { quality = "copy"; prepared = null })
                        Text("Copy (fast)")
                        Spacer(Modifier.width(12.dp))
                        Checkbox(checked = quality == "downgrade", onCheckedChange = { quality = "downgrade"; prepared = null })
                        Text("1080p transcode")
                    }
                }
                if (prepared == null) {
                    Button(onClick = { prepare() }, enabled = !busy) { Text("Prepare link") }
                } else {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(
                            checked = usePublic,
                            enabled = prepared?.public_url != null,
                            onCheckedChange = { usePublic = it },
                        )
                        Text("Public URL (works outside home)")
                    }
                    Text(
                        (if (usePublic) prepared?.public_url else prepared?.lan_url) ?: "",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Text(
                        "mode=${prepared?.mode} format=${prepared?.format} mime=${prepared?.mime}",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
                if (busy) CircularProgressIndicator()
                error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            }
        },
        confirmButton = {
            TextButton(
                onClick = { if (prepared == null) prepare() else start() },
                enabled = !busy,
            ) { Text(if (prepared == null) "Prepare" else "Start") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun RemoteScreen(
    api: Api,
    stream: StreamInfo,
    title: String,
    device: DlnaDevice?,
    onStop: () -> Unit,
    notify: (String) -> Unit,
) {
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    var session by remember { mutableStateOf<SessionInfo?>(null) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(stream.token) {
        val token = stream.token ?: return@LaunchedEffect
        while (true) {
            try {
                session = api.session(token)
                error = null
            } catch (e: Exception) {
                error = e.message
            }
            delay(2000)
        }
    }

    Column(Modifier.fillMaxSize().padding(20.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        Text(title, style = MaterialTheme.typography.headlineSmall)
        Text("session ${stream.token} · ${stream.mode}/${stream.format}", style = MaterialTheme.typography.bodySmall)
        val current = session
        if (current?.ok == true) {
            Text(
                "position ${current.position?.toInt() ?: 0}s / ${current.duration?.toInt() ?: 0}s",
                style = MaterialTheme.typography.titleMedium,
            )
            Text("served ${"%.1f".format((current.max_served ?: 0L) / 1_048_576.0)} MB · clients ${current.clients}")
        } else {
            Text(current?.error ?: "waiting for session…")
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            val token = stream.token
            Button(onClick = {
                if (token != null) scope.launch {
                    try {
                        val pos = (session?.position ?: 0.0) - 30.0
                        api.control(token, "seek", maxOf(0.0, pos))
                    } catch (e: Exception) {
                        notify(e.message ?: "seek failed")
                    }
                }
            }) { Text("-30s") }
            Button(onClick = {
                if (token != null) scope.launch {
                    try {
                        val pos = (session?.position ?: 0.0) + 30.0
                        api.control(token, "seek", pos)
                    } catch (e: Exception) {
                        notify(e.message ?: "seek failed")
                    }
                }
            }) { Text("+30s") }
        }
        Button(onClick = {
            scope.launch {
                try {
                    stream.token?.let { api.control(it, "stop") }
                    device?.let { runCatching { DlnaCaster(context).stop(it) } }
                    CastCaster.stop(context)
                } catch (_: Exception) {
                }
                onStop()
            }
        }) { Text("Stop") }
        error?.let { Text("Error: $it", color = MaterialTheme.colorScheme.error) }
    }
}
