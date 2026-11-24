import io
import os
import logging
import aiohttp
from PIL import Image as PILImage
from maxapi import Dispatcher, Bot, F
from maxapi.enums.attachment import AttachmentType
from maxapi.types import MessageCreated
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder
from maxapi.types.attachments.buttons import CallbackButton

from infrastructure.max_bot.context import ctx
from utils.bot_messages import (
    PLANT_NO_FILE, PLANT_IMAGE_ONLY, PLANT_IDENTIFYING,
    PLANT_IMAGE_LOAD_ERROR, PLANT_IMAGE_DOWNLOAD_ERROR, PLANT_IMAGE_FORMAT_ERROR,
    PLANT_NOT_FOUND,
)

logger = logging.getLogger(__name__)

# API Олега
PLANT_API_URL = os.getenv("PLANT_API_URL", "http://194.156.118.21:8020/predict")

# STT сервис
STT_SERVICE_URL = os.getenv("STT_SERVICE_URL", "http://stt-service:8081/transcribe")

def register_attachment_handlers(dp: Dispatcher, bot: Bot) -> None:

    @dp.message_created(F.message.body.attachments)
    async def handle_attachments(event: MessageCreated) -> None:
        try:
            try:
                chat_id: int = event.message.recipient.chat_id
            except AttributeError:
                chat_id, _ = event.get_ids()

            attachments = event.message.body.attachments if event.message.body else []
            if not attachments:
                await bot.send_message(chat_id=chat_id, text=PLANT_NO_FILE)
                return

            attachment = attachments[0]

            # ——————————Защита от "повторных" сообщений————————
            attachment_url = getattr(attachment.payload, "url", None) if attachment.payload else None
            if attachment_url:
                cache_key = f"max:processed_attachment:{chat_id}:{attachment_url}"
                is_processed = await ctx.redis_client.get(cache_key)

                if is_processed:
                    logger.info(f"[{chat_id}] Дубликат запроса проигнорирован (URL: {attachment_url})")
                    return  # Молча игнорируем повторную отправку

                # Запоминаем, что этот запрос на 120 секунд
                await ctx.redis_client.set(cache_key, "1", ex=120)
            # —————————————————————————————————————————————————

            # ——————————Обработка голосовых сообщений————————
            if attachment.type == AttachmentType.AUDIO:
                await _handle_voice_message(bot, chat_id, attachment)
                return
            # ——————————————————————————————————————————————

            if (attachment.type != AttachmentType.IMAGE
                    or not attachment.payload
                    or not getattr(attachment.payload, "url", None)):
                await bot.send_message(chat_id=chat_id, text=PLANT_IMAGE_ONLY)
                return

            image_url = attachment.payload.url
            logger.info(f"[{chat_id}] Plant identification request, image URL: {image_url}")

            # 1. Скачиваем картинку
            try:
                async with ctx.session.get(image_url) as resp:
                    if resp.status == 200:
                        image_bytes = await resp.read()
                        content_type = resp.headers.get("content-type", "image/jpeg")
                    else:
                        await bot.send_message(chat_id=chat_id, text=PLANT_IMAGE_LOAD_ERROR)
                        return
            except aiohttp.ClientError as e:
                logger.error(f"[{chat_id}] Failed to download image: {e}")
                await bot.send_message(chat_id=chat_id, text=PLANT_IMAGE_DOWNLOAD_ERROR)
                return

            # 2. Приводим к JPEG
            if content_type not in ("image/jpeg", "image/png"):
                try:
                    buf = io.BytesIO()
                    PILImage.open(io.BytesIO(image_bytes)).convert("RGB").save(buf, format="JPEG")
                    image_bytes = buf.getvalue()
                    content_type = "image/jpeg"
                except Exception as e:
                    logger.error(f"[{chat_id}] Image conversion failed: {e}")
                    await bot.send_message(chat_id=chat_id, text=PLANT_IMAGE_FORMAT_ERROR)
                    return

            # 3. Отправляем в API Олега
            plant_data = await _identify_plant(image_bytes, image_url, content_type)

            if not plant_data or not plant_data.get("predictions"):
                await bot.send_message(chat_id=chat_id, text=PLANT_NOT_FOUND)
                return

            predictions = plant_data["predictions"]
            top = predictions[0]
            name = top.get("russian_name") or top.get("latin_name") or "неизвестное растение"

            # 4. Ответ с кнопками
            message_text = f"На картинке — {name}."
            builder = InlineKeyboardBuilder()
            builder.row(CallbackButton(
                text="📖 Рассказать подробнее",
                payload=f"plant_more:{name[:100]}",
            ))

            # Дополнительные варианты
            for pred in predictions[1:3]:
                alt_name = pred.get("russian_name") or pred.get("latin_name")
                if not alt_name:
                    continue
                builder.row(CallbackButton(
                    text=f"Это {alt_name}?",
                    payload=f"plant_more:{alt_name[:100]}",
                ))

            await bot.send_message(
                chat_id=chat_id,
                text=message_text,
                attachments=[builder.as_markup()],
            )

        except Exception as e:
            logger.error(f"Attachment handler error: {e}", exc_info=True)


async def _identify_plant(image_bytes: bytes, image_url: str, content_type: str) -> dict | None:
    """Отправляет картинку в API Олега, возвращает JSON с предсказаниями."""
    form = aiohttp.FormData()
    filename = image_url.rsplit("/", 1)[-1].split("?")[0] or "plant.jpg"
    form.add_field(
        "file",
        image_bytes,
        filename=filename,
        content_type=content_type,
    )
    try:
        async with ctx.session.post(
            f"{PLANT_API_URL}?top_k=1",
            data=form,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error(f"Plant API error {resp.status}: {text[:300]}")
                return None
            return await resp.json(content_type=None)
    except Exception as e:
        logger.error(f"Plant API request failed: {e}")
        return None

# ——————————Обработка голосовых сообщений————————
async def _handle_voice_message(bot: Bot, chat_id: int, attachment) -> None:
    try:
        payload = getattr(attachment, 'payload', None)
        audio_url = None
        if payload:
            if hasattr(payload, "url"):
                audio_url = payload.url
            elif hasattr(payload, 'audio') and hasattr(payload.audio, 'url'):
                audio_url = payload.audio.url

        # Показывает индикатор "печатает"
        try:
            await bot.send_action(chat_id=chat_id, action="TYPING")
        except Exception:
            pass

        # 1. Скачивает аудиофайл с сервера
        async with ctx.session.get(audio_url) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                logger.error(f"[{chat_id}] Failed to download audio: {resp.status} - {error_text[:200]}")
                await bot.send_message(chat_id=chat_id, text="❌ Ошибка загрузки аудиосообщения.")
                return

            audio_bytes = await resp.read()
            content_type = resp.headers.get("content-type", "audio/ogg")

        # 2. Отправляем файл в STT-сервис
        form = aiohttp.FormData()
        form.add_field('file', audio_bytes, filename='voice.ogg', content_type=content_type)

        async with ctx.session.post(STT_SERVICE_URL, data=form) as stt_resp:
            if stt_resp.status != 200:
                error_text = await stt_resp.text()
                logger.error(f"[{chat_id}] STT error: {stt_resp.status} - {error_text[:300]}")
                await bot.send_message(
                    chat_id=chat_id,
                    text="❌ Ошибка распознавания речи. Попробуйте написать текстом."
                )
                return

            stt_data = await stt_resp.json()
            transcribed_text = stt_data.get("text", "").strip()

            logger.info(f"[{chat_id}] STT результат: '{transcribed_text}'")

            if not transcribed_text:
                await bot.send_message(
                    chat_id=chat_id,
                    text="❌ Не удалось распознать речь. Попробуйте говорить четче или напишите текстом."
                )
                return

        # 3. Передаем распознанный текст
        promo_val = await ctx.redis_client.get("settings:promo_enabled")
        promo_enabled = promo_val != "0"

        result = await ctx.orchestrator.process(
            transcribed_text,
            user_id=str(chat_id),
            promo_enabled=promo_enabled
        )
        # 4. Формируем и отправляем ответ пользователю
        answer = (
                result.get("result", {}).get("answer") or
                result.get("answer") or
                "Запрос обработан, но ответ пуст."
        )
        await bot.send_message(chat_id=chat_id, text=answer)

    except Exception as e:
        logger.error(f"[{chat_id}] Ошибка обработчика голосовых сообщений: {e}", exc_info=True)
        await bot.send_message(chat_id=chat_id, text="Ошибка обработчика голосовых сообщений")
# ———————————————————————————————————————————————