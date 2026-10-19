import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]

LLM_BASE_URL: str = os.environ.get("LLM_BASE_URL", "http://localhost:8086/v1")
LLM_MODEL: str = os.environ.get("LLM_MODEL", "current-model")

XSTREAM_BASE: str = os.environ["XSTREAM_BASE"].rstrip("/")
XSTREAM_USERNAME: str = os.environ["XSTREAM_USERNAME"]
XSTREAM_PASSWORD: str = os.environ["XSTREAM_PASSWORD"]
PROVIDER_USER_AGENT: str = os.environ.get("PROVIDER_USER_AGENT", "SanSatV4Player")
OPENSUBTITLES_API_KEY = os.getenv("OPENSUBTITLES_API_KEY", "")

INDEX_LANGS: set[str | None] = {"EN", "FR", "AR", None}


def vod_movie_url(stream_id: int, extension: str = "mkv") -> str:
    stream_host = XSTREAM_BASE.replace(":25461", ":88")
    return f"{stream_host}/movie/{XSTREAM_USERNAME}/{XSTREAM_PASSWORD}/{stream_id}.{extension}"
