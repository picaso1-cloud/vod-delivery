from src.osubs import os_hash, srt_to_vtt

SRT = (
    "1\n00:00:00,500 --> 00:00:01,500\nHello world\n\n"
    "2\n00:00:01,600 --> 00:00:02,000\nDeuxième ligne\n"
).encode()


def test_srt_to_vtt_adds_header_and_fixes_timestamps():
    out = srt_to_vtt(SRT).decode()
    assert out.startswith("WEBVTT")
    assert "00:00:00.500 --> 00:00:01.500" in out
    assert "Hello world" in out


def test_srt_to_vtt_strips_bom():
    out = srt_to_vtt(b"\xef\xbb\xbf1\n00:00:00,500 --> 00:00:01,500\nhi\n").decode()
    assert out.startswith("WEBVTT")


def test_os_hash_is_16_hex_and_size_sensitive(tmp_path):
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 100_000)
    h = os_hash(f)
    assert len(h) == 16
    assert int(h, 16) >= f.stat().st_size
    assert os_hash(f) == h
    f.write_bytes(b"y" * 100_000)
    assert os_hash(f) != h


def test_os_hash_matches_reference_vector(tmp_path):
    import struct

    data = bytes(range(256)) * 512
    f = tmp_path / "ref.bin"
    f.write_bytes(data)
    expected = len(data)
    for pos in (0, len(data) - 65536):
        chunk = data[max(0, pos):pos + 65536]
        for i in range(0, len(chunk), 8):
            expected = (expected + struct.unpack("<Q", chunk[i:i + 8])[0]) & 0xFFFFFFFFFFFFFFFF
    assert os_hash(f) == f"{expected:016x}"
