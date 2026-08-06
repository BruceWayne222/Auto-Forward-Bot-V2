import os
import re 
import sys
import typing
import asyncio 
import logging 
from database import db 
from config import Config, temp
from pyrogram import Client, filters
from pyrogram.raw.all import layer
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message 
from pyrogram.errors.exceptions.bad_request_400 import AccessTokenExpired, AccessTokenInvalid
from pyrogram.errors import FloodWait
from config import Config
from translation import Translation

from typing import Union, Optional, AsyncGenerator

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

BTN_URL_REGEX = re.compile(r"(\[([^\[]+?)]\[buttonurl:/{0,2}(.+?)(:same)?])")
BOT_TOKEN_TEXT = "<b>1) create a bot using @BotFather\n2) Then you will get a message with bot token\n3) Forward that message to me</b>"
SESSION_STRING_SIZE = 351

def get_file_unique_id(message):
   """Return the stable file_unique_id for a message's media, or None for text/non-media."""
   if not message.media:
      return None
   media = getattr(message, message.media.value, None)
   return getattr(media, 'file_unique_id', None) if media else None

async def start_clone_bot(FwdBot, data=None):
   await FwdBot.start()
   #
   async def iter_messages(
      self, 
      chat_id: Union[int, str], 
      limit: int, 
      offset: int = 0,
      search: str = None,
      filter: "types.TypeMessagesFilter" = None,
      continuous: bool = False,
      skip_duplicate: Union[list, bool] = False
      ) -> Optional[AsyncGenerator["types.Message", None]]:
        """Iterate through a chat sequentially."""
        current = offset
        while True:
            # If continuous, we don't really have a limit, effectively infinite
            # But we still use limit if provided to fetch batches
            # If continuous=True, loop forever waiting for new messages

            # If batch fetch size is 200
            new_diff = 200 # Default batch size

            if not continuous and limit > 0:
                new_diff = min(200, limit - current)
                if new_diff <= 0:
                    return

            try:
                messages = await self.get_messages(chat_id, list(range(current, current+new_diff+1)))
            except FloodWait as e:
                await asyncio.sleep(e.value)
                continue
            except Exception as e:
                print(f"Failed to fetch messages from {chat_id} (offset {current}): {e}")
                messages = []

            # Filter out None values (messages that don't exist yet)
            valid_messages = [m for m in messages if m and not m.empty]

            if not valid_messages:
                if continuous:
                    # No new messages, wait and retry
                    await asyncio.sleep(10)
                    continue
                else:
                    # End of chat
                    return

            for message in valid_messages:
                if skip_duplicate and message.media:
                    file_unique_id = get_file_unique_id(message)
                    if file_unique_id:
                        dup_uri, target_chat = skip_duplicate
                        is_dup = await db.is_duplicate_file(file_unique_id, target_chat, dup_uri)
                        if is_dup:
                            yield "DUPLICATE"
                            current = max(current, message.id) + 1
                            continue
                yield message
                current = max(current, message.id) + 1

            # If we got fewer messages than requested, and not continuous, it might be end?
            # But we are iterating by ID range, so gaps are possible.
            # We just increment current.
            if not valid_messages and not continuous:
                 return

            # Optimization: if valid_messages is empty but we are in continuous mode, we handled it above.
            # If valid_messages is NOT empty, we processed them.
            # Update current to be next ID.

            current = list(range(current, current+new_diff+1))[-1] + 1
            # Wait, the range logic above: range(current, current+new_diff+1)
            # If current=0, new_diff=200. range(0, 201). IDs 0..200.
            # Next iteration should start at 201.
            # So current += new_diff + 1?
            # No, if we yield, we just continue loop.
            # But we need to update 'current' for next batch.
            # My previous logic: `current += 1` inside loop was weird because `messages` is a batch.

            # Let's fix the batch logic properly
            # The original code:
            # messages = await self.get_messages(chat_id, list(range(current, current+new_diff+1)))
            # for message in messages: yield message; current += 1
            # This assumed sequential IDs and incrementing current.

            # New logic:
            # Just increment current by batch size at the end of loop
            current += (new_diff + 1)

   #
   FwdBot.iter_messages = iter_messages
   return FwdBot

class CLIENT: 
  def __init__(self):
     self.api_id = Config.API_ID
     self.api_hash = Config.API_HASH
    
  def client(self, data, user=None):
     if user == None and data.get('is_bot') == False:
        return Client("USERBOT", self.api_id, self.api_hash, session_string=data.get('session'))
     elif user == True:
        return Client("USERBOT", self.api_id, self.api_hash, session_string=data)
     elif user != False:
        data = data.get('token')
     return Client("BOT", self.api_id, self.api_hash, bot_token=data, in_memory=True)
  
  async def add_bot(self, bot, message):
     user_id = int(message.from_user.id)
     msg = await bot.ask(chat_id=user_id, text=BOT_TOKEN_TEXT)
     if msg.text=='/cancel':
        return await msg.reply('<b>process cancelled !</b>')
     elif not msg.forward_date:
       return await msg.reply_text("<b>This is not a forward message</b>")
     elif str(msg.forward_from.id) != "93372553":
       return await msg.reply_text("<b>This message was not forward from bot father</b>")
     bot_token = re.findall(r'\d[0-9]{8,10}:[0-9A-Za-z_-]{35}', msg.text, re.IGNORECASE)
     bot_token = bot_token[0] if bot_token else None
     if not bot_token:
       return await msg.reply_text("<b>There is no bot token in that message</b>")
     try:
       _client = await start_clone_bot(self.client(bot_token, False), True)
     except Exception as e:
       await msg.reply_text(f"<b>BOT ERROR:</b> `{e}`")
     _bot = _client.me
     details = {
       'id': _bot.id,
       'is_bot': True,
       'user_id': user_id,
       'name': _bot.first_name,
       'token': bot_token,
       'username': _bot.username 
     }
     await db.add_bot(details)
     return True
    
  async def add_session(self, bot, message):
     user_id = int(message.from_user.id)
     text = "<b>⚠️ DISCLAIMER ⚠️</b>\n\n<code>you can use your session for forward message from private chat to another chat.\nPlease add your pyrogram session with your own risk. Their is a chance to ban your account. My developer is not responsible if your account may get banned.</code>"
     await bot.send_message(user_id, text=text)
     msg = await bot.ask(chat_id=user_id, text="<b>send your pyrogram session.\nGet it from trusted sources.\n\n/cancel - cancel the process</b>")
     if msg.text=='/cancel':
        return await msg.reply('<b>process cancelled !</b>')
     elif len(msg.text) < SESSION_STRING_SIZE:
        return await msg.reply('<b>invalid session sring</b>')
     try:
       client = await start_clone_bot(self.client(msg.text, True), True)
     except Exception as e:
       await msg.reply_text(f"<b>USER BOT ERROR:</b> `{e}`")
     user = client.me
     details = {
       'id': user.id,
       'is_bot': False,
       'user_id': user_id,
       'name': user.first_name,
       'session': msg.text,
       'username': user.username
     }
     await db.add_bot(details)
     return True
    
@Client.on_message(filters.private & filters.command('reset'))
async def forward_tag(bot, m):
   default = await db.get_configs("01")
   temp.CONFIGS[m.from_user.id] = default
   await db.update_configs(m.from_user.id, default)
   await m.reply("successfully settings reseted ✔️")

@Client.on_message(filters.command('resetall') & filters.user(Config.BOT_OWNER_ID))
async def resetall(bot, message):
  users = await db.get_all_users()
  sts = await message.reply("**processing**")
  TEXT = "total: {}\nsuccess: {}\nfailed: {}\nexcept: {}"
  total = success = failed = already = 0
  ERRORS = []
  async for user in users:
      user_id = user['id']
      default = await get_configs(user_id)
      default['db_uri'] = None
      total += 1
      if total %10 == 0:
         await sts.edit(TEXT.format(total, success, failed, already))
      try: 
         await db.update_configs(user_id, default)
         success += 1
      except Exception as e:
         ERRORS.append(e)
         failed += 1
  if ERRORS:
     await message.reply(ERRORS[:100])
  await sts.edit("completed\n" + TEXT.format(total, success, failed, already))
  
async def get_configs(user_id):
  #configs = temp.CONFIGS.get(user_id)
  #if not configs:
  configs = await db.get_configs(user_id)
  #temp.CONFIGS[user_id] = configs 
  return configs
                          
async def update_configs(user_id, key, value):
  current = await db.get_configs(user_id)
  if key in ['caption', 'duplicate', 'db_uri', 'forward_tag', 'protect', 'file_size', 'size_limit', 'extension', 'keywords', 'button']:
     current[key] = value
  else: 
     current['filters'][key] = value
 # temp.CONFIGS[user_id] = value
  await db.update_configs(user_id, current)
    
def parse_buttons(text, markup=True):
    buttons = []
    for match in BTN_URL_REGEX.finditer(text):
        n_escapes = 0
        to_check = match.start(1) - 1
        while to_check > 0 and text[to_check] == "\\":
            n_escapes += 1
            to_check -= 1

        if n_escapes % 2 == 0:
            if bool(match.group(4)) and buttons:
                buttons[-1].append(InlineKeyboardButton(
                    text=match.group(2),
                    url=match.group(3).replace(" ", "")))
            else:
                buttons.append([InlineKeyboardButton(
                    text=match.group(2),
                    url=match.group(3).replace(" ", ""))])
    if markup and buttons:
       buttons = InlineKeyboardMarkup(buttons)
    return buttons if buttons else None
