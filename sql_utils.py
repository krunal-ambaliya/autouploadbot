import re
import time
from config import DEFAULT_GENRE, DEFAULT_STATUS, DEFAULT_TAGS, DEFAULT_TYPE


def normalize_language(audio_text):
    if not audio_text:
        return "Unknown"

    match = re.search(r"[A-Za-z]+", audio_text)
    return match.group(0) if match else audio_text.strip()


def pick_primary_quality(downloads, quality_text=None):
    preferred_order = ["4k", "2k", "1080p", "720p", "480p"]
    for quality in preferred_order:
        if quality in downloads:
            return quality

    if quality_text:
        match = re.search(r"(480p|720p|1080p|2k|4k)", quality_text, re.IGNORECASE)
        if match:
            return match.group(1).lower()

    return None


def sql_escape(value):
    return str(value).replace("'", "''")


def sql_array(values):
    if not values:
        return "ARRAY[]::text[]"

    items = ", ".join(f"'{sql_escape(item)}'" for item in values)
    return f"ARRAY[{items}]"


def sql_value(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    return f"'{sql_escape(value)}'"


def build_movie_insert_sql(record):
    title = record.get("movie") or record.get("title") or "Untitled"
    poster_url = record.get("poster_url")
    downloads = record.get("downloads", {})
    quality = pick_primary_quality(downloads, record.get("quality")) or "720p"
    language = normalize_language(record.get("audio"))
    description = record.get("description") or f"Auto generated entry for {title}"
    year = record.get("year") or time.gmtime().tm_year

    download_columns = {
        "download_480p": downloads.get("480p"),
        "download_720p": downloads.get("720p"),
        "download_1080p": downloads.get("1080p"),
        "download_2k": downloads.get("2k"),
    }

    columns = [
        "title",
        "type",
        "genre",
        "year",
        "language",
        "quality",
        "description",
        "poster_url",
        "sample_images",
        "download_480p",
        "download_720p",
        "download_1080p",
        "download_2k",
        "status",
        "tags",
        "views",
    ]

    values = [
        sql_value(title),
        sql_value(record.get("type") or DEFAULT_TYPE),
        sql_array(record.get("genre") or DEFAULT_GENRE),
        sql_value(int(year)),
        sql_value(language),
        sql_value(quality),
        sql_value(description),
        sql_value(poster_url),
        sql_array([poster_url] if poster_url else []),
        sql_value(download_columns["download_480p"]),
        sql_value(download_columns["download_720p"]),
        sql_value(download_columns["download_1080p"]),
        sql_value(download_columns["download_2k"]),
        sql_value(record.get("status") or DEFAULT_STATUS),
        sql_array(record.get("tags") or DEFAULT_TAGS),
        sql_value(int(record.get("views", 0) or 0)),
    ]

    return (
        "INSERT INTO movies "
        f"({', '.join(columns)})\nVALUES (\n"
        f"{', '.join(values)}\n"
        ");"
    )
