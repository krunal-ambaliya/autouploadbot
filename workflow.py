import asyncio
from sql_utils import build_movie_insert_sql
from storage import execute_neon_insert, save_to_json


async def finalize_pending_post(pending, description):
    record = dict(pending)
    record["description"] = description.strip()

    final_poster = record.get("poster_url")
    record["sample_images"] = [final_poster] if final_poster else []

    insert_sql = build_movie_insert_sql(record)
    record["insert_query"] = insert_sql

    record["neon_inserted"] = await asyncio.to_thread(execute_neon_insert, insert_sql)

    save_to_json(record)
    return record, insert_sql
