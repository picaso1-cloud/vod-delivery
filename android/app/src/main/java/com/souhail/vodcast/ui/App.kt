package com.souhail.vodcast.ui

import android.content.Intent
import android.widget.Toast
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.BugReport
import androidx.compose.material.icons.filled.Cast
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.FilterList
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material.icons.filled.Tv
import androidx.compose.material.icons.filled.VideoLibrary
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.SuggestionChip
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.mediarouter.app.MediaRouteButton
import coil.compose.AsyncImage
import com.google.android.gms.cast.framework.CastButtonFactory
import com.souhail.vodcast.cast.CastCaster
import com.souhail.vodcast.cast.DlnaCaster
import com.souhail.vodcast.cast.DlnaDevice
import com.souhail.vodcast.data.Api
import com.souhail.vodcast.data.CastMethod
import com.souhail.vodcast.data.ContinueItem
import com.souhail.vodcast.data.DiscoverResponse
import com.souhail.vodcast.data.EpisodesResponse
import com.souhail.vodcast.data.LibraryItem
import com.souhail.vodcast.data.LibraryResponse
import com.souhail.vodcast.data.QueueItem
import com.souhail.vodcast.data.QueueResponse
import com.souhail.vodcast.data.LiveCategory
import com.souhail.vodcast.data.LiveChannel
import com.souhail.vodcast.data.MediaGroup
import com.souhail.vodcast.data.MediaVariant
import com.souhail.vodcast.data.SessionInfo
import com.souhail.vodcast.data.ProgressInfo
import com.souhail.vodcast.data.Settings
import com.souhail.vodcast.data.StatusInfo
import com.souhail.vodcast.data.StreamInfo
import com.souhail.vodcast.data.TvInfo
import com.souhail.vodcast.data.WatchlistItem
import com.souhail.vodcast.data.WatchlistResponse
import com.souhail.vodcast.player.PlayerActivity
import com.souhail.vodcast.util.AppLog
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

sealed class Screen {
    data object Setup : Screen()
    data object Discover : Screen()
    data object Library : Screen()
    data object Tvs : Screen()
    data class Detail(val group: MediaGroup) : Screen()
    data object Remote : Screen()
    data object Logs : Screen()
}

private data class PendingPlay(
    val mediaKey: String,
    val title: String,
    val isLive: Boolean = false,
    val startPosition: Double = 0.0,
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun VodApp(settings: Settings) {
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    var api by remember {
        mutableStateOf(if (settings.configured) Api(settings.baseUrl, settings.token) else null)
    }
    var usingLan by remember { mutableStateOf(settings.usingLan) }
    var screen by remember { mutableStateOf<Screen>(if (settings.configured) Screen.Discover else Screen.Setup) }
    var selectedTv by remember { mutableStateOf<TvInfo?>(null) }
    var lastStream by remember { mutableStateOf<StreamInfo?>(null) }
    var lastDevice by remember { mutableStateOf<DlnaDevice?>(null) }
    var lastTitle by remember { mutableStateOf("") }
    var pendingPlay by remember { mutableStateOf<PendingPlay?>(null) }
    var toast by remember { mutableStateOf<String?>(null) }

    var contentType by remember { mutableStateOf("movie") }
    var query by remember { mutableStateOf("") }
    var discover by remember { mutableStateOf<DiscoverResponse?>(null) }
    var liveResults by remember { mutableStateOf<List<LiveChannel>>(emptyList()) }
    var selectedCategory by remember { mutableStateOf(0) }
    var searching by remember { mutableStateOf(false) }
    var searchError by remember { mutableStateOf<String?>(null) }

    var library by remember { mutableStateOf<LibraryResponse?>(null) }
    var watchlist by remember { mutableStateOf<WatchlistResponse?>(null) }
    var queue by remember { mutableStateOf<QueueResponse?>(null) }
    var libTab by remember { mutableStateOf(0) }
    var libLoading by remember { mutableStateOf(false) }
    var continueItems by remember { mutableStateOf<List<ContinueItem>>(emptyList()) }
    var filterRating by remember { mutableStateOf(0.0) }
    var filterYearFrom by remember { mutableStateOf(0) }
    var filterYearTo by remember { mutableStateOf(0) }
    var filterLang by remember { mutableStateOf("") }

    LaunchedEffect(api) {
        api?.let {
            runCatching { it.continueWatching() }
                .onSuccess { resp -> continueItems = resp.items }
                .onFailure { err -> AppLog.w("CONTINUE", err.message ?: "") }
        }
    }

    LaunchedEffect(toast) {
        toast?.let {
            AppLog.notify(it)
            Toast.makeText(context, it, Toast.LENGTH_SHORT).show()
            toast = null
        }
    }

    LaunchedEffect(screen) {
        AppLog.action("screen", screen::class.simpleName ?: "?")
    }

    BackHandler(enabled = screen !is Screen.Setup && screen !is Screen.Discover) {
        AppLog.action("back", screen::class.simpleName ?: "?")
        screen = Screen.Discover
    }

    LaunchedEffect(Unit) {
        if (settings.configured) AppLog.setRemote(settings.baseUrl, settings.token)
    }

    fun runSearch(text: String = query, kind: String = contentType, category: Int = selectedCategory) {
        if (text.isBlank() && kind != "live") return
        scope.launch {
            searching = true
            searchError = null
            AppLog.action(
                "search",
                "type=$kind q=$text category=$category filters=$filterRating/$filterYearFrom/$filterYearTo/$filterLang",
            )
            try {
                if (kind == "live") {
                    liveResults = api!!.live(text, category)
                } else {
                    val result = api!!.discover(
                        text, kind, filterYearFrom, filterYearTo, filterRating, filterLang,
                    )
                    discover = result
                    if (!result.ok) searchError = "search failed"
                    searching = false
                    scope.launch {
                        try {
                            val ai = api!!.discoverAi(
                                text, kind, filterYearFrom, filterYearTo, filterRating, filterLang,
                            )
                            discover = discover?.copy(
                                ai_picks = ai.ai_picks,
                                ai_note = ai.ai_note,
                                ai_pending = false,
                            )
                            AppLog.i("DISCOVER", "ai picks=${ai.ai_picks.size}")
                        } catch (e: Exception) {
                            AppLog.w("DISCOVER", "AI failed: ${e.message}")
                            discover = discover?.copy(ai_pending = false, ai_note = "AI picks unavailable")
                        }
                    }
                }
            } catch (e: Exception) {
                AppLog.e("SEARCH", "failed", e)
                searchError = e.message
            } finally {
                searching = false
            }
        }
    }

    fun openContinue(item: ContinueItem) {
        AppLog.action("continue", "${item.title} at ${item.position.toInt()}s")
        pendingPlay = PendingPlay(item.media_key, item.title, false, item.position)
    }

    fun loadLibrary() {
        scope.launch {
            libLoading = true
            try {
                library = api!!.library()
                watchlist = api!!.watchlist()
                queue = api!!.queue()
            } catch (e: Exception) {
                AppLog.e("LIBRARY", "load failed", e)
                toast = e.message
            } finally {
                libLoading = false
            }
        }
    }

    fun openGroup(group: MediaGroup) {
        AppLog.action("open_detail", group.title)
        screen = Screen.Detail(group)
    }

    fun playLibrary(item: LibraryItem) {
        scope.launch {
            try {
                val info = api!!.libraryStream(item.media_key)
                if (!info.ok) {
                    toast = info.error ?: "Cannot play this item"
                    return@launch
                }
                val url = if (usingLan) info.lan_url else info.public_url
                if (url.isNullOrBlank()) {
                    toast = "No playable URL"
                    return@launch
                }
                AppLog.action("library_play", "media=${item.media_key} route=${if (usingLan) "lan" else "public"}")
                context.startActivity(
                    Intent(context, PlayerActivity::class.java)
                        .putExtra("url", url)
                        .putExtra("title", item.title),
                )
            } catch (e: Exception) {
                AppLog.e("LIBRARY", "play failed", e)
                toast = e.message
            }
        }
    }

    fun castLibrary(item: LibraryItem) {
        val tv = selectedTv
        if (tv == null) {
            toast = "Pick a TV first"
            screen = Screen.Tvs
            return
        }
        scope.launch {
            try {
                AppLog.action("library_cast", "media=${item.media_key} tv=${tv.name}")
                val result = api!!.play(item.media_key, tv.name)
                if (!result.ok) {
                    toast = result.detail ?: result.error ?: "Cast failed"
                    return@launch
                }
                lastStream = StreamInfo(ok = true, mode = "server", title = item.title)
                lastDevice = null
                lastTitle = item.title
                screen = Screen.Remote
            } catch (e: Exception) {
                AppLog.e("LIBRARY", "cast failed", e)
                toast = e.message
            }
        }
    }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        topBar = {
            TopAppBar(
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.background,
                ),
                title = {
                    Text(
                        when (screen) {
                            is Screen.Setup -> "Connect"
                            is Screen.Library -> "Library"
                            is Screen.Tvs -> "TVs"
                            is Screen.Logs -> "Logs"
                            is Screen.Detail -> "Details"
                            is Screen.Remote -> "Now playing"
                            else -> "VOD Cast"
                        },
                        style = MaterialTheme.typography.titleLarge,
                    )
                },
                navigationIcon = {
                    if (screen is Screen.Detail || screen is Screen.Remote || screen is Screen.Logs) {
                        IconButton(onClick = { screen = Screen.Discover }) {
                            Icon(Icons.Default.ArrowBack, contentDescription = "Back")
                        }
                    }
                },
                actions = {
                    if (screen !is Screen.Setup) {
                        RoutePill(usingLan)
                        IconButton(onClick = { screen = Screen.Logs }) {
                            Icon(Icons.Default.BugReport, contentDescription = "Logs")
                        }
                        AndroidView(factory = { ctx ->
                            MediaRouteButton(ctx).also { CastButtonFactory.setUpMediaRouteButton(ctx, it) }
                        })
                        if (lastStream != null && screen !is Screen.Remote) {
                            TextButton(onClick = { screen = Screen.Remote }) { Text("Remote") }
                        }
                        IconButton(onClick = { screen = Screen.Setup }) {
                            Icon(Icons.Default.Settings, contentDescription = "Setup")
                        }
                    }
                },
            )
        },
        bottomBar = {
            if (screen is Screen.Discover || screen is Screen.Library || screen is Screen.Tvs) {
                NavigationBar(containerColor = MaterialTheme.colorScheme.surface) {
                    NavigationBarItem(
                        selected = screen is Screen.Discover,
                        onClick = { screen = Screen.Discover },
                        icon = { Icon(Icons.Default.Search, contentDescription = null) },
                        label = { Text("Discover") },
                    )
                    NavigationBarItem(
                        selected = screen is Screen.Library,
                        onClick = {
                            screen = Screen.Library
                            loadLibrary()
                        },
                        icon = { Icon(Icons.Default.VideoLibrary, contentDescription = null) },
                        label = { Text("Library") },
                    )
                    NavigationBarItem(
                        selected = screen is Screen.Tvs,
                        onClick = { screen = Screen.Tvs },
                        icon = { Icon(Icons.Default.Tv, contentDescription = null) },
                        label = { Text("TVs") },
                    )
                }
            }
        },
    ) { padding ->
        Box(Modifier.padding(padding).fillMaxSize()) {
            when (val current = screen) {
                Screen.Setup -> SetupScreen(
                    initialLan = settings.lanUrl,
                    initialPublic = settings.publicUrl,
                    initialToken = settings.token,
                ) { lan, pub, token ->
                    scope.launch {
                        var chosen: Api? = null
                        var lanOk = false
                        try {
                            val candidate = Api(lan, token)
                            candidate.config()
                            chosen = candidate
                            lanOk = true
                        } catch (_: Exception) {
                        }
                        if (chosen == null && pub.isNotBlank()) {
                            try {
                                val candidate = Api(pub, token)
                                candidate.config()
                                chosen = candidate
                            } catch (_: Exception) {
                            }
                        }
                        if (chosen == null) {
                            AppLog.w("CONNECT", "both URLs unreachable")
                            toast = "Cannot reach either URL — check the server and token"
                            return@launch
                        }
                        settings.lanUrl = lan
                        settings.publicUrl = pub
                        settings.baseUrl = if (lanOk) lan else pub
                        settings.token = token
                        settings.usingLan = lanOk
                        AppLog.setRemote(settings.baseUrl, token)
                        api = chosen
                        usingLan = lanOk
                        screen = Screen.Discover
                        AppLog.action("connect", "route=${if (lanOk) "lan" else "public"}")
                        toast = if (lanOk) "Connected over LAN" else "Connected over public URL"
                    }
                }

                Screen.Discover -> api?.let { client ->
                    DiscoverScreen(
                        api = client,
                        selectedTv = selectedTv,
                        contentType = contentType,
                        onContentType = { type ->
                            contentType = type
                            discover = null
                            liveResults = emptyList()
                            if (type == "live") runSearch(text = "", kind = "live", category = 0)
                        },
                        query = query,
                        onQuery = { query = it },
                        discover = discover,
                        liveResults = liveResults,
                        selectedCategory = selectedCategory,
                        onCategory = { id ->
                            selectedCategory = id
                            runSearch(text = query, kind = "live", category = id)
                        },
                        searching = searching,
                        error = searchError,
                        onSearch = { runSearch() },
                        onSuggestion = { text -> query = text; runSearch(text) },
                        onOpen = { openGroup(it) },
                        onOpenLive = {
                            AppLog.action("open_live", "${it.title} (${it.channel_id})")
                            pendingPlay = PendingPlay("live:${it.channel_id}", it.title, true)
                        },
                        continueItems = continueItems,
                        onContinue = { openContinue(it) },
                        filterRating = filterRating,
                        filterYearFrom = filterYearFrom,
                        filterYearTo = filterYearTo,
                        filterLang = filterLang,
                        onFilters = { rating, yf, yt, lang ->
                            filterRating = rating
                            filterYearFrom = yf
                            filterYearTo = yt
                            filterLang = lang
                            runSearch()
                        },
                    )
                }

                is Screen.Detail -> api?.let { client ->
                    DetailScreen(
                        group = current.group,
                        api = client,
                        onPlay = { key, title, start ->
                            pendingPlay = PendingPlay(key, title, false, start)
                        },
                        onOpen = { openGroup(it) },
                        notify = { toast = it },
                    )
                }

                Screen.Library -> api?.let {
                    LibraryScreen(
                        library = library,
                        watchlist = watchlist,
                        loading = libLoading,
                        tab = libTab,
                        onTab = { libTab = it },
                        queue = queue,
                        onCancelQueue = { item ->
                            scope.launch {
                                try {
                                    api!!.queueCancel(item.media_key)
                                    toast = "Removed from queue"
                                    loadLibrary()
                                } catch (e: Exception) {
                                    toast = e.message
                                }
                            }
                        },
                        onRefresh = { loadLibrary() },
                        onPlay = { playLibrary(it) },
                        onCast = { castLibrary(it) },
                        onOpenWatchlist = { item ->
                            val key = if (item.kind == "movie") "movie:${item.ref_id}" else "series:${item.ref_id}"
                            openGroup(
                                MediaGroup(
                                    kind = item.kind,
                                    title = item.title,
                                    year = item.year,
                                    poster = item.poster,
                                    variants = listOf(MediaVariant(media_key = key)),
                                )
                            )
                        },
                        onRemoveWatchlist = { item ->
                            scope.launch {
                                val key = if (item.kind == "movie") "movie:${item.ref_id}" else "series:${item.ref_id}"
                                try {
                                    api!!.watchlistRemove(key)
                                    toast = "Removed from watchlist"
                                    loadLibrary()
                                } catch (e: Exception) {
                                    toast = e.message
                                }
                            }
                        },
                        onPickTv = { screen = Screen.Tvs },
                        selectedTv = selectedTv,
                    )
                }

                Screen.Tvs -> api?.let { client ->
                    TvScreen(
                        api = client,
                        selected = selectedTv,
                        onSelect = { tv ->
                            AppLog.action("tv_select", "${tv.name} ${tv.ip ?: ""} kind=${tv.kind ?: "?"}")
                            selectedTv = tv
                            settings.lastTv = tv.name
                            screen = Screen.Discover
                        },
                        notify = { toast = it },
                    )
                }

                Screen.Remote -> {
                    val stream = lastStream
                    if (stream == null) {
                        EmptyState("Nothing playing", "Start a movie, show, or channel first.")
                    } else {
                        RemoteScreen(
                            api = api!!,
                            stream = stream,
                            title = lastTitle,
                            device = lastDevice,
                            onStop = {
                                lastStream = null
                                lastDevice = null
                                screen = Screen.Discover
                            },
                            notify = { toast = it },
                        )
                    }
                }

                Screen.Logs -> LogsScreen()
            }
        }
    }

    pendingPlay?.let { play ->
        PlayDialog(
            api = api!!,
            pending = play,
            selectedTv = selectedTv,
            usingLan = usingLan,
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
private fun RoutePill(usingLan: Boolean) {
    Box(
        Modifier
            .clip(RoundedCornerShape(50))
            .background(
                if (usingLan) MaterialTheme.colorScheme.primary.copy(alpha = 0.16f)
                else MaterialTheme.colorScheme.tertiary.copy(alpha = 0.16f)
            )
            .padding(horizontal = 10.dp, vertical = 4.dp),
    ) {
        Text(
            if (usingLan) "LAN" else "PUBLIC",
            style = MaterialTheme.typography.labelMedium,
            color = if (usingLan) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.tertiary,
        )
    }
}

@Composable
private fun SetupScreen(
    initialLan: String,
    initialPublic: String,
    initialToken: String,
    onSave: (String, String, String) -> Unit,
) {
    var lan by remember { mutableStateOf(initialLan.ifBlank { "http://192.168.1.240:8095" }) }
    var pub by remember { mutableStateOf(initialPublic.ifBlank { "https://vod.allocamera.com" }) }
    var token by remember { mutableStateOf(initialToken) }
    Column(
        modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Text("Your server", style = MaterialTheme.typography.headlineMedium)
        Text(
            "The app tries the LAN URL first and falls back to the public one.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        OutlinedTextField(
            value = lan, onValueChange = { lan = it }, label = { Text("LAN URL") },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = pub, onValueChange = { pub = it }, label = { Text("Public URL (optional)") },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        OutlinedTextField(
            value = token, onValueChange = { token = it }, label = { Text("App token") },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        Button(
            onClick = { onSave(lan.trim().trimEnd('/'), pub.trim().trimEnd('/'), token.trim()) },
            enabled = lan.isNotBlank() && token.isNotBlank(),
            modifier = Modifier.fillMaxWidth(),
        ) { Text("Connect") }
    }
}

@Composable
private fun DiscoverScreen(
    api: Api,
    selectedTv: TvInfo?,
    contentType: String,
    onContentType: (String) -> Unit,
    query: String,
    onQuery: (String) -> Unit,
    discover: DiscoverResponse?,
    liveResults: List<LiveChannel>,
    selectedCategory: Int,
    onCategory: (Int) -> Unit,
    searching: Boolean,
    error: String?,
    onSearch: () -> Unit,
    onSuggestion: (String) -> Unit,
    onOpen: (MediaGroup) -> Unit,
    onOpenLive: (LiveChannel) -> Unit,
    continueItems: List<ContinueItem>,
    onContinue: (ContinueItem) -> Unit,
    filterRating: Double,
    filterYearFrom: Int,
    filterYearTo: Int,
    filterLang: String,
    onFilters: (Double, Int, Int, String) -> Unit,
) {
    var categories by remember { mutableStateOf<List<LiveCategory>>(emptyList()) }
    var showFilters by remember { mutableStateOf(false) }

    LaunchedEffect(contentType) {
        if (contentType == "live" && categories.isEmpty()) {
            runCatching { api.liveCategories() }.onSuccess { categories = it }
        }
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(bottom = 24.dp),
    ) {
        item {
            Column(Modifier.padding(horizontal = 16.dp, vertical = 8.dp)) {
                Text("Find something", style = MaterialTheme.typography.displaySmall)
                Text(
                    "Search by title, a mood, or a whole sentence.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }
        }
        item {
            Row(
                Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                OutlinedTextField(
                    value = query,
                    onValueChange = onQuery,
                    placeholder = {
                        Text(if (contentType == "live") "Search channels" else "e.g. survival and zombies")
                    },
                    singleLine = true,
                    shape = RoundedCornerShape(28.dp),
                    modifier = Modifier.weight(1f),
                )
                Spacer(Modifier.width(8.dp))
                FilledIconButton(onClick = onSearch, enabled = !searching) {
                    Icon(Icons.Default.Search, contentDescription = "Search")
                }
            }
        }
        item {
            Row(
                Modifier.padding(horizontal = 16.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                listOf("movie" to "Movies", "series" to "Shows", "live" to "Live TV").forEach { (value, label) ->
                    FilterChip(
                        selected = contentType == value,
                        onClick = { onContentType(value) },
                        label = { Text(label) },
                    )
                }
            }
        }
        item {
            Row(
                Modifier.padding(horizontal = 16.dp, vertical = 2.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                FilterChip(
                    selected = filterRating > 0 || filterYearFrom > 0 || filterLang.isNotBlank(),
                    onClick = { showFilters = true },
                    label = {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Icon(Icons.Default.FilterList, contentDescription = null, modifier = Modifier.size(15.dp))
                            Spacer(Modifier.width(5.dp))
                            Text("Filters")
                        }
                    },
                )
                val summary = buildList {
                    if (filterRating > 0) add("★ ${filterRating.toInt()}+")
                    if (filterYearFrom > 0) add("${filterYearFrom}+")
                    if (filterToSummary(filterYearTo).isNotBlank()) add(filterToSummary(filterYearTo))
                    if (filterLang.isNotBlank()) add(filterLang)
                }.joinToString(" · ")
                if (summary.isNotBlank()) {
                    Text(summary, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
        if (query.isBlank() && continueItems.isNotEmpty()) {
            item { SectionHeader("Continue watching", "Pick up where you left off.") }
            item {
                LazyRow(
                    contentPadding = PaddingValues(horizontal = 16.dp),
                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    items(continueItems) { item -> ContinueCard(item, onClick = { onContinue(item) }) }
                }
            }
        }
        if (searching) {
            item {
                Column(Modifier.padding(16.dp)) {
                    LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                    Text(
                        "Searching name and meaning instantly; the curator is next…",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                }
            }
        }
        error?.let {
            item {
                Card(
                    colors = CardDefaults.cardColors(
                        containerColor = MaterialTheme.colorScheme.error.copy(alpha = 0.12f)
                    ),
                    modifier = Modifier.padding(16.dp).fillMaxWidth(),
                ) {
                    Text(
                        "Search failed: $it",
                        color = MaterialTheme.colorScheme.error,
                        modifier = Modifier.padding(14.dp),
                    )
                }
            }
        }
        if (contentType == "live") {
            if (categories.isNotEmpty()) {
                item {
                    LazyRow(
                        contentPadding = PaddingValues(horizontal = 16.dp),
                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                    ) {
                        item {
                            FilterChip(
                                selected = selectedCategory == 0,
                                onClick = { onCategory(0) },
                                label = { Text("All") },
                            )
                        }
                        items(categories) { category ->
                            FilterChip(
                                selected = selectedCategory == category.id,
                                onClick = { onCategory(category.id) },
                                label = { Text("${category.name} (${category.count})") },
                            )
                        }
                    }
                }
            }
            if (liveResults.isEmpty() && !searching) {
                item {
                    EmptyState(
                        "Browse live TV",
                        "Search a channel or pick a category, then play it on a TV or your phone.",
                    )
                }
            }
            items(liveResults) { channel ->
                ListItem(
                    leadingContent = {
                        AsyncImage(
                            model = channel.icon,
                            contentDescription = null,
                            contentScale = ContentScale.Crop,
                            modifier = Modifier.size(48.dp, 32.dp).clip(RoundedCornerShape(6.dp)),
                        )
                    },
                    headlineContent = { Text(channel.title, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                    supportingContent = { Text("Live · channel ${channel.channel_id}") },
                    modifier = Modifier.clickable { onOpenLive(channel) },
                )
            }
        } else {
            val data = discover
            if (data == null && !searching) {
                item {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                        Text("Try asking for a feeling", style = MaterialTheme.typography.titleMedium)
                        LazyRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            items(
                                listOf(
                                    "survival and zombies",
                                    "mind-bending sci-fi",
                                    "heist movies",
                                    "feel-good comedy",
                                    "space adventure",
                                )
                            ) { sample ->
                                SuggestionChip(
                                    onClick = { onSuggestion(sample) },
                                    label = { Text(sample) },
                                )
                            }
                        }
                    }
                }
            }
            if (data != null) {
                if (data.ai_pending && data.ai_picks.isEmpty()) {
                    item {
                        Row(
                            Modifier.padding(horizontal = 16.dp, vertical = 10.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp)
                            Spacer(Modifier.width(8.dp))
                            Text(
                                "Curating AI picks…",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                }
                if (data.ai_picks.isNotEmpty()) {
                    item {
                        SectionHeader(
                            "AI picks",
                            data.ai_note.ifBlank { "Curated from the catalog for your request." },
                        )
                    }
                    item {
                        LazyRow(
                            contentPadding = PaddingValues(horizontal = 16.dp),
                            horizontalArrangement = Arrangement.spacedBy(12.dp),
                        ) {
                            items(data.ai_picks) { group ->
                                AiPickCard(group, onClick = { onOpen(group) })
                            }
                        }
                    }
                }
                val aiTitles: Set<String> = data.ai_picks.map { group -> group.title.lowercase() }.toSet()
                val byName: List<MediaGroup> = data.by_name.filter { group -> group.title.lowercase() !in aiTitles }
                val byMeaning: List<MediaGroup> = data.by_meaning.filter { group -> group.title.lowercase() !in aiTitles }
                if (byName.isNotEmpty()) {
                    item { SectionHeader("By name", "Titles that contain your words.") }
                    items(byName) { group -> GroupRow(group, onClick = { onOpen(group) }) }
                }
                if (byMeaning.isNotEmpty()) {
                    item { SectionHeader("By meaning", "Close in theme and mood, even with different words.") }
                    items(byMeaning) { group -> GroupRow(group, onClick = { onOpen(group) }) }
                }
                if (data.ai_picks.isEmpty() && data.by_name.isEmpty() && data.by_meaning.isEmpty()) {
                    item { EmptyState("No matches", "Try a broader description or a different title.") }
                }
            }
        }
    }
    if (showFilters) {
        FilterDialog(
            rating = filterRating,
            yearFrom = filterYearFrom,
            yearTo = filterYearTo,
            lang = filterLang,
            onDismiss = { showFilters = false },
            onApply = { rating, yf, yt, language ->
                showFilters = false
                onFilters(rating, yf, yt, language)
            },
        )
    }
}

@Composable
private fun FilterDialog(
    rating: Double,
    yearFrom: Int,
    yearTo: Int,
    lang: String,
    onDismiss: () -> Unit,
    onApply: (Double, Int, Int, String) -> Unit,
) {
    var r by remember { mutableStateOf(rating) }
    var yf by remember { mutableStateOf(yearFrom) }
    var language by remember { mutableStateOf(lang) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Filters") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("Minimum rating", style = MaterialTheme.typography.labelLarge)
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    listOf(0.0 to "Any", 6.0 to "6+", 7.0 to "7+", 8.0 to "8+").forEach { (value, label) ->
                        FilterChip(selected = r == value, onClick = { r = value }, label = { Text(label) })
                    }
                }
                Text("From year", style = MaterialTheme.typography.labelLarge)
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    listOf(0 to "Any", 1990 to "1990", 2000 to "2000", 2010 to "2010", 2020 to "2020")
                        .forEach { (value, label) ->
                            FilterChip(selected = yf == value, onClick = { yf = value }, label = { Text(label) })
                        }
                }
                Text("Language", style = MaterialTheme.typography.labelLarge)
                Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    listOf("" to "Any", "EN" to "EN", "FR" to "FR", "AR" to "AR", "IT" to "IT")
                        .forEach { (value, label) ->
                            FilterChip(
                                selected = language == value,
                                onClick = { language = value },
                                label = { Text(label) },
                            )
                        }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = { onApply(r, yf, yearTo, language) }) { Text("Apply") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

private fun filterToSummary(yearTo: Int): String = if (yearTo > 0) "≤$yearTo" else ""

private fun formatTime(seconds: Double): String {
    val total = seconds.toInt().coerceAtLeast(0)
    val h = total / 3600
    val m = (total % 3600) / 60
    val s = total % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%d:%02d".format(m, s)
}

@Composable
private fun ContinueCard(item: ContinueItem, onClick: () -> Unit) {
    Column(
        Modifier
            .width(150.dp)
            .clip(RoundedCornerShape(16.dp))
            .background(MaterialTheme.colorScheme.surface)
            .clickable(onClick = onClick)
            .padding(8.dp),
    ) {
        PosterImage(
            poster = item.poster,
            modifier = Modifier.fillMaxWidth().aspectRatio(2f / 3f).clip(RoundedCornerShape(12.dp)),
        )
        Text(
            item.title,
            style = MaterialTheme.typography.titleMedium,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(top = 8.dp),
        )
        if (item.duration > 0) {
            LinearProgressIndicator(
                progress = { (item.position / item.duration).toFloat().coerceIn(0f, 1f) },
                modifier = Modifier.fillMaxWidth().padding(top = 6.dp).height(4.dp).clip(RoundedCornerShape(2.dp)),
            )
            Text(
                "${formatTime(item.position)} / ${formatTime(item.duration)}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 4.dp),
            )
        } else {
            Text(
                "Resume at ${formatTime(item.position)}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 4.dp),
            )
        }
    }
}

@Composable
private fun SectionHeader(title: String, subtitle: String? = null) {
    Column(Modifier.padding(start = 16.dp, end = 16.dp, top = 20.dp, bottom = 8.dp)) {
        Text(title, style = MaterialTheme.typography.titleLarge)
        subtitle?.takeIf { it.isNotBlank() }?.let {
            Text(
                it,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 2.dp),
            )
        }
    }
}

@Composable
private fun AiPickCard(group: MediaGroup, onClick: () -> Unit) {
    Column(
        Modifier
            .width(150.dp)
            .clip(RoundedCornerShape(16.dp))
            .background(MaterialTheme.colorScheme.surface)
            .clickable(onClick = onClick)
            .padding(8.dp),
    ) {
        Box {
            PosterImage(
                poster = group.poster,
                modifier = Modifier.fillMaxWidth().aspectRatio(2f / 3f).clip(RoundedCornerShape(12.dp)),
            )
            if (group.rating > 0) {
                RatingBadge(group.rating, Modifier.align(Alignment.TopEnd).padding(6.dp))
            }
        }
        Text(
            group.title,
            style = MaterialTheme.typography.titleMedium,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(top = 8.dp),
        )
        Text(
            listOfNotNull(group.year?.toString(), group.kind.replaceFirstChar { it.uppercase() }).joinToString(" · "),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        group.reason?.takeIf { it.isNotBlank() }?.let {
            Text(
                it,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 3,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.padding(top = 6.dp),
            )
        }
    }
}

@Composable
private fun GroupRow(group: MediaGroup, onClick: () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(horizontal = 16.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        PosterImage(
            poster = group.poster,
            modifier = Modifier.width(56.dp).height(84.dp).clip(RoundedCornerShape(8.dp)),
        )
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(
                group.title,
                style = MaterialTheme.typography.titleMedium,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                listOfNotNull(
                    group.year?.toString(),
                    group.kind.replaceFirstChar { it.uppercase() },
                    group.variants.firstOrNull()?.lang,
                ).joinToString(" · "),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 2.dp),
            )
        }
        if (group.rating > 0) RatingBadge(group.rating)
    }
}

@Composable
private fun PosterImage(poster: String?, modifier: Modifier) {
    Box(modifier.background(MaterialTheme.colorScheme.surfaceVariant), contentAlignment = Alignment.Center) {
        if (!poster.isNullOrBlank()) {
            AsyncImage(
                model = poster,
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
            )
        } else {
            Icon(
                Icons.Default.PlayArrow,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.size(28.dp),
            )
        }
    }
}

@Composable
private fun RatingBadge(rating: Double, modifier: Modifier = Modifier) {
    Box(
        modifier
            .clip(RoundedCornerShape(6.dp))
            .background(Color.Black.copy(alpha = 0.72f))
            .padding(horizontal = 6.dp, vertical = 3.dp),
    ) {
        Text(
            "★ %.1f".format(rating),
            style = MaterialTheme.typography.labelMedium,
            color = VodAmber,
            fontWeight = FontWeight.Bold,
        )
    }
}

@Composable
private fun EmptyState(title: String, message: String) {
    Column(
        Modifier.fillMaxWidth().padding(horizontal = 32.dp, vertical = 48.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Icon(
            Icons.Default.PlayArrow,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.size(44.dp),
        )
        Text(title, style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(top = 12.dp))
        Text(
            message,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 6.dp),
        )
    }
}

@Composable
private fun DetailScreen(
    group: MediaGroup,
    api: Api,
    onPlay: (String, String, Double) -> Unit,
    onOpen: (MediaGroup) -> Unit,
    notify: (String) -> Unit,
) {
    var episodes by remember { mutableStateOf<EpisodesResponse?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var added by remember { mutableStateOf(false) }
    var resume by remember { mutableStateOf(0.0) }
    var similar by remember { mutableStateOf<List<MediaGroup>>(emptyList()) }
    val scope = rememberCoroutineScope()
    val seriesId = group.variants.firstOrNull()?.media_key?.removePrefix("series:")?.toIntOrNull()

    LaunchedEffect(group) {
        if (group.kind == "series" && seriesId != null) {
            try {
                episodes = api.episodes(seriesId)
            } catch (e: Exception) {
                error = e.message
            }
        }
        val key = group.variants.firstOrNull()?.media_key
        if (key != null) {
            runCatching { api.progress(key) }.onSuccess { resume = it.position }
            runCatching { api.similar(key) }.onSuccess { similar = it.items }
        }
    }

    LazyColumn(Modifier.fillMaxSize(), contentPadding = PaddingValues(bottom = 28.dp)) {
        item {
            Box(Modifier.fillMaxWidth().height(280.dp)) {
                PosterImage(poster = group.poster, modifier = Modifier.fillMaxSize())
                Box(
                    Modifier.fillMaxSize().background(
                        Brush.verticalGradient(
                            colors = listOf(Color.Transparent, Color(0xCC08080B), VodBg),
                            startY = 120f,
                        )
                    )
                )
                Column(Modifier.align(Alignment.BottomStart).padding(16.dp)) {
                    Text(group.title, style = MaterialTheme.typography.headlineMedium)
                    Row(
                        Modifier.padding(top = 6.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(10.dp),
                    ) {
                        group.year?.let { Text("$it", color = MaterialTheme.colorScheme.onSurfaceVariant) }
                        if (group.rating > 0) RatingBadge(group.rating)
                        Text(
                            group.kind.replaceFirstChar { it.uppercase() },
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }
        }
        item {
            Row(
                Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
                horizontalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                Button(
                    onClick = {
                        val key = group.variants.firstOrNull()?.media_key
                        if (key == null) {
                            notify("No playable variant")
                            return@Button
                        }
                        if (group.kind == "movie") {
                            onPlay(key, group.title, resume)
                        } else {
                            notify("Pick an episode below")
                        }
                    },
                ) {
                    Icon(Icons.Default.PlayArrow, contentDescription = null)
                    Spacer(Modifier.width(6.dp))
                    Text(
                        when {
                            group.kind == "series" -> "Pick an episode"
                            resume > 30 -> "Resume ${formatTime(resume)}"
                            else -> "Play"
                        }
                    )
                }
                OutlinedButton(
                    onClick = {
                        val key = group.variants.firstOrNull()?.media_key ?: return@OutlinedButton
                        scope.launch {
                            try {
                                val result = api.watchlistAdd(key)
                                if (result.ok) {
                                    added = true
                                    notify("Added to watchlist")
                                } else {
                                    notify(result.error ?: "Could not add")
                                }
                            } catch (e: Exception) {
                                notify(e.message ?: "Could not add")
                            }
                        }
                    },
                    enabled = !added,
                ) {
                    Icon(if (added) Icons.Default.Check else Icons.Default.Add, contentDescription = null)
                    Spacer(Modifier.width(6.dp))
                    Text(if (added) "In watchlist" else "Watchlist")
                }
            }
        }
        group.plot?.takeIf { it.isNotBlank() }?.let { plot ->
            item {
                Text(
                    plot,
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
                )
            }
        }
        if (group.kind == "series") {
            val data = episodes
            if (data == null && error == null) {
                item {
                    Box(Modifier.fillMaxWidth().padding(24.dp), contentAlignment = Alignment.Center) {
                        CircularProgressIndicator()
                    }
                }
            }
            data?.items?.forEach { (season, entries) ->
                item { SectionHeader("Season $season") }
                items(entries) { ep ->
                    ListItem(
                        headlineContent = { Text(ep.title ?: "Episode ${ep.episode}") },
                        supportingContent = { Text("E${ep.episode}" + (ep.ext?.let { " · $it" } ?: "")) },
                        trailingContent = {
                            IconButton(onClick = {
                                scope.launch {
                                    val p = runCatching { api.progress(ep.media_key) }.getOrNull()
                                    onPlay(ep.media_key, "${group.title} S${season}E${ep.episode}", p?.position ?: 0.0)
                                }
                            }) {
                                Icon(Icons.Default.PlayArrow, contentDescription = "Play")
                            }
                        },
                        modifier = Modifier.clickable {
                            scope.launch {
                                val p = runCatching { api.progress(ep.media_key) }.getOrNull()
                                onPlay(ep.media_key, "${group.title} S${season}E${ep.episode}", p?.position ?: 0.0)
                            }
                        },
                    )
                }
            }
        } else {
            item { SectionHeader("Versions", "Language variants of this stream.") }
            group.variants.forEach { variant ->
                item {
                    ListItem(
                        headlineContent = { Text(variant.lang ?: "Original") },
                        supportingContent = { Text(variant.ext ?: "") },
                        trailingContent = {
                            IconButton(onClick = { onPlay(variant.media_key, group.title, resume) }) {
                                Icon(Icons.Default.PlayArrow, contentDescription = "Play")
                            }
                        },
                        modifier = Modifier.clickable { onPlay(variant.media_key, group.title, resume) },
                    )
                }
            }
        }
        if (similar.isNotEmpty()) {
            item { SectionHeader("More like this", "Because you opened ${group.title}.") }
            item {
                LazyRow(
                    contentPadding = PaddingValues(horizontal = 16.dp),
                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    items(similar) { related -> AiPickCard(related, onClick = { onOpen(related) }) }
                }
            }
        }
        error?.let {
            item {
                Text(
                    "Error: $it",
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(16.dp),
                )
            }
        }
    }
}

@Composable
private fun LibraryScreen(
    library: LibraryResponse?,
    watchlist: WatchlistResponse?,
    loading: Boolean,
    tab: Int,
    onTab: (Int) -> Unit,
    queue: QueueResponse?,
    onCancelQueue: (QueueItem) -> Unit,
    onRefresh: () -> Unit,
    onPlay: (LibraryItem) -> Unit,
    onCast: (LibraryItem) -> Unit,
    onOpenWatchlist: (WatchlistItem) -> Unit,
    onRemoveWatchlist: (WatchlistItem) -> Unit,
    onPickTv: () -> Unit,
    selectedTv: TvInfo?,
) {
    Column(Modifier.fillMaxSize()) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            SingleChoiceSegmentedButtonRow(Modifier.weight(1f)) {
                SegmentedButton(
                    selected = tab == 0,
                    onClick = { onTab(0) },
                    shape = SegmentedButtonDefaults.itemShape(0, 3),
                ) { Text("Downloads") }
                SegmentedButton(
                    selected = tab == 1,
                    onClick = { onTab(1) },
                    shape = SegmentedButtonDefaults.itemShape(1, 3),
                ) { Text("Watchlist") }
                SegmentedButton(
                    selected = tab == 2,
                    onClick = { onTab(2) },
                    shape = SegmentedButtonDefaults.itemShape(2, 3),
                ) { Text("Queue") }
            }
            IconButton(onClick = onRefresh) {
                Icon(Icons.Default.Refresh, contentDescription = "Refresh")
            }
        }
        library?.let { lib ->
            val usageGb = lib.usage_bytes / 1_000_000_000.0
            val limitGb = lib.limit_bytes / 1_000_000_000.0
            Column(Modifier.padding(horizontal = 16.dp, vertical = 4.dp)) {
                Text(
                    "Storage %.1f GB of %.0f GB".format(usageGb, limitGb),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                LinearProgressIndicator(
                    progress = { if (limitGb > 0) (usageGb / limitGb).toFloat().coerceIn(0f, 1f) else 0f },
                    modifier = Modifier.fillMaxWidth().padding(top = 4.dp).height(4.dp),
                )
            }
        }
        if (loading) {
            LinearProgressIndicator(Modifier.fillMaxWidth())
        }
        if (tab == 0) {
            val items = library?.items.orEmpty()
            if (items.isEmpty() && !loading) {
                EmptyState(
                    "No downloads yet",
                    "Items downloaded from your watchlist appear here, ready to watch offline.",
                )
            }
            LazyColumn(Modifier.fillMaxSize()) {
                items(items) { item ->
                    ListItem(
                        leadingContent = {
                            PosterImage(
                                poster = item.poster,
                                modifier = Modifier.width(48.dp).height(72.dp).clip(RoundedCornerShape(8.dp)),
                            )
                        },
                        headlineContent = { Text(item.title, maxLines = 2, overflow = TextOverflow.Ellipsis) },
                        supportingContent = {
                            val meta = listOfNotNull(
                                item.series_title?.takeIf { it.isNotBlank() },
                                item.season?.let { s -> item.episode?.let { e -> "S${s}E$e" } },
                                "%.0f MB".format(item.size_bytes / 1_048_576.0),
                            )
                            Text(meta.joinToString(" · "))
                        },
                        trailingContent = {
                            Row {
                                IconButton(onClick = { onPlay(item) }) {
                                    Icon(Icons.Default.PlayArrow, contentDescription = "Play on phone")
                                }
                                IconButton(onClick = { if (selectedTv == null) onPickTv() else onCast(item) }) {
                                    Icon(Icons.Default.Cast, contentDescription = "Cast to TV")
                                }
                            }
                        },
                    )
                    HorizontalDivider(
                        Modifier.padding(start = 76.dp),
                        color = MaterialTheme.colorScheme.outline.copy(alpha = 0.3f),
                    )
                }
            }
        } else if (tab == 1) {
            val items = watchlist?.items.orEmpty()
            if (items.isEmpty() && !loading) {
                EmptyState(
                    "Watchlist is empty",
                    "Add movies or shows from their detail page to keep them here.",
                )
            }
            LazyColumn(Modifier.fillMaxSize()) {
                items(items) { item ->
                    ListItem(
                        leadingContent = {
                            PosterImage(
                                poster = item.poster,
                                modifier = Modifier.width(48.dp).height(72.dp).clip(RoundedCornerShape(8.dp)),
                            )
                        },
                        headlineContent = { Text(item.title, maxLines = 2, overflow = TextOverflow.Ellipsis) },
                        supportingContent = {
                            Text(
                                listOfNotNull(
                                    item.year?.toString(),
                                    item.kind.replaceFirstChar { it.uppercase() },
                                ).joinToString(" · ")
                            )
                        },
                        trailingContent = {
                            IconButton(onClick = { onRemoveWatchlist(item) }) {
                                Icon(Icons.Default.Delete, contentDescription = "Remove")
                            }
                        },
                        modifier = Modifier.clickable { onOpenWatchlist(item) },
                    )
                    HorizontalDivider(
                        Modifier.padding(start = 76.dp),
                        color = MaterialTheme.colorScheme.outline.copy(alpha = 0.3f),
                    )
                }
            }
        } else {
            val items = queue?.items.orEmpty()
            if (items.isEmpty() && !loading) {
                EmptyState(
                    "Queue is empty",
                    "Downloads queued from the watchlist appear here with their status.",
                )
            }
            LazyColumn(Modifier.fillMaxSize()) {
                items(items) { item ->
                    ListItem(
                        headlineContent = {
                            Text(item.title, maxLines = 2, overflow = TextOverflow.Ellipsis)
                        },
                        supportingContent = {
                            Text(
                                listOfNotNull(item.status, item.error?.takeIf { it.isNotBlank() })
                                    .joinToString(" · ")
                            )
                        },
                        trailingContent = {
                            IconButton(onClick = { onCancelQueue(item) }) {
                                Icon(Icons.Default.Delete, contentDescription = "Cancel")
                            }
                        },
                    )
                    HorizontalDivider(
                        Modifier.padding(start = 16.dp),
                        color = MaterialTheme.colorScheme.outline.copy(alpha = 0.3f),
                    )
                }
            }
        }
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
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
            Button(
                onClick = {
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
                },
                enabled = !busy,
            ) {
                Icon(Icons.Default.Search, contentDescription = null)
                Spacer(Modifier.width(6.dp))
                Text("Discover DLNA")
            }
            if (busy) CircularProgressIndicator(Modifier.size(22.dp))
        }
        Spacer(Modifier.height(10.dp))
        LazyColumn(Modifier.fillMaxSize()) {
            items(tvs) { tv ->
                Card(
                    modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp).clickable { onSelect(tv) },
                    colors = CardDefaults.cardColors(
                        containerColor = if (selected?.name == tv.name) {
                            MaterialTheme.colorScheme.primary.copy(alpha = 0.14f)
                        } else {
                            MaterialTheme.colorScheme.surface
                        },
                    ),
                ) {
                    ListItem(
                        leadingContent = {
                            Icon(
                                if (tv.kind == "cast") Icons.Default.Cast else Icons.Default.Tv,
                                contentDescription = null,
                                tint = MaterialTheme.colorScheme.primary,
                            )
                        },
                        headlineContent = { Text(tv.name, fontWeight = FontWeight.SemiBold) },
                        supportingContent = {
                            Text(
                                listOfNotNull(
                                    tv.ip,
                                    tv.kind,
                                    tv.prefer_proxy.takeIf { it == 1 }?.let { "proxy" },
                                ).joinToString(" · ")
                            )
                        },
                        trailingContent = {
                            Row {
                                TextButton(onClick = {
                                    scope.launch {
                                        AppLog.action("wake", tv.name)
                                        try {
                                            val res = api.tvsWake(tv.name)
                                            notify(if (res.ok) "Wake packet sent to ${tv.name}" else (res.error ?: "Wake failed"))
                                        } catch (e: Exception) {
                                            notify(e.message ?: "Wake failed")
                                        }
                                    }
                                }) { Text("Wake") }
                                TextButton(onClick = {
                                scope.launch {
                                    AppLog.action("probe", tv.name)
                                    try {
                                        val detail = withContext(NonCancellable) { api.probe(tv.name) }
                                        methods = methods + (tv.name to detail.methods)
                                        AppLog.i("PROBE", "${tv.name}: ${detail.methods.joinToString { it.method }}")
                                    } catch (e: CancellationException) {
                                        throw e
                                    } catch (e: Exception) {
                                        AppLog.e("PROBE", "failed for ${tv.name}", e)
                                        notify(e.message ?: "probe failed")
                                    }
                                }
                                }) { Text("Probe") }
                            }
                        },
                    )
                }
                if (selected?.name == tv.name) {
                    Text(
                        "Selected",
                        color = MaterialTheme.colorScheme.primary,
                        style = MaterialTheme.typography.labelMedium,
                        modifier = Modifier.padding(start = 20.dp, bottom = 4.dp),
                    )
                }
                methods[tv.name]?.let { list ->
                    Text(
                        list.joinToString("  ") { "${it.method}:${if (it.available) "+" else "-"}" },
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(start = 20.dp, bottom = 6.dp),
                    )
                }
            }
            if (devices.isNotEmpty()) {
                item { SectionHeader("Discovered on this network") }
                items(devices) { device ->
                    ListItem(
                        headlineContent = { Text(device.name) },
                        supportingContent = { Text("${device.ip} · DLNA") },
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
    usingLan: Boolean,
    onPickTv: () -> Unit,
    onStart: (StreamInfo, DlnaDevice?, String) -> Unit,
    onDismiss: () -> Unit,
    notify: (String) -> Unit,
) {
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    var target by remember(selectedTv) {
        mutableStateOf(if (selectedTv?.kind == "cast") "cast" else "dlna")
    }
    var quality by remember { mutableStateOf("copy") }
    var usePublic by remember { mutableStateOf(!usingLan) }
    var prepared by remember { mutableStateOf<StreamInfo?>(null) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    fun prepare() {
        scope.launch {
            busy = true
            error = null
            AppLog.action("prepare", "media=${pending.mediaKey} target=$target live=${pending.isLive}")
            try {
                if (target == "cast" && selectedTv?.kind == "cast") {
                    AppLog.action("prepare", "server-side SCS cast to ${selectedTv.name}")
                    prepared = StreamInfo(ok = true, title = pending.title, mode = "server")
                    return@launch
                }
                val info = if (pending.isLive) {
                    api.liveStream(pending.mediaKey.removePrefix("live:").toInt())
                } else {
                    val effectiveQuality = when (target) {
                        "cast" -> "copy"
                        "player" -> "downgrade"
                        else -> quality
                    }
                    api.stream(
                        mediaKey = pending.mediaKey,
                        mode = "cache",
                        quality = effectiveQuality,
                        prepare = true,
                        format = if (target == "cast") "mp4" else null,
                        startPosition = pending.startPosition,
                    )
                }
                if (!info.ok) {
                    prepared = null
                    AppLog.e("PREPARE", "server refused: ${info.error}")
                    error = info.error ?: "stream failed"
                    return@launch
                }
                prepared = info
                usePublic = !usingLan
                AppLog.action(
                    "prepared",
                    "token=${info.token} mode=${info.mode} mime=${info.mime} route=${if (usePublic) "public" else "lan"}",
                )
            } catch (e: Exception) {
                AppLog.e("PREPARE", "failed", e)
                error = e.message
            } finally {
                busy = false
            }
        }
    }

    fun start() {
        val stream = prepared ?: return
        val picked = selectedTv
        if (target == "cast" && picked?.kind == "cast") {
            scope.launch {
                busy = true
                try {
                    val result = if (pending.isLive) {
                        api.livePlay(pending.mediaKey.removePrefix("live:").toInt(), picked.name)
                    } else {
                        api.play(pending.mediaKey, picked.name, pending.startPosition)
                    }
                    AppLog.action(
                        "server_cast",
                        "tv=${picked.name} ok=${result.ok} ${result.detail ?: result.error ?: ""}",
                    )
                    if (!result.ok) {
                        error = result.detail ?: result.error ?: "TV cast failed"
                        return@launch
                    }
                    onStart(StreamInfo(ok = true, title = pending.title, mode = "server"), null, pending.title)
                } catch (e: Exception) {
                    AppLog.e("SERVER_CAST", "failed for ${picked.name}", e)
                    error = e.message
                } finally {
                    busy = false
                }
            }
            return
        }
        val url = if (usePublic) stream.public_url else stream.lan_url
        AppLog.action(
            "start",
            "target=$target route=${if (usePublic) "public" else "lan"} url=${AppLog.redact(url ?: "")}",
        )
        if (url.isNullOrBlank()) {
            error = "No ${if (usePublic) "public" else "LAN"} URL — pick a different option."
            return
        }
        when (target) {
            "player" -> {
                context.startActivity(
                    Intent(context, PlayerActivity::class.java)
                        .putExtra("url", url)
                        .putExtra("title", pending.title)
                        .putExtra("start_offset", pending.startPosition)
                        .putExtra("media_key", pending.mediaKey),
                )
                onStart(stream, null, pending.title)
            }
            "cast" -> {
                val contentType = if (pending.isLive) "video/mp2t" else "video/mp4"
                val message = runCatching {
                    CastCaster.load(context, url, pending.title, contentType)
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
                        if (device == null) {
                            val found = devices.joinToString(", ") { "${it.name}@${it.ip}" }
                            AppLog.e("DLNA", "selected ${tv.name} ${tv.ip} not found; discovered: $found")
                            error = "Selected TV ${tv.name} (${tv.ip}) is not on the network. Found: $found"
                            return@launch
                        }
                        caster.cast(device, url, pending.title, stream.mime ?: "video/mpeg")
                        onStart(stream, device, pending.title)
                    } catch (e: Exception) {
                        AppLog.e("DLNA", "cast failed", e)
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
        title = {
            Column {
                Text(pending.title)
                if (pending.isLive) {
                    Text(
                        "LIVE",
                        color = MaterialTheme.colorScheme.primary,
                        style = MaterialTheme.typography.labelMedium,
                    )
                }
            }
        },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("Play on", style = MaterialTheme.typography.labelLarge)
                SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                    listOf("dlna" to "DLNA TV", "player" to "Phone", "cast" to "Cast")
                        .forEachIndexed { index, (value, label) ->
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
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (target == "dlna" && selectedTv == null) {
                    TextButton(onClick = onPickTv) { Text("Pick a TV") }
                }
                if (pending.isLive) {
                    Text(
                        "Live channels are non-seekable and use the server's live TS feed.",
                        style = MaterialTheme.typography.bodySmall,
                    )
                } else if (target == "player") {
                    Text(
                        "Phone plays the 1080p H.264/AAC transcode.",
                        style = MaterialTheme.typography.bodySmall,
                    )
                } else if (target != "cast") {
                    Text("Quality", style = MaterialTheme.typography.labelLarge)
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Checkbox(
                            checked = quality == "copy",
                            onCheckedChange = { quality = "copy"; prepared = null },
                        )
                        Text("Copy (fast)")
                        Spacer(Modifier.width(12.dp))
                        Checkbox(
                            checked = quality == "downgrade",
                            onCheckedChange = { quality = "downgrade"; prepared = null },
                        )
                        Text("1080p transcode")
                    }
                }
                if (prepared == null) {
                    Button(onClick = { prepare() }, enabled = !busy) { Text("Prepare link") }
                } else if (prepared?.mode == "server") {
                    Text(
                        "Casting through the server (SCS) to ${selectedTv?.name ?: "TV"}",
                        style = MaterialTheme.typography.bodySmall,
                    )
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
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Text(
                        "mode=${prepared?.mode} format=${prepared?.format} mime=${prepared?.mime}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                if (busy) CircularProgressIndicator(Modifier.size(22.dp))
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
    var status by remember { mutableStateOf<StatusInfo?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    val isServer = stream.mode == "server"
    val isLive = stream.mode == "live" || status?.diag?.is_live == true

    LaunchedEffect(stream.token, isServer) {
        if (isServer) {
            while (true) {
                try {
                    status = api.status()
                    error = null
                } catch (e: Exception) {
                    if (error != e.message) AppLog.e("REMOTE", "status poll failed", e)
                    error = e.message
                }
                delay(2000)
            }
        } else {
            val token = stream.token ?: return@LaunchedEffect
            while (true) {
                try {
                    session = api.session(token)
                    if (error != null) AppLog.i("REMOTE", "session recovered")
                    error = null
                } catch (e: Exception) {
                    if (error != e.message) AppLog.e("REMOTE", "session poll failed", e)
                    error = e.message
                }
                delay(2000)
            }
        }
    }

    Column(
        Modifier.fillMaxSize().padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Text(title, style = MaterialTheme.typography.headlineMedium)
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (isLive) {
                Box(
                    Modifier.clip(RoundedCornerShape(6.dp))
                        .background(MaterialTheme.colorScheme.primary.copy(alpha = 0.18f))
                        .padding(horizontal = 8.dp, vertical = 3.dp),
                ) {
                    Text(
                        "LIVE",
                        color = MaterialTheme.colorScheme.primary,
                        style = MaterialTheme.typography.labelMedium,
                    )
                }
            }
            Text(
                when {
                    isServer -> "Casting via server · ${status?.tv ?: "TV"}"
                    else -> "Session ${stream.token} · ${stream.mode}/${stream.format}"
                },
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        val st = status
        val current = session
        if (isServer) {
            if (st?.playing == true) {
                if (!isLive) {
                    Text(
                        "position ${st.position?.position?.toInt() ?: 0}s / ${st.position?.duration?.toInt() ?: 0}s",
                        style = MaterialTheme.typography.titleLarge,
                    )
                }
                Text("served ${"%.1f".format((st.diag?.max_served ?: 0L) / 1_048_576.0)} MB")
            } else {
                Text("Not playing")
            }
        } else if (current?.ok == true) {
            if (!isLive) {
                Text(
                    "position ${current.position?.toInt() ?: 0}s / ${current.duration?.toInt() ?: 0}s",
                    style = MaterialTheme.typography.titleLarge,
                )
            }
            Text(
                if (isLive) "streaming · clients ${current.clients}"
                else "served ${"%.1f".format((current.max_served ?: 0L) / 1_048_576.0)} MB · clients ${current.clients}"
            )
        } else {
            Text(current?.error ?: "waiting for session…")
        }
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            if (!isLive) {
                Button(onClick = {
                    scope.launch {
                        try {
                            if (isServer) {
                                AppLog.action("seek", "-30s (server)")
                                api.playback("seek", -30.0)
                            } else {
                                val token = stream.token ?: return@launch
                                val pos = (session?.position ?: 0.0) - 30.0
                                api.control(token, "seek", maxOf(0.0, pos))
                            }
                        } catch (e: Exception) {
                            notify(e.message ?: "seek failed")
                        }
                    }
                }) { Text("-30s") }
                Button(onClick = {
                    scope.launch {
                        try {
                            if (isServer) {
                                AppLog.action("seek", "+30s (server)")
                                api.playback("seek", 30.0)
                            } else {
                                val token = stream.token ?: return@launch
                                val pos = (session?.position ?: 0.0) + 30.0
                                api.control(token, "seek", pos)
                            }
                        } catch (e: Exception) {
                            notify(e.message ?: "seek failed")
                        }
                    }
                }) { Text("+30s") }
            }
            if (isServer) {
                Button(onClick = {
                    scope.launch {
                        try {
                            val paused = status?.paused == true
                            AppLog.action(if (paused) "resume" else "pause", "server")
                            api.playback(if (paused) "resume" else "pause")
                        } catch (e: Exception) {
                            notify(e.message ?: "control failed")
                        }
                    }
                }) {
                    Icon(
                        if (status?.paused == true) Icons.Default.PlayArrow else Icons.Default.Pause,
                        contentDescription = null,
                    )
                }
            }
            Button(onClick = {
                scope.launch {
                    try {
                        AppLog.action("stop", "token=${stream.token} live=$isLive server=$isServer")
                        if (isServer) {
                            api.playback("stop")
                        } else {
                            stream.token?.let { api.control(it, "stop") }
                        }
                        device?.let { runCatching { DlnaCaster(context).stop(it) } }
                        CastCaster.stop(context)
                    } catch (_: Exception) {
                    }
                    onStop()
                }
            }) {
                Icon(Icons.Default.Stop, contentDescription = null)
                Spacer(Modifier.width(6.dp))
                Text("Stop")
            }
        }
        error?.let { Text("Error: $it", color = MaterialTheme.colorScheme.error) }
    }
}

@Composable
private fun LogsScreen() {
    val context = LocalContext.current
    var lines by remember { mutableStateOf<List<String>>(emptyList()) }
    var reload by remember { mutableStateOf(0) }

    LaunchedEffect(reload) {
        lines = withContext(Dispatchers.IO) { AppLog.recent() }
    }

    Column(Modifier.fillMaxSize().padding(12.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { reload++ }) { Text("Refresh") }
            Button(onClick = { AppLog.share(context) }) { Text("Share") }
            TextButton(onClick = { AppLog.clear(); reload++ }) { Text("Clear") }
        }
        Spacer(Modifier.height(8.dp))
        Text("${lines.size} lines · newest last", style = MaterialTheme.typography.bodySmall)
        LazyColumn(Modifier.fillMaxSize()) {
            items(lines) { line ->
                Text(
                    line,
                    fontFamily = FontFamily.Monospace,
                    fontSize = 10.sp,
                    modifier = Modifier.padding(vertical = 1.dp),
                )
            }
        }
    }
}
