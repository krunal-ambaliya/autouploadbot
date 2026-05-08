import logging
import os
import asyncio
import threading
import sys
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
    handle_toggle_type,
)
from storage import init_db

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
WEBHOOK_LOOP_READY = None

# Determine if we're running in a non-interactive hosted environment.
# If so, require WEBHOOK_URL to be set (unless FORCE_WEBHOOK is explicitly false).
def _ensure_webhook_config_or_exit():
    force = os.environ.get("FORCE_WEBHOOK", "0").lower() in ("1", "true", "yes")
    if WEBHOOK_URL:
        return

    # If FORCE_WEBHOOK is set true, fail immediately.
    if force:
        logger.critical("WEBHOOK_URL is not set but FORCE_WEBHOOK is true. Set WEBHOOK_URL to your public app URL when deploying.")
        sys.exit(1)

    # If PORT is present and we're non-interactive, assume hosted deployment and fail with clear message.
    if "PORT" in os.environ and not sys.stdin.isatty():
        logger.critical(
            "WEBHOOK_URL is not set but PORT is present and process is non-interactive.\n"
            "In hosted deployments (like Koyeb) the app must run in webhook mode.\n"
            "Set environment variable WEBHOOK_URL to your public app URL (for example https://your-app.koyeb.app) and redeploy.\n"
            "Alternatively set FORCE_WEBHOOK=false to allow polling (not recommended on hosted platforms)."
        )
        sys.exit(1)



def _start_webhook_loop():
    global WEBHOOK_LOOP
    global WEBHOOK_LOOP_READY
    WEBHOOK_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(WEBHOOK_LOOP)
    # signal that the loop is ready
    try:
        if WEBHOOK_LOOP_READY is not None:
            WEBHOOK_LOOP_READY.set()
    except Exception:
        pass
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

    # create an event to wait until the loop is ready
    global WEBHOOK_LOOP_READY
    WEBHOOK_LOOP_READY = threading.Event()
    WEBHOOK_THREAD = threading.Thread(target=_start_webhook_loop, daemon=True)
    WEBHOOK_THREAD.start()

    # wait for the loop to be initialized (avoid race)
    if not WEBHOOK_LOOP_READY.wait(timeout=10):
        logger.error("Timed out waiting for webhook event loop to start")
        raise RuntimeError("Webhook loop failed to start in time")


def _register_handlers():
    application.add_handler(MessageHandler(filters.ALL, handle_message))
    application.add_handler(CallbackQueryHandler(handle_cancel, pattern="^cancel_pending$"))
    application.add_handler(CallbackQueryHandler(handle_continue, pattern="^tmdb_continue$"))
    application.add_handler(CallbackQueryHandler(handle_search_again, pattern="^tmdb_search_again$"))
    application.add_handler(CallbackQueryHandler(handle_manual, pattern="^tmdb_manual$"))
    application.add_handler(CallbackQueryHandler(handle_manual_send_again, pattern=r"^manual_send_again:"))
    application.add_handler(CallbackQueryHandler(handle_toggle_type, pattern="^tmdb_toggle_type$"))


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
    init_db()
    _register_handlers()
    if WEBHOOK_URL:
        _ensure_webhook_runtime()
        _run_webhook_coro(application.initialize())
        _run_webhook_coro(application.start())
        set_webhook()
        port = int(os.environ.get("PORT", 8000))
        logger.info(f"Starting Flask webhook server on port {port}")
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        local_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(local_loop)
        local_loop.run_until_complete(application.bot.delete_webhook(drop_pending_updates=True))
        local_loop.run_until_complete(application.initialize())
        application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)
