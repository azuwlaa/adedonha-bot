# main.py — app bootstrap
import asyncio
import logging
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters
import config
from db import init_db
import handlers

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    init_db()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_BOT_TOKEN.strip():
        print("Please set TELEGRAM_BOT_TOKEN in config.py before running.")
        return

    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

    # register handlers
    app.add_handler(CommandHandler("classicadedonha", handlers.classic_lobby))
    app.add_handler(CommandHandler("customadedonha", handlers.custom_lobby))
    app.add_handler(CommandHandler("fastadedonha", handlers.fast_lobby))
    app.add_handler(CommandHandler(["joingame","join"], handlers.joingame_command))
    app.add_handler(CallbackQueryHandler(handlers.join_callback))
    app.add_handler(CallbackQueryHandler(handlers.mode_info_callback, pattern="mode_info"))
    app.add_handler(CallbackQueryHandler(handlers.start_game_callback, pattern="start_game"))
    app.add_handler(CommandHandler("gamecancel", handlers.gamecancel_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.submission_handler))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
