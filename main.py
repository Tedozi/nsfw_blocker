import os
import sqlite3
import hashlib
import time
import datetime
from pathlib import Path
from collections import defaultdict
from os import remove
from pyrogram import Client, filters, enums
from pyrogram.types import Message, ChatPermissions
from pyrogram.errors import FloodWait
from opennsfw2 import predict_image, predict_video_frames
import asyncio

# Kết nối SQLite
db = sqlite3.connect("media.db", check_same_thread=False)
cursor = db.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS media (
    media_id TEXT PRIMARY KEY,
    nsfw INTEGER,
    type TEXT,
    timestamp INTEGER
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS free_users (
    user_id INTEGER PRIMARY KEY
)
""")
db.commit()

# Khởi tạo bot Telegram
bot = Client(
    "my_bot",
    bot_token="7686877608:AAEQYh95hN19scnF016KgpXM7waxiK20Eg",
    api_hash="f6db4721a0d2013e3d1760fa4b301276",
    api_id=22546461
)

# Lưu tin nhắn cảnh báo cũ và timestamps để xóa sau
last_warn_message = {}
user_media_timestamps = defaultdict(list)

async def delete_later(message_obj, delay=30):
    await asyncio.sleep(delay)
    try:
        await message_obj.delete()
    except:
        pass

async def is_user_free(user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM free_users WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None

async def check_nsfw_and_warn(message: Message, probability: float):
    if probability >= 0.75:
        await message.delete()
        if message.chat.id in last_warn_message:
            try: await last_warn_message[message.chat.id].delete()
            except: pass
        warn_msg = await message.reply_text(
            f"⚠️ {message.from_user.mention} nội dung không phù hợp!"
        )
        last_warn_message[message.chat.id] = warn_msg
        asyncio.create_task(delete_later(warn_msg, 30))
        return True
    return False

async def handle_media_message(message: Message):
    if not message.from_user or await is_user_free(message.from_user.id):
        return
    now = time.time(); uid = message.from_user.id

    # Xử lý sticker (Xóa và mute người gửi)
    if message.sticker:
        user_media_timestamps[uid] = [t for t in user_media_timestamps[uid] if now - t < 3]
        user_media_timestamps[uid].append(now)
        if len(user_media_timestamps[uid]) >= 3:
            # Xóa tin nhắn sticker
            try: await message.delete()
            except FloodWait as e:
                await asyncio.sleep(e.value); await message.delete()
            # Gửi cảnh báo mới
            warn = await message.reply_text(
                f"🚫 {message.from_user.mention} đã gửi sticker, Thì đừng spam!"
            )
            asyncio.create_task(delete_later(warn, 30))
            # Mute user 5 phút
            until = datetime.datetime.utcnow() + datetime.timedelta(seconds=300)
            await bot.restrict_chat_member(
                message.chat.id,
                uid,
                permissions=ChatPermissions(
                    can_send_messages=False,
                    can_send_media_messages=False,
                    can_send_other_messages=False,
                    can_add_web_page_previews=False
                ),
                until_date=until
            )
            user_media_timestamps[uid] = []
            return

    # Xử lý media bình thường
    file_path = await bot.download_media(message)
    ext = Path(file_path).suffix.lower().lstrip('.')
    if not ext:
        import mimetypes
        guessed = message.document.mime_type if message.document else None
        ext = (mimetypes.guess_extension(guessed) or '').lstrip('.')
        if ext:
            new = file_path + '.' + ext; os.rename(file_path, new); file_path = new
    with open(file_path, 'rb') as f: data = f.read()
    media_hash = hashlib.md5(data).hexdigest()
    cursor.execute("SELECT nsfw FROM media WHERE media_id = ?", (media_hash,))
    row = cursor.fetchone()
    if row:
        if row[0] == 1: await message.delete()
        try: remove(file_path)
        except: pass
        return
    video_exts = {'mp4','webm','mov','mkv','avi'}
    if ext in video_exts:
        frames = predict_video_frames(file_path, frame_interval=8)
        for frame_prob in frames[1]:
            if frame_prob > 0.75:
                prob = frame_prob
                break
        else:
            prob = max(frames[1])
        media_type = 'video'
    else:
        prob = predict_image(file_path); media_type = 'image'
    nsfw = await check_nsfw_and_warn(message, prob)
    cursor.execute(
        "INSERT INTO media (media_id, nsfw, type, timestamp) VALUES (?,?,?,?)",
        (media_hash, 1 if nsfw else 0, media_type, int(now))
    )
    db.commit()
    try: remove(file_path)
    except: pass

# Quản lý quyền admin
def is_admin_with_change_info(member):
    priv = getattr(member, 'privileges', None)
    return bool(priv and getattr(priv, 'can_change_info', False))

async def has_permission_to_manage(chat_id, user_id, client):
    member = await client.get_chat_member(chat_id, user_id)
    status = getattr(member, 'status', None)
    if status == enums.ChatMemberStatus.OWNER: return True
    if status == enums.ChatMemberStatus.ADMINISTRATOR and is_admin_with_change_info(member): return True
    return False

async def auto_delete_reply(orig_msg, bot_msg, delay=30):
    asyncio.create_task(delete_later(bot_msg, delay))
    try: await orig_msg.delete()
    except: pass

# Commands
@bot.on_message(filters.command("free") & filters.group)
async def cmd_free(_, message: Message):
    if not message.from_user or not await has_permission_to_manage(message.chat.id, message.from_user.id, bot):
        return await message.reply_text("❌ Không có quyền!")
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    cursor.execute("INSERT OR IGNORE INTO free_users(user_id) VALUES(?)", (target.id,))
    db.commit()
    bot_msg = await message.reply_text(f"✅ Miễn kiểm duyệt cho {target.mention}!")
    await auto_delete_reply(message, bot_msg)

@bot.on_message(filters.command("unfree") & filters.group)
async def cmd_unfree(_, message: Message):
    if not message.from_user or not await has_permission_to_manage(message.chat.id, message.from_user.id, bot):
        return await message.reply_text("❌ Không có quyền!")
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    cursor.execute("DELETE FROM free_users WHERE user_id=?", (target.id,))
    db.commit()
    bot_msg = await message.reply_text(f"✅ Hủy miễn kiểm duyệt {target.mention}!")
    await auto_delete_reply(message, bot_msg)

@bot.on_message(filters.command("stats") & filters.group)
async def cmd_stats(_, message: Message):
    cursor.execute("SELECT COUNT(*) FROM media"); total = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM media WHERE nsfw=1"); nsfwc = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM free_users"); freec = cursor.fetchone()[0]
    bot_msg = await message.reply_text(f"📊 Tổng:{total}, NSFW:{nsfwc}, Free:{freec}")
    await auto_delete_reply(message, bot_msg)

@bot.on_message(filters.command("listfree") & filters.group)
async def cmd_listfree(_, message: Message):
    cursor.execute("SELECT user_id FROM free_users"); rows = cursor.fetchall()
    text = "👥 Free list:\n" + "\n".join([f"- {(await bot.get_users(r[0])).mention}" for r in rows
    ]) if rows else "Danh sách trống."
    bot_msg = await message.reply_text(text)
    await auto_delete_reply(message, bot_msg)

# Handler chung
@bot.on_message(filters.group & (filters.photo|filters.sticker|filters.animation|filters.video))
async def media_handler(_, message: Message):
    await handle_media_message(message)

if __name__ == '__main__': bot.run()
