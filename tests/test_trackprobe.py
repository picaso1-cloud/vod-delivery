from src import trackprobe
from src.proxy import ProxySession, _map_args

STREAMS = [
    {"index": 0, "codec_type": "video", "codec_name": "h264"},
    {
        "index": 1,
        "codec_type": "audio",
        "codec_name": "eac3",
        "channels": 6,
        "tags": {"language": "deu", "title": "Deutsch"},
        "disposition": {"default": False},
    },
    {
        "index": 2,
        "codec_type": "audio",
        "codec_name": "eac3",
        "channels": 6,
        "tags": {"language": "eng"},
        "disposition": {"default": True},
    },
    {
        "index": 7,
        "codec_type": "subtitle",
        "codec_name": "subrip",
        "tags": {"language": "ara", "title": "العربية"},
        "disposition": {"default": True, "forced": False},
    },
    {
        "index": 9,
        "codec_type": "subtitle",
        "codec_name": "hdmv_pgs_subtitle",
        "tags": {"language": "fra"},
        "disposition": {"forced": True},
    },
]


def test_parse_streams_audio_and_subs():
    out = trackprobe.parse_streams(STREAMS)
    assert [a["lang"] for a in out["audio"]] == ["deu", "eng"]
    assert out["audio"][1]["default"] is True
    assert out["audio"][0]["channels"] == 6
    assert [s["lang"] for s in out["subs"]] == ["ara", "fra"]
    assert out["subs"][1]["forced"] is True


def test_parse_streams_ignores_video():
    out = trackprobe.parse_streams(STREAMS)
    assert all(a["index"] != 0 for a in out["audio"])
    assert all(s["index"] != 0 for s in out["subs"])


def test_parse_streams_empty():
    assert trackprobe.parse_streams([]) == {"audio": [], "subs": []}


def _session(**kw):
    return ProxySession(token="t", provider_url="u", title="x", **kw)


def test_map_args_absent_by_default():
    assert _map_args(_session(), "matroska") == []


def test_map_args_audio_selection():
    assert _map_args(_session(audio_index=2), "matroska") == ["-map", "0:v:0", "-map", "0:2"]
    assert _map_args(_session(audio_index=5), "mpegts") == ["-map", "0:v:0", "-map", "0:5"]


def test_map_args_subs_only_in_matroska():
    s = _session(sub_index=7)
    assert "-c:s" in _map_args(s, "matroska")
    assert _map_args(s, "mpegts") == ["-map", "0:v:0", "-map", "0:a:0?"]
