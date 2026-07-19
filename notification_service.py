import asyncio
import html
import logging
import os
from telegram import Bot
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from config import BOT_TOKEN, TELEGRAM_CHANNEL_ID

logger = logging.getLogger(__name__)


def format_channel_message(record):
    """Format a simple Telegram channel message."""
    title = record.get("movie") or "Unknown"
    year = record.get("year") or "N/A"
    media_type = str(record.get("type") or "Movie").title()
    description = (record.get("description") or "No description available.").strip()

    downloads = record.get("downloads", {})

    message = (
        f"🎥 <b>New {html.escape(media_type)} Uploaded</b>\n\n"
        f"📌 <b>Title:</b> {html.escape(str(title))}\n"
        f"📅 <b>Year:</b> {html.escape(str(year))}\n"
        f"🎭 <b>Type:</b> {html.escape(media_type)}\n\n"
        f"📝 <b>Description</b>\n"
        f"{html.escape(description)}"
    )

    # if downloads:
    #     message += "\n\n🔗 <b>Download Links</b>\n\n"
    #     for quality, link in downloads.items():
    #         quality = quality.upper() if quality else "UNKNOWN"
    #         message += (
    #             f"<b>{html.escape(quality)}:</b> "
    #             f"{html.escape(str(link), quote=False)}\n"
    #         )

    return message.strip()

def _get_notification_image(record):
    poster_url = record.get("poster_url")
    if poster_url:
        return poster_url

    sample_images = record.get("sample_images") or []
    if sample_images:
        return sample_images[0]

    return None


def _build_movie_url(record):
    movie_id = record.get("neon_inserted")
    if movie_id in (None, "", False):
        return None

    template = os.getenv("MOVIE_PAGE_URL_TEMPLATE")
    if template:
        template = template.strip()
        if template:
            if "{id}" in template or "{movie_id}" in template:
                return template.format(id=movie_id, movie_id=movie_id)
            if template.endswith("/"):
                return f"{template}{movie_id}"
            return f"{template}/{movie_id}"

    base_url = os.getenv("MOVIE_PAGE_BASE_URL") or os.getenv("MOVIE_PAGE_URL")
    if not base_url:
        return None

    base_url = base_url.strip().rstrip("/")
    if not base_url:
        return None

    media_type = str(record.get("type") or record.get("tmdb_media_type") or "movie").strip().lower()
    page_type = "series" if media_type in {"series", "tv"} else "movie"
    return f"{base_url}/{page_type}/{movie_id}"


def _build_notification_markup(record):
    movie_url = _build_movie_url(record)
    if not movie_url:
        return None

    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("👉 Click here to watch", url=movie_url)]]
    )


async def send_channel_notification(record):
    """Send notification to Telegram channel."""
    if not TELEGRAM_CHANNEL_ID or not BOT_TOKEN:
        logger.warning("TELEGRAM_CHANNEL_ID or BOT_TOKEN not configured. Skipping notification.")
        return
    
    try:
        bot = Bot(token=BOT_TOKEN)
        message_text = format_channel_message(record)
        image_url = _get_notification_image(record)
        reply_markup = _build_notification_markup(record)

        if image_url:
            if len(message_text) <= 1024:
                await bot.send_photo(
                    chat_id=TELEGRAM_CHANNEL_ID,
                    photo=image_url,
                    caption=message_text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
                logger.info(f"Channel notification sent for: {record.get('movie')}")
                return

            short_caption = (
                f"🎬 <b>NEW {html.escape(str(record.get('type', 'movie')).upper())} UPLOADED!</b>\n"
                f"<b>Title:</b> {html.escape(str(record.get('movie') or 'Unknown'))}\n"
                f"<b>Year:</b> {html.escape(str(record.get('year') or 'N/A'))}"
            )
            await bot.send_photo(
                chat_id=TELEGRAM_CHANNEL_ID,
                photo=image_url,
                caption=short_caption,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )

            await bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID,
                text=message_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            logger.info(f"Channel notification sent for: {record.get('movie')}")
            return
        
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=message_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )
        
        logger.info(f"Channel notification sent for: {record.get('movie')}")
        
    except TelegramError as e:
        logger.error(f"Failed to send channel notification: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending channel notification: {e}")
