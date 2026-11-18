SPECS = {
    "copy": {"rank": 5, "width": 0, "height": 0, "maxrate": 0, "bufsize": 0, "crf": 0},
    "1080p": {"rank": 4, "width": 1920, "height": 1080, "maxrate": 4000, "bufsize": 8000, "crf": 23},
    "720p": {"rank": 3, "width": 1280, "height": 720, "maxrate": 2200, "bufsize": 4400, "crf": 23},
    "480p": {"rank": 2, "width": 854, "height": 480, "maxrate": 1200, "bufsize": 2400, "crf": 24},
    "360p": {"rank": 1, "width": 640, "height": 360, "maxrate": 700, "bufsize": 1400, "crf": 25},
}

ORDER = ["copy", "1080p", "720p", "480p", "360p"]


def spec(name: str | None) -> dict:
    return SPECS.get((name or "").lower(), SPECS["1080p"])


def rank(name: str | None) -> int:
    return spec(name)["rank"]


def next_lower(name: str | None) -> str | None:
    current = (name or "1080p").lower()
    index = ORDER.index(current) if current in ORDER else ORDER.index("1080p")
    return ORDER[index + 1] if index + 1 < len(ORDER) else None


def one_above(name: str | None) -> str | None:
    current = (name or "").lower()
    index = ORDER.index(current) if current in ORDER else 0
    return ORDER[index - 1] if index - 1 >= 0 else None


def is_transcode(name: str | None) -> bool:
    return (name or "").lower() != "copy"
