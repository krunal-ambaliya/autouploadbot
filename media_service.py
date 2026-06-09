import html
import hashlib
import os
import re
import time
from urllib.parse import quote_plus, urljoin

import requests


IMDB_FIND_URL = "https://www.imdb.com/find/"
IMDB_BASE_URL = "https://www.imdb.com"
IMDB_SEARCH_API_URL = "https://api.imdbapi.dev/search/titles"
CLOUDINARY_UPLOAD_URL = "https://api.cloudinary.com/v1_1/{cloud_name}/image/upload"


def _build_cloudinary_auth_params(public_id=None):
    upload_preset = os.getenv("CLOUDINARY_UPLOAD_PRESET")
    if upload_preset:
        params = {"upload_preset": upload_preset}
        if public_id:
            params["public_id"] = public_id
        return params

    api_key = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")
    if not api_key or not api_secret:
        return None

    timestamp = int(time.time())
    sign_params = {"timestamp": timestamp}
    if public_id:
        sign_params["public_id"] = public_id

    base = "&".join(f"{k}={sign_params[k]}" for k in sorted(sign_params))
    signature = hashlib.sha1(f"{base}{api_secret}".encode("utf-8")).hexdigest()

    signed_payload = {
        "api_key": api_key,
        "timestamp": timestamp,
        "signature": signature,
    }
    if public_id:
        signed_payload["public_id"] = public_id
    return signed_payload


def _request_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }


def _normalize_imdb_media_type(value):
    normalized = str(value or "").strip().lower()
    if not normalized:
        return "movie"

    if normalized in {
        "tv",
        "tvseries",
        "tv series",
        "tvminiseries",
        "tv mini series",
        "tvmini-series",
        "tvepisode",
        "tv episode",
        "tvmovie",
        "tv movie",
        "series",
        "mini series",
        "miniseries",
        "show",
    }:
        return "tv"

    return "movie"


def _safe_text(value):
    if isinstance(value, str):
        return value.strip()
    return ""


def _extract_imdb_result(item):
    title = (
        _safe_text(item.get("primaryTitle"))
        or _safe_text(item.get("title"))
        or _safe_text((item.get("titleText") or {}).get("text"))
        or _safe_text((item.get("nameText") or {}).get("text"))
        or "Untitled"
    )

    year = item.get("startYear") or item.get("year")
    if isinstance(year, dict):
        year = year.get("year") or year.get("value")
    if year is not None:
        year = str(year)

    poster_url = (
        (item.get("primaryImage") or {}).get("url")
        or item.get("image")
        or item.get("poster")
        or item.get("posterUrl")
    )

    description = (
        _safe_text(item.get("description"))
        or _safe_text(item.get("plot"))
        or _safe_text(item.get("overview"))
        or _safe_text(item.get("shortDescription"))
        or _safe_text((item.get("plot") or {}).get("plotText"))
    )

    imdb_id = item.get("id") or item.get("titleId") or item.get("imdbId")
    source_url = item.get("url")
    if not source_url and imdb_id:
        source_url = f"{IMDB_BASE_URL}/title/{imdb_id}/"

    return {
        "tmdb_id": imdb_id,
        "title": title,
        "year": year,
        "poster_url": poster_url,
        "description": description,
        "media_type": _normalize_imdb_media_type(
            item.get("type") or item.get("titleType") or item.get("kind")
        ),
        "source_url": source_url,
        "source_provider": "imdbapi.dev",
    }


def search_imdb_titles(query, limit=15):
    if not query:
        return []

    response = requests.get(
        IMDB_SEARCH_API_URL,
        params={"query": query},
        headers=_request_headers(),
        timeout=20,
    )
    response.raise_for_status()

    payload = response.json() or {}
    items = payload.get("titles") or payload.get("results") or payload.get("data") or []
    if isinstance(items, dict):
        items = items.get("titles") or items.get("results") or items.get("data") or []

    results = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        results.append(_extract_imdb_result(item))

    return results


def scrape_poster_from_imdb(query):
    if not query:
        return None

    search_url = f"{IMDB_FIND_URL}?q={quote_plus(query)}&s=tt&ttype=ft"
    response = requests.get(search_url, headers=_request_headers(), timeout=20)
    response.raise_for_status()

    match = re.search(r'href="(/title/tt\d+[^\"]*)"', response.text, re.IGNORECASE)
    if not match:
        return None

    title_url = urljoin(IMDB_BASE_URL, html.unescape(match.group(1)))
    title_response = requests.get(title_url, headers=_request_headers(), timeout=20)
    title_response.raise_for_status()

    poster_match = re.search(
        r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"',
        title_response.text,
        re.IGNORECASE,
    )
    if not poster_match:
        poster_match = re.search(
            r'<meta[^>]+name="twitter:image"[^>]+content="([^"]+)"',
            title_response.text,
            re.IGNORECASE,
        )

    if not poster_match:
        return None

    title_match = re.search(
        r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
        title_response.text,
        re.IGNORECASE,
    )
    description_match = re.search(
        r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"',
        title_response.text,
        re.IGNORECASE,
    )
    year_match = re.search(r'\((\d{4})\)', title_match.group(1) if title_match else "")

    return {
        "poster_url": html.unescape(poster_match.group(1)),
        "title": html.unescape(title_match.group(1)).split(" - ")[0] if title_match else None,
        "description": html.unescape(description_match.group(1)) if description_match else None,
        "year": year_match.group(1) if year_match else None,
        "source_url": title_url,
    }


def upload_to_cloudinary(image_url, public_id=None):
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    auth_params = _build_cloudinary_auth_params(public_id=public_id)

    if not cloud_name or not auth_params or not image_url:
        return image_url

    upload_url = CLOUDINARY_UPLOAD_URL.format(cloud_name=cloud_name)
    payload = {"file": image_url, **auth_params}

    response = requests.post(upload_url, data=payload, timeout=40)
    response.raise_for_status()
    data = response.json()
    return data.get("secure_url") or image_url


def upload_bytes_to_cloudinary(image_bytes, filename="poster.jpg", public_id=None):
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    auth_params = _build_cloudinary_auth_params(public_id=public_id)

    if not cloud_name or not auth_params or not image_bytes:
        return None

    upload_url = CLOUDINARY_UPLOAD_URL.format(cloud_name=cloud_name)
    files = {
        "file": (filename, image_bytes, "image/jpeg"),
    }
    data = dict(auth_params)

    response = requests.post(upload_url, files=files, data=data, timeout=40)
    response.raise_for_status()
    payload = response.json()
    return payload.get("secure_url")


def resolve_poster_for_title(title, fallback_query=None):
    query = title or fallback_query
    scraped = scrape_poster_from_imdb(query)
    if not scraped:
        return None

    poster_url = scraped.get("poster_url")
    if not poster_url:
        return None

    uploaded_url = upload_to_cloudinary(poster_url, public_id=query)
    scraped["poster_url"] = uploaded_url or poster_url
    return scraped
