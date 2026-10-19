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

    private suspend fun exec(path: String, params: Map<String, String> = emptyMap(), post: Boolean = false): String =
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
                    AppLog.api(
                        method, display, resp.code, millis, body.length.toLong(),
                        if (resp.isSuccessful) null else body.take(160),
                    )
                    logged = true
                    if (!resp.isSuccessful) error("HTTP ${resp.code}: ${body.take(240)}")
                    body
                }
            } catch (e: Exception) {
                if (!logged) {
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
        return json.decodeFromString(exec("api/app/stream", params, post = true))
    }

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

    suspend fun play(mediaKey: String, tv: String, startPosition: Double = 0.0): ControlResult =
        json.decodeFromString(
            exec(
                "api/app/play",
                mapOf("media_key" to mediaKey, "tv" to tv, "start_position" to startPosition.toString()),
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

    suspend fun discover(q: String, kind: String = ""): DiscoverResponse =
        json.decodeFromString(exec("api/app/discover", mapOf("q" to q, "kind" to kind)))

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
