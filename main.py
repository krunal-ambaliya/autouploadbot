import logging
import time
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, MessageHandler, filters
from config import BOT_TOKEN, load_env_file
from handlers import handle_cancel, handle_continue, handle_manual, handle_message, handle_search_again

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    retry_count = 0
    max_retries = 5
    
    while True:
        try:
            app = ApplicationBuilder().token(BOT_TOKEN).build()
            app.add_handler(MessageHandler(filters.ALL, handle_message))
            app.add_handler(CallbackQueryHandler(handle_cancel, pattern="^cancel_pending$"))
            app.add_handler(CallbackQueryHandler(handle_continue, pattern="^tmdb_continue$"))
            app.add_handler(CallbackQueryHandler(handle_search_again, pattern="^tmdb_search_again$"))
            app.add_handler(CallbackQueryHandler(handle_manual, pattern="^tmdb_manual$"))

            logger.info("Bot starting...")
            app.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)
        except Exception as e:
            retry_count += 1
            logger.error(f"Bot error: {e}. Retry {retry_count}/{max_retries}")
            
            if retry_count >= max_retries:
                logger.critical("Max retries reached. Exiting.")
                break
            
            wait_time = min(60, 2 ** retry_count)
            logger.info(f"Waiting {wait_time} seconds before restart...")
            time.sleep(wait_time)
        else:
            retry_count = 0


if __name__ == "__main__":
    main()
