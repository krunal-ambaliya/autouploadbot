import logging
import os

import requests

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_POSTER_BASE_URL = "https://image.tmdb.org/t/p/w500"

logger = logging.getLogger(__name__)


def get_tmdb_api_key():
    return os.getenv("TMDB_API_KEY") or os.getenv("TMDB_API_TOKEN")


def extract_tmdb_details(item):
    title = item.get("title") or item.get("name") or "Untitled"
    date = item.get("release_date") or item.get("first_air_date") or ""
    year = date.split("-")[0] if date else None
    poster_path = item.get("poster_path")
    poster_url = f"{TMDB_POSTER_BASE_URL}{poster_path}" if poster_path else None
    media_type = item.get("media_type") or item.get("kind") or "movie"
    item_id = item.get("id")
    source_url = None
    if item_id:
        if media_type == "tv":
            source_url = f"https://www.themoviedb.org/tv/{item_id}"
        else:
            source_url = f"https://www.themoviedb.org/movie/{item_id}"

    return {
        "tmdb_id": item_id,
        "title": title,
        "year": year,
        "poster_url": poster_url,
        "description": item.get("overview") or "",
        "media_type": media_type,
        "source_url": source_url,
        "source_provider": "TMDb",
    }


def search_tmdb_titles(query, limit=15):
    api_key = get_tmdb_api_key()
    if not api_key or not query:
        return []

    try:
        response = requests.get(
            f"{TMDB_BASE_URL}/search/multi",
            params={"api_key": api_key, "query": query},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json() or {}
        results = payload.get("results") or []
    except (requests.RequestException, ValueError) as exc:
        logger.warning("TMDb search failed for %r: %s", query, exc)
        return []

    items = []
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("media_type") not in {"movie", "tv"}:
            continue
        items.append(extract_tmdb_details(item))
        if len(items) >= limit:
            break

    return items


def search_tmdb(query):
    results = search_tmdb_titles(query, limit=1)
    return results[0] if results else None
