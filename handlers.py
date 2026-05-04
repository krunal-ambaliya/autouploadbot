import asyncio
import os
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes
from config import PENDING_KEY
from parsing import build_review_prompt, extract_download_links, parse_message
from tmdb_service import search_tmdb
from workflow import finalize_pending_post


def extract_query_from_message(text, parsed):
    if parsed.get("movie"):
        return parsed["movie"]

    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            # Check if it looks like a movie title (not too long)
            if len(stripped) < 100:
                return stripped

    return (text or "").strip()[:100]


def build_preview_record(parsed, downloads, tmdb_details, query_text):
    record = dict(parsed)
    # Ensure we don't use any local image path if it was there
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
    lines = [
        "🔍 *TMDB Preview*",
        f"*Title:* {record.get('movie') or 'Untitled'}",
        f"*Year:* {record.get('year') or 'Unknown'}",
        f"*Description:* {record.get('description')[:300] or 'No description available'}...",
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    text = msg.text or msg.caption or ""
    pending = context.chat_data.get(PENDING_KEY)

    if pending:
        stage = pending.get("stage")

        if stage == "awaiting_search_query":
            query_text = text.strip()
            if not query_text:
                await msg.reply_text("Please send a movie title to search TMDB.")
                return

            tmdb_details = await asyncio.to_thread(search_tmdb, query_text)
            if not tmdb_details:
                pending["last_query"] = query_text
                context.chat_data[PENDING_KEY] = pending
                await msg.reply_text(
                    f"No results for '{query_text}'. Try another title or choose Manual.",
                    reply_markup=build_review_prompt(include_continue=False),
                )
                return

            record = build_preview_record(pending, pending.get("downloads", {}), tmdb_details, query_text)
            context.chat_data[PENDING_KEY] = record
            await send_preview_message(msg, record)
            return

        if stage == "awaiting_manual_title":
            manual_title = text.strip()
            if not manual_title:
                await msg.reply_text("Please send the title.")
                return

            # Still try to find a poster/details based on the manual title
            tmdb_details = await asyncio.to_thread(search_tmdb, manual_title)
            pending["movie"] = manual_title
            pending["stage"] = "awaiting_manual_description"

            if tmdb_details:
                pending["poster_url"] = tmdb_details.get("poster_url")
                pending["year"] = tmdb_details.get("year") or pending.get("year")
                pending["tmdb_id"] = tmdb_details.get("tmdb_id")
                pending["tmdb_media_type"] = tmdb_details.get("media_type")

            context.chat_data[PENDING_KEY] = pending
            await msg.reply_text("Now send the description.")
            return

        if stage == "awaiting_manual_description":
            description = text.strip()
            if not description:
                await msg.reply_text("Please send the description text.")
                return

            pending["description"] = description
            pending["stage"] = "review"
            context.chat_data[PENDING_KEY] = pending
            await send_preview_message(msg, pending)
            return

        if stage == "review":
            await msg.reply_text("Please use the buttons above to Continue or Search again.")
            return

    # New message handling (likely forwarded)
    parsed = parse_message(text)
    extracted_downloads = extract_download_links(msg)

    query_text = extract_query_from_message(text, parsed)

    if query_text:
        tmdb_details = await asyncio.to_thread(search_tmdb, query_text)
        if tmdb_details:
            record = build_preview_record(parsed, extracted_downloads or parsed.get("downloads", {}), tmdb_details, query_text)
            context.chat_data[PENDING_KEY] = record
            await send_preview_message(msg, record)
            return

        # No TMDB result, ask to search again or manual
        pending = dict(parsed)
        pending["downloads"] = extracted_downloads or parsed.get("downloads", {})
        pending["stage"] = "awaiting_search_query"
        pending["last_query"] = query_text
        context.chat_data[PENDING_KEY] = pending
        await msg.reply_text(
            f"Could not find '{query_text}' on TMDB. Please search again or enter details manually.",
            reply_markup=build_review_prompt(include_continue=False),
        )
        return

    # Fallback if no query text could be extracted
    await msg.reply_text(
        "I couldn't parse that message. Would you like to search TMDB or enter details manually?",
        reply_markup=build_review_prompt(include_continue=False),
    )


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()
    context.chat_data.pop(PENDING_KEY, None)
    if query.message and query.message.caption is not None:
        await query.edit_message_caption(caption="Canceled.")
    else:
        await query.edit_message_text(text="Canceled.")


async def handle_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()
    pending = context.chat_data.get(PENDING_KEY)

    if not pending:
        if query.message and query.message.caption is not None:
            await query.edit_message_caption(caption="Nothing pending.")
        else:
            await query.edit_message_text(text="Nothing pending.")
        return

    try:
        record, insert_sql = await finalize_pending_post(pending, pending.get("description") or "")
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

    await query.answer()
    pending = dict(context.chat_data.get(PENDING_KEY) or {})
    pending["stage"] = "awaiting_manual_title"
    context.chat_data[PENDING_KEY] = pending

    prompt_text = "Send the title manually. I will ask for the description next."
    if query.message and query.message.caption is not None:
        await query.edit_message_caption(caption=prompt_text)
    else:
        await query.edit_message_text(text=prompt_text)
