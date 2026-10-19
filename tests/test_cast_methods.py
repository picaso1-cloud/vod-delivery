from src.capabilities import derive_cast_methods


def test_dlna_kind_marks_dlna_with_protocol_count():
    methods = derive_cast_methods("dlna", {"8009": False}, {"mimes": ["video/mp4", "video/mpeg"]})
    by_name = {m["method"]: m for m in methods}
    assert by_name["dlna"]["available"] is True
    assert "2 protocols" in by_name["dlna"]["detail"]
    assert by_name["cast"]["available"] is False


def test_cast_kind_marks_cast_even_without_port_signal():
    by_name = {m["method"]: m for m in derive_cast_methods("cast")}
    assert by_name["cast"]["available"] is True
    assert by_name["dlna"]["available"] is False


def test_port_probes_enable_methods():
    by_name = {m["method"]: m for m in derive_cast_methods(None, {"8060": True, "7000": True})}
    assert by_name["roku"]["available"] is True
    assert by_name["airplay"]["available"] is True
    assert by_name["dial"]["available"] is False
    assert by_name["webos"]["available"] is False


def test_method_order_is_stable():
    assert [m["method"] for m in derive_cast_methods("dlna")] == [
        "dlna", "cast", "dial", "roku", "airplay", "webos",
    ]
