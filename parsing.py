import math
import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def parse_message(text):
    data = {}

    print("\n====== RAW MESSAGE ======\n", text, "\n========================\n")

    movie_match = re.search(r"movie\s*:\s*(.+)", text, re.IGNORECASE)
    if movie_match:
        data["movie"] = movie_match.group(1).strip()

    audio_match = re.search(r"Audio\s*:\s*(.+)", text, re.IGNORECASE)
    if audio_match:
        data["audio"] = audio_match.group(1).strip()

    quality_match = re.search(r"Quality\s*:\s*(.+)", text, re.IGNORECASE)
    if quality_match:
        data["quality"] = quality_match.group(1).strip()

    downloads = {}

    lines = text.splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Check for "quality: link" format
        q_link_match = re.search(r"(480p|720p|1080p|2k|4k)\s*[:\s-]*\s*(https?://[^\s]+)", line, re.IGNORECASE)
        if q_link_match:
            quality = q_link_match.group(1).lower()
            url = q_link_match.group(2)
            downloads[quality] = url
            continue

        # Fallback to multi-line detection
        q_match = re.search(r"(480p|720p|1080p|2k|4k)", line, re.IGNORECASE)
        url_match = re.search(r"https?://[^\s]+", line)

        if q_match and url_match:
            downloads[q_match.group(1).lower()] = url_match.group(0)
        elif q_match:
            # Maybe the URL is on the next line? (Old logic handled this poorly, let's stick to single line or explicit pairs)
            pass

    if downloads:
        data["downloads"] = downloads

    return data


def extract_download_links(msg):
    downloads = {}

    def collect_entities(text, entities):
        if not text or not entities:
            return

        for entity in entities:
            start = entity.offset
            end = entity.offset + entity.length
            visible_text = text[start:end]
            quality_match = re.search(r"(480p|720p|1080p|2k|4k)", visible_text, re.IGNORECASE)
            if not quality_match:
                continue

            quality = quality_match.group(1).lower()

            if entity.type == "text_link" and getattr(entity, "url", None):
                downloads[quality] = entity.url
            elif entity.type == "url":
                downloads[quality] = visible_text

    collect_entities(msg.text or "", getattr(msg, "entities", None))
    collect_entities(msg.caption or "", getattr(msg, "caption_entities", None))

    reply_markup = getattr(msg, "reply_markup", None)
    inline_keyboard = getattr(reply_markup, "inline_keyboard", None) or []
    for row in inline_keyboard:
        for button in row:
            url = getattr(button, "url", None)
            text = getattr(button, "text", "")
            quality_match = re.search(r"(480p|720p|1080p|2k|4k)", text, re.IGNORECASE)
            if not url or not quality_match:
                continue

            quality = quality_match.group(1).lower()
            if url:
                downloads[quality] = url

    return downloads


def build_description_prompt():
    keyboard = [[InlineKeyboardButton("Cancel", callback_data="cancel_pending")]]
    return InlineKeyboardMarkup(keyboard)


def _short_button_value(value, limit=26):
    text = str(value or "").strip()
    if not text:
        return "Set"
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _field_button_label(label, value, limit=26):
    return f"{label}: {_short_button_value(value, limit)}"


def _get_download_value(record, quality):
    downloads = record.get("downloads") or {}
    return downloads.get(quality) or "Set"


def build_review_prompt(
    record=None,
    include_continue=True,
    current_type="movie",
    include_search_again=True,
    include_type_toggle=True,
):
    keyboard = []

    if record:
        movie_value = record.get("movie") or record.get("title")
        type_value = "Series" if (record.get("tmdb_media_type") or record.get("type")) in {"tv", "series"} else "Movie"
        year_value = record.get("year") or "Set"
        language_value = record.get("audio") or record.get("language") or "Set"
        description_value = record.get("description") or "Set"
        poster_value = record.get("poster_url") or "Set"

        keyboard.append([InlineKeyboardButton(_field_button_label("Name", movie_value), callback_data="edit_field:movie")])
        if include_type_toggle:
            type_label = "Series" if current_type == "tv" else "Movie"
            keyboard.append(
                [
                    InlineKeyboardButton(f"Type: {type_label} (Toggle)", callback_data="tmdb_toggle_type"),
                ]
            )
        keyboard.append([InlineKeyboardButton(_field_button_label("Year", year_value), callback_data="edit_field:year"),])
        keyboard.append([InlineKeyboardButton(_field_button_label("Language", language_value), callback_data="edit_field:audio")])
        keyboard.append([InlineKeyboardButton(_field_button_label("Description", description_value, limit=20), callback_data="edit_field:description")])
        keyboard.append([
            InlineKeyboardButton(_field_button_label("480p", _get_download_value(record, "480p"), limit=18), callback_data="edit_field:480p"),
            InlineKeyboardButton(_field_button_label("720p", _get_download_value(record, "720p"), limit=18), callback_data="edit_field:720p"),
        ])
        keyboard.append([
            InlineKeyboardButton(_field_button_label("1080p", _get_download_value(record, "1080p"), limit=18), callback_data="edit_field:1080p"),
            InlineKeyboardButton(_field_button_label("2k", _get_download_value(record, "2k"), limit=18), callback_data="edit_field:2k"),
        ])
        keyboard.append([InlineKeyboardButton("Links: Edit", callback_data="edit_field:links")])
        keyboard.append([InlineKeyboardButton(_field_button_label("Poster", poster_value, limit=20), callback_data="edit_field:poster_url")])

    
    if include_type_toggle:
        type_label = "Series" if current_type == "tv" else "Movie"
        keyboard.append(
            [
                InlineKeyboardButton(f"Type: {type_label} (Toggle)", callback_data="tmdb_toggle_type"),
            ]
        )

    middle_row = []
    if include_search_again:
        middle_row.append(InlineKeyboardButton("Search again", callback_data="tmdb_search_again"))

    middle_row.append(InlineKeyboardButton("Manual", callback_data="tmdb_manual"))
    keyboard.append(middle_row)
    
    if include_continue:
            keyboard.append([InlineKeyboardButton("Continue", callback_data="tmdb_continue")])

    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_pending")])
    return InlineKeyboardMarkup(keyboard)


def _short_title(text, limit=42):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def _imdb_result_label(item):
    title = item.get("title") or "Untitled"
    year = item.get("year") or "?"
    media_type = item.get("media_type") or "movie"
    prefix = "TV" if media_type == "tv" else "MOV"
    return _short_title(f"{prefix} {title} ({year})")


def build_imdb_results_text(query, results, page=1, page_size=5):
    total = len(results or [])
    page_count = max(1, math.ceil(total / page_size))
    page = max(1, min(page, page_count))
    start = (page - 1) * page_size
    end = start + page_size
    visible = results[start:end]

    lines = [
        f"Search results for '{query}'",
        f"Top {total} result(s) found",
    ]

    if page_count > 1:
        lines.append(f"Page {page}/{page_count}")

    if visible:
        lines.append("")
        for offset, item in enumerate(visible, start=start + 1):
            title = item.get("title") or "Untitled"
            year = item.get("year") or "?"
            media_type = "TV" if (item.get("media_type") or "movie") == "tv" else "Movie"
            lines.append(f"{offset}. {title} ({year}) [{media_type}]")

    return "\n".join(lines)


def build_imdb_results_markup(results, page=1, page_size=5):
    total = len(results or [])
    page_count = max(1, math.ceil(total / page_size))
    page = max(1, min(page, page_count))
    start = (page - 1) * page_size
    end = start + page_size
    visible = results[start:end]

    keyboard = []
    for offset, item in enumerate(visible, start=start):
        keyboard.append(
            [
                InlineKeyboardButton(
                    _imdb_result_label(item),
                    callback_data=f"imdb_select:{offset}",
                )
            ]
        )

    nav_row = []
    if page > 1:
        nav_row.append(
            InlineKeyboardButton("Prev", callback_data=f"imdb_results_page:{page - 1}")
        )
    if page < page_count:
        nav_row.append(
            InlineKeyboardButton("Next", callback_data=f"imdb_results_page:{page + 1}")
        )
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append(
        [
            InlineKeyboardButton("Search again", callback_data="tmdb_search_again"),
            InlineKeyboardButton("Cancel", callback_data="cancel_pending"),
        ]
    )

    return InlineKeyboardMarkup(keyboard)
