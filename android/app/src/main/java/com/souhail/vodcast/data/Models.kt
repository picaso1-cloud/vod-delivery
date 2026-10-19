package com.souhail.vodcast.data

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement

@Serializable
data class MediaVariant(
    val media_key: String,
    val lang: String? = null,
    val ext: String? = null,
)

@Serializable
data class MediaGroup(
    val kind: String = "movie",
    val title: String = "",
    val year: Int? = null,
    val score: Double = 0.0,
    val poster: String? = null,
    val plot: String? = null,
    val variants: List<MediaVariant> = emptyList(),
    val channel_id: Int? = null,
)

@Serializable
data class EpisodeItem(
    val episode: Int = 0,
    val title: String? = null,
    val media_key: String = "",
    val ext: String? = null,
    val duration: String? = null,
)

@Serializable
data class EpisodesResponse(
    val episodes: Map<String, List<JsonElement>> = emptyMap(),
    val items: Map<String, List<EpisodeItem>> = emptyMap(),
    val series_id: Int = 0,
)

@Serializable
data class TvInfo(
    val name: String = "",
    val ip: String? = null,
    val kind: String? = null,
    val prefer_proxy: Int = 0,
    val downgrade: Int = 0,
    val ignored: Int = 0,
)

@Serializable
data class CastMethod(
    val method: String = "",
    val available: Boolean = false,
    val detail: String? = null,
    val verified_at: String? = null,
)

@Serializable
data class TvRow(
    val udn: String = "",
    val name: String = "",
    val ip: String? = null,
    val mac: String? = null,
    val kind: String? = null,
    val prefer_proxy: Int = 0,
    val downgrade: Int = 0,
    val location: String? = null,
)

@Serializable
data class TvDetail(
    val ok: Boolean = false,
    val tv: TvRow? = null,
    val methods: List<CastMethod> = emptyList(),
    val last_probe: String? = null,
    val error: String? = null,
)

@Serializable
data class StreamInfo(
    val ok: Boolean = false,
    val token: String? = null,
    val title: String? = null,
    val mime: String? = null,
    val duration: Double = 0.0,
    val mode: String? = null,
    val format: String? = null,
    val prepared: Boolean = false,
    val lan_url: String? = null,
    val public_url: String? = null,
    val expires_in: Int = 0,
    val start_offset: Double = 0.0,
    val error: String? = null,
)

@Serializable
data class SessionInfo(
    val ok: Boolean = false,
    val token: String? = null,
    val mode: String? = null,
    val position: Double? = null,
    val duration: Double? = null,
    val max_served: Long = 0,
    val clients: Int = 0,
    val error: String? = null,
)

@Serializable
data class ServerConfig(
    val ok: Boolean = false,
    val version: String = "",
    val lan_ip: String = "",
    val public_base: String? = null,
    val has_public: Boolean = false,
    val server_name: String = "",
)

@Serializable
data class ControlResult(
    val ok: Boolean = false,
    val detail: String? = null,
    val error: String? = null,
)

@Serializable
data class LiveCategory(
    val id: Int = 0,
    val name: String = "",
    val count: Int = 0,
)

@Serializable
data class LiveChannel(
    val kind: String = "live",
    val channel_id: Int = 0,
    val title: String = "",
    val category_id: Int = 0,
    val icon: String? = null,
)
