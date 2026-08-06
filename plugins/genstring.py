import asyncio
import logging

from pyrogram import Client, filters
from pyrogram.errors import (
    ApiIdInvalid,
    PhoneNumberInvalid,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    SessionPasswordNeeded,
    PasswordHashInvalid,
    FloodWait,
)

from config import Config, temp
from database import db

logger = logging.getLogger(__name__)

DISCLAIMER = (
    "<b>⚠️ DISCLAIMER ⚠️</b>\n\n"
    "<code>This creates a login session string for the Telegram account whose "
    "phone number you enter below. Anyone with this string has full control of "
    "that account. Only generate a session for an account you own, and never "
    "share the resulting string with anyone. There is a chance of the account "
    "getting banned for automated forwarding \u2014 use at your own risk.</code>\n\n"
    "Send /cancel at any point to stop."
)


@Client.on_message(filters.private & filters.command(["genstring", "getstring"]))
async def genstring(bot, message):
    user_id = message.from_user.id

    await bot.send_message(user_id, DISCLAIMER)

    phone_msg = await bot.ask(
        user_id,
        "<b>Send the phone number for the account, in international format.</b>\n"
        "Example: <code>+15551234567</code>"
    )
    if phone_msg.text == "/cancel":
        return await phone_msg.reply("<b>Process cancelled.</b>")
    phone_number = phone_msg.text.strip()

    userbot = Client(
        f"genstring_{user_id}",
        api_id=Config.API_ID,
        api_hash=Config.API_HASH,
        in_memory=True,
    )
    await userbot.connect()

    try:
        sent_code = await userbot.send_code(phone_number)
    except ApiIdInvalid:
        await userbot.disconnect()
        return await message.reply("<b>Your API_ID / API_HASH combo is invalid.</b>")
    except PhoneNumberInvalid:
        await userbot.disconnect()
        return await message.reply("<b>Invalid phone number.</b>")
    except FloodWait as e:
        await userbot.disconnect()
        return await message.reply(f"<b>Flood wait: try again in {e.value} seconds.</b>")
    except Exception as e:
        await userbot.disconnect()
        return await message.reply(f"<b>Error requesting code:</b> <code>{e}</code>")

    code_msg = await bot.ask(
        user_id,
        "<b>Telegram just sent a login code to that account.</b>\n\n"
        "Enter it here with a symbol between each digit, e.g. if the code is "
        "<code>12345</code> send <code>1-2-3-4-5</code>. This stops Telegram "
        "auto-invalidating the code because it was sent through a bot chat."
    )
    if code_msg.text == "/cancel":
        await userbot.disconnect()
        return await code_msg.reply("<b>Process cancelled.</b>")
    phone_code = code_msg.text.replace("-", "").replace(" ", "").strip()

    try:
        await userbot.sign_in(phone_number, sent_code.phone_code_hash, phone_code)
    except PhoneCodeInvalid:
        await userbot.disconnect()
        return await code_msg.reply("<b>Invalid code. Please run /genstring again.</b>")
    except PhoneCodeExpired:
        await userbot.disconnect()
        return await code_msg.reply("<b>Code expired. Please run /genstring again.</b>")
    except SessionPasswordNeeded:
        pw_msg = await bot.ask(
            user_id,
            "<b>This account has Two-Step Verification enabled.</b>\n"
            "Send the account's 2FA password."
        )
        if pw_msg.text == "/cancel":
            await userbot.disconnect()
            return await pw_msg.reply("<b>Process cancelled.</b>")
        try:
            await userbot.check_password(pw_msg.text)
        except PasswordHashInvalid:
            await userbot.disconnect()
            return await pw_msg.reply("<b>Wrong password. Please run /genstring again.</b>")
        except Exception as e:
            await userbot.disconnect()
            return await pw_msg.reply(f"<b>Error:</b> <code>{e}</code>")
    except FloodWait as e:
        await userbot.disconnect()
        return await code_msg.reply(f"<b>Flood wait: try again in {e.value} seconds.</b>")
    except Exception as e:
        await userbot.disconnect()
        return await code_msg.reply(f"<b>Error signing in:</b> <code>{e}</code>")

    session_string = await userbot.export_session_string()
    me = await userbot.get_me()
    await userbot.disconnect()

    warn = await bot.send_message(
        user_id,
        "<b>Session generated. Sending it below \u2014 this message will "
        "auto-delete in 60 seconds. Copy it now and store it somewhere safe.</b>"
    )
    string_msg = await bot.send_message(user_id, f"<code>{session_string}</code>")

    save_it = await bot.ask(
        user_id,
        f"<b>Logged in as {me.first_name} (@{me.username or 'no_username'}).</b>\n\n"
        "Save this as your forwarding userbot session now? Reply <code>yes</code> "
        "or <code>no</code>. (You can still copy the string above either way.)"
    )
    if save_it.text.strip().lower() == "yes":
        details = {
            "id": me.id,
            "is_bot": False,
            "user_id": user_id,
            "name": me.first_name,
            "session": session_string,
            "username": me.username,
        }
        if not await db.is_bot_exist(user_id):
            await db.add_bot(details)
            await save_it.reply("<b>Saved as your forwarding session \u2705</b>")
        else:
            await save_it.reply(
                "<b>You already have a bot/session added. Remove it first via "
                "/settings if you want to replace it.</b>"
            )

    await asyncio.sleep(60)
    try:
        await warn.delete()
        await string_msg.delete()
    except Exception:
        pass
