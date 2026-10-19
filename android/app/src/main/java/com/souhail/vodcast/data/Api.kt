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
import java.util.concurrent.TimeUnit

class Api(private val baseUrl: String, private val token: String) {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
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
            val req = Request.Builder()
                .url(url(path, params))
                .header("Authorization", "Bearer $token")
                .apply { if (post) post("".toRequestBody()) }
                .build()
            client.newCall(req).execute().use { resp ->
                val body = resp.body?.string() ?: ""
                if (!resp.isSuccessful) error("HTTP ${resp.code}: ${body.take(240)}")
                body
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

    suspend fun control(token: String, action: String, value: Double = 0.0): ControlResult =
        json.decodeFromString(
            exec(
                "api/app/control",
                mapOf("token" to token, "action" to action, "value" to value.toString()),
                post = true,
            )
        )
}
