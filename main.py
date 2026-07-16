# Copyright (C) @TheSmartBisnu
# Channel: https://t.me/itsSmartDev

import os
import io
from io import BytesIO
import shutil
import psutil
from pathlib import Path
import sys
import logging
import traceback
import asyncio
from time import time
from PIL import Image
from datetime import timedelta
from pprint import pformat  # For pretty-printing

from pyleaves import Leaves
from pyrogram.enums import ParseMode
from pyrogram import Client, filters
from pyrogram.errors import PeerIdInvalid, BadRequest, FloodWait
from pyrogram.types import (
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
    InputMediaAnimation,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    Message,
)

from helpers.forward import check_forward_permission, resolve_forward_chat_id

from helpers.utils import (
    processMediaGroup,
    progressArgs,
    send_media,
    json_parser,
    set_memory_template,
    save_template_to_file,
    reset_template,
    get_active_template,
    get_media_info,
    get_video_thumbnail,
    CUSTOM_THUMB_DIR
)

from helpers.files import (
    get_download_path,
    fileSizeLimit,
    get_readable_file_size,
    get_readable_time,
    cleanup_download
)

from helpers.msg import (
    getChatMsgID,
    get_file_name,
    get_parsed_msg
)

from config import PyroConf
from logger import LOGGER
from cmd_list import COMMANDS

START_TIME = time()

# Initialize the bot client
bot = Client(
    "media_bot",
    api_id=PyroConf.API_ID,
    api_hash=PyroConf.API_HASH,
    bot_token=PyroConf.BOT_TOKEN,
    workers=1000
)

# Client for user session
user = Client("user_session", workers=1000, session_string=PyroConf.SESSION_STRING)

RUNNING_TASKS = set()

MAX_MESSAGE_LENGTH = 4096
EVAL_TIMEOUT = 60  # Timeout in seconds
eval_history = []

COMMAND_TIMEOUT = 60  # Timeout in seconds
COMMAND_ALIASES = {
    "update": "git pull",
    "restart": "systemctl restart mybot.service",
}
command_history = []
forward_chat_id = None

def track_task(coro):
    task = asyncio.create_task(coro)
    RUNNING_TASKS.add(task)
    def _remove(_):
        RUNNING_TASKS.discard(task)
    task.add_done_callback(_remove)
    return task

@bot.on_message(filters.command("start") & filters.private)
async def start(_, message: Message):
    welcome_text = (
        "👋 **Welcome to Media Downloader Bot!**\n\n"
        "I can grab photos, videos, audio, and documents from any Telegram post.\n"
        "Just send me a link (paste it directly or use `/dl <link>`),\n"
        "or reply to a message with `/dl`.\n\n"
        "ℹ️ Use `/help` to view all commands and examples.\n"
        "🔒 Make sure the user client is part of the chat.\n\n"
        "Ready? Send me a Telegram post link!"
    )

    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Update Channel", url="https://t.me/itsSmartDev")]]
    )
    await message.reply(welcome_text, reply_markup=markup, disable_web_page_preview=True)

@bot.on_message(filters.command("help") & filters.private)
async def help_command(_, message: Message):
    help_text = (
        "💡 **Media Downloader Bot Help**\n\n"
        "➤ **Download Media**\n"
        "   – Send `/dl <post_URL>` **or** just paste a Telegram post link to fetch photos, videos, audio, or documents.\n\n"
        
        "➤ **Batch Download**\n"
        "   – Send `/bdl start_link end_link` to grab a series of posts in one go.\n"
        "     💡 Example: `/bdl https://t.me/mychannel/100 https://t.me/mychannel/120`\n"
        "**It will download all posts from ID 100 to 120.**\n\n"
        
        "➤ **Requirements**\n"
        "   – Make sure the user client is part of the chat.\n\n"
        "➤ **If the bot hangs**\n"
        "   – Send `/killall` to cancel any pending downloads.\n\n"
        "➤ **Logs**\n"
        "   – Send `/logs` to download the bot’s logs file.\n\n"
        "➤ **Stats**\n"
        "   – Send `/stats` to view current status:\n\n"
        "**Example**:\n"
        "  • `/dl https://t.me/itsSmartDev/547`\n"
        "  • `https://t.me/itsSmartDev/547`"
    )
    
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Update Channel", url="https://t.me/itsSmartDev")]]
    )
    await message.reply(help_text, reply_markup=markup, disable_web_page_preview=True)


async def handle_download(bot: Client, message: Message, post_url: str, force_stream: bool = False):
    global forward_chat_id
    # Cut off URL at '?' if present
    if "?" in post_url:
        post_url = post_url.split("?", 1)[0]

    try:
        effective_forward_chat_id = None
        if forward_chat_id:
            ok, err_msg = await check_forward_permission(bot, forward_chat_id)
            if not ok:
                await message.reply(f"⚠️ **Forward chat misconfigured:** {err_msg}\n\n""The file will be sent to you only.")
            else:
                effective_forward_chat_id = forward_chat_id if PyroConf.FORWARD_ENABLED else None

        # Special handling for t.me/b/ links
        if 't.me/b/' in post_url:
            parts = [p for p in post_url.split("/") if p]
            if len(parts) >= 5 and parts[2] == 'b':
                chat_id = str(parts[3])
                message_id = int(parts[4])
            else:
                raise ValueError("Invalid business link format")
        else:
            chat_id, message_id = getChatMsgID(post_url)

        chat_message = await user.get_messages(chat_id=chat_id, message_ids=message_id)

        LOGGER(__name__).info(f"Downloading media from URL: {post_url}")

        if chat_message.document or chat_message.video or chat_message.audio:
            file_size = (
                chat_message.document.file_size
                if chat_message.document
                else chat_message.video.file_size
                if chat_message.video
                else chat_message.audio.file_size
            )

            if not await fileSizeLimit(
                file_size, message, "download", user.me.is_premium
            ):
                return

        parsed_caption = await get_parsed_msg(
            chat_message.caption or "", chat_message.caption_entities
        )
        parsed_text = await get_parsed_msg(
            chat_message.text or "", chat_message.entities
        )

        if chat_message.media_group_id:
            if not await processMediaGroup(
                chat_message, bot, message, user,
                forward_chat_id=effective_forward_chat_id,
                force_stream=force_stream
            ):
                await message.reply(
                    "**Could not extract any valid media from the media group.**"
                )
            return

        elif chat_message.media:
            start_time = time()
            progress_message = await message.reply("**__📥 Downloading Progress...__**")

            filename = get_file_name(message_id, chat_message)
            download_path = get_download_path(message.id, filename)

            media_path = await chat_message.download(
                file_name=download_path,
                progress=Leaves.progress_for_pyrogram,
                progress_args=progressArgs(
                    "📥 **__Downloading Progress__**", progress_message, start_time
                ),
            )

            LOGGER(__name__).info(f"Downloaded media: {media_path}")

            media_type = (
                "photo"
                if chat_message.photo
                else "video"
                if chat_message.video
                else "audio"
                if chat_message.audio
                else "animation"
                if chat_message.animation
                else "document"
            )
            await send_media(
                bot,
                message,
                chat_message,
                user,
                media_path,
                media_type,
                parsed_caption,
                progress_message,
                start_time,
                forward_chat_id=effective_forward_chat_id,
                force_stream=force_stream
                # NOTE: force_stream not passed here yet — send_media to be updated later
            )

            cleanup_download(media_path)
            await progress_message.delete()

        elif chat_message.text or chat_message.caption:
            await message.reply(parsed_text or parsed_caption)
        else:
            await message.reply("**No media or text found in the post URL.**")

    except FloodWait as e:
        wait_s = int(getattr(e, "value", 0) or 0)
        LOGGER(__name__).warning(f"FloodWait in handle_download: {wait_s}s")
        if wait_s > 0:
            await asyncio.sleep(wait_s + 1)
        return
    except (PeerIdInvalid, BadRequest, KeyError):
        await message.reply("**Make sure the user client is part of the chat.**")
    except Exception as e:
        error_message = f"**❌ {str(e)}**"
        await message.reply(error_message)
        LOGGER(__name__).error(e)


@bot.on_message(filters.command("dl") & filters.user(PyroConf.OWNER_ID))
async def download_media(bot: Client, message: Message):
    if len(message.command) < 2:
        await message.reply("**Provide a post URL after the /dl command.**")
        return

    parts = message.command[1:]

    # CHECK IF LAST PART IS "s" FLAG
    if parts and parts[-1].lower() == "s":
        force_stream = True
        parts = parts[:-1]
    else:
        force_stream = False

    links = [p for p in parts if p.startswith("https://t.me/")]

    if not links:
        return await message.reply("**No valid t.me links found.**")

    for url in links:
        await track_task(handle_download(bot, message, url, force_stream=force_stream))
        await asyncio.sleep(3)


@bot.on_message(filters.command("bdl") & filters.user(PyroConf.OWNER_ID))
async def download_range(bot: Client, message: Message):
    args = message.text.split()
    force_stream = args[-1].lower() == "s"
    link_args = [a for a in args[1:] if a.lower() != "s"]
 
    if len(link_args) != 2 or not all(a.startswith("https://t.me/") for a in link_args):
        await message.reply(
            "🚀 **Batch Download Process**\n"
            "`/bdl start_link end_link`\n\n"
            "💡 **Example:**\n"
            "`/bdl https://t.me/mychannel/100 https://t.me/mychannel/120`"
        )
        return
 
    try:
        start_chat, start_id = getChatMsgID(link_args[0])
        end_chat,   end_id   = getChatMsgID(link_args[1])
    except Exception as e:
        return await message.reply(f"**❌ Error parsing links:\n{e}**")
 
    if start_chat != end_chat:
        return await message.reply("**❌ Both links must be from the same channel.**")
    if start_id > end_id:
        return await message.reply("**❌ Invalid range: start ID cannot exceed end ID.**")
 
    try:
        await user.get_chat(start_chat)
    except Exception:
        pass
 
    prefix = link_args[0].rsplit("/", 1)[0]
    loading = await message.reply(f"📥 **__Downloading posts {start_id}–{end_id}…__**")
 
    downloaded = skipped = failed = 0
 
    for msg_id in range(start_id, end_id + 1):
        url = f"{prefix}/{msg_id}"
        try:
            chat_msg = await user.get_messages(chat_id=start_chat, message_ids=msg_id)
            if not chat_msg:
                skipped += 1
                continue
 
            has_media = bool(chat_msg.media_group_id or chat_msg.media)
            has_text  = bool(chat_msg.text or chat_msg.caption)
            if not (has_media or has_text):
                skipped += 1
                continue
 
            task = track_task(handle_download(bot, message, url, force_stream=force_stream))
            try:
                await task
                downloaded += 1
            except asyncio.CancelledError:
                await loading.delete()
                return await message.reply(
                    f"**❌ Batch canceled** after downloading `{downloaded}` posts."
                )
 
        except Exception as e:
            failed += 1
            LOGGER(__name__).error(f"Error at {url}: {e}")
 
        await asyncio.sleep(3)
 
    await loading.delete()
    await message.reply(
        "**✅ Batch Process Complete!**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📥 **Downloaded** : `{downloaded}` post(s)\n"
        f"⏭️ **Skipped**    : `{skipped}` (no content)\n"
        f"❌ **Failed**     : `{failed}` error(s)"
    )

@bot.on_message(filters.command("gdl") & filters.user(PyroConf.OWNER_ID))
async def download_range_group(bot: Client, message: Message):
    global forward_chat_id

    args = message.text.split()
    chat_id = message.chat.id

    # 👉 If user only sends /gdl → start interactive mode
    if len(args) == 1:
        try:
            start_msg = await bot.ask(
                chat_id,
                "🔗 Send **start message link**",
                timeout=60
            )

            end_msg = await bot.ask(
                chat_id,
                "🔗 Send **end message link**",
                timeout=60
            )

            stream_msg = await bot.ask(
                chat_id,
                "📡 Send `s` to force streamable sending, or anything else to skip.",
                timeout=60
            )

            start_link = start_msg.text.strip()
            end_link = end_msg.text.strip()
            force_stream = stream_msg.text.strip().lower() == "s"

            raw_forward_id = forward_chat_id

        except Exception:
            return await message.reply("❌ Timeout or cancelled.")

    else:
        # 👉 Normal command usage
        # Extract optional flags: forward_chat_id and/or "s"
        # Valid forms:
        #   /gdl start end
        #   /gdl start end s
        #   /gdl start end -100xxx
        #   /gdl start end -100xxx s

        if len(args) < 3 or not all(arg.startswith("https://t.me/") for arg in args[1:3]):
            return await message.reply(
                "🚀 **Batch Download as Group**\n"
                "`/gdl start_link end_link [forward_chat_id] [s]`\n\n"
                "💡 **Example:**\n"
                "`/gdl https://t.me/mychannel/100 https://t.me/mychannel/120`\n"
                "`/gdl https://t.me/mychannel/100 https://t.me/mychannel/120 s`\n"
                "`/gdl https://t.me/mychannel/100 https://t.me/mychannel/120 -1001234567890`\n"
                "`/gdl https://t.me/mychannel/100 https://t.me/mychannel/120 -1001234567890 s`\n\n"
                "📡 Add `s` to force documents to be sent as streamable photo/video."
            )

        # Parse remaining args after the two links
        remaining = args[3:]
        force_stream = "s" in [r.lower() for r in remaining]
        raw_forward_id = next(
            (r for r in remaining if r.lower() != "s"),
            forward_chat_id
        )

        start_link = args[1]
        end_link = args[2]

    # 👉 Parse links
    try:
        start_chat, start_id = getChatMsgID(start_link)
        end_chat, end_id = getChatMsgID(end_link)
    except Exception as e:
        return await message.reply(f"**❌ Error parsing links:\n{e}**")

    if start_chat != end_chat:
        return await message.reply("**❌ Both links must be from the same channel.**")

    if start_id > end_id:
        return await message.reply("**❌ Invalid range: start ID cannot exceed end ID.**")

    # 👉 Forward chat handling
    effective_forward_chat_id = None
    if raw_forward_id:
        ok, err_msg = await check_forward_permission(bot, raw_forward_id)
        if not ok:
            await message.reply(
                f"⚠️ **Forward chat misconfigured:** {err_msg}\n\n"
                "The file will be sent to you only."
            )
        else:
            effective_forward_chat_id = raw_forward_id if PyroConf.FORWARD_ENABLED else None

    try:
        await user.get_chat(start_chat)
    except Exception:
        pass

    loading = await message.reply(
        f"📥 **__Fetching posts {start_id}–{end_id}…__**"
        + (" `[stream mode]`" if force_stream else "")
    )

    # COLLECT ALL MEDIA AND ANIMATIONS SEPARATELY
    valid_media = []       # compatible media for group sending
    animation_media = []   # animations to send separately
    temp_paths = []
    thumb_paths = []
    skipped = failed = 0
    start_time = time()

    for msg_id in range(start_id, end_id + 1):
        try:
            chat_msg = await user.get_messages(chat_id=start_chat, message_ids=msg_id)
            if not chat_msg or not chat_msg.media:
                skipped += 1
                continue

            # SKIP IF PART OF MEDIA GROUP - THOSE SHOULD BE HANDLED SEPARATELY
            if chat_msg.media_group_id:
                skipped += 1
                continue

            try:
                media_path = await chat_msg.download(
                    progress=Leaves.progress_for_pyrogram,
                    progress_args=progressArgs(
                        "📥 **__Downloading Progress__**", loading, start_time
                    ),
                )
                temp_paths.append(media_path)
                caption = await get_parsed_msg(chat_msg.caption or "", chat_msg.caption_entities)

                if chat_msg.photo:
                    valid_media.append(
                        InputMediaPhoto(media=media_path, caption=caption)
                    )
                elif chat_msg.video:
                    duration = (await get_media_info(media_path))[0]
                    thumb = None
                    width = 480
                    height = 320

                    thumb_filename = f"bdlg_thumb_{int(time())}.jpg"
                    custom_thumb_path = os.path.join(CUSTOM_THUMB_DIR, thumb_filename)

                    if hasattr(chat_msg.video, 'thumbs') and chat_msg.video.thumbs:
                        try:
                            thumb = await user.download_media(
                                chat_msg.video.thumbs[0].file_id,
                                file_name=custom_thumb_path
                            )
                            if thumb and os.path.exists(thumb):
                                with Image.open(thumb) as img:
                                    width, height = img.size
                                thumb_paths.append(thumb)
                        except Exception as e:
                            LOGGER(__name__).warning(f"Failed to download video thumbnail: {e}")
                            thumb = None

                    if thumb is None:
                        thumb = await get_video_thumbnail(media_path, duration)
                        if thumb and thumb != "none":
                            with Image.open(thumb) as img:
                                width, height = img.size
                            thumb_paths.append(thumb)
                        elif thumb == "none":
                            thumb = None

                    valid_media.append(
                        InputMediaVideo(
                            media=media_path,
                            caption=caption,
                            duration=duration,
                            thumb=thumb,
                            width=width,
                            height=height
                        )
                    )
                elif chat_msg.document:
                    mime = getattr(chat_msg.document, "mime_type", "") or ""

                    # ── STREAM MODE: re-cast document as photo or video ──
                    if force_stream and mime.startswith("image/"):
                        valid_media.append(
                            InputMediaPhoto(media=media_path, caption=caption)
                        )

                    elif force_stream and mime.startswith("video/"):
                        duration = (await get_media_info(media_path))[0]
                        thumb = None
                        width = 480
                        height = 320

                        thumb_filename = f"bdlg_thumb_{int(time())}.jpg"
                        custom_thumb_path = os.path.join(CUSTOM_THUMB_DIR, thumb_filename)

                        if hasattr(chat_msg.document, 'thumbs') and chat_msg.document.thumbs:
                            try:
                                thumb = await user.download_media(
                                    chat_msg.document.thumbs[0].file_id,
                                    file_name=custom_thumb_path
                                )
                                if thumb and os.path.exists(thumb):
                                    with Image.open(thumb) as img:
                                        width, height = img.size
                                    thumb_paths.append(thumb)
                                else:
                                    thumb = None
                            except Exception as e:
                                LOGGER(__name__).warning(f"Failed to download doc-video thumbnail: {e}")
                                thumb = None

                        if thumb is None:
                            thumb = await get_video_thumbnail(media_path, duration)
                            if thumb and thumb != "none":
                                with Image.open(thumb) as img:
                                    width, height = img.size
                                thumb_paths.append(thumb)
                            elif thumb == "none":
                                thumb = None

                        valid_media.append(
                            InputMediaVideo(
                                media=media_path,
                                caption=caption,
                                duration=duration,
                                thumb=thumb,
                                width=width,
                                height=height
                            )
                        )

                    else:
                        # ── DEFAULT: send as document ──
                        thumb = None
                        width, height = 320, 320

                        thumb_filename = f"bdlg_doc_thumb_{int(time())}.jpg"
                        custom_thumb_path = os.path.join(CUSTOM_THUMB_DIR, thumb_filename)

                        if hasattr(chat_msg.document, 'thumbs') and chat_msg.document.thumbs:
                            try:
                                thumb = await user.download_media(
                                    chat_msg.document.thumbs[0].file_id,
                                    file_name=custom_thumb_path
                                )
                                if thumb and os.path.exists(thumb):
                                    with Image.open(thumb) as img:
                                        width, height = img.size
                                    thumb_paths.append(thumb)
                                else:
                                    thumb = None
                            except Exception as e:
                                LOGGER(__name__).warning(f"Failed to download document thumbnail: {e}")
                                thumb = None

                        valid_media.append(
                            InputMediaDocument(
                                media=media_path,
                                caption=caption,
                                thumb=thumb
                            )
                        )

                elif chat_msg.audio:
                    valid_media.append(
                        InputMediaAudio(media=media_path, caption=caption)
                    )
                elif chat_msg.animation:
                    # COLLECT ANIMATIONS SEPARATELY
                    animation_media.append(
                        InputMediaAnimation(media=media_path, caption=caption)
                    )
                else:
                    skipped += 1
                    continue

            except Exception as e:
                LOGGER(__name__).error(f"Error downloading msg {msg_id}: {e}")
                failed += 1
                continue

        except Exception as e:
            failed += 1
            LOGGER(__name__).error(f"Error fetching msg {msg_id}: {e}")

        await asyncio.sleep(1)

    if not valid_media and not animation_media:
        await loading.delete()
        await message.reply("❌ No valid media found in the given range.")
        for path in temp_paths + thumb_paths:
            cleanup_download(path)
        return

    # NOTIFY USER IF ANIMATIONS EXIST
    anim_notice = None
    if animation_media:
        anim_notice = await message.reply(
            f"ℹ️ **Found {len(animation_media)} GIF(s) in the range, they will be sent separately after the media group.**"
        )

    sent_messages = []
    group_sent_messages = []
    animation_sent_messages = []

    # SPLIT valid_media INTO CHUNKS OF 10 AND SEND EACH AS A GROUP
    if valid_media:
        chunks = [valid_media[i:i+10] for i in range(0, len(valid_media), 10)]
        LOGGER(__name__).info(f"Sending {len(valid_media)} media in {len(chunks)} group(s)")

        for idx, chunk in enumerate(chunks):
            try:
                await loading.edit(f"📤 **__Sending group {idx+1}/{len(chunks)}…__**")
                group_sent = await bot.send_media_group(chat_id=message.chat.id, media=chunk)
                group_sent_messages.extend(group_sent)
                sent_messages.extend(group_sent)
            except Exception as e:
                await message.reply(
                    f"**❌ Failed to send group {idx+1}, trying individually**\n`{e}`"
                )
                for media in chunk:
                    try:
                        if isinstance(media, InputMediaPhoto):
                            sent = await bot.send_photo(
                                chat_id=message.chat.id,
                                photo=media.media,
                                caption=media.caption,
                            )
                        elif isinstance(media, InputMediaVideo):
                            sent = await bot.send_video(
                                chat_id=message.chat.id,
                                video=media.media,
                                caption=media.caption,
                                thumb=media.thumb,
                                width=media.width,
                                height=media.height,
                                supports_streaming=True,
                            )
                        elif isinstance(media, InputMediaDocument):
                            sent = await bot.send_document(
                                chat_id=message.chat.id,
                                document=media.media,
                                caption=media.caption,
                                thumb=media.thumb if hasattr(media, "thumb") else None,
                            )
                        elif isinstance(media, InputMediaAudio):
                            sent = await bot.send_audio(
                                chat_id=message.chat.id,
                                audio=media.media,
                                caption=media.caption,
                            )
                        sent_messages.append(sent)
                        group_sent_messages.append(sent)
                    except Exception as individual_e:
                        await message.reply(f"Failed to upload media: {individual_e}")

            await asyncio.sleep(2)

    # SEND ANIMATIONS INDIVIDUALLY AFTER ALL GROUPS
    if animation_media:
        await loading.edit(f"📤 **__Sending {len(animation_media)} GIF(s)…__**")
        for anim in animation_media:
            try:
                sent = await bot.send_animation(
                    chat_id=message.chat.id,
                    animation=anim.media,
                    caption=anim.caption,
                )
                animation_sent_messages.append(sent)
                sent_messages.append(sent)
                LOGGER(__name__).info("Sent animation separately")
            except Exception as e:
                await message.reply(f"Failed to upload animation: {e}")
            await asyncio.sleep(1)

    # FORWARD TO ADDITIONAL CHAT IF REQUESTED
    if effective_forward_chat_id and sent_messages:
        source_chat_id = sent_messages[0].chat.id
        try:
            # FORWARD MEDIA GROUPS USING copy_media_group
            if group_sent_messages:
                # SPLIT group_sent_messages BACK INTO CHUNKS OF 10 FOR FORWARDING
                group_chunks = [group_sent_messages[i:i+10] for i in range(0, len(group_sent_messages), 10)]
                for chunk in group_chunks:
                    for attempt in range(2):
                        try:
                            await bot.copy_media_group(
                                chat_id=effective_forward_chat_id,
                                from_chat_id=source_chat_id,
                                message_id=chunk[0].id,
                            )
                            LOGGER(__name__).info(f"Copied media group chunk to chat: {effective_forward_chat_id}")
                            break
                        except FloodWait as e:
                            wait_s = int(getattr(e, "value", 0) or 0)
                            LOGGER(__name__).warning(f"FloodWait while copying group: {wait_s}s")
                            if wait_s > 0 and attempt == 0:
                                await asyncio.sleep(wait_s + 1)
                                continue
                            raise
                    await asyncio.sleep(2)

            # FORWARD ANIMATIONS INDIVIDUALLY USING copy_message
            for anim_msg in animation_sent_messages:
                for attempt in range(2):
                    try:
                        await bot.copy_message(
                            chat_id=effective_forward_chat_id,
                            from_chat_id=source_chat_id,
                            message_id=anim_msg.id,
                        )
                        LOGGER(__name__).info(f"Copied animation to chat: {effective_forward_chat_id}")
                        break
                    except FloodWait as e:
                        wait_s = int(getattr(e, "value", 0) or 0)
                        LOGGER(__name__).warning(f"FloodWait while copying animation: {wait_s}s")
                        if wait_s > 0 and attempt == 0:
                            await asyncio.sleep(wait_s + 1)
                            continue
                        raise
                await asyncio.sleep(1)

        except Exception as e:
            LOGGER(__name__).error(f"Failed to forward to {effective_forward_chat_id}: {e}")

    # CLEAN UP NOTICE AND LOADING MESSAGES
    if anim_notice:
        await anim_notice.delete()
    await loading.delete()

    # FINAL SUMMARY
    await message.reply(
        "**✅ Batch Group Process Complete!**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📤 **Sent as group** : `{len(group_sent_messages)}` file(s)\n"
        f"🎞️ **GIFs sent**     : `{len(animation_sent_messages)}` file(s)\n"
        f"⏭️ **Skipped**       : `{skipped}` (no media/in album)\n"
        f"❌ **Failed**        : `{failed}` error(s)\n"
        + (f"📡 **Stream mode**   : `on`" if force_stream else "")
    )

    for path in temp_paths + thumb_paths:
        cleanup_download(path)
        

@bot.on_message(filters.command("mdl") & filters.user(PyroConf.OWNER_ID))
async def download_multi_group(bot: Client, message: Message):
    global forward_chat_id

    raw = message.text.split(None, 1)
    if len(raw) < 2:
        await message.reply(
            "🚀 **Multi-link Download as Group**\n"
            "`/mdl link1 link2 link3 ... [s]`\n\n"
            "💡 **Example:**\n"
            "`/mdl https://t.me/chat1/100 https://t.me/chat2/50`\n"
            "`/mdl https://t.me/chat1/100 https://t.me/chat2/50 s`\n\n"
            "📎 Links can be space or newline separated.\n"
            "📡 Add `s` to force documents to be sent as streamable photo/video."
        )
        return

    parts = raw[1].split()

    # CHECK IF LAST PART IS "s" FLAG
    if parts and parts[-1].lower() == "s":
        force_stream = True
        parts = parts[:-1]
    else:
        force_stream = False

    # VALIDATE LINKS
    links = [p for p in parts if p.startswith("https://t.me/")]
    invalid = [p for p in parts if not p.startswith("https://t.me/")]

    if not links:
        return await message.reply("**❌ No valid t.me links found.**")

    if invalid:
        await message.reply(
            f"⚠️ **Skipping {len(invalid)} invalid entry(ies):**\n"
            + "\n".join(f"`{i}`" for i in invalid)
        )

    # FORWARD CHAT HANDLING
    effective_forward_chat_id = None
    if forward_chat_id:
        ok, err_msg = await check_forward_permission(bot, forward_chat_id)
        if not ok:
            await message.reply(
                f"⚠️ **Forward chat misconfigured:** {err_msg}\n\n"
                "The file will be sent to you only."
            )
        else:
            effective_forward_chat_id = forward_chat_id if PyroConf.FORWARD_ENABLED else None

    loading = await message.reply(
        f"📥 **__Fetching {len(links)} link(s)…__**"
        + (" `[stream mode]`" if force_stream else "")
    )

    # COLLECT ALL MEDIA AND ANIMATIONS SEPARATELY
    valid_media = []
    animation_media = []
    temp_paths = []
    thumb_paths = []
    skipped = failed = 0
    start_time = time()

    for idx, url in enumerate(links, 1):
        await loading.edit(f"📥 **__Downloading {idx}/{len(links)}…__**")
        try:
            # PARSE LINK
            if "?" in url:
                url = url.split("?", 1)[0]

            chat_id, msg_id = getChatMsgID(url)
            chat_msg = await user.get_messages(chat_id=chat_id, message_ids=msg_id)

            if not chat_msg or not chat_msg.media:
                skipped += 1
                continue

            # SKIP MEDIA GROUPS - NOT SUPPORTED IN THIS CMD
            if chat_msg.media_group_id:
                skipped += 1
                await message.reply(f"⚠️ `{url}` is part of a media group, skipping.")
                continue

            try:
                media_path = await chat_msg.download(
                    progress=Leaves.progress_for_pyrogram,
                    progress_args=progressArgs(
                        "📥 **__Downloading Progress__**", loading, start_time
                    ),
                )
                temp_paths.append(media_path)
                caption = await get_parsed_msg(chat_msg.caption or "", chat_msg.caption_entities)

                if chat_msg.photo:
                    valid_media.append(
                        InputMediaPhoto(media=media_path, caption=caption)
                    )
                elif chat_msg.video:
                    duration = (await get_media_info(media_path))[0]
                    thumb = None
                    width = 480
                    height = 320

                    thumb_filename = f"mdl_thumb_{int(time())}.jpg"
                    custom_thumb_path = os.path.join(CUSTOM_THUMB_DIR, thumb_filename)

                    if hasattr(chat_msg.video, 'thumbs') and chat_msg.video.thumbs:
                        try:
                            thumb = await user.download_media(
                                chat_msg.video.thumbs[0].file_id,
                                file_name=custom_thumb_path
                            )
                            if thumb and os.path.exists(thumb):
                                with Image.open(thumb) as img:
                                    width, height = img.size
                                thumb_paths.append(thumb)
                        except Exception as e:
                            LOGGER(__name__).warning(f"Failed to download video thumbnail: {e}")
                            thumb = None

                    if thumb is None:
                        thumb = await get_video_thumbnail(media_path, duration)
                        if thumb and thumb != "none":
                            with Image.open(thumb) as img:
                                width, height = img.size
                            thumb_paths.append(thumb)
                        elif thumb == "none":
                            thumb = None

                    valid_media.append(
                        InputMediaVideo(
                            media=media_path,
                            caption=caption,
                            duration=duration,
                            thumb=thumb,
                            width=width,
                            height=height
                        )
                    )
                elif chat_msg.document:
                    mime = getattr(chat_msg.document, "mime_type", "") or ""

                    # ── STREAM MODE: re-cast document as photo or video ──
                    if force_stream and mime.startswith("image/"):
                        valid_media.append(
                            InputMediaPhoto(media=media_path, caption=caption)
                        )

                    elif force_stream and mime.startswith("video/"):
                        duration = (await get_media_info(media_path))[0]
                        thumb = None
                        width = 480
                        height = 320

                        thumb_filename = f"mdl_thumb_{int(time())}.jpg"
                        custom_thumb_path = os.path.join(CUSTOM_THUMB_DIR, thumb_filename)

                        if hasattr(chat_msg.document, 'thumbs') and chat_msg.document.thumbs:
                            try:
                                thumb = await user.download_media(
                                    chat_msg.document.thumbs[0].file_id,
                                    file_name=custom_thumb_path
                                )
                                if thumb and os.path.exists(thumb):
                                    with Image.open(thumb) as img:
                                        width, height = img.size
                                    thumb_paths.append(thumb)
                                else:
                                    thumb = None
                            except Exception as e:
                                LOGGER(__name__).warning(f"Failed to download doc-video thumbnail: {e}")
                                thumb = None

                        if thumb is None:
                            thumb = await get_video_thumbnail(media_path, duration)
                            if thumb and thumb != "none":
                                with Image.open(thumb) as img:
                                    width, height = img.size
                                thumb_paths.append(thumb)
                            elif thumb == "none":
                                thumb = None

                        valid_media.append(
                            InputMediaVideo(
                                media=media_path,
                                caption=caption,
                                duration=duration,
                                thumb=thumb,
                                width=width,
                                height=height
                            )
                        )

                    else:
                        # ── DEFAULT: send as document ──
                        thumb = None
                        width, height = 320, 320

                        thumb_filename = f"mdl_doc_thumb_{int(time())}.jpg"
                        custom_thumb_path = os.path.join(CUSTOM_THUMB_DIR, thumb_filename)

                        if hasattr(chat_msg.document, 'thumbs') and chat_msg.document.thumbs:
                            try:
                                thumb = await user.download_media(
                                    chat_msg.document.thumbs[0].file_id,
                                    file_name=custom_thumb_path
                                )
                                if thumb and os.path.exists(thumb):
                                    with Image.open(thumb) as img:
                                        width, height = img.size
                                    thumb_paths.append(thumb)
                                else:
                                    thumb = None
                            except Exception as e:
                                LOGGER(__name__).warning(f"Failed to download document thumbnail: {e}")
                                thumb = None

                        valid_media.append(
                            InputMediaDocument(
                                media=media_path,
                                caption=caption,
                                thumb=thumb
                            )
                        )

                elif chat_msg.audio:
                    valid_media.append(
                        InputMediaAudio(media=media_path, caption=caption)
                    )
                elif chat_msg.animation:
                    animation_media.append(
                        InputMediaAnimation(media=media_path, caption=caption)
                    )
                else:
                    skipped += 1
                    continue

            except Exception as e:
                LOGGER(__name__).error(f"Error downloading {url}: {e}")
                failed += 1
                continue

        except Exception as e:
            failed += 1
            LOGGER(__name__).error(f"Error fetching {url}: {e}")

        await asyncio.sleep(1)

    if not valid_media and not animation_media:
        await loading.delete()
        await message.reply("❌ No valid media found in the given links.")
        for path in temp_paths + thumb_paths:
            cleanup_download(path)
        return

    # NOTIFY USER IF ANIMATIONS EXIST
    anim_notice = None
    if animation_media:
        anim_notice = await message.reply(
            f"ℹ️ **Found {len(animation_media)} GIF(s), they will be sent separately after the media group.**"
        )

    sent_messages = []
    group_sent_messages = []
    animation_sent_messages = []

    # SPLIT valid_media INTO CHUNKS OF 10 AND SEND EACH AS A GROUP
    if valid_media:
        chunks = [valid_media[i:i+10] for i in range(0, len(valid_media), 10)]
        LOGGER(__name__).info(f"Sending {len(valid_media)} media in {len(chunks)} group(s)")

        for idx, chunk in enumerate(chunks):
            try:
                await loading.edit(f"📤 **__Sending group {idx+1}/{len(chunks)}…__**")
                group_sent = await bot.send_media_group(chat_id=message.chat.id, media=chunk)
                group_sent_messages.extend(group_sent)
                sent_messages.extend(group_sent)
            except Exception as e:
                await message.reply(
                    f"**❌ Failed to send group {idx+1}, trying individually**\n`{e}`"
                )
                for media in chunk:
                    try:
                        if isinstance(media, InputMediaPhoto):
                            sent = await bot.send_photo(
                                chat_id=message.chat.id,
                                photo=media.media,
                                caption=media.caption,
                            )
                        elif isinstance(media, InputMediaVideo):
                            sent = await bot.send_video(
                                chat_id=message.chat.id,
                                video=media.media,
                                caption=media.caption,
                                thumb=media.thumb,
                                width=media.width,
                                height=media.height,
                                supports_streaming=True,
                            )
                        elif isinstance(media, InputMediaDocument):
                            sent = await bot.send_document(
                                chat_id=message.chat.id,
                                document=media.media,
                                caption=media.caption,
                                thumb=media.thumb if hasattr(media, "thumb") else None,
                            )
                        elif isinstance(media, InputMediaAudio):
                            sent = await bot.send_audio(
                                chat_id=message.chat.id,
                                audio=media.media,
                                caption=media.caption,
                            )
                        sent_messages.append(sent)
                        group_sent_messages.append(sent)
                    except Exception as individual_e:
                        await message.reply(f"Failed to upload media: {individual_e}")

            await asyncio.sleep(2)

    # SEND ANIMATIONS INDIVIDUALLY AFTER ALL GROUPS
    if animation_media:
        await loading.edit(f"📤 **__Sending {len(animation_media)} GIF(s)…__**")
        for anim in animation_media:
            try:
                sent = await bot.send_animation(
                    chat_id=message.chat.id,
                    animation=anim.media,
                    caption=anim.caption,
                )
                animation_sent_messages.append(sent)
                sent_messages.append(sent)
                LOGGER(__name__).info("Sent animation separately")
            except Exception as e:
                await message.reply(f"Failed to upload animation: {e}")
            await asyncio.sleep(1)

    # FORWARD TO ADDITIONAL CHAT IF REQUESTED
    if effective_forward_chat_id and sent_messages:
        source_chat_id = sent_messages[0].chat.id
        try:
            if group_sent_messages:
                group_chunks = [group_sent_messages[i:i+10] for i in range(0, len(group_sent_messages), 10)]
                for chunk in group_chunks:
                    for attempt in range(2):
                        try:
                            await bot.copy_media_group(
                                chat_id=effective_forward_chat_id,
                                from_chat_id=source_chat_id,
                                message_id=chunk[0].id,
                            )
                            LOGGER(__name__).info(f"Copied media group chunk to: {effective_forward_chat_id}")
                            break
                        except FloodWait as e:
                            wait_s = int(getattr(e, "value", 0) or 0)
                            LOGGER(__name__).warning(f"FloodWait while copying group: {wait_s}s")
                            if wait_s > 0 and attempt == 0:
                                await asyncio.sleep(wait_s + 1)
                                continue
                            raise
                    await asyncio.sleep(2)

            for anim_msg in animation_sent_messages:
                for attempt in range(2):
                    try:
                        await bot.copy_message(
                            chat_id=effective_forward_chat_id,
                            from_chat_id=source_chat_id,
                            message_id=anim_msg.id,
                        )
                        LOGGER(__name__).info(f"Copied animation to: {effective_forward_chat_id}")
                        break
                    except FloodWait as e:
                        wait_s = int(getattr(e, "value", 0) or 0)
                        LOGGER(__name__).warning(f"FloodWait while copying animation: {wait_s}s")
                        if wait_s > 0 and attempt == 0:
                            await asyncio.sleep(wait_s + 1)
                            continue
                        raise
                await asyncio.sleep(1)

        except Exception as e:
            LOGGER(__name__).error(f"Failed to forward to {effective_forward_chat_id}: {e}")

    # CLEAN UP
    if anim_notice:
        await anim_notice.delete()
    await loading.delete()

    # FINAL SUMMARY
    await message.reply(
        "**✅ Multi-link Group Process Complete!**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 **Links processed** : `{len(links)}` link(s)\n"
        f"📤 **Sent as group**   : `{len(group_sent_messages)}` file(s)\n"
        f"🎞️ **GIFs sent**       : `{len(animation_sent_messages)}` file(s)\n"
        f"⏭️ **Skipped**         : `{skipped}` (no media/album)\n"
        f"❌ **Failed**          : `{failed}` error(s)\n"
        + (f"📡 **Stream mode**     : `on`" if force_stream else "")
    )

    for path in temp_paths + thumb_paths:
        cleanup_download(path)



@bot.on_message(filters.command("sdl") & filters.user(PyroConf.OWNER_ID))
async def download_range_all(bot: Client, message: Message):
    # SUPPORT BOTH SPACE AND NEWLINE SEPARATED LINKS
    raw = message.text.split(None, 1)  # split off the command
    if len(raw) < 2:
        await message.reply(
            "🚀 **Batch Download Process**\n"
            "`/sdl link1 link2 link3 ... [s]`\n\n"
            "💡 **Example:**\n"
            "`/sdl https://t.me/chat1/100 https://t.me/chat2/50`\n"
            "`/sdl https://t.me/chat1/100 https://t.me/chat2/50 s`\n\n"
            "📎 Links can be space or newline separated.\n"
            "📡 Add `s` at the end to force streamable sending."
        )
        return

    # SPLIT ON WHITESPACE/NEWLINES, FILTER OUT EMPTY STRINGS
    parts = raw[1].split()

    # CHECK IF LAST PART IS "s" FLAG
    if parts and parts[-1].lower() == "s":
        force_stream = True
        parts = parts[:-1]
    else:
        force_stream = False

    # VALIDATE ALL REMAINING PARTS ARE t.me LINKS
    links = [p for p in parts if p.startswith("https://t.me/")]
    invalid = [p for p in parts if not p.startswith("https://t.me/")]

    if not links:
        return await message.reply("**❌ No valid t.me links found.**")

    if invalid:
        await message.reply(
            f"⚠️ **Skipping {len(invalid)} invalid entry(ies):**\n"
            + "\n".join(f"`{i}`" for i in invalid)
        )

    loading = await message.reply(
        f"📥 **__Processing {len(links)} link(s)…__**"
        + (" `[stream mode]`" if force_stream else "")
    )

    downloaded = skipped = failed = 0

    for idx, url in enumerate(links, 1):
        await loading.edit(f"📥 **__Downloading {idx}/{len(links)}…__**")
        try:
            task = track_task(handle_download(bot, message, url, force_stream=force_stream))
            try:
                await task
                downloaded += 1
            except asyncio.CancelledError:
                await loading.delete()
                return await message.reply(
                    f"**❌ Batch canceled** after downloading `{downloaded}` post(s)."
                )
        except Exception as e:
            failed += 1
            LOGGER(__name__).error(f"Error at {url}: {e}")

        await asyncio.sleep(3)

    await loading.delete()
    await message.reply(
        "**✅ Batch Process Complete!**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📥 **Downloaded** : `{downloaded}` post(s)\n"
        f"⏭️ **Skipped**    : `{skipped}` (no content)\n"
        f"❌ **Failed**     : `{failed}` error(s)\n"
        + (f"📡 **Stream mode** : `on`" if force_stream else "")
    )


@bot.on_message(filters.command("dlrange") & filters.private & filters.user(PyroConf.OWNER_ID))
async def download_range_old(bot: Client, message: Message):
    args = message.text.split()

    if len(args) != 3 or not all(arg.startswith("https://t.me/") for arg in args[1:]):
        await message.reply("❌ Usage:\n`/dlrange <start_link> <end_link>`\n\nExample:\n`/dlrange https://t.me/mychannel/100 https://t.me/mychannel/120`")
        return

    try:
        start_chat, start_id = getChatMsgID(args[1])
        end_chat, end_id = getChatMsgID(args[2])
    except Exception as e:
        return await message.reply(f"❌ Error parsing links:\n{e}")

    if start_chat != end_chat:
        return await message.reply("❌ Both links must be from the same channel.")

    if start_id > end_id:
        return await message.reply("❌ Start ID must be less than or equal to End ID.")

    await message.reply(f"📥 **Downloading posts from {start_id} to {end_id}...**")

    for msg_id in range(start_id, end_id + 1):
        try:
            url = f"https://t.me/{start_chat}/{msg_id}"
            await handle_download(bot, message, url)
            await asyncio.sleep(2)
        except Exception as e:
            await message.reply(f"❌ Error at {url}: {e}")


@bot.on_message(filters.private & ~filters.command(COMMANDS))
async def handle_any_message(bot: Client, message: Message):
    if message.text and not message.text.startswith("/"):
        parts = message.text.strip().split()

        # CHECK IF LAST WORD IS "s" FLAG
        if parts and parts[-1].lower() == "s":
            force_stream = True
            parts = parts[:-1]
        else:
            force_stream = False

        # FILTER VALID LINKS
        links = [p for p in parts if p.startswith("https://t.me/")]

        if not links:
            return

        for url in links:
            await track_task(handle_download(bot, message, url, force_stream=force_stream))
            await asyncio.sleep(3)



@bot.on_message(filters.command("stats"))
async def stats(_, message: Message):
    currentTime = get_readable_time(time() - PyroConf.BOT_START_TIME)
    total, used, free = shutil.disk_usage(".")
    total = get_readable_file_size(total)
    used = get_readable_file_size(used)
    free = get_readable_file_size(free)
    sent = get_readable_file_size(psutil.net_io_counters().bytes_sent)
    recv = get_readable_file_size(psutil.net_io_counters().bytes_recv)
    cpuUsage = psutil.cpu_percent(interval=0.5)
    memory = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    process = psutil.Process(os.getpid())

    stats = (
        "**≧◉◡◉≦ Bot is Up and Running successfully.**\n\n"
        f"**➜ Bot Uptime:** `{currentTime}`\n"
        f"**➜ Total Disk Space:** `{total}`\n"
        f"**➜ Used:** `{used}`\n"
        f"**➜ Free:** `{free}`\n"
        f"**➜ Memory Usage:** `{round(process.memory_info()[0] / 1024**2)} MiB`\n\n"
        f"**➜ Upload:** `{sent}`\n"
        f"**➜ Download:** `{recv}`\n\n"
        f"**➜ CPU:** `{cpuUsage}%` | "
        f"**➜ RAM:** `{memory}%` | "
        f"**➜ DISK:** `{disk}%`"
    )
    await message.reply(stats)


@bot.on_message(filters.command("logs") & filters.private & filters.user(PyroConf.OWNER_ID))
async def logs(_, message: Message):
    if os.path.exists("logs.txt"):
        await message.reply_document(document="logs.txt", caption="**Logs**")
    else:
        await message.reply("**Not exists**")


@bot.on_message(filters.command("killall") & filters.user(PyroConf.OWNER_ID))
async def cancel_all_tasks(_, message: Message):
    cancelled = 0
    for task in list(RUNNING_TASKS):
        if not task.done():
            task.cancel()
            cancelled += 1
    await message.reply(f"**Cancelled {cancelled} running task(s).**")
    
    
@bot.on_message(filters.command("eval") & filters.user(PyroConf.OWNER_ID))
async def eval_command(client, message):
    status_message = await message.reply_text("`Processing ...`")
    cmd = message.text.split(" ", maxsplit=1)[1]

    reply_to_ = message
    if message.reply_to_message:
        reply_to_ = message.reply_to_message

    old_stderr = sys.stderr
    old_stdout = sys.stdout
    redirected_output = sys.stdout = io.StringIO()
    redirected_error = sys.stderr = io.StringIO()
    stdout, stderr, exc, result = None, None, None, None

    try:
        # Run the user-provided code and capture the result of the last expression
        result = await aexec(cmd, client, message)
    except Exception as e:
        exc = traceback.format_exc()
        error_type = e.__class__.__name__
        error_message = str(e)
        evaluation = (
            f"❌ **Error**: `{error_type}`\n"
            f"**Message**: `{error_message}`\n"
            f"**Traceback**:\n<code>{exc}</code>"
        )
    else:
        stdout = redirected_output.getvalue()
        stderr = redirected_error.getvalue()
        formatted_result = json_parser(result, indent=2)
        if stderr:
            evaluation = f"⚠️ **Stderr**:\n<code>{stderr}</code>"
        elif stdout:
            evaluation = f"<code>{stdout}</code>"
        elif result is not None:  # If the last expression returned something
            evaluation = f"<code>{formatted_result}</code>"
        else:
            evaluation = "✅ **Success**"
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    final_output = "<b>EVAL</b>: "
    final_output += f"<code>{cmd}</code>\n\n"
    final_output += "<b>OUTPUT</b>:\n"
    final_output += f"{evaluation.strip()} \n"

    # Maintain a history of eval commands (max 25 entries)
    eval_history.append(cmd)
    if len(eval_history) > 25:
        eval_history.pop(0)

    if len(final_output) > MAX_MESSAGE_LENGTH:
        with io.BytesIO(str.encode(final_output)) as out_file:
            out_file.name = "eval.txt"
            await reply_to_.reply_document(
                document=out_file,
                caption=cmd[: MAX_MESSAGE_LENGTH // 4 - 1],
                disable_notification=True,
                quote=True,
            )
            os.remove("eval.txt")
    else:
        await reply_to_.reply_text(final_output, quote=True)
    await status_message.delete()


async def aexec(code, client, message):
    indent = "    "  # 4 spaces for consistent indentation
    
    header = (
        "async def __aexec(client, message):\n"
        f"{indent}import os\n"
        f"{indent}import requests\n"
        f"{indent}from pprint import pformat\n"
        f"{indent}neo = message\n"
        f"{indent}e = message = event = neo\n"
        f"{indent}r = reply = message.reply_to_message\n"
        f"{indent}chat = message.chat.id\n"
        f"{indent}c = client\n"
        f"{indent}to_photo = message.reply_photo\n"
        f"{indent}to_video = message.reply_video\n"
        f"{indent}p = print\n"
        f"{indent}_result = None\n"
    )
    
    lines = code.split("\n")
    try:
        # Try to compile the last line as an expression.
        compile(lines[-1], "<string>", "eval")
        # Indent all lines except the last.
        body = "\n".join(indent + l for l in lines[:-1])
        # Append the last line to capture its return value.
        last_line = "\n" + indent + "_result = " + lines[-1]
    except SyntaxError:
        body = "\n".join(indent + l for l in lines)
        last_line = ""
    
    # Add a final return statement to return the captured result.
    return_line = "\n" + indent + "return _result\n"
    full_code = header + body + last_line + return_line
    
    # Dynamically compile and execute the function definition.
    exec(full_code)
    result = await locals()["__aexec"](client, message)
    return result


# Add a command to view history
@bot.on_message(filters.command("ehis") & filters.user(PyroConf.OWNER_ID))
async def show_eval_history(_, message):
    # Add numbering to each command and wrap in <code> tags
    formatted_history = "\n".join(f"<b>{i + 1}.</b> <code>{cmd}</code>" for i, cmd in enumerate(reversed(eval_history)))

    # Check if the message exceeds Telegram's character limit
    if len(formatted_history) > MAX_MESSAGE_LENGTH:
        # Send as a text file
        with io.BytesIO(str.encode(formatted_history)) as out_file:
            out_file.name = "eval_history.txt"
            await message.reply_document(
                document=out_file,
                caption="__Limit exceeded, so sending as file__",
                quote=True,
            )
            os.remove("eval_history.txt")
    else:
        # Send as a regular message
        await message.reply_text(f"<b>EVAL HISTORY:</b>\n{formatted_history}", quote=True)
    

@bot.on_message(filters.command("bash") & filters.user(PyroConf.OWNER_ID))
async def execution(_, message):
    status_message = await message.reply_text("`Processing ...`")
    cmd = message.text.split(" ", maxsplit=1)[1]

    # Replace command with alias if it exists
    cmd = COMMAND_ALIASES.get(cmd, cmd)

    reply_to_ = message
    if message.reply_to_message:
        reply_to_ = message.reply_to_message

    try:
        # Log the command
        logging.info(f"Command executed by {message.from_user.id}: {cmd}")

        # Run the command with a timeout
        process = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            await process.kill()  # Kill the process if it times out
            await status_message.edit_text("❌ **Timeout**: The command took too long to execute.")
            return

        e = stderr.decode().strip() if stderr else "😂"
        o = stdout.decode().strip() if stdout else "😐"

        OUTPUT = ""
        OUTPUT += f"<b>QUERY:</b>\n<u>Command:</u>\n<code>{cmd}</code> \n"
        OUTPUT += f"<u>PID</u>: <code>{process.pid}</code>\n\n"
        OUTPUT += f"<b>stderr</b>: \n<code>{e}</code>\n\n"
        OUTPUT += f"<b>stdout</b>: \n<code>{o}</code>"

        if len(OUTPUT) > MAX_MESSAGE_LENGTH:
            with BytesIO(str.encode(OUTPUT)) as out_file:
                out_file.name = "exec.txt"
                await reply_to_.reply_document(
                    document=out_file,
                    caption=cmd[: MAX_MESSAGE_LENGTH // 4 - 1],
                    disable_notification=True,
                    quote=True,
                )
                os.remove("exec.txt")
        else:
            await reply_to_.reply_text(OUTPUT, quote=True)

        # Add command to history
        command_history.append(cmd)
        if len(command_history) > 25:  # Keep only the last 10 commands
            command_history.pop(0)

    except Exception as ex:
        await reply_to_.reply_text(f"❌ **Error**: {str(ex)}", quote=True)
    finally:
        await status_message.delete()
        
        
@bot.on_message(filters.command("bhis") & filters.user(PyroConf.OWNER_ID))
async def show_history(_, message):
    # Add numbering to each command and wrap in <code> tags
    formatted_history = "\n".join(f"<b>{i + 1}.</b> <code>{cmd}</code>" for i, cmd in enumerate(reversed(command_history)))

    # Check if the message exceeds Telegram's character limit
    if len(formatted_history) > MAX_MESSAGE_LENGTH:
        # Send as a text file
        with BytesIO(str.encode(formatted_history)) as out_file:
            out_file.name = "command_history.txt"
            await message.reply_document(
                document=out_file,
                caption="__Limit exceeded, so sending as file__",
                quote=True,
            )
            os.remove("command_history.txt")
    else:
        # Send as a regular message
        await message.reply_text(f"<b>Command History:</b>\n{formatted_history}", quote=True)
        

@bot.on_message(filters.command("template") & filters.private)
async def set_template(client, message):
    if len(message.command) > 1 and message.command[1].lower() == "save":
        save_template_to_file(get_active_template())
        return await message.reply("💾 __Template saved to file (persistent).__")

    try:
        response = await message.ask(
            "**Please send your new progress template now.**\n\n"
            "**Placeholders:** `{bar}` `{percentage}` `{current}` `{total}` `{speed}` `{elapsed}` `{eta}` `{status_emoji}` `{status_message}`\n\n"
            "__You can type__ `/cancel` __to abort.__",
            timeout=60
        )

        if response.text.strip().lower() == "/cancel":
            return await message.reply("❌ Cancelled.")

        set_memory_template(response.text)
        await message.reply("✅ __Custom progress template updated (in-memory).__")

    except asyncio.TimeoutError:
        await message.reply("⌛ **Timeout:** `No response received.`")
    except Exception as e:
        await message.reply(f"⚠️ **Unexpected error:** `{e}`")
        
        
@bot.on_message(filters.command("retemp") & filters.private)
async def reset_template_command(client, message):
    reset_template()
    await message.reply("🔄 **Template reset to default (in-memory and file).**")
    

@bot.on_message(filters.command("ping"))
async def ping_command(client, message):
    start = time()
    reply = await message.reply("🏓 **Pong!**")
    end = time()

    ping_ms = round((end - start) * 1000, 2)
    uptime_sec = time() - START_TIME
    uptime_str = get_readable_time(uptime_sec)

    # Optional: create a color bar based on ping quality
    if ping_ms < 100:
        ping_color = "🟢"
    elif ping_ms < 250:
        ping_color = "🟡"
    else:
        ping_color = "🔴"

    text = f"""
🏓 **PONG!**

{ping_color} **Ping:** `{ping_ms} ms`
⏱️ **Uptime:** `{uptime_str}`

⚙️ **Bot Status:** __Online & Stable__
"""
    await reply.edit(text)
    
    
@bot.on_message(filters.command("forward") & filters.user(PyroConf.OWNER_ID))
async def forward_toggle(client, message):
    if not PyroConf.FORWARD_CHAT_ID:
        return await message.reply("❌ `FORWARD_CHAT_ID` is not set in config.")

    args = message.command
    if len(args) < 2:
        return await message.reply("on, off, status")

    action = args[1].lower()

    if action == "on":
        PyroConf.FORWARD_ENABLED = True
        await message.reply(f"✅ File forwarding **enabled**.\nForwarding to: `{PyroConf.FORWARD_CHAT_ID}`")
    elif action == "off":
        PyroConf.FORWARD_ENABLED = False
        await message.reply("🚫 File forwarding **disabled**.")
    elif action == "status":
        state = "✅ Enabled" if PyroConf.FORWARD_ENABLED else "🚫 Disabled"
        chat = f"`{PyroConf.FORWARD_CHAT_ID}`" if PyroConf.FORWARD_CHAT_ID else "Not set"
        await message.reply(
            f"**Forward Status:** {state}\n"
            f"**Forward Chat ID:** {chat}"
        )
    else:
        await message.reply("on, off, status")    
    
    
async def initialize():
    global forward_chat_id

    if PyroConf.FORWARD_CHAT_ID:
        forward_chat_id = await resolve_forward_chat_id(PyroConf.FORWARD_CHAT_ID)
        PyroConf.FORWARD_ENABLED = True  # sync with resolved state
        LOGGER(__name__).info(f"Auto-forward enabled. Target chat: {forward_chat_id}")
    else:
        PyroConf.FORWARD_ENABLED = False
        LOGGER(__name__).info("Auto-forward disabled. FORWARD_CHAT_ID not set.")


if __name__ == "__main__":
    # Create folders if they don't exist
    Path("assets").mkdir(parents=True, exist_ok=True)
    Path("default_thumbs").mkdir(parents=True, exist_ok=True)
    try:
        LOGGER(__name__).info("Bot Started!")
        asyncio.get_event_loop().run_until_complete(initialize())
        user.start()
        bot.run()
    except KeyboardInterrupt:
        pass
    except Exception as err:
        LOGGER(__name__).error(err)
    finally:
        LOGGER(__name__).info("Bot Stopped")
