package com.souhail.vodcast.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import com.souhail.vodcast.util.AppLog
import java.util.concurrent.TimeUnit

class Api(private val baseUrl: String, private val token: String) {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }
    private val client = OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(180, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .build()

    private fun url(path: String, params: Map<String, String>): HttpUrl {
        val base = baseUrl.trimEnd('/').toHttpUrlOrNull() ?: error("Invalid server URL: $baseUrl")
        val builder = base.newBuilder().addPathSegments(path.trimStart('/'))
        params.forEach { (key, value) -> builder.addQueryParameter(key, value) }
        return builder.build()
    }

    private suspend fun exec(path: String, params: Map<String, String> = emptyMap(), post: Boolean = false,
                             log: Boolean = true): String =
        withContext(Dispatchers.IO) {
            val httpUrl = url(path, params)
            val display = AppLog.redact(
                httpUrl.encodedPath + if (httpUrl.encodedQuery.isNullOrBlank()) "" else "?${httpUrl.encodedQuery}"
            )
            val method = if (post) "POST" else "GET"
            val started = System.nanoTime()
            var logged = false
            val req = Request.Builder()
                .url(httpUrl)
                .header("Authorization", "Bearer $token")
                .apply { if (post) post("".toRequestBody()) }
                .build()
            try {
                client.newCall(req).execute().use { resp ->
                    val body = resp.body?.string() ?: ""
                    val millis = (System.nanoTime() - started) / 1_000_000
                    if (log) {
                        AppLog.api(
                            method, display, resp.code, millis, body.length.toLong(),
                            if (resp.isSuccessful) null else body.take(160),
                        )
                    }
                    logged = true
                    if (!resp.isSuccessful) error("HTTP ${resp.code}: ${body.take(240)}")
                    body
                }
            } catch (e: Exception) {
                if (!logged && log) {
                    val millis = (System.nanoTime() - started) / 1_000_000
                    AppLog.api(method, display, 0, millis, 0, "${e.javaClass.simpleName}: ${e.message}")
                }
                throw e
            }
        }

    suspend fun config(): ServerConfig = json.decodeFromString(exec("api/app/config"))

    suspend fun search(q: String, type: String = "movie"): List<MediaGroup> =
        json.decodeFromString(exec("api/app/search", mapOf("q" to q, "type" to type)))

    suspend fun episodes(seriesId: Int): EpisodesResponse =
        json.decodeFromString(exec("api/app/episodes", mapOf("series_id" to seriesId.toString())))

    suspend fun tvs(): List<TvInfo> = json.decodeFromString(exec("api/app/tvs"))

    suspend fun tvDetail(name: String): TvDetail =
        json.decodeFromString(exec("api/app/tvs/detail", mapOf("name" to name)))

    suspend fun bannedTvs(): BannedTvs = json.decodeFromString(exec("api/app/tvs/banned"))

    suspend fun banTv(mac: String, label: String = ""): ControlResult =
        json.decodeFromString(exec("api/app/tvs/ban", mapOf("mac" to mac, "label" to label), post = true))

    suspend fun unbanTv(mac: String): ControlResult =
        json.decodeFromString(exec("api/app/tvs/unban", mapOf("mac" to mac), post = true))

    suspend fun probe(name: String): TvDetail =
        json.decodeFromString(exec("api/app/tvs/probe", mapOf("name" to name), post = true))

    suspend fun stream(
        mediaKey: String,
        mode: String = "cache",
        quality: String = "copy",
        prepare: Boolean = true,
        format: String? = null,
        startPosition: Double = 0.0,
        declareCurrent: Boolean = true,
        tv: String? = null,
        audio: Int = -1,
        sub: Int = -1,
    ): StreamInfo {
        val params = mutableMapOf(
            "media_key" to mediaKey,
            "mode" to mode,
            "quality" to quality,
            "start_position" to startPosition.toString(),
        )
        if (prepare) params["prepare"] = "1"
        if (!format.isNullOrBlank()) params["format"] = format
        params["declare_current"] = if (declareCurrent) "1" else "0"
        if (!tv.isNullOrBlank()) params["tv"] = tv
        if (audio >= 0) params["audio"] = audio.toString()
        if (sub >= 0) params["sub"] = sub.toString()
        return json.decodeFromString(exec("api/app/stream", params, post = true))
    }

    suspend fun sessions(): SessionsResponse = json.decodeFromString(exec("api/app/sessions"))

    suspend fun registerPlayer(name: String): PlayerRegistration =
        json.decodeFromString(
            exec("api/app/player/register", mapOf("name" to name), post = true)
        )

    suspend fun update(current: Int): UpdateInfo =
        json.decodeFromString(exec("api/app/update", mapOf("current" to current.toString())))

    private val publicClient = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .build()

    suspend fun githubLatest(): GithubRelease? = withContext(Dispatchers.IO) {
        val req = Request.Builder()
            .url("https://api.github.com/repos/picaso1-cloud/vod-delivery/releases/latest")
            .header("Accept", "application/vnd.github+json")
            .build()
        runCatching {
            publicClient.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return@use null
                val body = resp.body?.string() ?: return@use null
                json.decodeFromString<GithubRelease>(body)
            }
        }.getOrNull()
    }

    suspend fun downloadUrl(url: String, dest: java.io.File, onProgress: (Float) -> Unit = {}) =
        withContext(Dispatchers.IO) {
            val req = Request.Builder().url(url).build()
            client.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) error("HTTP ${resp.code} downloading update")
                val body = resp.body ?: error("empty update body")
                val total = body.contentLength()
                dest.parentFile?.mkdirs()
                body.byteStream().use { input ->
                    dest.outputStream().use { out ->
                        val buffer = ByteArray(128 * 1024)
                        var done = 0L
                        while (true) {
                            val read = input.read(buffer)
                            if (read < 0) break
                            out.write(buffer, 0, read)
                            done += read
                            if (total > 0) onProgress(done.toFloat() / total)
                        }
                    }
                }
            }
            dest.length()
        }

    suspend fun downloadUpdate(dest: java.io.File, onProgress: (Float) -> Unit = {}) =
        withContext(Dispatchers.IO) {
            val httpUrl = url("api/app/update/apk", emptyMap())
            val req = Request.Builder()
                .url(httpUrl)
                .header("Authorization", "Bearer $token")
                .build()
            client.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) error("HTTP ${resp.code} downloading update")
                val body = resp.body ?: error("empty update body")
                val total = body.contentLength()
                dest.parentFile?.mkdirs()
                body.byteStream().use { input ->
                    dest.outputStream().use { out ->
                        val buffer = ByteArray(128 * 1024)
                        var done = 0L
                        while (true) {
                            val read = input.read(buffer)
                            if (read < 0) break
                            out.write(buffer, 0, read)
                            done += read
                            if (total > 0) onProgress(done.toFloat() / total)
                        }
                    }
                }
            }
            dest.length()
        }

    suspend fun tracks(mediaKey: String, force: Boolean = false): TracksResponse =
        json.decodeFromString(
            exec("api/app/tracks", mapOf("media_key" to mediaKey, "force" to if (force) "1" else "0"))
        )

    suspend fun session(token: String): SessionInfo =
        json.decodeFromString(exec("api/app/session", mapOf("token" to token)))

    suspend fun liveCategories(): List<LiveCategory> =
        json.decodeFromString(exec("api/app/live/categories"))

    suspend fun live(q: String = "", categoryId: Int = 0): List<LiveChannel> =
        json.decodeFromString(
            exec("api/app/live", mapOf("q" to q, "category_id" to categoryId.toString()))
        )

    suspend fun liveStream(channelId: Int): StreamInfo =
        json.decodeFromString(
            exec("api/app/live/stream", mapOf("channel_id" to channelId.toString()), post = true)
        )

    suspend fun play(
        mediaKey: String,
        tv: String,
        startPosition: Double = 0.0,
        audio: Int = -1,
        sub: Int = -1,
    ): ControlResult =
        json.decodeFromString(
            exec(
                "api/app/play",
                mapOf(
                    "media_key" to mediaKey,
                    "tv" to tv,
                    "start_position" to startPosition.toString(),
                    "audio" to audio.toString(),
                    "sub" to sub.toString(),
                ),
                post = true,
            )
        )

    suspend fun livePlay(channelId: Int, tv: String): ControlResult =
        json.decodeFromString(
            exec(
                "api/app/live/play",
                mapOf("channel_id" to channelId.toString(), "tv" to tv),
                post = true,
            )
        )

    suspend fun playback(action: String, value: Double = 0.0): ControlResult =
        json.decodeFromString(
            exec("api/app/playback", mapOf("action" to action, "value" to value.toString()), post = true)
        )

    suspend fun status(): StatusInfo = json.decodeFromString(exec("api/app/status"))

    suspend fun discover(
        q: String,
        kind: String = "",
        yearFrom: Int = 0,
        yearTo: Int = 0,
        ratingMin: Double = 0.0,
        language: String = "",
        genre: String = "",
        sort: String = "relevance",
        page: Int = 1,
        pageSize: Int = 20,
    ): DiscoverResponse =
        json.decodeFromString(
            exec(
                "api/app/discover",
                mapOf(
                    "sort" to sort,
                    "page" to page.toString(),
                    "page_size" to pageSize.toString(),
                    "q" to q, "kind" to kind,
                    "year_from" to yearFrom.toString(), "year_to" to yearTo.toString(),
                    "rating_min" to ratingMin.toString(), "lang" to language, "genre" to genre,
                ),
            )
        )

    suspend fun discoverAi(
        q: String,
        kind: String = "",
        yearFrom: Int = 0,
        yearTo: Int = 0,
        ratingMin: Double = 0.0,
        language: String = "",
        genre: String = "",
    ): DiscoverAiResponse =
        json.decodeFromString(
            exec(
                "api/app/discover/ai",
                mapOf(
                    "q" to q, "kind" to kind,
                    "year_from" to yearFrom.toString(), "year_to" to yearTo.toString(),
                    "rating_min" to ratingMin.toString(), "lang" to language, "genre" to genre,
                ),
            )
        )

    suspend fun similar(mediaKey: String): SimilarResponse =
        json.decodeFromString(exec("api/app/similar", mapOf("media_key" to mediaKey)))

    suspend fun continueWatching(): ContinueResponse =
        json.decodeFromString(exec("api/app/continue"))

    suspend fun progress(mediaKey: String): ProgressInfo =
        json.decodeFromString(exec("api/app/progress", mapOf("media_key" to mediaKey)))

    suspend fun saveProgress(mediaKey: String, position: Double, duration: Double): ControlResult =
        json.decodeFromString(
            exec(
                "api/app/progress",
                mapOf("media_key" to mediaKey, "position" to position.toString(), "duration" to duration.toString()),
                post = true,
                log = false,
            )
        )

    suspend fun queue(): QueueResponse = json.decodeFromString(exec("api/app/queue"))

    suspend fun queueCancel(mediaKey: String): ControlResult =
        json.decodeFromString(exec("api/app/queue/cancel", mapOf("media_key" to mediaKey), post = true))

    suspend fun tvsWake(name: String): ControlResult =
        json.decodeFromString(exec("api/app/tvs/wake", mapOf("name" to name), post = true))

    suspend fun clientLog(level: String, tag: String, message: String): ControlResult =
        json.decodeFromString(
            exec(
                "api/app/client-log",
                mapOf("level" to level, "tag" to tag, "message" to message),
                post = true,
                log = false,
            )
        )

    suspend fun library(): LibraryResponse =
        json.decodeFromString(exec("api/app/library"))

    suspend fun libraryStream(mediaKey: String): StreamInfo =
        json.decodeFromString(
            exec("api/app/library/stream", mapOf("media_key" to mediaKey), post = true)
        )

    suspend fun watchlist(): WatchlistResponse =
        json.decodeFromString(exec("api/app/watchlist"))

    suspend fun watchlistAdd(mediaKey: String): ControlResult =
        json.decodeFromString(
            exec("api/app/watchlist/add", mapOf("media_key" to mediaKey), post = true)
        )

    suspend fun watchlistRemove(mediaKey: String): ControlResult =
        json.decodeFromString(
            exec("api/app/watchlist/remove", mapOf("media_key" to mediaKey), post = true)
        )

    suspend fun control(token: String, action: String, value: Double = 0.0): ControlResult =
        json.decodeFromString(
            exec(
                "api/app/control",
                mapOf("token" to token, "action" to action, "value" to value.toString()),
                post = true,
            )
        )
}
