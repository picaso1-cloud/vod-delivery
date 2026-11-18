import re

LANG_PREFIX_RE = re.compile(r"^\|\s*([A-Za-z]{2,3})\s*\|+\s*(.+)$")
SUB_PREFIX_RE = re.compile(r"^\|*\s*SUB-([A-Za-z]{2})\s*\|+\s*")
YEAR_RE = re.compile(r"\((\d{4})\)")
SEASON_EPISODE_RE = re.compile(r"\bS(\d{1,2})\s*E(\d{1,3})\b", re.IGNORECASE)

LANG_CODES = {
    "EN", "FR", "IT", "ES", "DE", "PT", "TR", "NL", "US", "MA", "KO", "BR", "AR",
    "RU", "HI", "JA", "ZH", "CN", "SE", "NO", "DK", "FI", "PL", "CZ", "GR", "HE",
    "TH", "VN", "ID", "MY", "PH", "RO", "HU", "BG", "UA", "RS", "HR", "IN", "MX",
    "JP", "KR", "KW", "EG", "LB", "DZ", "TN", "BE", "AT", "CH", "Multi", "VOSTFR",
}


_LANG_CODES_UPPER = {c.upper() for c in LANG_CODES}


def _looks_like_lang(part: str) -> bool:
    return part.upper() in _LANG_CODES_UPPER


def _clean(title: str) -> str:
    title = re.sub(r"\s{2,}", " ", title)
    return title.strip(" -–—|·").strip()


def parse_movie_name(raw: str) -> dict:
    lang = None
    subs_lang = None
    title = raw.strip()
    sub_m = SUB_PREFIX_RE.match(title)
    if sub_m:
        subs_lang = sub_m.group(1).upper()
        title = title[sub_m.end() :]
    m = LANG_PREFIX_RE.match(title)
    if m:
        lang = m.group(1).upper()
        title = m.group(2)
    year = None
    ym = YEAR_RE.search(title)
    if ym:
        year = int(ym.group(1))
        title = title[: ym.start()] + " " + title[ym.end() :]
    return {"raw": raw, "lang": lang, "subs_lang": subs_lang, "title": _clean(title), "year": year}


_PROVIDER_TAG_RE = re.compile(r"^[A-Za-z]{1,4}\.[A-Za-z0-9]{2,}$")


def parse_series_name(raw: str) -> dict:
    name = raw.strip().strip("|").strip()
    parts = [p.strip() for p in name.split("|") if p.strip()]
    lang = None
    romanized = None
    native = None
    year = None
    m = LANG_PREFIX_RE.match(raw.strip())
    if m:
        lang = m.group(1).upper()
    for part in parts:
        if part.isdigit() and len(part) == 4:
            year = int(part)
        elif _PROVIDER_TAG_RE.match(part):
            continue
        elif _looks_like_lang(part) and (lang is None or part.upper() == lang):
            if lang is None:
                lang = part.upper()
        elif romanized is None:
            romanized = part
        elif native is None:
            native = part
    if romanized is None:
        romanized = raw
    return {"raw": raw, "lang": lang, "title": _clean(romanized), "native_title": _clean(native or ""), "year": year}


def normalize_for_grouping(title: str) -> str:
    t = title.casefold()
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


_RELEASE_TAG_RE = re.compile(
    r"(\b(2160p|1080p|1080i|720p|480p|4k|uhd|x264|x265|h264|h\.?264|h265|hevc|avc|xvid|divx|"
    r"aac2?|ac3|eac3|dts|ddp?|dd\+|truehd|atmos|web[ \-]?dl|webrip|web|bdrip|brrip|"
    r"dvdrip|dvdscr|hdrip|hdtv|bluray|blu[ \-]?ray|remux|hdr10\+?|hdr|dolby|vision|"
    r"yify|rarbg|ettv|ethd|amzn|nf|nx|proper|repack|extended|unrated|remastered|multi|"
    r"subs?|subbed|arabic|english|french|ar|en|fr)\b[. \-]*"
    r"|\bs\d{1,2}[. \-]?e\d{1,2}\b[. \-]*"
    r"|\bddp?[. \-]?\d([. \-]\d)?\b[. \-]*"
    r"|\b[57][. \-]1\b[. \-]*"
    r"|\b2[. \-]0\b[. \-]*"
    r"|\bh[. \-]?26[45]\b[. \-]*"
    r"|\b\d{3,4}[ \u00d7x]\u00d7?\d{3,4}\b[. \-]*)",
    re.IGNORECASE,
)


def guess_title_from_filename(stem: str) -> tuple[str, int | None]:
    name = re.sub(r"[._]+", " ", stem)
    name = re.sub(r"[\[\{]", "(", name).replace("]", ")").replace("}", ")")
    year_match = re.search(r"\b(19\d{2}|20\d{2})\b(?!\s*[x\u00d7]\s*\d{3})", name)
    year = int(year_match.group(1)) if year_match else None
    name = re.sub(r"\([^)]*\)", " ", name)
    name = _RELEASE_TAG_RE.sub(" ", name)
    if len(name.split()) > 1:
        name = re.sub(r"[\s\-]+[A-Z0-9]{2,10}$", "", name)
    if year:
        name = re.sub(rf"\b{year}\b", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" -\u2013\u2014.")
    return (name or stem), year
