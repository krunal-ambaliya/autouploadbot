import logging
import os
import asyncio
import threading
from flask import Flask, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, MessageHandler, filters
from config import BOT_TOKEN, load_env_file
from handlers import (
    handle_cancel,
    handle_continue,
    handle_manual,
    handle_manual_send_again,
    handle_message,
    handle_search_again,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
application = ApplicationBuilder().token(BOT_TOKEN).build()

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
WEBHOOK_LOOP = None
WEBHOOK_THREAD = None


def _start_webhook_loop():
    global WEBHOOK_LOOP
    WEBHOOK_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(WEBHOOK_LOOP)
    WEBHOOK_LOOP.run_forever()


def _run_webhook_coro(coro):
    if WEBHOOK_LOOP is None:
        raise RuntimeError("Webhook loop is not running")
    future = asyncio.run_coroutine_threadsafe(coro, WEBHOOK_LOOP)
    return future.result()


def _ensure_webhook_runtime():
    global WEBHOOK_THREAD
    if WEBHOOK_THREAD and WEBHOOK_THREAD.is_alive():
        return

    WEBHOOK_THREAD = threading.Thread(target=_start_webhook_loop, daemon=True)
    WEBHOOK_THREAD.start()


def _register_handlers():
    application.add_handler(MessageHandler(filters.ALL, handle_message))
    application.add_handler(CallbackQueryHandler(handle_cancel, pattern="^cancel_pending$"))
    application.add_handler(CallbackQueryHandler(handle_continue, pattern="^tmdb_continue$"))
    application.add_handler(CallbackQueryHandler(handle_search_again, pattern="^tmdb_search_again$"))
    application.add_handler(CallbackQueryHandler(handle_manual, pattern="^tmdb_manual$"))
    application.add_handler(CallbackQueryHandler(handle_manual_send_again, pattern=r"^manual_send_again:"))


@app.route("/", methods=["GET"])
def health():
    return "Bot is running", 200


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        if not WEBHOOK_URL:
            return "Webhook mode is disabled", 404

        update_data = request.get_json()
        update = Update.de_json(update_data, application.bot)

        _run_webhook_coro(application.process_update(update))
        return "OK", 200
    except Exception as e:
        logger.error(f"Error processing update: {e}")
        return "Error", 500


def set_webhook():
    """Automatically set webhook URL"""
    try:
        if not WEBHOOK_URL:
            logger.info("WEBHOOK_URL not set; using local polling mode")
            return

        webhook_path = f"{WEBHOOK_URL}/{BOT_TOKEN}"
        _run_webhook_coro(application.bot.set_webhook(url=webhook_path))
        logger.info(f"Webhook set to {webhook_path}")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")


if __name__ == "__main__":
    _register_handlers()
    if WEBHOOK_URL:
        _ensure_webhook_runtime()
        _run_webhook_coro(application.initialize())
        set_webhook()
        port = int(os.environ.get("PORT", 8000))
        logger.info(f"Starting Flask webhook server on port {port}")
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        logger.info("Starting local polling mode")
        local_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(local_loop)
        local_loop.run_until_complete(application.bot.delete_webhook(drop_pending_updates=True))
        application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)
