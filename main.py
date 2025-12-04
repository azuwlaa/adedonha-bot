# main.py - entrypoint
import logging
import asyncio
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters

from . import handlers  # package import
from .database import setup_db
from .utils import TELEGRAM_BOT_TOKEN

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    # Run async DB setup inside the event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(setup_db())
    except Exception as e:
        logger.exception("DB setup failed: %s", e)
        return

    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.strip() == "":
        print("Please set TELEGRAM_BOT_TOKEN in utils.py before running.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # register handlers
    app.add_handler(CommandHandler("runinfo", handlers.runinfo_command))
    app.add_handler(CommandHandler("classicadedonha", handlers.classic_lobby))
    app.add_handler(CommandHandler("customadedonha", handlers.custom_lobby))
    app.add_handler(CommandHandler("fastadedonha", handlers.fast_lobby))
    app.add_handler(CommandHandler(["joingame","join"], handlers.joingame_command))
    app.add_handler(CallbackQueryHandler(handlers.callback_router))
    app.add_handler(CommandHandler("gamecancel", handlers.gamecancel_command))
    app.add_handler(CommandHandler("categories", handlers.categories_command))
    app.add_handler(CommandHandler("mystats", handlers.mystats_command))
    app.add_handler(CommandHandler("dumpstats", handlers.dumpstats_command))
    app.add_handler(CommandHandler("statsreset", handlers.statsreset_command))
    app.add_handler(CommandHandler("leaderboard", handlers.leaderboard_command))
    app.add_handler(CommandHandler("validate", handlers.validate_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.submission_handler))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
