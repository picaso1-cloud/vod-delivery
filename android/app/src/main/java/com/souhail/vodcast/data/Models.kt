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
    val native_title: String? = null,
    val year: Int? = null,
    val score: Double = 0.0,
    val poster: String? = null,
    val plot: String? = null,
    val variants: List<MediaVariant> = emptyList(),
    val channel_id: Int? = null,
    val rating: Double = 0.0,
    val reason: String? = null,
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
    val mac: String? = null,
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
data class BannedEntry(
    val mac: String = "",
    val label: String? = null,
    val created_at: String? = null,
)

@Serializable
data class BannedTvs(
    val ok: Boolean = false,
    val banned: List<BannedEntry> = emptyList(),
    val macs: List<String> = emptyList(),
    val ips: List<String> = emptyList(),
    val names: List<String> = emptyList(),
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
    val live: Boolean = false,
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
data class ActiveSession(
    val token: String = "",
    val title: String = "",
    val mode: String = "",
    val mime: String? = null,
    val position: Double? = null,
    val duration: Double = 0.0,
    val max_served: Long = 0,
    val clients: Int = 0,
    val live: Boolean = false,
    val tv: String? = null,
    val started_at: Double = 0.0,
)

@Serializable
data class ActivePlayInfo(
    val tv: String? = null,
    val paused: Boolean = false,
    val token: String? = null,
)

@Serializable
data class SessionsResponse(
    val ok: Boolean = false,
    val sessions: List<ActiveSession> = emptyList(),
    val active_play: ActivePlayInfo? = null,
)

@Serializable
data class AudioTrack(
    val index: Int = -1,
    val codec: String? = null,
    val lang: String? = null,
    val title: String? = null,
    val channels: Int? = null,
    val default: Boolean = false,
)

@Serializable
data class SubTrack(
    val index: Int = -1,
    val codec: String? = null,
    val lang: String? = null,
    val title: String? = null,
    val forced: Boolean = false,
    val default: Boolean = false,
)

@Serializable
data class PlayerRegistration(
    val ok: Boolean = false,
    val player_id: String = "",
    val name: String = "",
)

@Serializable
data class UpdateInfo(
    val ok: Boolean = false,
    val version_name: String = "",
    val version_code: Int = 0,
    val size: Long = 0,
    val notes: String = "",
    val available: Boolean = false,
)

@Serializable
data class TracksResponse(
    val ok: Boolean = false,
    val media_key: String = "",
    val title: String = "",
    val audio: List<AudioTrack> = emptyList(),
    val subs: List<SubTrack> = emptyList(),
    val probed_at: String? = null,
    val empty: Boolean = false,
    val deferred: Boolean = false,
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

@Serializable
data class StatusPosition(
    val position: Double = 0.0,
    val duration: Double = 0.0,
)

@Serializable
data class StatusDiag(
    val token: String? = null,
    val max_served: Long = 0,
    val is_live: Boolean = false,
)

@Serializable
data class StatusInfo(
    val playing: Boolean = false,
    val tv: String? = null,
    val paused: Boolean = false,
    val title: String? = null,
    val position: StatusPosition? = null,
    val diag: StatusDiag? = null,
)

@Serializable
data class DiscoverResponse(
    val ok: Boolean = false,
    val query: String = "",
    val ai_pending: Boolean = false,
    val ai_note: String = "",
    val ai_picks: List<MediaGroup> = emptyList(),
    val by_name: List<MediaGroup> = emptyList(),
    val by_meaning: List<MediaGroup> = emptyList(),
    val items: List<MediaGroup> = emptyList(),
    val total: Int = 0,
    val page: Int = 1,
    val page_size: Int = 20,
    val has_more: Boolean = false,
    val sort: String = "relevance",
)

@Serializable
data class LibraryItem(
    val media_key: String = "",
    val kind: String = "",
    val title: String = "",
    val series_title: String? = null,
    val season: Int? = null,
    val episode: Int? = null,
    val size_bytes: Long = 0,
    val status: String = "",
    val poster: String? = null,
)

@Serializable
data class LibraryResponse(
    val ok: Boolean = false,
    val items: List<LibraryItem> = emptyList(),
    val usage_bytes: Long = 0,
    val limit_bytes: Long = 0,
)

@Serializable
data class WatchlistItem(
    val kind: String = "",
    val ref_id: Int = 0,
    val title: String = "",
    val year: Int? = null,
    val poster: String? = null,
)

@Serializable
data class WatchlistResponse(
    val ok: Boolean = false,
    val items: List<WatchlistItem> = emptyList(),
)

@Serializable
data class ContinueItem(
    val media_key: String = "",
    val kind: String = "",
    val title: String = "",
    val poster: String? = null,
    val year: Int? = null,
    val position: Double = 0.0,
    val duration: Double = 0.0,
    val updated_at: String? = null,
)

@Serializable
data class ContinueResponse(
    val ok: Boolean = false,
    val items: List<ContinueItem> = emptyList(),
)

@Serializable
data class ProgressInfo(
    val ok: Boolean = false,
    val position: Double = 0.0,
    val duration: Double = 0.0,
)

@Serializable
data class SimilarResponse(
    val ok: Boolean = false,
    val title: String = "",
    val items: List<MediaGroup> = emptyList(),
)

@Serializable
data class DiscoverAiResponse(
    val ok: Boolean = false,
    val query: String = "",
    val ai_note: String = "",
    val ai_picks: List<MediaGroup> = emptyList(),
)

@Serializable
data class QueueItem(
    val media_key: String = "",
    val title: String = "",
    val status: String = "",
    val error: String? = null,
)

@Serializable
data class QueueResponse(
    val ok: Boolean = false,
    val items: List<QueueItem> = emptyList(),
)
