import asyncio
from sql_utils import build_movie_insert_sql
from storage import execute_neon_insert, save_to_json
from media_service import resolve_poster_for_title


def ensure_poster_url(record):
    if record.get("poster_url"):
        return record

    fallback = resolve_poster_for_title(record.get("movie") or record.get("source_query"))
    if fallback and fallback.get("poster_url"):
        record["poster_url"] = fallback["poster_url"]
        if fallback.get("title") and not record.get("movie"):
            record["movie"] = fallback["title"]
        if fallback.get("description") and not record.get("description"):
            record["description"] = fallback["description"]
        if fallback.get("year") and not record.get("year"):
            record["year"] = fallback["year"]
    return record


async def finalize_pending_post(pending, description):
    record = dict(pending)
    record["description"] = description.strip()
    record = ensure_poster_url(record)
    if not record.get("type") and record.get("tmdb_media_type"):
        record["type"] = record["tmdb_media_type"]

    if not record.get("poster_url"):
        raise ValueError("Poster image is required before finalizing the post.")

    final_poster = record.get("poster_url")
    record["sample_images"] = [final_poster] if final_poster else []

    insert_sql = build_movie_insert_sql(record)
    record["insert_query"] = insert_sql

    record["neon_inserted"] = await asyncio.to_thread(execute_neon_insert, insert_sql)

    save_to_json(record)
    return record, insert_sql
