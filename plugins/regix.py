import os
import sys 
import math
import time
import random
import asyncio 
import logging
from .utils import STS
from database import db 
from .test import CLIENT , start_clone_bot
from config import Config, temp
from translation import Translation
from pyrogram import Client, filters 
#from pyropatch.utils import unpack_new_file_id
from pyrogram.errors import FloodWait, MessageNotModified, RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message 

CLIENT = CLIENT()
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
TEXT = Translation.TEXT

# ---------- Rate-limit helpers (fast but ban-safe) ----------
_FLOOD_STREAK = {}   # user_id -> consecutive flood count
_LAST_FLOOD = {}     # user_id -> timestamp of last FloodWait

def _jitter(base: float, pct: float = 0.25) -> float:
    """Return base ± pct random jitter (floor 0.35s)."""
    spread = base * pct
    return max(0.35, base + random.uniform(-spread, spread))

async def smart_sleep(user_id: int, base_delay: float, safe_mode: bool, m=None, sts=None):
    """
    Sleep with light jitter. Adaptive slowdown only after real FloodWaits.
    Occasional longer pauses only in safe_mode for userbots.
    """
    delay = float(base_delay)
    streak = _FLOOD_STREAK.get(user_id, 0)

    if streak > 0:
        # Grow delay after floods, but cap the multiplier
        delay *= min(1.0 + (streak * 0.4), 3.0)

    if safe_mode and streak == 0:
        # Light anti-pattern rest every ~60 successful forwards (userbots mainly)
        total = sts.get('total_files') if sts else 0
        if total and total % 60 == 0 and total > 0:
            delay += random.uniform(6, 14)
            if m and sts:
                await edit(m, 'Progressing', int(delay), sts)

    delay = _jitter(delay, 0.30 if safe_mode else 0.15)
    await asyncio.sleep(delay)

def note_flood(user_id: int):
    _FLOOD_STREAK[user_id] = _FLOOD_STREAK.get(user_id, 0) + 1
    _LAST_FLOOD[user_id] = time.time()

def clear_flood_streak(user_id: int):
    last = _LAST_FLOOD.get(user_id, 0)
    if time.time() - last > 90:          # decay faster → recover speed sooner
        _FLOOD_STREAK[user_id] = 0

@Client.on_callback_query(filters.regex(r'^start_public'))
async def pub_(bot, message):
    user = message.from_user.id
    temp.CANCEL[user] = False
    frwd_id = message.data.split("_")[2]
    if temp.lock.get(user) and str(temp.lock.get(user))=="True":
      return await message.answer("please wait until previous task complete", show_alert=True)
    sts = STS(frwd_id)
    if not sts.verify():
      await message.answer("your are clicking on my old button", show_alert=True)
      return await message.message.delete()
    i = sts.get(full=True)
    if i.TO in temp.IS_FRWD_CHAT:
      return await message.answer("In Target chat a task is progressing. please wait until task complete", show_alert=True)
    m = await msg_edit(message.message, "<code>verifying your data's, please wait.</code>")
    _bot, caption, forward_tag, data, protect, button = await sts.get_data(user)
    if not _bot:
      return await msg_edit(m, "<code>You didn't added any bot. Please add a bot using /settings !</code>", wait=True)
    try:
      client = await start_clone_bot(CLIENT.client(_bot))
    except Exception as e:  
      return await m.edit(e)
    await msg_edit(m, "<code>processing..</code>")
    try: 
       # Just check if we can access messages. If continuous, limit might be huge.
       await client.get_messages(sts.get("FROM"), 1)
    except:
       await msg_edit(m, f"**Source chat may be a private channel / group. Use userbot (user must be member over there) or  if Make Your [Bot](t.me/{_bot['username']}) an admin over there**", retry_btn(frwd_id), True)
       return await stop(client, user)
    try:
       k = await client.send_message(i.TO, "Testing")
       await k.delete()
    except:
       await msg_edit(m, f"**Please Make Your [UserBot / Bot](t.me/{_bot['username']}) Admin In Target Channel With Full Permissions**", retry_btn(frwd_id), True)
       return await stop(client, user)
    temp.forwardings += 1
    await db.add_frwd(user)
    await send(client, user, "<b>ғᴏʀᴡᴀʀᴅɪɴɢ sᴛᴀʀᴛᴇᴅ <a href=https://t.me/dev_gagan>Dev Gagan</a></b>")
    sts.add(time=True)

    # ----- Rate-limit settings (fast defaults, still ban-safe) -----
    user_delay = data.get('delay') if isinstance(data, dict) else None
    safe_mode = data.get('safe_mode', True) if isinstance(data, dict) else True
    batch_size = data.get('batch_size', 25) if isinstance(data, dict) else 25
    try:
        batch_size = max(5, min(int(batch_size), 80))
    except Exception:
        batch_size = 25

    is_bot_account = bool(_bot.get('is_bot'))

    if user_delay is not None:
        try:
            base_sleep = max(0.5, float(user_delay))
        except Exception:
            base_sleep = 1.2 if is_bot_account else 6
    else:
        # Bots can be aggressive; userbots need breathing room
        base_sleep = 1.2 if is_bot_account else 6

    if safe_mode and not is_bot_account:
        # Userbot + safe mode floor
        base_sleep = max(base_sleep, 4)

    # Parallel copy workers (only for non-forward_tag / copy mode)
    # Bots: 4 concurrent, userbots: 2 concurrent → big speedup, low flood risk
    copy_concurrency = 4 if is_bot_account else 2
    if safe_mode and not is_bot_account:
        copy_concurrency = 1

    await msg_edit(
        m,
        f"<code>Processing… delay≈{base_sleep}s  safe={'ON' if safe_mode else 'OFF'}  "
        f"batch={batch_size}  workers={copy_concurrency if not forward_tag else 1}</code>"
    )
    temp.IS_FRWD_CHAT.append(i.TO)
    temp.lock[user] = locked = True
    _FLOOD_STREAK[user] = 0

    if locked:
        try:
          MSG = []
          pling = 0
          await edit(m, 'Progressing', 10, sts)
          print(
              f"Starting Forwarding… From:{sts.get('FROM')} To:{sts.get('TO')} "
              f"Total:{sts.get('limit')} skip:{sts.get('skip')} "
              f"delay={base_sleep}s safe={safe_mode} batch={batch_size}"
          )

          is_continuous = getattr(sts, 'continuous', False)
          skip_duplicate = data.get('skip_duplicate', False) if isinstance(data, dict) else False
          disabled_types = data.get('filters', []) if isinstance(data, dict) else []

          # Semaphore for parallel copy mode
          sem = asyncio.Semaphore(copy_concurrency)
          pending_tasks = set()

          async def _bounded_copy(details):
              async with sem:
                  await copy(client, details, m, sts, user)
                  sts.add('total_files')
                  await smart_sleep(user, base_sleep, safe_mode, m, sts)

          async for message in client.iter_messages(
              client,
              chat_id=sts.get('FROM'),
              limit=int(sts.get('limit')),
              offset=int(sts.get('skip')) if sts.get('skip') else 0,
              continuous=is_continuous,
              skip_duplicate=skip_duplicate
          ):
                if await is_cancelled(client, user, m, sts):
                    # drain pending
                    if pending_tasks:
                        await asyncio.gather(*pending_tasks, return_exceptions=True)
                    return

                pling += 1
                if pling % 25 == 0:
                    await edit(m, 'Progressing', 10, sts)

                sts.add('fetched')
                clear_flood_streak(user)

                if message == "DUPLICATE":
                    sts.add('duplicate')
                    continue
                if message == "FILTERED":
                    sts.add('filtered')
                    continue
                if getattr(message, 'empty', False) or getattr(message, 'service', False):
                    sts.add('deleted')
                    continue

                msg_type = message.media.value if message.media else "text"
                if msg_type in disabled_types:
                    sts.add('filtered')
                    continue

                if forward_tag:
                    MSG.append(message.id)
                    if len(MSG) >= batch_size:
                        await forward(client, MSG, m, sts, protect, user)
                        sts.add('total_files', len(MSG))
                        # Sleep once per batch (much faster than per-message)
                        await smart_sleep(user, base_sleep * 1.5, safe_mode, m, sts)
                        MSG = []
                else:
                    new_caption = custom_caption(message, caption)
                    details = {
                        "msg_id": message.id,
                        "media": media(message),
                        "caption": new_caption,
                        "button": button,
                        "protect": protect,
                    }
                    if copy_concurrency <= 1:
                        await _bounded_copy(details)
                    else:
                        task = asyncio.create_task(_bounded_copy(details))
                        pending_tasks.add(task)
                        task.add_done_callback(pending_tasks.discard)
                        # Soft back-pressure so we don't queue thousands
                        if len(pending_tasks) >= copy_concurrency * 3:
                            done, pending_tasks = await asyncio.wait(
                                pending_tasks, return_when=asyncio.FIRST_COMPLETED
                            )
                            pending_tasks = set(pending_tasks)

          # Flush remaining forward batch
          if forward_tag and MSG:
              await forward(client, MSG, m, sts, protect, user)
              sts.add('total_files', len(MSG))

          # Wait for any leftover parallel copies
          if pending_tasks:
              await asyncio.gather(*pending_tasks, return_exceptions=True)

        except Exception as e:
            await msg_edit(m, f'<b>ERROR:</b>\n<code>{e}</code>', wait=True)
            try:
                temp.IS_FRWD_CHAT.remove(sts.TO)
            except ValueError:
                pass
            return await stop(client, user)

        try:
            temp.IS_FRWD_CHAT.remove(sts.TO)
        except ValueError:
            pass
        await send(client, user, "<b>🎉 ғᴏʀᴡᴀᴅɪɴɢ ᴄᴏᴍᴘʟᴇᴛᴇᴅ 🥀 <a href=https://t.me/dev_gagan>SUPPORT</a>🥀</b>")
        await edit(m, 'Completed', "completed", sts)
        await stop(client, user)
            
async def copy(bot, msg, m, sts, user_id=None, _retries=0):
    """Copy / send one message. Retries on FloodWait and a few transient errors."""
    MAX_RETRIES = 4
    try:
        if msg.get("media") and msg.get("caption") is not None:
            await bot.send_cached_media(
                chat_id=sts.get('TO'),
                file_id=msg.get("media"),
                caption=msg.get("caption"),
                reply_markup=msg.get('button'),
                protect_content=msg.get("protect"),
            )
        else:
            await bot.copy_message(
                chat_id=sts.get('TO'),
                from_chat_id=sts.get('FROM'),
                caption=msg.get("caption"),
                message_id=msg.get("msg_id"),
                reply_markup=msg.get('button'),
                protect_content=msg.get("protect"),
            )
    except FloodWait as e:
        if user_id is not None:
            note_flood(user_id)
        wait = e.value + random.uniform(1.0, 3.0)
        await edit(m, 'Progressing', int(wait), sts)
        await asyncio.sleep(wait)
        await edit(m, 'Progressing', 10, sts)
        if _retries < MAX_RETRIES:
            return await copy(bot, msg, m, sts, user_id, _retries + 1)
        print(f"copy gave up after FloodWaits msg={msg.get('msg_id')}")
        sts.add('deleted')
    except Exception as e:
        err = str(e).lower()
        # Retry a couple of times on transient network / timeout style errors
        transient = any(x in err for x in (
            "timeout", "timed out", "connection", "network", "internal server",
            "writeerror", "readerror", "server closed"
        ))
        if transient and _retries < MAX_RETRIES:
            await asyncio.sleep(1.5 + _retries)
            return await copy(bot, msg, m, sts, user_id, _retries + 1)
        print(f"Failed to copy message {msg.get('msg_id')}: {e}")
        sts.add('deleted')


async def forward(bot, msg, m, sts, protect, user_id=None, _retries=0):
    """Forward a batch of message IDs. Retries on FloodWait."""
    MAX_RETRIES = 4
    try:
        await bot.forward_messages(
            chat_id=sts.get('TO'),
            from_chat_id=sts.get('FROM'),
            protect_content=protect,
            message_ids=msg,
        )
    except FloodWait as e:
        if user_id is not None:
            note_flood(user_id)
        wait = e.value + random.uniform(1.0, 3.0)
        await edit(m, 'Progressing', int(wait), sts)
        await asyncio.sleep(wait)
        await edit(m, 'Progressing', 10, sts)
        if _retries < MAX_RETRIES:
            return await forward(bot, msg, m, sts, protect, user_id, _retries + 1)
        print(f"forward gave up after FloodWaits ids={msg}")
        sts.add('deleted', len(msg) if isinstance(msg, list) else 1)
    except Exception as e:
        err = str(e).lower()
        transient = any(x in err for x in (
            "timeout", "timed out", "connection", "network", "internal server"
        ))
        if transient and _retries < MAX_RETRIES:
            await asyncio.sleep(1.5 + _retries)
            return await forward(bot, msg, m, sts, protect, user_id, _retries + 1)
        print(f"Failed to forward messages {msg}: {e}")
        # Don't mark the whole batch as deleted on partial failure —
        # try one-by-one as last resort
        if isinstance(msg, list) and len(msg) > 1 and _retries == 0:
            for mid in msg:
                await forward(bot, [mid], m, sts, protect, user_id, _retries=1)
        else:
            sts.add('deleted', len(msg) if isinstance(msg, list) else 1)

PROGRESS = """
📈 Percetage: {0} %

♻️ Feched: {1}

♻️ Fowarded: {2}

♻️ Remaining: {3}

♻️ Stataus: {4}

⏳️ ETA: {5}
"""

async def msg_edit(msg, text, button=None, wait=None):
    try:
        return await msg.edit(text, reply_markup=button)
    except MessageNotModified:
        pass 
    except FloodWait as e:
        if wait:
           await asyncio.sleep(e.value)
           return await msg_edit(msg, text, button, wait)
        
async def edit(msg, title, status, sts):
   i = sts.get(full=True)
   status = 'Forwarding' if status == 10 else f"Sleeping {status} s" if str(status).isnumeric() else status
   # Handle division by zero if total is 0 (which happens if infinite/continuous without known total)
   total = float(i.total) if float(i.total) > 0 else 1.0
   percentage = "{:.0f}".format(float(i.fetched)*100/total)
   
   now = time.time()
   diff = int(now - i.start)
   speed = sts.divide(i.fetched, diff)
   elapsed_time = round(diff) * 1000
   time_to_completion = round(sts.divide(i.total - i.fetched, int(speed))) * 1000
   estimated_total_time = elapsed_time + time_to_completion  
   progress = "◉{0}{1}".format(
       ''.join(["◉" for i in range(math.floor(int(percentage) / 10))]),
       ''.join(["◎" for i in range(10 - math.floor(int(percentage) / 10))]))
   button =  [[InlineKeyboardButton(title, f'fwrdstatus#{status}#{estimated_total_time}#{percentage}#{i.id}')]]
   estimated_total_time = TimeFormatter(milliseconds=estimated_total_time)
   estimated_total_time = estimated_total_time if estimated_total_time != '' else '0 s'

   text = TEXT.format(i.fetched, i.total_files, i.duplicate, i.deleted, i.skip, status, percentage, estimated_total_time, progress)
   if status in ["cancelled", "completed"]:
      button.append(
         [InlineKeyboardButton('Support', url='https://t.me/dev_gagan'),
         InlineKeyboardButton('Updates', url='https://t.me/dev_gagan')]
         )
   else:
      button.append([InlineKeyboardButton('• ᴄᴀɴᴄᴇʟ', 'terminate_frwd')])
   await msg_edit(msg, text, InlineKeyboardMarkup(button))
   
async def is_cancelled(client, user, msg, sts):
   if temp.CANCEL.get(user)==True:
      temp.IS_FRWD_CHAT.remove(sts.TO)
      await edit(msg, "Cancelled", "completed", sts)
      await send(client, user, "<b>❌ Forwarding Process Cancelled</b>")
      await stop(client, user)
      return True 
   return False 

async def stop(client, user):
   try:
     await client.stop()
   except:
     pass 
   await db.rmve_frwd(user)
   temp.forwardings -= 1
   temp.lock[user] = False 
    
async def send(bot, user, text):
   try:
      await bot.send_message(user, text=text)
   except:
      pass 
     
def custom_caption(msg, caption):
  if msg.media:
    if (msg.video or msg.document or msg.audio or msg.photo):
      media = getattr(msg, msg.media.value, None)
      if media:
        file_name = getattr(media, 'file_name', '')
        file_size = getattr(media, 'file_size', '')
        fcaption = getattr(msg, 'caption', '')
        if fcaption:
          fcaption = fcaption.html
        if caption:
          return caption.format(filename=file_name, size=get_size(file_size), caption=fcaption)
        return fcaption
  return None

def get_size(size):
  units = ["Bytes", "KB", "MB", "GB", "TB", "PB", "EB"]
  size = float(size)
  i = 0
  while size >= 1024.0 and i < len(units):
     i += 1
     size /= 1024.0
  return "%.2f %s" % (size, units[i]) 

def media(msg):
  if msg.media:
     media = getattr(msg, msg.media.value, None)
     if media:
        return getattr(media, 'file_id', None)
  return None 

def TimeFormatter(milliseconds: int) -> str:
    seconds, milliseconds = divmod(int(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    tmp = ((str(days) + "d, ") if days else "") + \
        ((str(hours) + "h, ") if hours else "") + \
        ((str(minutes) + "m, ") if minutes else "") + \
        ((str(seconds) + "s, ") if seconds else "") + \
        ((str(milliseconds) + "ms, ") if milliseconds else "")
    return tmp[:-2]

def retry_btn(id):
    return InlineKeyboardMarkup([[InlineKeyboardButton('♻️ RETRY ♻️', f"start_public_{id}")]])

@Client.on_callback_query(filters.regex(r'^terminate_frwd$'))
async def terminate_frwding(bot, m):
    user_id = m.from_user.id 
    temp.lock[user_id] = False
    temp.CANCEL[user_id] = True 
    await m.answer("Forwarding cancelled !", show_alert=True)
          
@Client.on_callback_query(filters.regex(r'^fwrdstatus'))
async def status_msg(bot, msg):
    _, status, est_time, percentage, frwd_id = msg.data.split("#")
    sts = STS(frwd_id)
    if not sts.verify():
       fetched, forwarded, remaining = 0
    else:
       fetched, forwarded = sts.get('fetched'), sts.get('total_files')
       remaining = fetched - forwarded 
    est_time = TimeFormatter(milliseconds=est_time)
    est_time = est_time if (est_time != '' or status not in ['completed', 'cancelled']) else '0 s'
    return await msg.answer(PROGRESS.format(percentage, fetched, forwarded, remaining, status, est_time), show_alert=True)
                  
@Client.on_callback_query(filters.regex(r'^close_btn$'))
async def close(bot, update):
    await update.answer()
    await update.message.delete()
