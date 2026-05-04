import os

import requests

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_POSTER_BASE_URL = "https://image.tmdb.org/t/p/w500"


def get_tmdb_api_key():
    return os.getenv("TMDB_API_KEY") or os.getenv("TMDB_API_TOKEN")


def extract_tmdb_details(item):
    title = item.get("title") or item.get("name") or "Untitled"
    date = item.get("release_date") or item.get("first_air_date") or ""
    year = date.split("-")[0] if date else None
    poster_path = item.get("poster_path")
    poster_url = f"{TMDB_POSTER_BASE_URL}{poster_path}" if poster_path else None

    return {
        "tmdb_id": item.get("id"),
        "title": title,
        "year": year,
        "poster_url": poster_url,
        "description": item.get("overview") or "",
        "media_type": item.get("media_type") or item.get("kind") or "movie",
    }


def search_tmdb(query):
    api_key = get_tmdb_api_key()
    if not api_key:
        return None

    response = requests.get(
        f"{TMDB_BASE_URL}/search/multi",
        params={"api_key": api_key, "query": query},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    results = payload.get("results") or []

    for item in results:
        if item.get("media_type") in {"movie", "tv"}:
            return extract_tmdb_details(item)

    if results:
        return extract_tmdb_details(results[0])

    return None
