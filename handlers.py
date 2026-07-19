import asyncio
import logging
import threading
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TimedOut
from telegram.ext import ContextTypes

from config import PENDING_KEY
from media_service import resolve_poster_for_title, search_imdb_titles, upload_bytes_to_cloudinary
from parsing import (
    build_imdb_results_markup,
    build_imdb_results_text,
    build_review_prompt,
    parse_message,
)
from storage import add_admin, is_admin, remove_admin, get_admins
from tmdb_service import search_tmdb, search_tmdb_titles
from validation import (
    extract_downloads_from_message,
    get_missing_record_fields,
)
from workflow import finalize_pending_post
from sql_utils import normalize_media_type


MANUAL_TIMEOUT_SECONDS = 300
MANUAL_TIMEOUTS = {}
logger = logging.getLogger(__name__)


def _manual_field_prompt(field_name):
    prompts = {
        "title": (
            "Please send movie title only.\n"
            "Example: Interstellar\n"
            "I will search IMDb first and fall back to TMDb if needed."
        ),
        "description": "Please send movie description text only.",
        "links": (
            "Please send download links with quality.\n"
            "Example:\n480p: https://link1.com\n720p: https://link2.com\n1080p: https://link3.com"
        ),
        "poster": (
            "Please send poster as image URL or upload an image file.\n"
            "Example URL: https://.../poster.jpg"
        ),
    }
    return prompts.get(field_name, "Please send the missing value.")


def _manual_confirmation_markup(field_name):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Send Again", callback_data=f"manual_send_again:{field_name}"),
            InlineKeyboardButton("Cancel", callback_data="cancel_pending"),
        ]]
    )


async def _search_title_candidates(query_text):
    imdb_results = await asyncio.to_thread(search_imdb_titles, query_text)
    if imdb_results:
        return "IMDb", imdb_results

    tmdb_results = await asyncio.to_thread(search_tmdb_titles, query_text)
    if tmdb_results:
        return "TMDb", tmdb_results

    return None, []


async def _send_title_results(chat_id, application, context, query_text, source_name, results):
    pending = dict(context.chat_data.get(PENDING_KEY) or {})
    pending["imdb_search_results"] = results
    pending["imdb_search_query"] = query_text
    pending["imdb_search_page"] = 1
    pending["imdb_search_source"] = source_name or "IMDb"
    pending["stage"] = "awaiting_imdb_selection"
    pending["is_fully_manual"] = False
    context.chat_data[PENDING_KEY] = pending
    _schedule_manual_timeout(application, chat_id)

    prompt_text = build_imdb_results_text(query_text, results, page=1, source_name=source_name or "IMDb")
    reply_markup = build_imdb_results_markup(results, page=1)
    await application.bot.send_message(
        chat_id=chat_id,
        text=prompt_text,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


def _edit_cancel_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Cancel", callback_data="edit_cancel")]]
    )


def _edit_field_prompt(field_name, pending):
    movie = pending.get("movie") or pending.get("title") or "Untitled"
    current_title = f"Current: {movie}"
    prompts = {
        "movie": f"Send new title/name only.\n{current_title}",
        "type": f"Send new type: Movie or Series.\nCurrent: {pending.get('tmdb_media_type') or pending.get('type') or 'movie'}",
        "year": f"Send new year only.\nCurrent: {pending.get('year') or 'Set'}",
        "description": f"Send new description text only.\n{current_title}",
        "audio": f"Send new language text only.\nCurrent: {pending.get('audio') or pending.get('language') or 'Set'}",
        "poster_url": f"Send new poster URL or upload an image.\nCurrent: {pending.get('poster_url') or 'Set'}",
        "480p": f"Send new 480p download link only.\nCurrent: {(pending.get('downloads') or {}).get('480p') or 'Set'}",
        "720p": f"Send new 720p download link only.\nCurrent: {(pending.get('downloads') or {}).get('720p') or 'Set'}",
        "1080p": f"Send new 1080p download link only.\nCurrent: {(pending.get('downloads') or {}).get('1080p') or 'Set'}",
        "2k": f"Send new 2k download link only.\nCurrent: {(pending.get('downloads') or {}).get('2k') or 'Set'}",
        "links": (
            "Send all download links in quality:link format.\n"
            "Example:\n480p: https://link1.com\n720p: https://link2.com\n1080p: https://link3.com"
        ),
    }
    return prompts.get(field_name, "Send the replacement value."), _edit_cancel_markup()


def _manual_value_summary(field_name, pending):
    if field_name == "title":
        return f"✅ Received title:\n{pending.get('movie') or '-'}"
    if field_name == "description":
        description = (pending.get("description") or "-").strip()
        return f"✅ Received description:\n{description[:500]}"
    if field_name == "poster":
        return f"✅ Received poster URL:\n{pending.get('poster_url') or '-'}"
    if field_name == "links":
        downloads = pending.get("downloads") or {}
        if not downloads:
            return "✅ Received links:\n-"
        lines = ["✅ Received links:"]
        for quality, link in downloads.items():
            lines.append(f"{quality.upper()}: {link}")
        return "\n".join(lines)
    return "✅ Value received."


async def _send_manual_confirmation(msg, pending, field_name):
    await _reply_text_safe(
        msg,
        _manual_value_summary(field_name, pending),
        reply_markup=_manual_confirmation_markup(field_name),
        disable_web_page_preview=True,
    )


async def _reply_text_safe(msg, text, **kwargs):
    send_kwargs = {
        "connect_timeout": 10,
        "read_timeout": 30,
        "write_timeout": 30,
        "pool_timeout": 30,
    }
    send_kwargs.update(kwargs)

    for attempt in range(2):
        try:
            return await msg.reply_text(text, **send_kwargs)
        except TimedOut as exc:
            if attempt == 0:
                await asyncio.sleep(1)
                continue
            logger.warning("Timed out sending reply to chat %s: %s", getattr(msg, "chat_id", None), exc)
            return None


def _missing_fields_prompt(missing_fields):
    first_missing = missing_fields[0]
    if len(missing_fields) == 1:
        return _manual_field_prompt(first_missing)

    missing_text = ", ".join(missing_fields)
    return f"Missing: {missing_text}.\n{_manual_field_prompt(first_missing)}"


def _cancel_manual_timeout(chat_id):
    timer = MANUAL_TIMEOUTS.pop(chat_id, None)
    if timer:
        timer.cancel()


def _schedule_manual_timeout(application, chat_id):
    _cancel_manual_timeout(chat_id)

    def remind():
        try:
            chat_data = application.chat_data.get(chat_id) or {}
            pending = chat_data.get(PENDING_KEY)
            if not pending:
                return
            stage = str(pending.get("stage") or "")
            if not (stage.startswith("awaiting_manual") or stage.startswith("awaiting_edit_") or stage == "awaiting_imdb_selection"):
                return

            reminder_text = "No response for 5 minutes. Please resend the missing details."
            if stage == "awaiting_imdb_selection":
                reminder_text = "No response for 5 minutes. Please select a result or send another title."
            elif stage.startswith("awaiting_edit_"):
                reminder_text = "No response for 5 minutes. Please send the replacement value or cancel."

            asyncio.run(
                application.bot.send_message(
                    chat_id=chat_id,
                    text=reminder_text,
                )
            )
        finally:
            MANUAL_TIMEOUTS.pop(chat_id, None)

    timer = threading.Timer(MANUAL_TIMEOUT_SECONDS, remind)
    timer.daemon = True
    MANUAL_TIMEOUTS[chat_id] = timer
    timer.start()


def _set_pending(context, pending, manual=False):
    context.chat_data[PENDING_KEY] = pending
    chat_id = pending.get("chat_id")
    if chat_id is None and context.message is not None:
        chat_id = context.message.chat_id
    if chat_id is None and context.update and context.update.effective_chat:
        chat_id = context.update.effective_chat.id

    if chat_id is None:
        return

    if manual:
        _schedule_manual_timeout(context.application, chat_id)
    else:
        _cancel_manual_timeout(chat_id)


def _extract_image_url_from_message(msg):
    text = msg.text or msg.caption or ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("http://") or stripped.startswith("https://"):
            return stripped
    return None


def extract_query_from_message(text, parsed):
    if parsed.get("movie"):
        return parsed["movie"]

    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            if len(stripped) < 100:
                return stripped

    return (text or "").strip()[:100]


def detect_type_from_title(title):
    if not title:
        return "movie"
    # Patterns for series: S01, E01, Season, Complete, Episode
    series_patterns = [
        r"S\d{1,2}E\d{1,2}",
        r"S\d{1,2}",
        r"Season\s*\d+",
        r"Episode\s*\d+",
        r"Complete\s*Season",
    ]
    import re
    for pattern in series_patterns:
        if re.search(pattern, title, re.IGNORECASE):
            return "tv"
    return "movie"


def build_preview_record(parsed, downloads, tmdb_details, query_text):
    record = dict(parsed)
    record.pop("image", None)

    record["movie"] = tmdb_details.get("title") or record.get("movie") or query_text
    record["description"] = tmdb_details.get("description") or record.get("description") or ""
    record["poster_url"] = tmdb_details.get("poster_url")
    record["year"] = tmdb_details.get("year") or record.get("year") or datetime.utcnow().year
    record["tmdb_id"] = tmdb_details.get("tmdb_id")
    
    # Use TMDB media type if available, otherwise detect from title
    tmdb_type = tmdb_details.get("media_type")
    if not tmdb_type:
        tmdb_type = detect_type_from_title(record["movie"])
    
    record["tmdb_media_type"] = tmdb_type
    record["type"] = normalize_media_type(tmdb_type)
    record["downloads"] = downloads or record.get("downloads", {})
    record["source_query"] = query_text
    record["stage"] = "review"
    return record


def build_preview_text(record):
    description = record.get("description") or "No description available"
    media_type = record.get("tmdb_media_type") or record.get("type") or "movie"
    type_label = "Series" if media_type in {"tv", "series"} else "Movie"
    language = record.get("audio") or record.get("language") or "Unknown"
    source_provider = record.get("source_provider")
    imdb_rating = record.get("imdb_rating")
    imdb_votes = record.get("imdb_vote_count")
    
    lines = [
        "🔍 *Preview*",
        f"*Title:* {record.get('movie') or 'Untitled'}",
        f"*Type:* {type_label}",
        f"*Year:* {record.get('year') or 'Unknown'}",
        f"*Language:* {language}",
    ]

    if source_provider:
        lines.append(f"*Source:* {source_provider}")
    if imdb_rating is not None:
        rating_text = f"{imdb_rating}"
        if imdb_votes is not None:
            rating_text = f"{rating_text} ({imdb_votes} votes)"
        lines.append(f"*IMDb Rating:* {rating_text}")

    lines.extend([
        f"*Description:* {description[:300]}...",
    ])

    downloads = record.get("downloads") or {}
    if downloads:
        lines.append("\n📥 *Downloads:*")
        for quality, link in downloads.items():
            lines.append(f"• {quality.upper()}: [Link]({link})")

    return "\n".join(lines)


async def send_preview_message(msg, record, include_continue=True):
    preview_text = build_preview_text(record)
    poster_url = record.get("poster_url")
    current_type = record.get("tmdb_media_type", "movie")

    reply_markup = build_review_prompt(
        record=record,
        include_continue=include_continue, 
        current_type=current_type
    )

    if poster_url:
        await msg.reply_photo(
            photo=poster_url,
            caption=preview_text,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
    else:
        await msg.reply_text(
            preview_text,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )


async def _prompt_next_manual_step(msg, context, pending):
    missing = get_missing_record_fields(pending)
    pending["stage"] = f"awaiting_manual_{missing[0]}" if missing else "review"
    context.chat_data[PENDING_KEY] = pending

    if missing:
        _schedule_manual_timeout(context.application, msg.chat_id)
        await msg.reply_text(_missing_fields_prompt(missing))
        return False

    _cancel_manual_timeout(msg.chat_id)
    await send_preview_message(msg, pending)
    return True


async def _send_review_preview(msg, pending, context=None):
    if context is not None:
        context.chat_data[PENDING_KEY] = pending

    pending["stage"] = "review"
    if msg:
        await send_preview_message(msg, pending)


async def _prompt_edit_field(msg, context, pending, field_name):
    pending["stage"] = f"awaiting_edit_{field_name}"
    pending["edit_field"] = field_name
    context.chat_data[PENDING_KEY] = pending

    prompt_text, reply_markup = _edit_field_prompt(field_name, pending)
    await msg.reply_text(prompt_text, reply_markup=reply_markup, disable_web_page_preview=True)


def _normalize_edit_type(value):
    normalized = (value or "").strip().lower()
    if normalized in {"movie", "film"}:
        return "movie"
    if normalized in {"series", "tv", "show"}:
        return "tv"
    return None


async def _apply_edit_value(msg, context, pending, field_name, text):
    value = (text or "").strip()

    if field_name == "movie":
        if not value:
            await msg.reply_text("Please send a valid title.")
            return
        pending["movie"] = value
        pending["source_query"] = value

    elif field_name == "type":
        normalized_type = _normalize_edit_type(value)
        if not normalized_type:
            await msg.reply_text("Please send Movie or Series.")
            return
        pending["tmdb_media_type"] = normalized_type
        pending["type"] = normalize_media_type(normalized_type)

    elif field_name == "year":
        if not value.isdigit():
            await msg.reply_text("Please send a valid year like 2025.")
            return
        pending["year"] = int(value)

    elif field_name == "description":
        if not value:
            await msg.reply_text("Please send a valid description.")
            return
        pending["description"] = value

    elif field_name == "audio":
        if not value:
            await msg.reply_text("Please send a valid language.")
            return
        pending["audio"] = value

    elif field_name == "poster_url":
        poster_url = _extract_image_url_from_message(msg)
        if not poster_url and getattr(msg, "photo", None):
            try:
                best_photo = msg.photo[-1]
                tg_file = await context.application.bot.get_file(best_photo.file_id)
                image_bytes = await tg_file.download_as_bytearray()
                uploaded = await asyncio.to_thread(
                    upload_bytes_to_cloudinary,
                    bytes(image_bytes),
                    "poster.jpg",
                    (pending.get("movie") or "poster").strip().replace(" ", "-")[:80],
                )
                poster_url = uploaded
            except Exception:
                poster_url = None

        if not poster_url:
            await msg.reply_text("Please send a valid poster URL or upload an image.")
            return
        pending["poster_url"] = poster_url

    elif field_name in {"480p", "720p", "1080p", "2k"}:
        if not value:
            await msg.reply_text("Please send a valid download link.")
            return
        downloads = dict(pending.get("downloads") or {})
        downloads[field_name] = value
        pending["downloads"] = downloads

    elif field_name == "links":
        parsed = parse_message(text)
        downloads = extract_downloads_from_message(msg, parsed)
        if not downloads:
            await msg.reply_text("Please send at least one valid download link.")
            return
        pending_downloads = dict(pending.get("downloads") or {})
        pending_downloads.update(downloads)
        pending["downloads"] = pending_downloads

    else:
        await msg.reply_text("Unknown field.")
        return

    pending.pop("edit_field", None)
    await _send_review_preview(msg, pending, context)


async def _apply_manual_title(msg, context, pending, text):
    parsed = parse_message(text)
    title = extract_query_from_message(text, parsed).strip()
    if not title:
        await msg.reply_text("Please send the title.")
        _schedule_manual_timeout(context.application, msg.chat_id)
        return

    pending["movie"] = title
    pending["source_query"] = title

    search_source, search_results = await _search_title_candidates(title)
    if search_results:
        context.chat_data[PENDING_KEY] = pending
        await _send_title_results(msg.chat_id, context.application, context, title, search_source or "IMDb", search_results)
        return

    fallback = await asyncio.to_thread(resolve_poster_for_title, title)
    if fallback:
        if fallback.get("title"):
            pending["movie"] = fallback.get("title")
        if fallback.get("description") and not pending.get("description"):
            pending["description"] = fallback.get("description")
        if fallback.get("poster_url"):
            pending["poster_url"] = fallback.get("poster_url")
        if fallback.get("year") and not pending.get("year"):
            pending["year"] = fallback.get("year")

        pending["tmdb_media_type"] = detect_type_from_title(pending.get("movie") or title)
        pending["stage"] = "review"
        context.chat_data[PENDING_KEY] = pending
        await _send_manual_confirmation(msg, pending, "title")
        await _prompt_next_manual_step(msg, context, pending)
        return

    pending["stage"] = "awaiting_manual_title"
    context.chat_data[PENDING_KEY] = pending
    _schedule_manual_timeout(context.application, msg.chat_id)
    await _reply_text_safe(
        msg,
        f"No IMDb or TMDb results found for '{title}'. Please send another title."
    )


async def _apply_manual_description(msg, context, pending, text):
    description = text.strip()
    if not description:
        await msg.reply_text("Please send the description text.")
        _schedule_manual_timeout(context.application, msg.chat_id)
        return

    pending["description"] = description
    await _send_manual_confirmation(msg, pending, "description")
    await _prompt_next_manual_step(msg, context, pending)


async def _apply_manual_links(msg, context, pending, text):
    parsed = parse_message(text)
    downloads = extract_downloads_from_message(msg, parsed)
    if not downloads:
        await msg.reply_text("Please send at least one valid download link.")
        _schedule_manual_timeout(context.application, msg.chat_id)
        return

    pending["downloads"] = downloads
    await _send_manual_confirmation(msg, pending, "links")
    await _prompt_next_manual_step(msg, context, pending)


async def _apply_manual_poster(msg, context, pending, text):
    poster_url = _extract_image_url_from_message(msg)
    if not poster_url and getattr(msg, "photo", None):
        try:
            best_photo = msg.photo[-1]
            tg_file = await context.application.bot.get_file(best_photo.file_id)
            image_bytes = await tg_file.download_as_bytearray()
            uploaded = await asyncio.to_thread(
                upload_bytes_to_cloudinary,
                bytes(image_bytes),
                "poster.jpg",
                (pending.get("movie") or "manual-poster").strip().replace(" ", "-")[:80],
            )
            if uploaded:
                poster_url = uploaded
            else:
                await msg.reply_text(
                    "Image received, but Cloudinary upload failed or is not configured. Please send a poster URL instead."
                )
                _schedule_manual_timeout(context.application, msg.chat_id)
                return
        except Exception:
            await msg.reply_text("Could not process that image. Please send poster URL or upload image again.")
            _schedule_manual_timeout(context.application, msg.chat_id)
            return

    if not poster_url:
        await msg.reply_text("Please send a valid poster image URL or upload an image file.")
        _schedule_manual_timeout(context.application, msg.chat_id)
        return

    pending["poster_url"] = poster_url
    await _send_manual_confirmation(msg, pending, "poster")
    await _prompt_next_manual_step(msg, context, pending)


async def _handle_manual_stage(update: Update, context: ContextTypes.DEFAULT_TYPE, pending):
    msg = update.message
    if not msg:
        return

    text = msg.text or msg.caption or ""
    stage = pending.get("stage") or ""

    if stage == "awaiting_manual_title":
        await _apply_manual_title(msg, context, pending, text)
        return

    if stage == "awaiting_manual_description":
        await _apply_manual_description(msg, context, pending, text)
        return

    if stage == "awaiting_manual_links":
        await _apply_manual_links(msg, context, pending, text)
        return

    if stage == "awaiting_manual_poster":
        await _apply_manual_poster(msg, context, pending, text)
        return

    if stage.startswith("awaiting_edit_"):
        field_name = stage.replace("awaiting_edit_", "", 1)
        await _apply_edit_value(msg, context, pending, field_name, text)
        return

    if stage == "awaiting_imdb_selection":
        await msg.reply_text("Please choose one of the search result buttons above, or use Search again.")
        return

    if stage == "review":
        await msg.reply_text("Please use the buttons above to Continue or Search again.")
        return

    await msg.reply_text("Please send the missing details.")
    _schedule_manual_timeout(context.application, msg.chat_id)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    user_id = msg.from_user.id
    text = msg.text or msg.caption or ""

    if text.startswith("/manual"):
        if not is_admin(user_id):
            await msg.reply_text("❌ You are not authorized.")
            return
        
        pending = {
            "chat_id": msg.chat_id,
            "stage": "awaiting_manual_title",
            "is_fully_manual": True,
            "downloads": {},
        }
        _set_pending(context, pending, manual=True)
        await msg.reply_text(
            "Manual upload started.\n"
            "Please send movie title only.\n"
            "Example: Interstellar\n"
            "I will search IMDb first, and you can use TMDb Search if you want TMDb results."
        )
        return

    if text.startswith("/start") or text.startswith("/help"):
        help_text = (
            "👋 **Welcome to the AutoUpload Bot!**\n\n"
            "I can help you search IMDb or TMDb for movie/series metadata and prepare posts for your channel.\n\n"
            "📜 **Available Commands:**\n"
            "• /manual - Start a fully manual upload flow.\n"
            "• /cancel - Stop the current process and start fresh.\n"
            "• /addadmin {userid} - Add a new admin (Admins only).\n"
            "• /removeadmin {userid} - Remove an admin (Master only).\n"
            "• /listadmins - List all current admins.\n"
            "• /help - Show this help message.\n\n"
            "💡 **How to use:**\n"
            "1. Send a movie title to search IMDb first.\n"
            "2. Use the **TMDb Search** button to search TMDb separately.\n"
            "3. Or forward a post with download links.\n"
            "4. Use the buttons to toggle between Movie/Series or enter details manually."
        )
        await msg.reply_text(help_text, parse_mode="Markdown")
        return

    if text.startswith("/cancel"):
        if not is_admin(user_id):
            await msg.reply_text("❌ You are not authorized.")
            return
        
        _cancel_manual_timeout(msg.chat_id)
        context.chat_data.pop(PENDING_KEY, None)
        await msg.reply_text("Current process stopped. You can upload manually by using /manual")
        return


    if text.startswith("/addadmin"):
        if not is_admin(user_id):
            await msg.reply_text("❌ You are not authorized to use this command.")
            return

        parts = text.split()
        if len(parts) < 2:
            await msg.reply_text("Usage: /addadmin {userid}")
            return

        new_admin_id = parts[1].strip()
        if add_admin(new_admin_id):
            await msg.reply_text(f"✅ Admin added: {new_admin_id}")
        else:
            await msg.reply_text(f"ℹ️ {new_admin_id} is already an admin.")
        return

    if text.startswith("/removeadmin"):
        if not is_admin(user_id):
            await msg.reply_text("❌ You are not authorized.")
            return

        parts = text.split()
        if len(parts) < 2:
            await msg.reply_text("Usage: /removeadmin {userid}")
            return

        target_id = parts[1].strip()
        success, message = remove_admin(target_id)
        if success:
            await msg.reply_text(f"✅ {message}")
        else:
            await msg.reply_text(f"❌ {message}")
        return

    if text.startswith("/listadmins"):
        if not is_admin(user_id):
            await msg.reply_text("❌ You are not authorized.")
            return

        admins = get_admins()
        from config import DEFAULT_ADMIN_ID
        admin_list = []
        for a in admins:
            if str(a) == str(DEFAULT_ADMIN_ID):
                admin_list.append(f"• {a} (Master)")
            else:
                admin_list.append(f"• {a}")
        
        await msg.reply_text("👥 **Current Admins:**\n\n" + "\n".join(admin_list), parse_mode="Markdown")
        return

    if not is_admin(user_id):
        await msg.reply_text("❌ You are not authorized to use this bot. Only admins can use it.")
        return

    pending = context.chat_data.get(PENDING_KEY)
    if pending:
        stage = pending.get("stage") or ""
        if stage.startswith("awaiting_manual") or stage.startswith("awaiting_edit_") or stage in {"review", "awaiting_imdb_selection"}:
            await _handle_manual_stage(update, context, pending)
            return

        if stage == "awaiting_search_query":
            query_text = text.strip()
            if not query_text:
                await msg.reply_text("Please send a movie title to search TMDB.")
                return

            tmdb_details = await asyncio.to_thread(search_tmdb, query_text)
            if tmdb_details:
                record = build_preview_record(pending, pending.get("downloads", {}), tmdb_details, query_text)
                if not record.get("poster_url"):
                    fallback = await asyncio.to_thread(resolve_poster_for_title, record.get("movie") or query_text)
                    if fallback and fallback.get("poster_url"):
                        record["poster_url"] = fallback.get("poster_url")
                        if fallback.get("description") and not record.get("description"):
                            record["description"] = fallback.get("description")
                context.chat_data[PENDING_KEY] = record
                await send_preview_message(msg, record)
                return

            fallback = await asyncio.to_thread(resolve_poster_for_title, query_text)
            if fallback and fallback.get("poster_url"):
                record = dict(pending)
                record.pop("image", None)
                record["movie"] = fallback.get("title") or query_text
                record["description"] = fallback.get("description") or record.get("description") or ""
                record["poster_url"] = fallback.get("poster_url")
                record["year"] = fallback.get("year") or record.get("year") or datetime.utcnow().year
                record["downloads"] = pending.get("downloads", {})
                record["source_query"] = query_text
                record["stage"] = "review"
                context.chat_data[PENDING_KEY] = record
                await send_preview_message(msg, record)
                return

            pending = dict(pending)
            pending["stage"] = "awaiting_manual_title"
            pending["last_query"] = query_text
            context.chat_data[PENDING_KEY] = pending
            _schedule_manual_timeout(context.application, msg.chat_id)
            await msg.reply_text(
                f"Could not find '{query_text}' on TMDB. Send the title manually.",
                reply_markup=build_review_prompt(include_continue=False, include_search_again=False, include_type_toggle=False),
            )
            return

        await msg.reply_text("Please use the buttons above to Continue, Search again, or Manual.")
        return

    parsed = parse_message(text)
    extracted_downloads = extract_downloads_from_message(msg, parsed)
    query_text = extract_query_from_message(text, parsed)

    if query_text:
        tmdb_details = await asyncio.to_thread(search_tmdb, query_text)
        if tmdb_details:
            record = build_preview_record(parsed, extracted_downloads or parsed.get("downloads", {}), tmdb_details, query_text)
            if not record.get("poster_url"):
                fallback = await asyncio.to_thread(resolve_poster_for_title, record.get("movie") or query_text)
                if fallback and fallback.get("poster_url"):
                    record["poster_url"] = fallback.get("poster_url")
                    if fallback.get("description") and not record.get("description"):
                        record["description"] = fallback.get("description")
            context.chat_data[PENDING_KEY] = record
            await send_preview_message(msg, record)
            return

        fallback = await asyncio.to_thread(resolve_poster_for_title, query_text)
        if fallback and fallback.get("poster_url"):
            record = dict(parsed)
            record.pop("image", None)
            record["movie"] = fallback.get("title") or parsed.get("movie") or query_text
            record["description"] = fallback.get("description") or record.get("description") or ""
            record["poster_url"] = fallback.get("poster_url")
            record["year"] = fallback.get("year") or record.get("year") or datetime.utcnow().year
            record["downloads"] = extracted_downloads or parsed.get("downloads", {})
            record["source_query"] = query_text
            record["stage"] = "review"
            context.chat_data[PENDING_KEY] = record
            await send_preview_message(msg, record)
            return

        pending = {
            "chat_id": msg.chat_id,
            "downloads": {},
            "stage": "awaiting_manual_title",
            "is_fully_manual": True,
            "last_query": query_text
        }
        context.chat_data[PENDING_KEY] = pending
        _schedule_manual_timeout(context.application, msg.chat_id)
        await msg.reply_text(
            f"Could not find '{query_text}' on TMDB. Send the title manually.",
            reply_markup=build_review_prompt(include_continue=False, include_search_again=False, include_type_toggle=False),
        )
        return

    await msg.reply_text(
        "I couldn't parse that message. Would you like to search TMDB or enter details manually?",
        reply_markup=build_review_prompt(include_continue=False, include_search_again=True),
    )


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("You are not authorized.", show_alert=True)
        return

    await query.answer()
    chat_id = query.message.chat_id if query.message else query.from_user.id
    _cancel_manual_timeout(chat_id)
    context.chat_data.pop(PENDING_KEY, None)
    if query.message and query.message.caption is not None:
        await query.edit_message_caption(caption="Canceled.")
    else:
        await query.edit_message_text(text="Canceled.")


async def handle_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("You are not authorized.", show_alert=True)
        return

    await query.answer()
    pending = context.chat_data.get(PENDING_KEY)

    if not pending:
        if query.message and query.message.caption is not None:
            await query.edit_message_caption(caption="Nothing pending.")
        else:
            await query.edit_message_text(text="Nothing pending.")
        return

    missing_fields = get_missing_record_fields(pending)
    if missing_fields:
        pending["stage"] = f"awaiting_manual_{missing_fields[0]}"
        context.chat_data[PENDING_KEY] = pending
        chat_id = query.message.chat_id if query.message else query.from_user.id
        _schedule_manual_timeout(context.application, chat_id)
        message_text = _missing_fields_prompt(missing_fields)
        if query.message and query.message.caption is not None:
            await query.edit_message_caption(caption=message_text)
        else:
            await query.edit_message_text(text=message_text)
        return

    try:
        status_text = "Checking..."
        if query.message and query.message.caption is not None:
            await query.edit_message_caption(caption=status_text)
        else:
            await query.edit_message_text(text=status_text)
        await asyncio.sleep(0.6)

        status_text += "\nConnecting DB..."
        await query.edit_message_caption(caption=status_text) if query.message.caption is not None else await query.edit_message_text(text=status_text)
        await asyncio.sleep(0.6)

        status_text += "\nQuery..."
        await query.edit_message_caption(caption=status_text) if query.message.caption is not None else await query.edit_message_text(text=status_text)
        
        record, insert_sql = await finalize_pending_post(pending, pending.get("description") or "")
        
        status_text += "\nUpdating..."
        await query.edit_message_caption(caption=status_text) if query.message.caption is not None else await query.edit_message_text(text=status_text)
        await asyncio.sleep(0.6)

        chat_id = query.message.chat_id if query.message else query.from_user.id
        _cancel_manual_timeout(chat_id)
        context.chat_data.pop(PENDING_KEY, None)
        
        print("\n====== NEON INSERT QUERY ======\n", insert_sql, "\n===============================\n")
        
        if not record.get('neon_inserted'):
            result_text = "❌ Database upload FAILED! Check logs for error details."
        else:
            result_text = (
                "✅ Uploaded Successfully!"
                f"\n\nPoster: {record.get('poster_url')}"
                f"\nDatabase ID: {record.get('neon_inserted')}"
            )
        
        if query.message and query.message.caption is not None:
            await query.edit_message_caption(caption=result_text)
        else:
            await query.edit_message_text(text=result_text)
    except Exception as exc:
        if query.message and query.message.caption is not None:
            await query.edit_message_caption(caption=f"Failed to finalize post: {exc}")
        else:
            await query.edit_message_text(text=f"Failed to finalize post: {exc}")


async def handle_search_again(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("You are not authorized.", show_alert=True)
        return

    pending = dict(context.chat_data.get(PENDING_KEY) or {})
    query_text = (
        pending.get("movie")
        or pending.get("source_query")
        or pending.get("imdb_search_query")
        or pending.get("last_query")
        or ""
    ).strip()

    if not query_text:
        await query.answer("No title available to search.", show_alert=True)
        return

    search_source, results = await _search_title_candidates(query_text)
    if results:
        await _send_title_results(
            query.message.chat_id if query.message else query.from_user.id,
            context.application,
            context,
            query_text,
            search_source or "IMDb",
            results,
        )

        await query.answer()
        return

    pending["stage"] = "awaiting_manual_title"
    pending["is_fully_manual"] = False
    context.chat_data[PENDING_KEY] = pending
    _schedule_manual_timeout(context.application, query.message.chat_id if query.message else query.from_user.id)

    prompt_text = f"No IMDb or TMDb results found for '{query_text}'. Please send another title."
    await context.application.bot.send_message(
        chat_id=query.message.chat_id if query.message else query.from_user.id,
        text=prompt_text,
    )

    await query.answer()


async def handle_tmdb_search_results(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("You are not authorized.", show_alert=True)
        return

    pending = dict(context.chat_data.get(PENDING_KEY) or {})
    query_text = (
        pending.get("imdb_search_query")
        or pending.get("movie")
        or pending.get("source_query")
        or pending.get("last_query")
        or ""
    ).strip()

    if not query_text:
        await query.answer("No title available to search.", show_alert=True)
        return

    results = await asyncio.to_thread(search_tmdb_titles, query_text)
    if not results:
        await query.answer("No TMDb results found.", show_alert=True)
        return

    chat_id = query.message.chat_id if query.message else query.from_user.id
    pending["imdb_search_results"] = results
    pending["imdb_search_query"] = query_text
    pending["imdb_search_page"] = 1
    pending["imdb_search_source"] = "TMDb"
    pending["stage"] = "awaiting_imdb_selection"
    pending["is_fully_manual"] = False
    context.chat_data[PENDING_KEY] = pending
    _schedule_manual_timeout(context.application, chat_id)

    prompt_text = build_imdb_results_text(query_text, results, page=1, source_name="TMDb")
    reply_markup = build_imdb_results_markup(results, page=1)
    await context.application.bot.send_message(
        chat_id=chat_id,
        text=prompt_text,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )

    await query.answer()


async def handle_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("You are not authorized.", show_alert=True)
        return

    await query.answer()
    # Start completely fresh, clear everything including downloads
    pending = {
        "chat_id": query.message.chat_id if query.message else query.from_user.id,
        "downloads": {},
        "is_fully_manual": True,
        "stage": "awaiting_manual_title",
    }
    context.chat_data[PENDING_KEY] = pending

    chat_id = pending["chat_id"]
    _schedule_manual_timeout(context.application, chat_id)
    prompt_text = "Manual upload started.\n" + _manual_field_prompt("title")

    # Delete the old message to remove the incorrect poster/preview
    if query.message:
        try:
            await query.message.delete()
        except Exception:
            pass

    await context.application.bot.send_message(
        chat_id=chat_id,
        text=prompt_text
    )


async def handle_toggle_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("You are not authorized.", show_alert=True)
        return

    await query.answer("Type toggled!")
    pending = dict(context.chat_data.get(PENDING_KEY) or {})
    if not pending:
        return

    current_type = pending.get("tmdb_media_type", "movie")
    new_type = "tv" if current_type == "movie" else "movie"
    pending["tmdb_media_type"] = new_type
    pending["type"] = normalize_media_type(new_type)
    context.chat_data[PENDING_KEY] = pending

    preview_text = build_preview_text(pending)
    reply_markup = build_review_prompt(record=pending, include_continue=True, current_type=new_type)

    if query.message and query.message.caption is not None:
        await query.edit_message_caption(
            caption=preview_text,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
    elif query.message:
        await query.edit_message_text(
            text=preview_text,
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )


async def handle_imdb_results_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("You are not authorized.", show_alert=True)
        return

    pending = dict(context.chat_data.get(PENDING_KEY) or {})
    results = pending.get("imdb_search_results") or []
    if not results:
        await query.answer("No search results available.", show_alert=True)
        return

    callback_data = query.data or ""
    _, _, page_text = callback_data.partition(":")
    try:
        page = int(page_text)
    except ValueError:
        await query.answer("Invalid page.", show_alert=True)
        return

    pending["imdb_search_page"] = page
    pending["stage"] = "awaiting_imdb_selection"
    context.chat_data[PENDING_KEY] = pending
    _schedule_manual_timeout(context.application, query.message.chat_id if query.message else query.from_user.id)

    query_text = pending.get("imdb_search_query") or pending.get("movie") or "title"
    prompt_text = build_imdb_results_text(
        query_text,
        results,
        page=page,
        source_name=pending.get("imdb_search_source") or "IMDb",
    )
    reply_markup = build_imdb_results_markup(results, page=page)

    if query.message and query.message.caption is not None:
        await query.edit_message_caption(caption=prompt_text, reply_markup=reply_markup)
    elif query.message:
        await query.edit_message_text(text=prompt_text, reply_markup=reply_markup)

    await query.answer()


async def handle_imdb_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("You are not authorized.", show_alert=True)
        return

    pending = dict(context.chat_data.get(PENDING_KEY) or {})
    results = pending.get("imdb_search_results") or []
    callback_data = query.data or ""
    _, _, index_text = callback_data.partition(":")
    try:
        index = int(index_text)
    except ValueError:
        await query.answer("Invalid selection.", show_alert=True)
        return

    if index < 0 or index >= len(results):
        await query.answer("Selection out of range.", show_alert=True)
        return

    selected = results[index]
    selected_title = selected.get("title") or pending.get("movie") or "Untitled"
    pending["movie"] = selected_title
    pending["source_query"] = pending.get("imdb_search_query") or pending.get("source_query") or selected_title
    if selected.get("description"):
        pending["description"] = selected.get("description")
    if selected.get("poster_url"):
        pending["poster_url"] = selected.get("poster_url")
    if selected.get("year"):
        pending["year"] = selected.get("year")
    if selected.get("end_year") and not pending.get("year"):
        pending["year"] = selected.get("end_year")

    pending["tmdb_id"] = selected.get("tmdb_id")
    pending["imdb_id"] = selected.get("imdb_id") or selected.get("tmdb_id")
    pending["tmdb_media_type"] = selected.get("media_type") or "movie"
    pending["type"] = normalize_media_type(pending["tmdb_media_type"])
    if selected.get("rating") is not None:
        pending["imdb_rating"] = selected.get("rating")
    if selected.get("vote_count") is not None:
        pending["imdb_vote_count"] = selected.get("vote_count")
    if selected.get("original_title"):
        pending["imdb_original_title"] = selected.get("original_title")
    if selected.get("source_url"):
        pending["source_url"] = selected.get("source_url")
    pending["source_provider"] = selected.get("source_provider") or "IMDb"
    pending["stage"] = "review"
    pending["imdb_selected_index"] = index
    pending["imdb_selected_title"] = selected_title
    context.chat_data[PENDING_KEY] = pending
    _schedule_manual_timeout(context.application, query.message.chat_id if query.message else query.from_user.id)
    await query.answer(f"Selected {selected_title}")

    if query.message:
        try:
            await query.edit_message_text(text=f"Selected: {selected_title}\nLoading preview...")
        except Exception:
            pass

    if not pending.get("poster_url"):
        fallback = await asyncio.to_thread(resolve_poster_for_title, selected_title)
        if fallback:
            if fallback.get("poster_url"):
                pending["poster_url"] = fallback.get("poster_url")
            if fallback.get("description") and not pending.get("description"):
                pending["description"] = fallback.get("description")
            if fallback.get("year") and not pending.get("year"):
                pending["year"] = fallback.get("year")
            context.chat_data[PENDING_KEY] = pending

    if query.message:
        await send_preview_message(query.message, pending)
    else:
        await context.application.bot.send_message(
            chat_id=query.from_user.id,
            text=build_preview_text(pending),
        )


async def handle_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("You are not authorized.", show_alert=True)
        return

    callback_data = query.data or ""
    _, _, field_name = callback_data.partition(":")
    allowed_fields = {"movie", "type", "year", "description", "audio", "poster_url", "480p", "720p", "1080p", "2k", "links"}
    if field_name not in allowed_fields:
        await query.answer("Unknown field.", show_alert=True)
        return

    pending = dict(context.chat_data.get(PENDING_KEY) or {})
    if not pending:
        await query.answer("Nothing pending.", show_alert=True)
        return

    await query.answer()

    chat_id = query.message.chat_id if query.message else query.from_user.id
    pending["stage"] = f"awaiting_edit_{field_name}"
    pending["edit_field"] = field_name
    context.chat_data[PENDING_KEY] = pending
    _schedule_manual_timeout(context.application, chat_id)

    prompt_text, reply_markup = _edit_field_prompt(field_name, pending)
    if query.message and query.message.caption is not None:
        await query.edit_message_caption(caption=prompt_text, reply_markup=reply_markup)
    elif query.message:
        await query.edit_message_text(text=prompt_text, reply_markup=reply_markup)
    else:
        await context.application.bot.send_message(chat_id=chat_id, text=prompt_text, reply_markup=reply_markup)


async def handle_edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("You are not authorized.", show_alert=True)
        return

    pending = dict(context.chat_data.get(PENDING_KEY) or {})
    if not pending:
        await query.answer("Nothing pending.", show_alert=True)
        return

    await query.answer()

    pending["stage"] = "review"
    pending.pop("edit_field", None)
    context.chat_data[PENDING_KEY] = pending

    if query.message and query.message.caption is not None:
        await query.edit_message_caption(
            caption=build_preview_text(pending),
            reply_markup=build_review_prompt(record=pending, current_type=pending.get("tmdb_media_type", "movie")),
        )
    elif query.message:
        await query.edit_message_text(
            text=build_preview_text(pending),
            reply_markup=build_review_prompt(record=pending, current_type=pending.get("tmdb_media_type", "movie")),
        )


async def handle_manual_send_again(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("You are not authorized.", show_alert=True)
        return

    await query.answer()
    pending = dict(context.chat_data.get(PENDING_KEY) or {})

    callback_data = query.data or ""
    _, _, field_name = callback_data.partition(":")
    if field_name not in {"title", "description", "links", "poster"}:
        await query.edit_message_text(text="Unknown field. Please try Manual again.")
        return

    pending["stage"] = f"awaiting_manual_{field_name}"
    context.chat_data[PENDING_KEY] = pending

    chat_id = query.message.chat_id if query.message else query.from_user.id
    _schedule_manual_timeout(context.application, chat_id)

    prompt_text = _manual_field_prompt(field_name)
    if query.message and query.message.caption is not None:
        await query.edit_message_caption(caption=prompt_text)
    else:
        await query.edit_message_text(text=prompt_text)
