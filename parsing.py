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
    current_quality = None

    for line in lines:
        line = line.strip()

        q_match = re.search(r"(480p|720p|1080p|2k|4k)", line, re.IGNORECASE)
        if q_match:
            current_quality = q_match.group(1).lower()

        url_match = re.search(r"https?://[^\s]+", line)
        if url_match and current_quality:
            downloads[current_quality] = url_match.group(0)
            current_quality = None
            continue

        link_match = re.search(r"https://t\.me/[^\s]+", line)
        if link_match and current_quality:
            downloads[current_quality] = link_match.group(0)
            current_quality = None

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


def build_review_prompt(include_continue=True):
    keyboard = []

    if include_continue:
        keyboard.append([InlineKeyboardButton("Continue", callback_data="tmdb_continue")])

    keyboard.append(
        [
            InlineKeyboardButton("Search again", callback_data="tmdb_search_again"),
            InlineKeyboardButton("Manual", callback_data="tmdb_manual"),
        ]
    )
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_pending")])
    return InlineKeyboardMarkup(keyboard)
