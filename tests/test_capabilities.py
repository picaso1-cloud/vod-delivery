from src.capabilities import (
    best_delivery_mime,
    choose_protocol_info,
    delivery_supported,
    digest_protocols,
    find_control_url,
    minimal_protocol_info,
    origin_of,
    parse_sink_protocols,
    supports_mime,
)

SINK_XML = (
    "<u:GetProtocolInfoResponse><SinkProtocolInfo>"
    "http-get:*:video/mp4:DLNA.ORG_PN=AVC_MP4_MP_SD_AAC_MULT5;DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000,"
    "http-get:*:video/mpeg:*,"
    "http-get:*:video/x-matroska:*,"
    "http-get:*:audio/mpeg:DLNA.ORG_PN=MP3;DLNA.ORG_OP=00"
    "</SinkProtocolInfo></u:GetProtocolInfoResponse>"
)


def test_parse_sink_protocols():
    entries = parse_sink_protocols(SINK_XML)
    assert len(entries) == 4
    assert entries[0].startswith("http-get:*:video/mp4:")


def test_parse_sink_protocols_empty():
    assert parse_sink_protocols("<x></x>") == []


def test_digest_protocols():
    digest = digest_protocols(parse_sink_protocols(SINK_XML))
    assert digest["count"] == 4
    assert digest["mimes"] == ["audio/mpeg", "video/mp4", "video/mpeg", "video/x-matroska"]
    assert "AVC_MP4_MP_SD_AAC_MULT5" in digest["profiles"]
    assert "01" in digest["ops"]
    assert "00" in digest["ops"]
    assert any(flag.startswith("0170") for flag in digest["flags"])


def test_supports_mime_exact_and_params():
    mimes = ["video/mp4", "audio/mpeg"]
    assert supports_mime(mimes, "video/mp4")
    assert supports_mime(mimes, "video/mp4; codecs=avc1")
    assert not supports_mime(mimes, "video/mpeg")


def test_supports_mime_wildcards():
    assert supports_mime(["video/*"], "video/x-matroska")
    assert supports_mime(["*/*"], "application/octet-stream")
    assert supports_mime(["*"], "video/mp4")


def test_minimal_protocol_info():
    assert minimal_protocol_info("video/mpeg") == "http-get:*:video/mpeg:DLNA.ORG_OP=01"


def test_choose_protocol_info_without_digest():
    assert choose_protocol_info("video/mpeg", None) == "http-get:*:video/mpeg:DLNA.ORG_OP=01"


def test_choose_protocol_info_is_connect_sdk_minimal():
    digest = digest_protocols(parse_sink_protocols(SINK_XML))
    assert choose_protocol_info("video/mp4", digest) == "http-get:*:video/mp4:DLNA.ORG_OP=01"


def test_choose_protocol_info_unsupported_mime_stays_minimal():
    digest = digest_protocols(["http-get:*:audio/mpeg:DLNA.ORG_OP=00"])
    assert choose_protocol_info("video/mpeg", digest) == "http-get:*:video/mpeg:DLNA.ORG_OP=01"


def test_find_control_url():
    desc = (
        "<root><device><serviceList>"
        "<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>"
        "<controlURL>/upnp/control/AVTransport1</controlURL></service>"
        "<service><serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>"
        "<controlURL>/upnp/control/ConnectionManager1</controlURL></service>"
        "</serviceList></device></root>"
    )
    assert find_control_url(desc, "urn:schemas-upnp-org:service:ConnectionManager:1") == "/upnp/control/ConnectionManager1"
    assert find_control_url(desc, "urn:schemas-upnp-org:service:AVTransport:1") == "/upnp/control/AVTransport1"
    assert find_control_url(desc, "urn:schemas-upnp-org:service:RenderingControl:1") is None


def test_origin_of():
    assert origin_of("http://192.168.1.183:9197/dmr") == "http://192.168.1.183:9197"
    assert origin_of("http://192.168.1.107:2870/dmr.xml") == "http://192.168.1.107:2870"


def test_didl_capability_protocol_info_override():
    from src.caster import _didl
    didl = _didl("T", "http://x/1.ts", "video/mpeg", None, "http-get:*:video/mpeg:DLNA.ORG_OP=01")
    assert 'protocolInfo="http-get:*:video/mpeg:DLNA.ORG_OP=01"' in didl


def test_didl_default_unchanged():
    from src.caster import _didl
    default = _didl("T", "http://x/1.ts", "video/mpeg")
    minimal = _didl("T", "http://x/1.ts", "video/mpeg", None, "http-get:*:video/mpeg:DLNA.ORG_OP=01")
    assert "protocolInfo" in default
    assert default != minimal


REAL_SINK = (
    "<u:GetProtocolInfoResponse><Sink>"
    "http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_SM,"
    "http-get:*:audio/mpeg:DLNA.ORG_PN=MP3,"
    "http-get:*:video/mp4:DLNA.ORG_PN=AVC_MP4_BL_CIF15_AAC_520,"
    "http-get:*:video/mpeg:DLNA.ORG_PN=MPEG_PS_NTSC,"
    "http-get:*:video/mpeg:DLNA.ORG_PN=MPEG_PS_PAL,"
    "http-get:*:video/vnd.dlna.mpeg-tts:DLNA.ORG_PN=MPEG_TS_SD_NA,"
    "http-get:*:video/vnd.dlna.mpeg-tts:DLNA.ORG_PN=MPEG_TS_HD_NA,"
    "http-get:*:video/mp4:*,"
    "http-get:*:video/x-matroska:*,"
    "http-get:*:video/avi:*,"
    "http-get:*:video/mpeg:*"
    "</Sink></u:GetProtocolInfoResponse>"
)


def test_parse_sink_tag_variant():
    entries = parse_sink_protocols(REAL_SINK)
    assert len(entries) == 11
    assert "video/x-matroska" in entries[8]


def test_real_sink_digest():
    digest = digest_protocols(parse_sink_protocols(REAL_SINK))
    assert "video/x-matroska" in digest["mimes"]
    assert "MPEG_TS_HD_NA" in digest["profiles"]


def test_delivery_mime_samsung_x_mkv_alias():
    sink = ("<S><Sink>http-get:*:video/x-mkv:*,http-get:*:video/mpeg:*,"
            "http-get:*:video/mp4:*</Sink></S>")
    digest = digest_protocols(parse_sink_protocols(sink))
    assert best_delivery_mime("mkv", digest, "video/x-matroska") == "video/x-mkv"
    assert choose_protocol_info("video/x-mkv", digest) == "http-get:*:video/x-mkv:DLNA.ORG_OP=01"


def test_delivery_mime_lifemaxx_matroska():
    digest = digest_protocols(parse_sink_protocols(REAL_SINK))
    assert best_delivery_mime("mkv", digest, "video/x-matroska") == "video/x-matroska"


def test_delivery_mime_uses_real_declarations():
    digest = digest_protocols(parse_sink_protocols(REAL_SINK))
    assert best_delivery_mime("mkv", digest, "video/x-matroska") == "video/x-matroska"
    assert best_delivery_mime("ts", digest, "video/mpeg") == "video/mpeg"
    assert best_delivery_mime("mp4", digest, "video/mp4") == "video/mp4"


def test_delivery_mime_unsupported_falls_back():
    digest = digest_protocols(parse_sink_protocols(REAL_SINK))
    assert best_delivery_mime("flv", digest, "video/x-flv") == "video/x-flv"
    assert delivery_supported("mkv", digest) is True
    assert delivery_supported("flv", digest) is False
