# Copyright (C) @TheSmartBisnu
# Channel: https://t.me/itsSmartDev

import os
import json
import asyncio
from json import JSONDecodeError
from time import time
from PIL import Image
from logger import LOGGER, logger
from typing import Optional, Union, Any
from asyncio.subprocess import PIPE
from asyncio import create_subprocess_exec, create_subprocess_shell, wait_for

from pyleaves import Leaves
from pyrogram.parser import Parser
from pyrogram.utils import get_channel_id
from pyrogram.errors import FloodWait, BadRequest
from pyrogram.types import (
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
    InputMediaAnimation,
    Voice,
)

from helpers.files import (
    fileSizeLimit,
    cleanup_download
)

from helpers.msg import (
    get_parsed_msg
)

# VIDEO_THUMB_LOCATION = os.path.join(os.getcwd(), "assets", "video_thumb.jpg")
CUSTOM_THUMB_DIR = os.path.join(os.getcwd(), "default_thumbs")
os.makedirs(CUSTOM_THUMB_DIR, exist_ok=True)  # Create directory if it doesn't exist

# Default progress bar template
PROGRESS_BAR = """🌊 {bar} `{percentage:.1f}%`

**➜ Progress:** `{current}` **of** `{total}`
**➜ Speed:** `{speed}/s`
**➜ Elapsed:** `{elapsed}`
**➜ ETA:** `{eta}`
{status_emoji} __{status_message}__
"""

TEMPLATE_FILE = "progress_template.txt"
memory_template = PROGRESS_BAR


def get_active_template():
    """Get the current progress bar template (prefers in-memory)."""
    return memory_template or load_template_from_file()


def load_template_from_file():
    """Load saved template from file."""
    if os.path.exists(TEMPLATE_FILE):
        with open(TEMPLATE_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return PROGRESS_BAR


def save_template_to_file(template: str):
    """Save template to disk (persistent)."""
    with open(TEMPLATE_FILE, "w", encoding="utf-8") as f:
        f.write(template)


def reset_template():
    """Reset both in-memory and file to default."""
    global memory_template
    memory_template = PROGRESS_BAR
    save_template_to_file(PROGRESS_BAR)


def set_memory_template(template: str):
    """Set a new in-memory template."""
    global memory_template
    memory_template = template
    

async def cmd_exec(cmd, shell=False):
    if shell:
        proc = await create_subprocess_shell(cmd, stdout=PIPE, stderr=PIPE)
    else:
        proc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await proc.communicate()
    try:
        stdout = stdout.decode().strip()
    except:
        stdout = "Unable to decode the response!"
    try:
        stderr = stderr.decode().strip()
    except:
        stderr = "Unable to decode the error!"
    return stdout, stderr, proc.returncode


async def get_media_info(path):
    try:
        result = await cmd_exec(
            [
                "ffprobe",
                "-hide_banner",
                "-loglevel",
                "error",
                "-print_format",
                "json",
                "-show_format",
                path,
            ]
        )
    except Exception as e:
        LOGGER(__name__).error(
            f"Get Media Info: {e}. Mostly File not found! - File: {path}"
        )
        return 0, None, None
    if result[0] and result[2] == 0:
        fields = eval(result[0]).get("format")
        if fields is None:
            LOGGER(__name__).info(f"get_media_info: {result}")
            return 0, None, None
        duration = round(float(fields.get("duration", 0)))
        tags = fields.get("tags", {})
        artist = tags.get("artist") or tags.get("ARTIST") or tags.get("Artist")
        title = tags.get("title") or tags.get("TITLE") or tags.get("Title")
        return duration, artist, title
    return 0, None, None


async def get_video_thumbnail(video_file, duration):
    os.makedirs(os.path.join(os.getcwd(), "assets"), exist_ok=True)
    # Generate a unique filename based on the video filename and timestamp
    base_name = os.path.splitext(os.path.basename(video_file))[0]
    thumb_location = os.path.join(os.getcwd(), "assets", f"{base_name}_thumb_{int(time())}.jpg")
    
    if duration is None:
        duration = (await get_media_info(video_file))[0]
    if duration == 0:
        duration = 3
    duration = duration // 2
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{duration}",
        "-i",
        video_file,
        "-vf",
        "thumbnail",
        "-q:v",
        "1",
        "-frames:v",
        "1",
        "-threads",
        f"{os.cpu_count() // 2}",
        thumb_location,  # Use the unique path
    ]
    try:
        _, err, code = await wait_for(cmd_exec(cmd), timeout=60)
        if code != 0 or not os.path.exists(thumb_location):
            logger.error(
                f"Error while extracting thumbnail from video. Name: {video_file} stderr: {err}"
            )
            return None
    except:
        logger.error(
            f"Error while extracting thumbnail from video. Name: {video_file}. Error: Timeout some issues with ffmpeg with specific arch!"
        )
        return None
    return thumb_location


# Generate progress bar args dynamically using template
def progressArgs(action: str, progress_message, start_time):
    return (
        action,
        progress_message,
        start_time,
        get_active_template()  # <- uses latest template
    )



async def send_media(
    bot, message, chat_message, user, media_path, media_type, caption, progress_message, start_time,
    forward_chat_id=None,
    force_stream: bool = False
):
    file_size = os.path.getsize(media_path)

    if not await fileSizeLimit(file_size, message, "upload"):
        return

    progress_args = progressArgs("📥 Uploading Progress", progress_message, start_time)
    LOGGER(__name__).info(f"Uploading media: {media_path} ({media_type})")

    # Generate unique filename for the thumbnail
    thumb_filename = f"thumb_{int(time())}.jpg"
    custom_thumb_path = os.path.join(CUSTOM_THUMB_DIR, thumb_filename)

    sent_message = None  # TRACK SENT MESSAGE

    if media_type == "photo":
        sent_message = await message.reply_photo(
            media_path,
            caption=caption or "",
            progress=Leaves.progress_for_pyrogram,
            progress_args=progress_args,
        )
    elif media_type == "video":
        duration = (await get_media_info(media_path))[0]
        thumb = None
        width = 480
        height = 320

        # 1. FIRST TRY TO USE EXISTING TELEGRAM THUMBNAIL
        if hasattr(chat_message.video, 'thumbs') and chat_message.video.thumbs:
            try:
                thumb = await user.download_media(
                    chat_message.video.thumbs[0].file_id,
                    file_name=custom_thumb_path
                )
                if thumb and os.path.exists(thumb):
                    with Image.open(thumb) as img:
                        width, height = img.size
                    LOGGER(__name__).info(f"Using existing Telegram thumbnail: {thumb} {width}, {height}")
                else:
                    thumb = None
            except Exception as e:
                LOGGER(__name__).warning(f"Failed to download Telegram thumbnail: {e}")
                if os.path.exists(custom_thumb_path):
                    os.remove(custom_thumb_path)
                thumb = None

        # 2. IF NO EXISTING THUMBNAIL, GENERATE ONE
        if thumb is None:
            thumb = await get_video_thumbnail(media_path, duration)
            if thumb is not None and thumb != "none":
                with Image.open(thumb) as img:
                    width, height = img.size
            elif thumb == "none":
                thumb = None
            LOGGER(__name__).info("Generated new thumbnail")

        sent_message = await message.reply_video(
            media_path,
            duration=duration,
            width=width,
            height=height,
            thumb=thumb,
            caption=caption or "",
            supports_streaming=True,
            progress=Leaves.progress_for_pyrogram,
            progress_args=progress_args,
        )
        if thumb and os.path.exists(thumb):
            os.remove(thumb)

    elif media_type == "audio":
        duration, artist, title = await get_media_info(media_path)
        sent_message = await message.reply_audio(
            media_path,
            duration=duration,
            performer=artist,
            title=title,
            caption=caption or "",
            progress=Leaves.progress_for_pyrogram,
            progress_args=progress_args,
        )

    elif media_type == "document":
        mime = getattr(chat_message.document, "mime_type", "") or ""

        # ── STREAM MODE: re-cast document as photo or video ──
        if force_stream and mime.startswith("image/"):
            LOGGER(__name__).info(f"Stream mode: sending document as photo (mime: {mime})")
            sent_message = await message.reply_photo(
                media_path,
                caption=caption or "",
                progress=Leaves.progress_for_pyrogram,
                progress_args=progress_args,
            )

        elif force_stream and mime.startswith("video/"):
            LOGGER(__name__).info(f"Stream mode: sending document as video (mime: {mime})")
            duration = (await get_media_info(media_path))[0]
            thumb = None
            width = 480
            height = 320

            if hasattr(chat_message.document, 'thumbs') and chat_message.document.thumbs:
                try:
                    thumb = await user.download_media(
                        chat_message.document.thumbs[0].file_id,
                        file_name=custom_thumb_path
                    )
                    if thumb and os.path.exists(thumb):
                        with Image.open(thumb) as img:
                            width, height = img.size
                        LOGGER(__name__).info(f"Using existing Telegram thumbnail for doc-video: {thumb}")
                    else:
                        thumb = None
                except Exception as e:
                    LOGGER(__name__).warning(f"Failed to download doc-video thumbnail: {e}")
                    if os.path.exists(custom_thumb_path):
                        os.remove(custom_thumb_path)
                    thumb = None

            if thumb is None:
                thumb = await get_video_thumbnail(media_path, duration)
                if thumb is not None and thumb != "none":
                    with Image.open(thumb) as img:
                        width, height = img.size
                elif thumb == "none":
                    thumb = None
                LOGGER(__name__).info("Generated new thumbnail for doc-video")

            sent_message = await message.reply_video(
                media_path,
                duration=duration,
                width=width,
                height=height,
                thumb=thumb,
                caption=caption or "",
                supports_streaming=True,
                progress=Leaves.progress_for_pyrogram,
                progress_args=progress_args,
            )
            if thumb and os.path.exists(thumb):
                os.remove(thumb)

        else:
            # ── DEFAULT: send as document ──
            thumb = None
            width, height = 320, 320

            if hasattr(chat_message.document, 'thumbs') and chat_message.document.thumbs:
                try:
                    thumb = await user.download_media(
                        chat_message.document.thumbs[0].file_id,
                        file_name=custom_thumb_path
                    )
                    if thumb and os.path.exists(thumb):
                        with Image.open(thumb) as img:
                            width, height = img.size
                        LOGGER(__name__).info(f"Using existing Telegram document thumbnail: {thumb} {width}, {height}")
                    else:
                        thumb = None
                except Exception as e:
                    LOGGER(__name__).warning(f"Failed to download Telegram document thumbnail: {e}")
                    if os.path.exists(custom_thumb_path):
                        os.remove(custom_thumb_path)
                    thumb = None

            sent_message = await message.reply_document(
                media_path,
                caption=caption or "",
                thumb=thumb,
                progress=Leaves.progress_for_pyrogram,
                progress_args=progress_args,
            )
            if thumb and os.path.exists(thumb):
                os.remove(thumb)

    elif media_type == "animation":
        sent_message = await message.reply_animation(
            media_path,
            caption=caption or "",
            progress=Leaves.progress_for_pyrogram,
            progress_args=progress_args,
        )

    # FORWARD TO ADDITIONAL CHAT IF REQUESTED
    if forward_chat_id and sent_message:
        for attempt in range(2):
            try:
                await bot.copy_message(
                    chat_id=forward_chat_id,
                    from_chat_id=sent_message.chat.id,
                    message_id=sent_message.id,
                )
                LOGGER(__name__).info(f"Copied media to chat: {forward_chat_id}")
                break
            except FloodWait as e:
                wait_s = int(getattr(e, "value", 0) or 0)
                LOGGER(__name__).warning(f"FloodWait while copying media: {wait_s}s")
                if wait_s > 0 and attempt == 0:
                    await asyncio.sleep(wait_s + 1)
                    continue
                LOGGER(__name__).error(f"Failed to copy media after retry: FloodWait")
            except Exception as e:
                LOGGER(__name__).error(f"Failed to copy media to {forward_chat_id}: {e}")
                break
        

async def processMediaGroup(chat_message, bot, message, user, forward_chat_id=None, force_stream: bool = False):
    media_group_messages = await chat_message.get_media_group()
    valid_media = []
    animation_media = []  # SEPARATE LIST FOR ANIMATIONS
    temp_paths = []
    invalid_paths = []
    thumb_paths = []

    start_time = time()
    progress_message = await message.reply("📥 **__Downloading media group...__**")
    LOGGER(__name__).info(
        f"Downloading media group with {len(media_group_messages)} items..."
    )

    for msg in media_group_messages:
        if msg.photo or msg.video or msg.document or msg.audio or msg.animation:
            try:
                media_path = await msg.download(
                    progress=Leaves.progress_for_pyrogram,
                    progress_args=progressArgs(
                        "📥 **__Downloading Progress__**", progress_message, start_time
                    ),
                )
                temp_paths.append(media_path)

                caption = await get_parsed_msg(msg.caption or "", msg.caption_entities)

                if msg.photo:
                    valid_media.append(
                        InputMediaPhoto(media=media_path, caption=caption)
                    )
                elif msg.video:
                    duration = (await get_media_info(media_path))[0]
                    thumb = None
                    width = 480
                    height = 320

                    thumb_filename = f"group_thumb_{int(time())}.jpg"
                    custom_thumb_path = os.path.join(CUSTOM_THUMB_DIR, thumb_filename)

                    if hasattr(msg.video, 'thumbs') and msg.video.thumbs:
                        try:
                            thumb = await user.download_media(msg.video.thumbs[0].file_id, file_name=custom_thumb_path)
                            if thumb and os.path.exists(thumb):
                                with Image.open(thumb) as img:
                                    width, height = img.size
                                thumb_paths.append(thumb)
                                LOGGER(__name__).info("Using existing Telegram thumbnail for media group video")
                        except Exception as e:
                            LOGGER(__name__).warning(f"Failed to download Telegram thumbnail: {e}")
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
                elif msg.document:
                    mime = getattr(msg.document, "mime_type", "") or ""

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

                        thumb_filename = f"group_thumb_{int(time())}.jpg"
                        custom_thumb_path = os.path.join(CUSTOM_THUMB_DIR, thumb_filename)

                        if hasattr(msg.document, 'thumbs') and msg.document.thumbs:
                            try:
                                thumb = await user.download_media(
                                    msg.document.thumbs[0].file_id,
                                    file_name=custom_thumb_path
                                )
                                if thumb and os.path.exists(thumb):
                                    with Image.open(thumb) as img:
                                        width, height = img.size
                                    thumb_paths.append(thumb)
                                    LOGGER(__name__).info("Using existing Telegram thumbnail for doc-video (stream mode)")
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

                        thumb_filename = f"group_doc_thumb_{int(time())}.jpg"
                        custom_thumb_path = os.path.join(CUSTOM_THUMB_DIR, thumb_filename)

                        if hasattr(msg.document, 'thumbs') and msg.document.thumbs:
                            try:
                                thumb = await user.download_media(
                                    msg.document.thumbs[0].file_id,
                                    file_name=custom_thumb_path
                                )
                                if thumb and os.path.exists(thumb):
                                    with Image.open(thumb) as img:
                                        width, height = img.size
                                    thumb_paths.append(thumb)
                                    LOGGER(__name__).info("Using existing Telegram thumbnail for document")
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

                elif msg.audio:
                    valid_media.append(
                        InputMediaAudio(media=media_path, caption=caption)
                    )
                elif msg.animation:
                    # COLLECT ANIMATIONS SEPARATELY
                    animation_media.append(
                        InputMediaAnimation(media=media_path, caption=caption)
                    )

            except Exception as e:
                LOGGER(__name__).info(f"Error downloading media: {e}")
                if media_path and os.path.exists(media_path):
                    invalid_paths.append(media_path)
                continue

    LOGGER(__name__).info(
        f"Valid media count: {len(valid_media)}, Animation count: {len(animation_media)}"
    )
    anim_notice = None
    if animation_media:
        anim_notice = await message.reply("ℹ️ **Your album contains GIF(s), they will be sent separately after the media group.**")

    if not valid_media and not animation_media:
        await progress_message.delete()
        await message.reply("❌ No valid media found in the media group.")
        for path in invalid_paths + thumb_paths:
            cleanup_download(path)
        return False

    sent_messages = []
    group_sent_messages = []      # ONLY non-animation messages
    animation_sent_messages = []  # ONLY animation messages

    # SEND NON-ANIMATION MEDIA AS GROUP
    if valid_media:
        try:
            group_sent = await bot.send_media_group(chat_id=message.chat.id, media=valid_media)
            group_sent_messages.extend(group_sent)
            sent_messages.extend(group_sent)
            await progress_message.delete()
        except Exception as e:
            await message.reply(
                f"**❌ Failed to send media group, trying individual uploads**\n`{e}`"
            )
            for media in valid_media:
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
                    elif isinstance(media, Voice):
                        sent = await bot.send_voice(
                            chat_id=message.chat.id,
                            voice=media.media,
                            caption=media.caption,
                        )
                    sent_messages.append(sent)
                    group_sent_messages.append(sent)
                except Exception as individual_e:
                    await message.reply(
                        f"Failed to upload individual media: {individual_e}"
                    )

            await progress_message.delete()

    # SEND ANIMATIONS INDIVIDUALLY AFTER THE GROUP
    for anim in animation_media:
        try:
            sent = await bot.send_animation(
                chat_id=message.chat.id,
                animation=anim.media,
                caption=anim.caption,
            )
            animation_sent_messages.append(sent)
            sent_messages.append(sent)
            LOGGER(__name__).info("Sent animation separately after media group")
        except Exception as e:
            await message.reply(f"Failed to upload animation: {e}")

    # FORWARD TO ADDITIONAL CHAT IF REQUESTED
    if forward_chat_id and sent_messages:
        source_chat_id = sent_messages[0].chat.id
        try:
            # FORWARD MEDIA GROUP USING copy_media_group
            if group_sent_messages:
                for attempt in range(2):
                    try:
                        await bot.copy_media_group(
                            chat_id=forward_chat_id,
                            from_chat_id=source_chat_id,
                            message_id=group_sent_messages[0].id,
                        )
                        LOGGER(__name__).info(f"Copied media group to chat: {forward_chat_id}")
                        break
                    except FloodWait as e:
                        wait_s = int(getattr(e, "value", 0) or 0)
                        LOGGER(__name__).warning(f"FloodWait while copying media group: {wait_s}s")
                        if wait_s > 0 and attempt == 0:
                            await asyncio.sleep(wait_s + 1)
                            continue
                        raise

            # FORWARD ANIMATIONS INDIVIDUALLY USING copy_message
            for anim_msg in animation_sent_messages:
                for attempt in range(2):
                    try:
                        await bot.copy_message(
                            chat_id=forward_chat_id,
                            from_chat_id=source_chat_id,
                            message_id=anim_msg.id,
                        )
                        LOGGER(__name__).info(f"Copied animation to chat: {forward_chat_id}")
                        break
                    except FloodWait as e:
                        wait_s = int(getattr(e, "value", 0) or 0)
                        LOGGER(__name__).warning(f"FloodWait while copying animation: {wait_s}s")
                        if wait_s > 0 and attempt == 0:
                            await asyncio.sleep(wait_s + 1)
                            continue
                        raise

        except Exception as e:
            LOGGER(__name__).error(f"Failed to forward to {forward_chat_id}: {e}")

    if anim_notice:
        await anim_notice.delete()

    for path in temp_paths + invalid_paths + thumb_paths:
        cleanup_download(path)
    return True

    
    
def json_parser(data: Any, indent: Union[int, None] = None, ensure_ascii: bool = False) -> Any:
    """
    Parses and formats JSON-like data.
    
    Args:
        data: The input data to parse and format
        indent: Number of spaces for indentation. None for compact output
        ensure_ascii: If False, non-ASCII characters are allowed (default)
    
    Returns:
        Parsed and formatted data
    """
    if isinstance(data, (dict, list)):
        try:
            return json.dumps(data, indent=indent, ensure_ascii=ensure_ascii) if indent is not None else data
        except Exception:
            return data
 
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
            return json.dumps(parsed, indent=indent, ensure_ascii=ensure_ascii) if indent is not None else parsed
        except JSONDecodeError:
            return data
 
    return data
    