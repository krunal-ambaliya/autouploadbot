import logging
import os
import asyncio
from flask import Flask, request
from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, MessageHandler, filters
from config import BOT_TOKEN, load_env_file
from handlers import handle_cancel, handle_continue, handle_manual, handle_message, handle_search_again

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
bot = Bot(token=BOT_TOKEN)

# Create Application with all handlers
application = ApplicationBuilder().token(BOT_TOKEN).build()
application.add_handler(MessageHandler(filters.ALL, handle_message))
application.add_handler(CallbackQueryHandler(handle_cancel, pattern="^cancel_pending$"))
application.add_handler(CallbackQueryHandler(handle_continue, pattern="^tmdb_continue$"))
application.add_handler(CallbackQueryHandler(handle_search_again, pattern="^tmdb_search_again$"))
application.add_handler(CallbackQueryHandler(handle_manual, pattern="^tmdb_manual$"))


@app.route("/", methods=["GET"])
def health():
    return "Bot is running", 200


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        update_data = request.get_json()
        update = Update.de_json(update_data, bot)
        
        asyncio.run(application.process_update(update))
        return "OK", 200
    except Exception as e:
        logger.error(f"Error processing update: {e}")
        return "Error", 500


def set_webhook():
    """Automatically set webhook URL"""
    try:
        webhook_url = os.environ.get("WEBHOOK_URL")
        if not webhook_url:
            logger.warning("WEBHOOK_URL not set in environment")
            return
        
        webhook_path = f"{webhook_url}/{BOT_TOKEN}"
        bot.set_webhook(url=webhook_path)
        logger.info(f"Webhook set to {webhook_path}")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")


if __name__ == "__main__":
    set_webhook()
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"Starting Flask webhook server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
