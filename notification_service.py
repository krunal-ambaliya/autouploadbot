import asyncio
import logging
from telegram import Bot
from telegram.error import TelegramError
from config import BOT_TOKEN, TELEGRAM_CHANNEL_ID

logger = logging.getLogger(__name__)


def format_channel_message(record):
    """Format a message for the Telegram channel."""
    title = record.get("movie") or "Unknown"
    year = record.get("year") or "N/A"
    media_type = record.get("type", "movie").upper()
    
    downloads = record.get("downloads", {})
    description = (record.get("description") or "").strip()
    
    # Build quality/links section
    links_text = ""
    if downloads:
        links_text = "\n\n🔗 **Download Links:**\n"
        for quality, link in downloads.items():
            quality_upper = quality.upper() if quality else "UNKNOWN"
            links_text += f"{quality_upper}: {link}\n"
    
    # Format message
    message = f"""
🎬 **NEW {media_type} UPLOADED!**

**Title:** {title}
**Year:** {year}
**Type:** {media_type}

📝 **Description:**
{description}{links_text}
    """.strip()
    
    return message


async def send_channel_notification(record):
    """Send notification to Telegram channel."""
    if not TELEGRAM_CHANNEL_ID or not BOT_TOKEN:
        logger.warning("TELEGRAM_CHANNEL_ID or BOT_TOKEN not configured. Skipping notification.")
        return
    
    try:
        bot = Bot(token=BOT_TOKEN)
        message_text = format_channel_message(record)
        
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=message_text,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
        
        logger.info(f"Channel notification sent for: {record.get('movie')}")
        
    except TelegramError as e:
        logger.error(f"Failed to send channel notification: {e}")
    except Exception as e:
        logger.error(f"Unexpected error sending channel notification: {e}")
