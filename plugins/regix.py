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

# Target indexing is intentionally OFF by default. Duplicate checks are still
# performed against MongoDB for every source file, and successful forwards are
# recorded automatically. Set ENABLE_TARGET_INDEX=true on Render only when you
# explicitly want to bootstrap the duplicate database from existing target
# messages.
ENABLE_TARGET_INDEX = os.getenv("ENABLE_TARGET_INDEX", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
TARGET_INDEX_LIMIT = max(100, int(os.getenv("TARGET_INDEX_LIMIT", "3000")))

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

    if temp.lock.get(user) and str(temp.lock.get(user)) == "True":
        try:
            return await message.answer("please wait until previous task complete", show_alert=True)
        except Exception:
            return
    sts = STS(frwd_id)
    if not sts.verify():
        try:
            await message.answer("old button — run /fwd again", show_alert=True)
        except Exception:
            pass
        try:
            await message.message.delete()
        except Exception:
            pass
        return
    i = sts.get(full=True)
    if i.TO in temp.IS_FRWD_CHAT:
        try:
            return await message.answer(
                "Target chat busy — wait until current task finishes",
                show_alert=True,
            )
        except Exception:
            return

    # Answer callback so Telegram stops the loading spinner
    try:
        await message.answer("Starting…")
    except Exception:
        pass

    m = await msg_edit(message.message, "<code>① Loading settings from database…</code>")
    print(f"[fwd] user={user} step=get_data")
    try:
        _bot, caption, forward_tag, data, protect, button = await asyncio.wait_for(
            sts.get_data(user), timeout=20
        )
    except asyncio.TimeoutError:
        return await msg_edit(
            m,
            "<b>Database timeout (20s).</b>\nCheck <code>DATABASE</code> URI on Render.\nThen /unlock and /fwd again.",
            wait=True,
        )
    except Exception as e:
        print(f"[fwd] get_data error: {e}")
        return await msg_edit(m, f"<b>DB error:</b>\n<code>{e}</code>", wait=True)

    print(
        f"[fwd] settings loaded: bot={'yes' if _bot else 'no'} "
        f"duplicate={'on' if data.get('skip_duplicate') else 'off'} "
        f"delay={data.get('delay')} safe={data.get('safe_mode')}"
    )

    if not _bot:
        return await msg_edit(
            m,
            "<code>You didn't add any bot. Please add a bot using /settings !</code>",
            wait=True,
        )

    await msg_edit(
        m,
        f"<code>② Starting {'bot' if _bot.get('is_bot') else 'userbot'} client… (max 45s)</code>",
    )
    print(f"[fwd] user={user} step=start_client is_bot={_bot.get('is_bot')}")
    try:
        client = await start_clone_bot(CLIENT.client(_bot))
    except Exception as e:
        err = str(e)
        print(f"[fwd] Client start failed for user {user}: {err}")
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
    print(f"[fwd] user={user} step=client_ok")

    await msg_edit(m, "<code>③ Checking source chat…</code>")
    print(f"[fwd] user={user} step=check_source from={sts.get('FROM')}")
    try:
        await asyncio.wait_for(client.get_messages(sts.get("FROM"), 1), timeout=30)
    except Exception as e:
        print(f"[fwd] Source check failed: {e}")
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

    await msg_edit(m, "<code>④ Checking target chat…</code>")
    print(f"[fwd] user={user} step=check_target to={i.TO}")
    try:
        k = await asyncio.wait_for(client.send_message(i.TO, "Testing"), timeout=30)
        await k.delete()
    except Exception as e:
        print(f"[fwd] Target check failed: {e}")
        await msg_edit(
            m,
            f"**Make your [UserBot / Bot](t.me/{_bot.get('username', '')}) admin in the target chat** "
            f"with post permission.\n\n<code>{e}</code>",
            retry_btn(frwd_id),
            True,
        )
        return await stop(client, user)
    print(f"[fwd] user={user} step=checks_ok — starting forward")
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
    temp.lock[user] = True
    _FLOOD_STREAK[user] = 0
    fatal_error = None
    cancelled = False

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
        # Size limit: [max_mb, size_limit_flag]
        # size_limit_flag True  = "more than" (only forward files bigger than max_mb)
        # size_limit_flag False = "less than" (only forward files smaller than max_mb)
        media_size_cfg = data.get('media_size') if isinstance(data, dict) else None
        size_limit_mb = None
        size_limit_more_than = False
        if media_size_cfg and isinstance(media_size_cfg, (list, tuple)) and len(media_size_cfg) >= 2:
            try:
                size_limit_mb = float(media_size_cfg[0])
                size_limit_more_than = bool(media_size_cfg[1])
            except Exception:
                size_limit_mb = None
        if size_limit_mb:
            print(f"[fwd] size filter active: {'more than' if size_limit_more_than else 'less than'} {size_limit_mb} MB")

        # Always auto-skip files already in the target
        if skip_duplicate:
            dup_uri, target_for_dup = skip_duplicate
        else:
            skip_duplicate = [None, sts.get('TO')]
            dup_uri, target_for_dup = skip_duplicate

        if ENABLE_TARGET_INDEX:
            try:
                await msg_edit(
                    m,
                    f"<code>Scanning target for duplicates (max {TARGET_INDEX_LIMIT:,})…</code>"
                )
                print(
                    f"[fwd] target indexing ENABLED: chat={target_for_dup} "
                    f"limit={TARGET_INDEX_LIMIT}"
                )
                await index_target_chat(
                    client,
                    target_for_dup,
                    dup_uri,
                    status_msg=m,
                    sts=sts,
                    max_scan=TARGET_INDEX_LIMIT,
                )
            except Exception as idx_err:
                # Indexing is best-effort — never abort the whole job for it.
                print(f"Index step error (continuing): {idx_err}")
                try:
                    await msg_edit(
                        m,
                        f"<code>Index warning: {idx_err} — continuing…</code>"
                    )
                except Exception:
                    pass
        else:
            print("[fwd] target indexing disabled; using MongoDB duplicate history")
            await msg_edit(
                m,
                "<code>⑤ Duplicate index skipped — checking saved history while forwarding…</code>"
            )

        await edit(m, 'Progressing', 10, sts)

        sem = asyncio.Semaphore(copy_concurrency)
        pending_tasks = set()

        async def _bounded_copy(details):
            async with sem:
                try:
                    ok = await copy(client, details, m, sts, user)
                    if ok is not False:
                        sts.add('total_files')
                        fid = details.get("file_unique_id")
                        if fid:
                            try:
                                await db.mark_file_forwarded(fid, target_for_dup, dup_uri)
                            except Exception as mark_err:
                                print(f"mark_file_forwarded error: {mark_err}")
                except Exception as copy_err:
                    print(f"bounded_copy error msg={details.get('msg_id')}: {copy_err}")
                    sts.add('deleted')
                try:
                    await smart_sleep(user, base_sleep, safe_mode, m, sts)
                except Exception:
                    await asyncio.sleep(base_sleep)

        async for message in client.iter_messages(
            client,
            chat_id=sts.get('FROM'),
            limit=int(sts.get('limit')),
            offset=int(sts.get('skip')) if sts.get('skip') else 0,
            continuous=is_continuous,
            skip_duplicate=skip_duplicate
        ):
            try:
                if await is_cancelled(client, user, m, sts):
                    cancelled = True
                    if pending_tasks:
                        await asyncio.gather(*pending_tasks, return_exceptions=True)
                    break

                pling += 1
                if pling % 25 == 0:
                    try:
                        await edit(m, 'Progressing', 10, sts)
                    except Exception:
                        pass

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

                # --- Size limit filter (skip oversized / undersized files) ---
                if size_limit_mb is not None and message.media:
                    try:
                        media_obj = getattr(message, message.media.value, None)
                        fsize = getattr(media_obj, "file_size", None) if media_obj else None
                        if fsize:
                            fsize_mb = fsize / (1024 * 1024)
                            if size_limit_more_than:
                                # only forward files BIGGER than limit
                                if fsize_mb <= size_limit_mb:
                                    print(f"  skip msg {message.id}: {fsize_mb:.1f} MB <= {size_limit_mb} MB (more-than filter)")
                                    sts.add('filtered')
                                    continue
                            else:
                                # only forward files SMALLER than limit
                                if fsize_mb >= size_limit_mb:
                                    print(f"  skip msg {message.id}: {fsize_mb:.1f} MB >= {size_limit_mb} MB (less-than filter)")
                                    sts.add('filtered')
                                    continue
                    except Exception as size_err:
                        print(f"size check error msg={getattr(message, 'id', '?')}: {size_err}")

                if forward_tag:
                    MSG.append(message.id)
                    if len(MSG) >= batch_size:
                        try:
                            await forward(client, MSG, m, sts, protect, user)
                            sts.add('total_files', len(MSG))
                        except Exception as fwd_err:
                            print(f"batch forward error: {fwd_err}")
                            sts.add('deleted', len(MSG))
                        MSG = []
                        try:
                            await smart_sleep(user, base_sleep * 1.5, safe_mode, m, sts)
                        except Exception:
                            await asyncio.sleep(base_sleep)
                else:
                    try:
                        new_caption = custom_caption(message, caption)
                    except Exception:
                        new_caption = getattr(message, 'caption', None)
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
                        if len(pending_tasks) >= copy_concurrency * 3:
                            done, pending_tasks = await asyncio.wait(
                                pending_tasks, return_when=asyncio.FIRST_COMPLETED
                            )
                            pending_tasks = set(pending_tasks)

            except FloodWait as fw:
                note_flood(user)
                wait = fw.value + random.uniform(1.0, 3.0)
                print(f"FloodWait in main loop: sleeping {wait:.0f}s")
                try:
                    await edit(m, 'Progressing', int(wait), sts)
                except Exception:
                    pass
                await asyncio.sleep(wait)
            except Exception as loop_err:
                # Per-message errors must NOT kill the whole job
                print(f"Loop error on message (continuing): {loop_err}")
                try:
                    sts.add('deleted')
                except Exception:
                    pass
                await asyncio.sleep(1)

        if not cancelled:
            if forward_tag and MSG:
                try:
                    await forward(client, MSG, m, sts, protect, user)
                    sts.add('total_files', len(MSG))
                except Exception as fwd_err:
                    print(f"final batch forward error: {fwd_err}")

            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)

    except asyncio.CancelledError:
        cancelled = True
        print(f"Forward task cancelled for user {user}")
        raise
    except Exception as e:
        fatal_error = e
        print(f"FATAL forward error user={user}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # ALWAYS release lock + cleanup, no matter how we exit
        try:
            if i.TO in temp.IS_FRWD_CHAT:
                temp.IS_FRWD_CHAT.remove(i.TO)
        except Exception:
            try:
                temp.IS_FRWD_CHAT.clear()
            except Exception:
                pass
        temp.lock[user] = False
        temp.CANCEL[user] = False

        if fatal_error is not None:
            err_text = _format_error(fatal_error)
            try:
                await msg_edit(
                    m,
                    f"<b>❌ Forwarding stopped</b>\n\n{err_text}\n\n"
                    f"<i>Lock cleared. Send /fwd to retry.\n"
                    f"Already-sent files will be auto-skipped.</i>",
                    retry_btn(frwd_id),
                    True,
                )
            except Exception:
                pass
            try:
                await send(
                    client, user,
                    f"<b>❌ Forwarding error:</b>\n<code>{fatal_error}</code>\n"
                    f"Use /fwd to resume — duplicates are skipped automatically."
                )
            except Exception:
                pass
        elif cancelled:
            try:
                await edit(m, "Cancelled", "completed", sts)
                await send(client, user, "<b>❌ Forwarding Process Cancelled</b>")
            except Exception:
                pass
        else:
            try:
                await send(
                    client, user,
                    "<b>🎉 ғᴏʀᴡᴀᴅɪɴɢ ᴄᴏᴍᴘʟᴇᴛᴇᴅ 🥀 "
                    "<a href=https://t.me/dev_gagan>SUPPORT</a>🥀</b>"
                )
                await edit(m, 'Completed', "completed", sts)
            except Exception as done_err:
                print(f"Completion notify error: {done_err}")

        try:
            await stop(client, user)
        except Exception as stop_err:
            print(f"stop() error: {stop_err}")
            temp.lock[user] = False


def _format_error(e: Exception) -> str:
    """User-friendly error message from an exception."""
    msg = str(e)
    low = msg.lower()
    if "flood" in low:
        return (
            f"<b>FloodWait / rate limit</b>\n<code>{msg}</code>\n"
            f"Wait a bit, then /fwd again. Increase delay in /settings if this repeats."
        )
    if "timeout" in low or "timed out" in low:
        return (
            f"<b>Timeout</b>\n<code>{msg}</code>\n"
            f"Network or Telegram was too slow. /fwd to resume — sent files are skipped."
        )
    if "auth" in low or "session" in low or "key" in low:
        return (
            f"<b>Session / auth error</b>\n<code>{msg}</code>\n"
            f"Re-add your bot or userbot in /settings."
        )
    if "permission" in low or "admin" in low or "forbidden" in low or "chat_write" in low:
        return (
            f"<b>Permission error</b>\n<code>{msg}</code>\n"
            f"Make sure the bot/userbot can read the source and post in the target."
        )
    if "database" in low or "mongo" in low:
        return (
            f"<b>Database error</b>\n<code>{msg}</code>\n"
            f"Check DATABASE_URI on Render."
        )
    return f"<b>Error:</b>\n<code>{msg}</code>"

# Timeouts (seconds) so a stalled Telegram transfer can't freeze the whole job
# Reduced for Render Free (512MB) — long hangs were common with large files
DOWNLOAD_TIMEOUT = 420   # 7 min
UPLOAD_TIMEOUT = 300     # 5 min base (large files get a bit more)
STALL_TIMEOUT = 90       # If progress % doesn't move for this many seconds → fail


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

    # Hard safety: never download files larger than 200 MB (Render Free / low RAM)
    try:
        media_obj = getattr(original, original.media.value, None)
        fsize = getattr(media_obj, "file_size", None) if media_obj else None
        if fsize and (fsize / (1024 * 1024)) > 200:
            print(f"  HARD SKIP msg {msg_id}: {fsize / (1024*1024):.1f} MB > 200 MB safety limit")
            raise ValueError(f"File too large ({fsize / (1024*1024):.1f} MB) — skipped")
    except ValueError:
        raise
    except Exception:
        pass

    last_pct = {"value": -1}
    last_update = {"ts": time.time()}
    phase = {"name": "↓"}
    stalled = {"flag": False}

    async def _progress(current, total):
        if not total:
            return
        now = time.time()
        pct = int(current * 100 / total) if total else 0

        # Stall detection: no % movement for STALL_TIMEOUT seconds
        if pct <= last_pct["value"] and (now - last_update["ts"]) > STALL_TIMEOUT:
            stalled["flag"] = True
            raise TimeoutError(
                f"Transfer stalled at {pct}% for >{STALL_TIMEOUT}s (msg {msg_id})"
            )

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
        last_update["ts"] = time.time()
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

        # Dynamic upload timeout: give larger files a bit more time (max 8 min)
        try:
            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        except Exception:
            file_size_mb = 0
        dynamic_upload_timeout = min(480, max(UPLOAD_TIMEOUT, int(file_size_mb * 4) + 60))

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
        last_update["ts"] = time.time()
        stalled["flag"] = False
        print(f"  ↑ msg {msg_id}: uploading ({media_type}, {file_size_mb:.1f} MB, timeout={dynamic_upload_timeout}s)…")

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
            await asyncio.wait_for(_do_upload(), timeout=dynamic_upload_timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Upload timed out after {dynamic_upload_timeout}s for msg {msg_id}"
            )

        print(f"  ✓ msg {msg_id}: done ({media_type})")
        if m:
            try:
                await edit(m, "Progressing", 10, sts)
            except Exception:
                pass

    finally:
        # Always clean temp files aggressively (important on Render Free)
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
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
        # Native Telegram copy/forward does not expose byte-level download/upload
        # progress because no local download happens. Still show a live stage so
        # the user never gets stuck looking at "Loading settings".
        try:
            if m:
                await msg_edit(m, f"<code>Forwarding message {msg.get('msg_id', '?')}…</code>")
        except Exception:
            pass

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
    """Best-effort status update that never hides unexpected Telegram errors."""
    if msg is None:
        return None
    try:
        return await msg.edit(text, reply_markup=button)
    except MessageNotModified:
        return msg
    except FloodWait as e:
        if wait:
            await asyncio.sleep(e.value)
            return await msg_edit(msg, text, button, wait)
        print(f"[status] FloodWait while editing status: {e.value}s")
    except Exception as e:
        # The forwarding job must continue even if Telegram rejects a status edit,
        # but the reason must be visible in Render logs instead of being swallowed.
        print(f"[status] edit failed: {type(e).__name__}: {e}")
    return msg
        
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
    """Only detects cancel flag. Cleanup is handled by the main loop's finally block."""
    return temp.CANCEL.get(user) is True


async def stop(client, user):
    """Stop clone client and clear user lock. Safe to call multiple times."""
    try:
        await client.stop()
    except Exception:
        pass
    try:
        await db.rmve_frwd(user)
    except Exception:
        pass
    try:
        if temp.forwardings > 0:
            temp.forwardings -= 1
    except Exception:
        temp.forwardings = 0
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


@Client.on_message(filters.private & filters.command(["cancel"]))
async def cancel_task(bot, message):
    """Cancel the current user's running forward task via command."""
    user_id = message.from_user.id
    was_running = bool(temp.lock.get(user_id)) and str(temp.lock.get(user_id)) == "True"
    temp.CANCEL[user_id] = True
    if was_running:
        await message.reply(
            "<b>🛑 Cancel requested.</b>\n"
            "The current forward job will stop shortly.\n"
            "Use <code>/task</code> to check status, or <code>/unlock</code> if it stays stuck."
        )
    else:
        temp.lock[user_id] = False
        await message.reply(
            "<b>No active task found.</b>\n"
            "If something feels stuck, try <code>/unlock</code> then <code>/fwd</code>."
        )


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
