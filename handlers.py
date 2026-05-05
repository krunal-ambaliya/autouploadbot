import asyncio
import threading
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import PENDING_KEY
from media_service import resolve_poster_for_title, upload_bytes_to_cloudinary
from parsing import build_review_prompt, parse_message
from storage import add_admin, is_admin
from tmdb_service import search_tmdb
from validation import (
    extract_downloads_from_message,
    get_missing_record_fields,
)
from workflow import finalize_pending_post


MANUAL_TIMEOUT_SECONDS = 300
MANUAL_TIMEOUTS = {}


def _manual_field_prompt(field_name):
    prompts = {
        "title": "Please send movie title only.\nExample: Interstellar",
        "description": "Please send movie description text only.",
        "links": (
            "Please send download links with quality.\n"
            "Example:\n720p https://example.com/file720\n480p https://example.com/file480"
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
    await msg.reply_text(
        _manual_value_summary(field_name, pending),
        reply_markup=_manual_confirmation_markup(field_name),
        disable_web_page_preview=True,
    )


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
            if not str(pending.get("stage") or "").startswith("awaiting_manual"):
                return

            asyncio.run(
                application.bot.send_message(
                    chat_id=chat_id,
                    text="No response for 5 minutes. Please resend the missing details.",
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


def build_preview_record(parsed, downloads, tmdb_details, query_text):
    record = dict(parsed)
    record.pop("image", None)

    record["movie"] = tmdb_details.get("title") or record.get("movie") or query_text
    record["description"] = tmdb_details.get("description") or record.get("description") or ""
    record["poster_url"] = tmdb_details.get("poster_url")
    record["year"] = tmdb_details.get("year") or record.get("year") or datetime.utcnow().year
    record["tmdb_id"] = tmdb_details.get("tmdb_id")
    record["tmdb_media_type"] = tmdb_details.get("media_type")
    record["downloads"] = downloads or record.get("downloads", {})
    record["source_query"] = query_text
    record["stage"] = "review"
    return record


def build_preview_text(record):
    description = record.get("description") or "No description available"
    lines = [
        "🔍 *Preview*",
        f"*Title:* {record.get('movie') or 'Untitled'}",
        f"*Year:* {record.get('year') or 'Unknown'}",
        f"*Description:* {description[:300]}...",
    ]

    downloads = record.get("downloads") or {}
    if downloads:
        lines.append("\n📥 *Downloads:*")
        for quality, link in downloads.items():
            lines.append(f"• {quality.upper()}: [Link]({link})")

    return "\n".join(lines)


async def send_preview_message(msg, record, include_continue=True):
    preview_text = build_preview_text(record)
    poster_url = record.get("poster_url")

    if poster_url:
        await msg.reply_photo(
            photo=poster_url,
            caption=preview_text,
            parse_mode="Markdown",
            reply_markup=build_review_prompt(include_continue=include_continue),
        )
    else:
        await msg.reply_text(
            preview_text,
            parse_mode="Markdown",
            reply_markup=build_review_prompt(include_continue=include_continue),
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


async def _apply_manual_title(msg, context, pending, text):
    title = text.strip()
    if not title:
        await msg.reply_text("Please send the title.")
        _schedule_manual_timeout(context.application, msg.chat_id)
        return

    pending["movie"] = title
    pending["source_query"] = title

    tmdb_details = await asyncio.to_thread(search_tmdb, title)
    if tmdb_details:
        if tmdb_details.get("title"):
            pending["movie"] = tmdb_details.get("title")
        if tmdb_details.get("description") and not pending.get("description"):
            pending["description"] = tmdb_details.get("description")
        if tmdb_details.get("poster_url"):
            pending["poster_url"] = tmdb_details.get("poster_url")
        if tmdb_details.get("year"):
            pending["year"] = tmdb_details.get("year")
        pending["tmdb_id"] = tmdb_details.get("tmdb_id")
        pending["tmdb_media_type"] = tmdb_details.get("media_type")
    else:
        fallback = await asyncio.to_thread(resolve_poster_for_title, title)
        if fallback:
            if fallback.get("title") and not pending.get("movie"):
                pending["movie"] = fallback.get("title")
            if fallback.get("description") and not pending.get("description"):
                pending["description"] = fallback.get("description")
            if fallback.get("poster_url"):
                pending["poster_url"] = fallback.get("poster_url")
            if fallback.get("year") and not pending.get("year"):
                pending["year"] = fallback.get("year")

    await _send_manual_confirmation(msg, pending, "title")
    await _prompt_next_manual_step(msg, context, pending)


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

    if not is_admin(user_id):
        await msg.reply_text("❌ You are not authorized to use this bot. Only admins can use it.")
        return

    pending = context.chat_data.get(PENDING_KEY)
    if pending:
        stage = pending.get("stage") or ""
        if stage.startswith("awaiting_manual") or stage == "review":
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
                reply_markup=build_review_prompt(include_continue=False),
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

        pending = dict(parsed)
        pending["downloads"] = extracted_downloads or parsed.get("downloads", {})
        pending["stage"] = "awaiting_manual_title"
        pending["last_query"] = query_text
        context.chat_data[PENDING_KEY] = pending
        _schedule_manual_timeout(context.application, msg.chat_id)
        await msg.reply_text(
            f"Could not find '{query_text}' on TMDB. Send the title manually.",
            reply_markup=build_review_prompt(include_continue=False),
        )
        return

    await msg.reply_text(
        "I couldn't parse that message. Would you like to search TMDB or enter details manually?",
        reply_markup=build_review_prompt(include_continue=False),
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
        record, insert_sql = await finalize_pending_post(pending, pending.get("description") or "")
        chat_id = query.message.chat_id if query.message else query.from_user.id
        _cancel_manual_timeout(chat_id)
        context.chat_data.pop(PENDING_KEY, None)
        print("\n====== NEON INSERT QUERY ======\n", insert_sql, "\n===============================\n")
        result_text = (
            "Prepared the Neon insert."
            f"\nPoster URL: {record.get('poster_url')}"
            f"\nNeon inserted: {record.get('neon_inserted')}"
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

    await query.answer()
    pending = dict(context.chat_data.get(PENDING_KEY) or {})
    pending["stage"] = "awaiting_search_query"
    context.chat_data[PENDING_KEY] = pending

    prompt_text = "Send another title to search TMDB."
    if query.message and query.message.caption is not None:
        await query.edit_message_caption(caption=prompt_text)
    else:
        await query.edit_message_text(text=prompt_text)


async def handle_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    if not is_admin(user_id):
        await query.answer("You are not authorized.", show_alert=True)
        return

    await query.answer()
    pending = dict(context.chat_data.get(PENDING_KEY) or {})
    missing_fields = get_missing_record_fields(pending)
    pending["stage"] = f"awaiting_manual_{missing_fields[0]}" if missing_fields else "review"
    context.chat_data[PENDING_KEY] = pending

    chat_id = query.message.chat_id if query.message else query.from_user.id
    if missing_fields:
        _schedule_manual_timeout(context.application, chat_id)
        prompt_text = _missing_fields_prompt(missing_fields)
    else:
        _cancel_manual_timeout(chat_id)
        prompt_text = "All fields are ready. Use Continue to finalize."

    if query.message and query.message.caption is not None:
        await query.edit_message_caption(caption=prompt_text)
    else:
        await query.edit_message_text(text=prompt_text)


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
