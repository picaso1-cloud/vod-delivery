from src.signing import make_sig, signed_query, stream_path, verify

SECRET = "s3cret"


def _parts(query: str):
    exp = query.split("exp=")[1].split("&")[0]
    sig = query.split("sig=")[1]
    return exp, sig


def test_roundtrip_and_expiry():
    exp, sig = _parts(signed_query(SECRET, "abc", ttl=60, now=1000))
    assert verify(SECRET, "abc", exp, sig, now=1000)
    assert not verify(SECRET, "abc", exp, sig, now=1061)


def test_wrong_token_or_secret_rejected():
    exp, sig = _parts(signed_query(SECRET, "abc", ttl=60, now=1000))
    assert not verify(SECRET, "xyz", exp, sig, now=1000)
    assert not verify("other", "abc", exp, sig, now=1000)


def test_tampered_exp_rejected():
    exp, sig = _parts(signed_query(SECRET, "abc", ttl=60, now=1000))
    assert not verify(SECRET, "abc", str(int(exp) + 1), sig, now=1000)


def test_garbage_rejected():
    assert not verify(SECRET, "abc", "", "", now=1000)
    assert not verify(SECRET, "abc", "nope", "sig", now=1000)


def test_make_sig_stable():
    assert make_sig(SECRET, "abc", 123) == make_sig(SECRET, "abc", 123)


def test_stream_path_shape():
    path = stream_path(SECRET, "tok123", ttl=60)
    assert path.startswith("/s/tok123?exp=")
    assert "&sig=" in path
