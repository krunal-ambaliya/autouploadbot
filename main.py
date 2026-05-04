from telegram.ext import ApplicationBuilder, CallbackQueryHandler, MessageHandler, filters
from config import BOT_TOKEN, load_env_file
from handlers import handle_cancel, handle_continue, handle_manual, handle_message, handle_search_again


def main():

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    app.add_handler(CallbackQueryHandler(handle_cancel, pattern="^cancel_pending$"))
    app.add_handler(CallbackQueryHandler(handle_continue, pattern="^tmdb_continue$"))
    app.add_handler(CallbackQueryHandler(handle_search_again, pattern="^tmdb_search_again$"))
    app.add_handler(CallbackQueryHandler(handle_manual, pattern="^tmdb_manual$"))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
