import json
import logging
import re

import httpx

from .config import LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger("vod-delivery")

SYSTEM_PROMPT = """You interpret messages from a home media Telegram bot. The user watches movies/series on their TVs through this bot.

First think briefly about what the user actually wants. Then output ONE JSON object as the LAST thing in your reply.

Schema: {"intent": "...", "title": string|null, "year": number|null, "season": number|null, "episode": number|null, "language": string|null, "subtitles": string|null, "media_type": "movie"|"series"|null, "start_position": number|null, "answer": string|null, "titles": [{"title": string, "year": number|null}]|null}

- "intent": one of:
  "search"       — user wants to watch/find a specific movie or series (even loosely phrased)
  "resume"       — user wants to continue/resume where they left off (a series or movie they were watching)
  "next_episode" — user wants the NEXT episode of a series they are watching
  "status"       — user asks what is playing / why nothing plays / about the current stream state
  "feedback"     — user reports a problem or complains (stream failed, buffering, quality bad, wrong episode...)
  "smalltalk"    — greetings, thanks, questions about the bot itself, anything that is not a watch request
  "recommend"    — user asks a KNOWLEDGE QUESTION about movies/series: sequel/order questions ("what came after X",
                   "which movie is first"), "movies like X", "best movie of actor Y", "what should I watch if I
                   liked Z". Answer factually from your own film knowledge in "answer" (1-3 short sentences),
                   then put the 1-3 SPECIFIC films/series that best answer the question in "titles" — real,
                   exact titles with their release year (e.g. "resident evil apocalypse" 2004), never vague
                   descriptions. For "movies like X" prefer well-known titles the catalog plausibly has.
- "title": the movie/series name they want (only for intent=search). EXPAND well-known abbreviations
  to the full title when confident: "got"->"game of thrones", "twd"->"the walking dead",
  "bb"->"breaking bad", "hp"->"harry potter". Keep as-is if unsure.
- "year": release year if stated, else null
- "season"/"episode": numbers when the user names them ("episode 3", "s2e5", "season 2"), else null.
  "episode 3 of got" -> title="game of thrones", episode=3, season=null (unknown).
- "language": preferred AUDIO language as 2-letter code: EN, AR, FR, IT, ES, TR, DE, PT, NL, RU, HI, JA, KO, ZH. null if unsure.
- "subtitles": desired SUBTITLE language as 2-letter code (same map). null if not mentioned.
- "media_type": "series" if they mean a show/season/episode, "movie" if a film, else null
- "start_position": SECONDS to start playback from, when the user names a specific time position ("from minute 30" -> 1800, "start at 1:20:00" -> 4800, "30 minutes in" -> 1800, "1h20m mark" -> 4800, Arabic "من الدقيقة 30" -> 1800, "من الساعة 1:20" -> 4800). null if no time position is given. Valid for search and resume intents.

Examples:
"i need to watch the movie greenland 2020 english" -> think: wants to watch Greenland, 2020, English audio -> {"intent":"search","title":"greenland","year":2020,"season":null,"episode":null,"language":"EN","subtitles":null,"media_type":"movie"}
"the stream didnt start and failed" -> think: user reports a playback problem, not asking for a movie -> {"intent":"feedback","title":null,"year":null,"season":null,"episode":null,"language":null,"subtitles":null,"media_type":null}
"what are you playing right now" -> {"intent":"status","title":null,"year":null,"season":null,"episode":null,"language":null,"subtitles":null,"media_type":null}
"la casa de papel season 2" -> {"intent":"search","title":"la casa de papel","year":null,"season":2,"episode":null,"language":null,"subtitles":null,"media_type":"series"}
"thanks man" -> {"intent":"smalltalk","title":null,"year":null,"season":null,"episode":null,"language":null,"subtitles":null,"media_type":null}
"put on aladdin in arabic with english subs" -> {"intent":"search","title":"aladdin","year":null,"season":null,"episode":null,"language":"AR","subtitles":"EN","media_type":"movie"}
"lets get into episode 3 of got now" -> think: "got" = game of thrones, they want episode 3 -> {"intent":"search","title":"game of thrones","year":null,"season":null,"episode":3,"language":null,"subtitles":null,"media_type":"series"}
"continue where i left in the walking dead" -> {"intent":"resume","title":"the walking dead","year":null,"season":null,"episode":null,"language":null,"subtitles":null,"media_type":"series"}
"restart the resident evil 2004 movie from the 30min in its play time" -> think: wants to watch Resident Evil (2004) starting at minute 30 -> {"intent":"search","title":"resident evil","year":2004,"season":null,"episode":null,"language":null,"subtitles":null,"media_type":"movie","start_position":1800}
"next episode of got" -> {"intent":"next_episode","title":"game of thrones","year":null,"season":null,"episode":null,"language":null,"subtitles":null,"media_type":"series","answer":null,"titles":null}
"what is the next movie released after the first resident evil 2002" -> think: knowledge question about film order; RE Apocalypse came 2004 -> {"intent":"recommend","title":null,"year":null,"season":null,"episode":null,"language":null,"subtitles":null,"media_type":null,"answer":"After the first Resident Evil (2002), the next movie released in the series was Resident Evil: Apocalypse in 2004, followed by Extinction in 2007.","titles":[{"title":"resident evil apocalypse","year":2004},{"title":"resident evil extinction","year":2007}]}
"""

JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)


def _balanced_objects(content: str):
    out = []
    i = 0
    while i < len(content):
        if content[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        for j in range(i, len(content)):
            ch = content[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(content[i:j + 1])
                    except json.JSONDecodeError:
                        break
                    out.append(obj)
                    i = j
                    break
        i += 1
    return out
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

EMPTY = {"intent": "search", "title": None, "year": None, "season": None, "episode": None, "language": None, "subtitles": None, "media_type": None, "start_position": None, "answer": None, "titles": None}
INTENTS = ("search", "resume", "next_episode", "status", "feedback", "smalltalk", "recommend")


async def parse_request(text: str) -> dict:
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.1,
        "max_tokens": 2500,
    }
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(f"{LLM_BASE_URL}/chat/completions", json=payload)
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        content = message.get("content") or ""
        if not content.strip():
            content = message.get("reasoning_content") or ""
    content = THINK_RE.sub("", content)

    parsed = None
    for obj in reversed(_balanced_objects(content)):
        if isinstance(obj, dict) and "intent" in obj:
            parsed = obj
            break
    if parsed is None:
        for candidate in reversed(JSON_RE.findall(content)):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and "intent" in candidate:
                parsed = candidate
                break
    if not isinstance(parsed, dict):
        logger.warning("llm returned no usable JSON for %r; raw tail: %r", text, content[-200:])
        return {**EMPTY, "title": text}

    result = dict(EMPTY)
    intent = parsed.get("intent")
    result["intent"] = intent if intent in INTENTS else "search"
    result["answer"] = str(parsed.get("answer") or "").strip() or None
    try:
        sp = float(parsed.get("start_position"))
        result["start_position"] = sp if sp > 0 else None
    except (TypeError, ValueError):
        result["start_position"] = None
    raw_titles = parsed.get("titles")
    if isinstance(raw_titles, list):
        titles = []
        for t in raw_titles[:3]:
            if not isinstance(t, dict):
                continue
            t_title = str(t.get("title") or "").strip()
            if not t_title:
                continue
            t_year = t.get("year")
            try:
                t_year = int(t_year) if t_year else None
            except (TypeError, ValueError):
                t_year = None
            titles.append({"title": t_title, "year": t_year})
        if titles:
            result["titles"] = titles
    title = str(parsed.get("title") or "").strip()
    result["title"] = title if title else (text if result["intent"] == "search" else None)
    try:
        result["year"] = int(parsed["year"]) if parsed.get("year") else None
    except (TypeError, ValueError):
        result["year"] = None
    for field in ("season", "episode"):
        try:
            result[field] = int(parsed[field]) if parsed.get(field) else None
        except (TypeError, ValueError):
            result[field] = None
    result["language"] = str(parsed["language"]).upper() if parsed.get("language") else None
    result["subtitles"] = str(parsed["subtitles"]).upper() if parsed.get("subtitles") else None
    if parsed.get("media_type") in ("movie", "series"):
        result["media_type"] = parsed["media_type"]
    return result


SUGGEST_PROMPT = """You are a film curator for a private media catalog. The user describes what they want to watch in natural language. You receive a candidate list from the catalog (title, year, kind, rating, short plot). Pick the 5-8 titles that best match the intent, mood, genre and themes. If the query contains several themes, prefer titles matching ALL of them (for example zombies AND survival) over titles matching only one. Prefer higher ratings and well-known titles. Only pick from the candidates.

Output ONE JSON object as the last thing in your reply:
{"note": "one short sentence explaining the selection", "picks": [{"title": "exact candidate title", "year": 2020, "reason": "one short reason"}]}

If nothing fits, return {"note": "...", "picks": []}. Output the JSON object only, no prose before or after."""


async def suggest(query: str, candidates: list[dict]) -> dict | None:
    lines = []
    for c in candidates:
        plot = (c.get("plot") or "").replace("\n", " ")[:140]
        lines.append(
            f"- {c.get('title')} ({c.get('year') or '?'}) [{c.get('kind')}] "
            f"rating={float(c.get('rating') or 0):.1f} :: {plot}"
        )
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SUGGEST_PROMPT},
            {"role": "user", "content": f"QUERY: {query}\n\nCANDIDATES:\n" + "\n".join(lines)},
        ],
        "temperature": 0.2,
        "max_tokens": 6000,
    }
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(f"{LLM_BASE_URL}/chat/completions", json=payload)
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            content = message.get("content") or message.get("reasoning_content") or ""
        for obj in reversed(_balanced_objects(content)):
            if isinstance(obj, dict) and "picks" in obj:
                return obj
        logger.warning("llm suggest: no picks JSON; tail=%r", content[-200:])
    except Exception:
        logger.warning("llm suggest failed", exc_info=True)
    return None
