import asyncio
import logging 
import logging.config
from database import db 
from config import Config  
from pyrogram import Client, __version__
from pyrogram.raw.all import layer 
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait 
from pyrogram.types import BotCommand

logging.config.fileConfig('logging.conf')
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)

class Bot(Client): 
    def __init__(self):
        super().__init__(
            Config.BOT_SESSION,
            api_hash=Config.API_HASH,
            api_id=Config.API_ID,
            plugins={
                "root": "plugins"
            },
            workers=50,
            bot_token=Config.BOT_TOKEN
        )
        self.log = logging

    async def start(self):
        await super().start()
        me = await self.get_me()
        logging.info(f"{me.first_name} with for pyrogram v{__version__} (Layer {layer}) started on @{me.username}.")
        self.id = me.id
        self.username = me.username
        self.first_name = me.first_name
        self.set_parse_mode(ParseMode.DEFAULT)
        text = "**๏[-ิ_•ิ]๏ bot restarted !**"
        logging.info(text)

        # Register commands in Telegram dropdown menu
        try:
            await self.set_bot_commands([
                BotCommand("start", "Start the bot"),
                BotCommand("fwd", "Start forwarding messages"),
                BotCommand("forward", "Start forwarding messages"),
                BotCommand("task", "Check if a forward task is running"),
                BotCommand("status", "Check your current task status"),
                BotCommand("mystatus", "Check your current task status"),
                BotCommand("cancel", "Cancel the running forward task"),
                BotCommand("settings", "Open bot settings"),
                BotCommand("unlock", "Force unlock a stuck task"),
                BotCommand("forceunlock", "Force unlock a stuck task"),
                BotCommand("reset", "Reset your data"),
                BotCommand("unequify", "Remove duplicate files"),
                BotCommand("genstring", "Generate userbot session string"),
                BotCommand("getstring", "Generate userbot session string"),
                BotCommand("help", "Show help"),
            ])
            logging.info("Bot commands menu registered successfully")
        except Exception as e:
            logging.error(f"Failed to set bot commands: {e}")

        # Check if database URI is default broken one
        if "mongodb+srv://chhjgjkkjhkjhkjh@cluster0.xowzpr4.mongodb.net/" in Config.DATABASE_URI:
             logging.error("You have not set the DATABASE environment variable. The bot will not function correctly.")
             return

        # Notify users who had active forwards — with a hard timeout so deploy
        # health checks are not blocked for minutes on a slow Mongo / FloodWait.
        async def _notify_restart():
            try:
                success = failed = 0
                users = await db.get_all_frwd()
                async for user in users:
                    chat_id = user['user_id']
                    try:
                        await self.send_message(chat_id, text)
                        success += 1
                    except FloodWait as e:
                        await asyncio.sleep(min(e.value + 1, 30))
                        try:
                            await self.send_message(chat_id, text)
                            success += 1
                        except Exception:
                            failed += 1
                    except Exception:
                        failed += 1
                if (success + failed) != 0:
                    await db.rmve_frwd(all=True)
                    logging.info(f"Restart notify: success={success} failed={failed}")
            except Exception as e:
                logging.error(f"Restart notify / DB error: {e}")

        try:
            await asyncio.wait_for(_notify_restart(), timeout=45)
        except asyncio.TimeoutError:
            logging.warning("Restart notify timed out after 45s — continuing startup")
        except Exception as e:
            logging.error(f"Failed to send restart messages or connect to DB: {e}")

    async def stop(self, *args):
        msg = f"@{self.username} stopped. Bye."
        await super().stop()
        logging.info(msg)
