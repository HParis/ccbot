"""Safe message sending helpers with MarkdownV2 fallback.

Provides utility functions for sending Telegram messages with automatic
format conversion and fallback to plain text on failure.

Uses telegramify-markdown for MarkdownV2 formatting.

Functions:
  - send_with_fallback: Send with formatting → plain text fallback
  - send_photo: Image sending — auto-switches photo vs document by size
  - safe_reply: Reply with formatting, fallback to plain text
  - safe_edit: Edit message with formatting, fallback to plain text
  - safe_send: Send message with formatting, fallback to plain text

Rate limiting is handled globally by AIORateLimiter on the Application.
RetryAfter exceptions are re-raised so callers (queue worker) can handle them.
"""

import io
import logging
from typing import Any

from PIL import Image, UnidentifiedImageError
from telegram import (
    Bot,
    InputMediaDocument,
    InputMediaPhoto,
    LinkPreviewOptions,
    Message,
)
from telegram.error import RetryAfter

from ..markdown_v2 import convert_markdown
from ..rich_message import markdown_to_rich_blocks
from ..transcript_parser import TranscriptParser

logger = logging.getLogger(__name__)

# Telegram's sendPhoto re-encodes anything past ~1600px or a few MB. Below
# this threshold the inline preview is lossless enough; above it we fall back
# to sendDocument to preserve detail.
_PHOTO_MAX_SIDE_PX = 1600
_PHOTO_MAX_BYTES = 5 * 1024 * 1024

# JPEG q=85 keeps screenshot detail effectively lossless to the eye while
# shrinking PNGs significantly. WebP would compress better, but Telegram
# renders static WebP files as stickers (no zoom, no preview), so we stick
# with JPEG for compatibility.
_JPEG_QUALITY = 85


def strip_sentinels(text: str) -> str:
    """Strip expandable quote sentinel markers for plain text fallback."""
    for s in (
        TranscriptParser.EXPANDABLE_QUOTE_START,
        TranscriptParser.EXPANDABLE_QUOTE_END,
    ):
        text = text.replace(s, "")
    return text


def _ensure_formatted(text: str) -> str:
    """Convert markdown to MarkdownV2."""
    return convert_markdown(text)


PARSE_MODE = "MarkdownV2"


# Disable link previews in all messages to reduce visual noise
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def send_with_fallback(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs: Any,
) -> Message | None:
    """Send message with MarkdownV2, falling back to plain text on failure.

    Returns the sent Message on success, None on failure.
    RetryAfter is re-raised for caller handling.
    """
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            return await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
            return None


async def send_rich_message(
    bot: Bot,
    chat_id: int,
    blocks: list[dict],
    message_thread_id: int | None = None,
    **kwargs: Any,
) -> Message | None:
    """Send a Bot API 10.2 Rich Message via PTB's do_api_request escape hatch.

    PTB 22.6 has no native `sendRichMessage` wrapper; `do_api_request` forwards
    the call while reusing PTB's auth, request pool, and AIORateLimiter. `blocks`
    is a list of InputRichBlock dicts — build them with
    `rich_message.markdown_to_rich_blocks`.

    Returns the sent Message, or None on failure. RetryAfter is re-raised for
    the queue worker to handle.
    """
    if not blocks:
        return None
    if message_thread_id is not None:
        kwargs.setdefault("message_thread_id", message_thread_id)
    try:
        return await bot.do_api_request(
            "sendRichMessage",
            api_kwargs={
                "chat_id": chat_id,
                "rich_message": {"blocks": blocks},
                **kwargs,
            },
            return_type=Message,
        )
    except RetryAfter:
        raise
    except Exception as e:
        logger.error("Failed to send rich message to %d: %s", chat_id, e)
        return None


async def send_markdown_as_rich(
    bot: Bot,
    chat_id: int,
    text: str,
    message_thread_id: int | None = None,
    **kwargs: Any,
) -> Message | None:
    """Render Markdown to rich blocks and send, falling back to plain send.

    Renders `text` (tables/headings/lists/math + inline styles) into a Rich
    Message. If rendering yields nothing or the rich send fails, falls back to
    the normal MarkdownV2 `send_with_fallback` path so no message is lost.
    """
    blocks = markdown_to_rich_blocks(text)
    if blocks:
        sent = await send_rich_message(
            bot, chat_id, blocks, message_thread_id=message_thread_id, **kwargs
        )
        if sent is not None:
            return sent
    return await send_with_fallback(
        bot, chat_id, text, message_thread_id=message_thread_id, **kwargs
    )


def _filename_for(media_type: str, index: int) -> str:
    """Pick a filename for a tool_result image based on its media type.

    Telegram uses the filename's extension to render an inline preview for
    document uploads, so we map the MIME type back to a sensible suffix.
    """
    ext = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
    }.get(media_type.lower(), "png")
    return f"image_{index}.{ext}"


def _maybe_compress(media_type: str, raw_bytes: bytes) -> tuple[str, bytes]:
    """Re-encode large images to JPEG q=85 to cut size before sending.

    Skips work when the image already fits the photo path — there's nothing
    to gain. For larger images, the JPEG version is used only if it actually
    came out smaller than the original. RGBA images are flattened onto a
    white background since JPEG has no alpha channel.
    """
    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            w, h = im.size
            if len(raw_bytes) <= _PHOTO_MAX_BYTES and max(w, h) <= _PHOTO_MAX_SIDE_PX:
                return media_type, raw_bytes
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[-1])
                source = bg
            else:
                source = im.convert("RGB") if im.mode != "RGB" else im
            buf = io.BytesIO()
            source.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
            compressed = buf.getvalue()
            if compressed and len(compressed) < len(raw_bytes):
                return "image/jpeg", compressed
    except (UnidentifiedImageError, OSError) as e:
        logger.debug("Skipping JPEG compression: %s", e)
    return media_type, raw_bytes


def _photo_safe(raw_bytes: bytes) -> bool:
    """Return True if the image is small enough to send as a photo without
    Telegram visibly downscaling or re-compressing it.

    Telegram's sendPhoto re-encodes anything past roughly 1600px on the long
    side or a few megabytes; below that the inline preview is effectively
    lossless. For anything larger, sendDocument preserves the original bytes.
    """
    if len(raw_bytes) > _PHOTO_MAX_BYTES:
        return False
    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            w, h = im.size
    except (UnidentifiedImageError, OSError):
        return False
    return max(w, h) <= _PHOTO_MAX_SIDE_PX


async def send_photo(
    bot: Bot,
    chat_id: int,
    image_data: list[tuple[str, bytes]],
    **kwargs: Any,
) -> None:
    """Send image(s), picking sendPhoto vs sendDocument per size.

    - Small images (<=1600px long side, <=5MB) go through sendPhoto so the
      user sees an inline preview. Telegram doesn't visibly recompress them
      at that size.
    - Larger images go through sendDocument to preserve the original pixels,
      since sendPhoto would JPEG-re-encode and downscale them.

    Telegram's sendMediaGroup can't mix photo and document items, so a batch
    falls back to documents if any single image needs document treatment.

    Rate limiting is handled globally by AIORateLimiter on the Application.

    Args:
        bot: Telegram Bot instance
        chat_id: Target chat ID
        image_data: List of (media_type, raw_bytes) tuples
        **kwargs: Extra kwargs passed to send_photo/send_document/send_media_group
    """
    if not image_data:
        return
    processed = [_maybe_compress(mt, raw) for mt, raw in image_data]
    use_photo = all(_photo_safe(raw) for _, raw in processed)
    try:
        if len(processed) == 1:
            media_type, raw_bytes = processed[0]
            buf = io.BytesIO(raw_bytes)
            buf.name = _filename_for(media_type, 0)
            if use_photo:
                await bot.send_photo(chat_id=chat_id, photo=buf, **kwargs)
            else:
                await bot.send_document(
                    chat_id=chat_id,
                    document=buf,
                    disable_content_type_detection=False,
                    **kwargs,
                )
            return

        media: list[InputMediaPhoto | InputMediaDocument] = []
        for i, (media_type, raw_bytes) in enumerate(processed):
            buf = io.BytesIO(raw_bytes)
            buf.name = _filename_for(media_type, i)
            if use_photo:
                media.append(InputMediaPhoto(media=buf))
            else:
                media.append(
                    InputMediaDocument(
                        media=buf,
                        disable_content_type_detection=False,
                    )
                )
        await bot.send_media_group(chat_id=chat_id, media=media, **kwargs)
    except RetryAfter:
        raise
    except Exception as e:
        logger.error("Failed to send image to %d: %s", chat_id, e)


async def safe_reply(message: Message, text: str, **kwargs: Any) -> Message:
    """Reply with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        return await message.reply_text(
            _ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            return await message.reply_text(strip_sentinels(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to reply: {e}")
            raise


async def safe_edit(target: Any, text: str, **kwargs: Any) -> None:
    """Edit message with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    try:
        await target.edit_message_text(
            _ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            await target.edit_message_text(strip_sentinels(text), **kwargs)
        except RetryAfter:
            raise
        except Exception as e:
            logger.error("Failed to edit message: %s", e)


async def safe_send(
    bot: Bot,
    chat_id: int,
    text: str,
    message_thread_id: int | None = None,
    **kwargs: Any,
) -> None:
    """Send message with formatting, falling back to plain text on failure."""
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    if message_thread_id is not None:
        kwargs.setdefault("message_thread_id", message_thread_id)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=_ensure_formatted(text),
            parse_mode=PARSE_MODE,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception:
        try:
            await bot.send_message(
                chat_id=chat_id, text=strip_sentinels(text), **kwargs
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error(f"Failed to send message to {chat_id}: {e}")
