import os
import sys
import math
import time
import random
import asyncio
import logging
import tempfile
import shutil
from .utils import STS
from database import db
from .test import CLIENT, start_clone_bot
from config import Config, temp
from translation import Translation
from pyrogram import Client, filters
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
    try:
      _bot, caption, forward_tag, data, protect, button = await sts.get_data(user)
    except Exception as e:
      return await msg_edit(m, f"<b>DB error:</b>\n<code>{e}</code>", wait=True)
    if not _bot:
      return await msg_edit(m, "<code>You didn't added any bot. Please add a bot using /settings !</code>", wait=True)

    await msg_edit(m, "<code>Starting client… (max 45s)</code>")
    try:
      client = await start_clone_bot(CLIENT.client(_bot))
    except Exception as e:
      err = str(e)
      print(f"Client start failed for user {user}: {err}")
      return await msg_edit(
        m,
        f"<b>Failed to start bot/userbot:</b>\n<code>{err}</code>\n\n"
        f"<b>Fix:</b>\n"
        f"• /settings → remove bot/userbot → add again\n"
        f"• Userbot: generate a fresh session string\n"
        f"• Check API_ID / API_HASH on Render\n"
        f"• Then send /unlock and retry",
        retry_btn(frwd_id),
        True,
      )

    await msg_edit(m, "<code>Checking source chat…</code>")
    try:
      await asyncio.wait_for(client.get_messages(sts.get("FROM"), 1), timeout=30)
    except Exception as e:
      print(f"Source check failed: {e}")
      await msg_edit(
        m,
        f"**Source chat may be private / restricted.**\n"
        f"Use a userbot (must be a member) or make your "
        f"[Bot](t.me/{_bot.get('username', '')}) admin there.\n\n"
        f"<code>{e}</code>",
        retry_btn(frwd_id),
        True,
      )
      return await stop(client, user)

    await msg_edit(m, "<code>Checking target chat…</code>")
    try:
      k = await asyncio.wait_for(client.send_message(i.TO, "Testing"), timeout=30)
      await k.delete()
    except Exception as e:
      print(f"Target check failed: {e}")
      await msg_edit(
        m,
        f"**Make your [UserBot / Bot](t.me/{_bot.get('username', '')}) admin in the target chat** "
        f"with post permission.\n\n<code>{e}</code>",
        retry_btn(frwd_id),
        True,
      )
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

    # Parallel copy workers.
    # Restricted channels need download+reupload (heavy on disk/RAM) — keep concurrency low
    # so Render doesn't OOM / hang. Fast path can be a bit higher.
    copy_concurrency = 2 if is_bot_account else 1
    if safe_mode:
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

          # Always try to auto-skip files already in the target.
          # If user disabled duplicate in settings, still index when possible.
          dup_uri = None
          target_for_dup = sts.get('TO')
          if skip_duplicate:
              dup_uri, target_for_dup = skip_duplicate
          else:
              # Enable in-memory/db skip for this run using bot's default DB
              skip_duplicate = [None, sts.get('TO')]
              dup_uri, target_for_dup = skip_duplicate

          await msg_edit(m, "<code>Scanning target for already-uploaded files…</code>")
          await index_target_chat(
              client, target_for_dup, dup_uri, status_msg=m, sts=sts, max_scan=10000
          )
          await edit(m, 'Progressing', 10, sts)

          # Semaphore for parallel copy mode
          sem = asyncio.Semaphore(copy_concurrency)
          pending_tasks = set()

          async def _bounded_copy(details):
              async with sem:
                  ok = await copy(client, details, m, sts, user)
                  if ok is not False:
                      sts.add('total_files')
                      # Mark as forwarded only after success
                      fid = details.get("file_unique_id")
                      if fid:
                          try:
                              await db.mark_file_forwarded(fid, target_for_dup, dup_uri)
                          except Exception:
                              pass
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
                    from .test import get_file_unique_id as _gfui
                    details = {
                        "msg_id": message.id,
                        "media": media(message),
                        "caption": new_caption,
                        "button": button,
                        "protect": protect,
                        "file_unique_id": getattr(message, "_fwd_file_unique_id", None) or _gfui(message),
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
            
# Timeouts (seconds) so a stalled Telegram transfer can't freeze the whole job
DOWNLOAD_TIMEOUT = 600   # 10 min
UPLOAD_TIMEOUT = 600     # 10 min


async def index_target_chat(client, target_chat, dup_uri, status_msg=None, sts=None, max_scan=10000):
    """
    Fast scan of target chat for media already present.
    - Minimal UI updates (time-based, not per-N messages)
    - Large bulk DB writes
    - Overlaps next Telegram fetch with DB write
    Returns number of messages scanned.
    """
    from .test import get_file_unique_id

    FLUSH_AT = 1500          # media ids per bulk write
    UI_EVERY_SECS = 3.0      # don't spam Telegram with edit()
    collected = []
    scanned = 0
    media_total = 0
    pending_writes = set()
    last_ui = 0.0

    async def _flush(batch):
        if not batch:
            return
        try:
            await db.mark_files_bulk(batch, target_chat, dup_uri)
        except Exception as e:
            print(f"[index] bulk write error: {e}")

    def _schedule_flush():
        nonlocal collected, media_total
        if not collected:
            return
        batch = collected
        collected = []
        media_total += len(batch)
        task = asyncio.create_task(_flush(batch))
        pending_writes.add(task)
        task.add_done_callback(pending_writes.discard)

    try:
        if status_msg and sts:
            try:
                await edit(status_msg, "Indexing target…", 10, sts)
            except Exception:
                pass
        print(f"Indexing target {target_chat} (max {max_scan})…")
        t0 = time.time()

        async for msg in client.get_chat_history(target_chat, limit=max_scan):
            scanned += 1
            # Fast path: only touch media messages
            if msg.media:
                uid = get_file_unique_id(msg)
                if uid:
                    collected.append(uid)
                    if len(collected) >= FLUSH_AT:
                        _schedule_flush()

            # UI / log at most every UI_EVERY_SECS
            now = time.time()
            if now - last_ui >= UI_EVERY_SECS:
                last_ui = now
                rate = scanned / max(now - t0, 0.1)
                print(f"  index: {scanned} msgs ({rate:.0f}/s), media buffered={len(collected)}")
                if status_msg and sts:
                    try:
                        await edit(status_msg, f"Indexing {scanned} ({rate:.0f}/s)", 10, sts)
                    except Exception:
                        pass

            # Bound pending DB tasks so we don't queue forever
            if len(pending_writes) >= 3:
                done, pending_writes = await asyncio.wait(
                    pending_writes, return_when=asyncio.FIRST_COMPLETED
                )
                pending_writes = set(pending_writes)

        _schedule_flush()
        if pending_writes:
            await asyncio.gather(*pending_writes, return_exceptions=True)

        elapsed = time.time() - t0
        print(
            f"Target index done: scanned={scanned} media≈{media_total} "
            f"in {elapsed:.1f}s ({scanned / max(elapsed, 0.1):.0f} msg/s)"
        )
        return scanned
    except Exception as e:
        print(f"Target indexing failed (continuing): {e}")
        _schedule_flush()
        if pending_writes:
            await asyncio.gather(*pending_writes, return_exceptions=True)
        return scanned


async def _download_and_reupload(bot, msg, sts, m=None):
    """
    Fallback for restricted channels (CHAT_FORWARDS_RESTRICTED).
    Downloads media with the userbot and re-uploads it to the target chat.
    Hard timeouts prevent silent hangs (common cause of "forwarding stopped").
    """
    msg_id = msg.get("msg_id")
    caption = msg.get("caption")
    button = msg.get("button")
    protect = msg.get("protect", False)
    from_chat = sts.get("FROM")
    to_chat = sts.get("TO")

    try:
        original = await asyncio.wait_for(
            bot.get_messages(from_chat, msg_id), timeout=60
        )
    except Exception as e:
        print(f"Could not re-fetch message {msg_id}: {e}")
        raise

    if original.empty or original.service:
        raise ValueError("Message is empty or service message")

    if not original.media:
        text_body = caption if caption is not None else (original.text.html if original.text else "")
        if text_body:
            await bot.send_message(
                chat_id=to_chat,
                text=text_body,
                reply_markup=button,
                protect_content=protect,
                disable_web_page_preview=True,
            )
        return

    last_pct = {"value": -1}
    last_update = {"ts": 0.0}
    phase = {"name": "↓"}

    async def _progress(current, total):
        if not total:
            return
        now = time.time()
        pct = int(current * 100 / total) if total else 0
        if pct < last_pct["value"] + 5 and (now - last_update["ts"]) < 2.0 and pct < 100:
            return
        last_pct["value"] = pct
        last_update["ts"] = now
        try:
            size_mb = total / (1024 * 1024)
            cur_mb = current / (1024 * 1024)
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            label = "Downloading" if phase["name"] == "↓" else "Uploading"
            if m:
                await edit(m, f"{label} {pct}%", 10, sts)
            print(f"  {phase['name']} msg {msg_id}: {cur_mb:.1f}/{size_mb:.1f} MB ({pct}%) [{bar}]")
        except Exception:
            pass

    tmp_dir = tempfile.mkdtemp(prefix="fwd_")
    file_path = None
    try:
        if m:
            try:
                await edit(m, "Downloading…", 10, sts)
            except Exception:
                pass

        phase["name"] = "↓"
        try:
            file_path = await asyncio.wait_for(
                bot.download_media(
                    original,
                    file_name=os.path.join(tmp_dir, ""),
                    progress=_progress,
                ),
                timeout=DOWNLOAD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Download timed out after {DOWNLOAD_TIMEOUT}s for msg {msg_id}"
            )

        if not file_path or not os.path.exists(file_path):
            raise FileNotFoundError("download_media returned no file")

        send_caption = caption
        if send_caption is None and original.caption:
            send_caption = original.caption.html

        media_type = original.media.value if original.media else None
        kwargs = {
            "chat_id": to_chat,
            "caption": send_caption,
            "reply_markup": button,
            "protect_content": protect,
            "progress": _progress,
        }

        if m:
            try:
                await edit(m, "Uploading…", 10, sts)
            except Exception:
                pass
        phase["name"] = "↑"
        last_pct["value"] = -1
        print(f"  ↑ msg {msg_id}: uploading ({media_type})…")

        async def _do_upload():
            if media_type == "photo":
                await bot.send_photo(photo=file_path, **kwargs)
            elif media_type == "video":
                await bot.send_video(video=file_path, **kwargs)
            elif media_type == "animation":
                await bot.send_animation(animation=file_path, **kwargs)
            elif media_type == "audio":
                await bot.send_audio(audio=file_path, **kwargs)
            elif media_type == "voice":
                await bot.send_voice(voice=file_path, **kwargs)
            elif media_type == "video_note":
                await bot.send_video_note(
                    video_note=file_path, chat_id=to_chat, protect_content=protect
                )
            elif media_type == "sticker":
                await bot.send_sticker(
                    sticker=file_path, chat_id=to_chat, protect_content=protect
                )
            else:
                await bot.send_document(document=file_path, **kwargs)

        try:
            await asyncio.wait_for(_do_upload(), timeout=UPLOAD_TIMEOUT)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Upload timed out after {UPLOAD_TIMEOUT}s for msg {msg_id}"
            )

        print(f"  ✓ msg {msg_id}: done ({media_type})")
        if m:
            try:
                await edit(m, "Progressing", 10, sts)
            except Exception:
                pass

    finally:
        try:
            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def _is_network_error(err: str) -> bool:
    """Return True for typical network / timeout style errors."""
    keys = (
        "timeout", "timed out", "connection", "network", "internal server",
        "writeerror", "readerror", "server closed", "broken pipe",
        "connection reset", "connection aborted", "temporarily unavailable",
        "name or service not known", "temporary failure", "ssl",
        "httpx", "aiohttp", "clientconnector", "server disconnected",
        "oserror", "errno 104", "errno 110", "errno 111",
    )
    return any(k in err for k in keys)


async def copy(bot, msg, m, sts, user_id=None, _retries=0):
    """
    Copy / send one message.
    Returns True on success, False on permanent failure.
    1) Fast path: send_cached_media / copy_message (with timeout)
    2) On CHAT_FORWARDS_RESTRICTED → download + re-upload
    3) Retries on FloodWait and network/timeout errors (with backoff)
    """
    MAX_RETRIES = 6
    FAST_TIMEOUT = 120  # seconds for non-download copy path
    try:
        if msg.get("media") and msg.get("caption") is not None:
            await asyncio.wait_for(
                bot.send_cached_media(
                    chat_id=sts.get('TO'),
                    file_id=msg.get("media"),
                    caption=msg.get("caption"),
                    reply_markup=msg.get('button'),
                    protect_content=msg.get("protect"),
                ),
                timeout=FAST_TIMEOUT,
            )
        else:
            await asyncio.wait_for(
                bot.copy_message(
                    chat_id=sts.get('TO'),
                    from_chat_id=sts.get('FROM'),
                    caption=msg.get("caption"),
                    message_id=msg.get("msg_id"),
                    reply_markup=msg.get('button'),
                    protect_content=msg.get("protect"),
                ),
                timeout=FAST_TIMEOUT,
            )
        return True
    except asyncio.TimeoutError:
        print(f"Fast-path copy timed out msg={msg.get('msg_id')} (try {_retries+1})")
        if _retries < MAX_RETRIES:
            await asyncio.sleep(2 + _retries)
            return await copy(bot, msg, m, sts, user_id, _retries + 1)
        sts.add('deleted')
        return False
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
        return False
    except Exception as e:
        err = str(e).lower()

        # Restricted-content channels – fall back to download + re-upload
        if "chat_forwards_restricted" in err or "chat_send_media_forbidden" in err:
            try:
                print(f"Restricted content detected for msg {msg.get('msg_id')} – downloading & re-uploading…")
                await _download_and_reupload(bot, msg, sts, m=m)
                return True
            except FloodWait as fw:
                if user_id is not None:
                    note_flood(user_id)
                wait = fw.value + random.uniform(1.0, 3.0)
                await edit(m, 'Progressing', int(wait), sts)
                await asyncio.sleep(wait)
                if _retries < MAX_RETRIES:
                    return await copy(bot, msg, m, sts, user_id, _retries + 1)
                print(f"download/reupload gave up after FloodWaits msg={msg.get('msg_id')}")
                sts.add('deleted')
                return False
            except Exception as dl_err:
                dl_err_s = str(dl_err).lower()
                if _is_network_error(dl_err_s) and _retries < MAX_RETRIES:
                    backoff = min(2 ** _retries + random.uniform(0.5, 2.0), 45)
                    print(f"Network error on download/reupload msg={msg.get('msg_id')} "
                          f"(try {_retries+1}/{MAX_RETRIES}), retry in {backoff:.1f}s: {dl_err}")
                    try:
                        await edit(m, f"Network retry {_retries+1}", int(backoff), sts)
                    except Exception:
                        pass
                    await asyncio.sleep(backoff)
                    return await copy(bot, msg, m, sts, user_id, _retries + 1)
                print(f"Failed to download/reupload message {msg.get('msg_id')}: {dl_err}")
                sts.add('deleted')
                return False

        # Network / timeout style errors on the fast path
        if _is_network_error(err) and _retries < MAX_RETRIES:
            backoff = min(2 ** _retries + random.uniform(0.5, 2.0), 45)
            print(f"Network/timeout on copy msg={msg.get('msg_id')} "
                  f"(try {_retries+1}/{MAX_RETRIES}), retry in {backoff:.1f}s: {e}")
            try:
                await edit(m, f"Network retry {_retries+1}", int(backoff), sts)
            except Exception:
                pass
            await asyncio.sleep(backoff)
            return await copy(bot, msg, m, sts, user_id, _retries + 1)

        print(f"Failed to copy message {msg.get('msg_id')}: {e}")
        sts.add('deleted')
        return False


async def forward(bot, msg, m, sts, protect, user_id=None, _retries=0):
    """
    Forward a batch of message IDs.
    On CHAT_FORWARDS_RESTRICTED falls back to per-message copy
    (which itself can download + re-upload).
    Retries network/timeout errors with exponential backoff.
    """
    MAX_RETRIES = 6
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

        # Restricted channel → fall back to copy (download+reupload) path
        if "chat_forwards_restricted" in err or "chat_send_media_forbidden" in err:
            print(f"Restricted content on forward batch – switching to copy mode for ids={msg}")
            ids = msg if isinstance(msg, list) else [msg]
            for mid in ids:
                details = {
                    "msg_id": mid,
                    "media": None,
                    "caption": None,
                    "button": None,
                    "protect": protect,
                }
                await copy(bot, details, m, sts, user_id)
            return

        # Network / timeout → exponential backoff retry
        if _is_network_error(err) and _retries < MAX_RETRIES:
            backoff = min(2 ** _retries + random.uniform(0.5, 2.0), 45)
            print(f"Network/timeout on forward ids={msg} "
                  f"(try {_retries+1}/{MAX_RETRIES}), retry in {backoff:.1f}s: {e}")
            try:
                await edit(m, f"Network retry {_retries+1}", int(backoff), sts)
            except Exception:
                pass
            await asyncio.sleep(backoff)
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


@Client.on_message(filters.private & filters.command(["unlock", "forceunlock"]))
async def force_unlock(bot, message):
    """Clear a stuck lock so /fwd can start again without restarting the service."""
    user_id = message.from_user.id
    was_locked = bool(temp.lock.get(user_id))
    temp.lock[user_id] = False
    temp.CANCEL[user_id] = True
    # Also free any target chat marked as busy
    try:
        # Remove all entries belonging to this user is hard; clear the list if only one task
        if hasattr(temp, "IS_FRWD_CHAT") and temp.IS_FRWD_CHAT:
            temp.IS_FRWD_CHAT.clear()
    except Exception:
        pass
    if was_locked:
        await message.reply(
            "<b>✅ Lock cleared.</b>\nYou can start a new <code>/fwd</code> now."
        )
    else:
        await message.reply(
            "<b>No active lock found.</b>\nYou can use <code>/fwd</code> normally."
        )
          
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
