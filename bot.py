import asyncio
import logging
import logging.handlers
from datetime import datetime, timedelta, timezone
import json
import os
from typing import List, Optional, Dict, Any, Union
import random
import io
import base64
import time

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.markdown import hbold, hlink
from aiogram.client.default import DefaultBotProperties

from aiohttp import ClientSession
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status, Depends, Request
from fastapi.security import APIKeyHeader
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from gtts import gTTS
from croniter import croniter

import web_parser
import telegram_parser
import rss_parser
import social_media_parser

load_dotenv()

API_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(',') if x.strip()]
NEWS_CHANNEL_LINK = os.getenv("NEWS_CHANNEL_LINK", "https://t.me/newsone234")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
MONOBANK_CARD_NUMBER = "4441111153021484"
HELP_BUY_CHANNEL_LINK = "https://t.me/+gT7TDOMh81M3YmY6"
HELP_SELL_BOT_LINK = "https://t.me/BigmoneycreateBot"
CHANNEL_ID = -1002766273069

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

file_handler = logging.handlers.RotatingFileHandler('bot.log', maxBytes=10*1024*1024, backupCount=5)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

error_file_handler = logging.handlers.RotatingFileHandler('errors.log', maxBytes=10*1024*1024, backupCount=5)
error_file_handler.setLevel(logging.ERROR)
error_file_handler.setFormatter(formatter)
logger.addHandler(error_file_handler)

stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

AI_REQUEST_LIMIT_DAILY_FREE = 3

app = FastAPI(title="Telegram AI News Bot API", version="1.0.0")
app.mount("/static", StaticFiles(directory="."), name="static")

api_key_header = APIKeyHeader(name="X-API-Key")

async def get_api_key(api_key: str = Depends(api_key_header)):
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="ADMIN_API_KEY not configured.")
    if api_key is None or api_key != ADMIN_API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key.")
    return api_key

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

db_pool: Optional[AsyncConnectionPool] = None

async def get_db_pool():
    global db_pool
    if db_pool is None:
        try:
            db_pool = AsyncConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, open=psycopg.AsyncConnection.connect)
            async with db_pool.connection() as conn:
                await conn.execute("SELECT 1")
            logger.info("DB pool initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize DB pool: {e}", exc_info=True)
            raise
    return db_pool

from pydantic import BaseModel, HttpUrl

class News(BaseModel):
    id: Optional[int] = None
    source_id: Optional[int] = None
    title: str
    content: str
    source_url: HttpUrl
    image_url: Optional[HttpUrl] = None
    ai_summary: Optional[str] = None
    ai_classified_topics: Optional[List[str]] = None
    published_at: datetime
    moderation_status: str = 'pending'
    expires_at: Optional[datetime] = None
    is_published_to_channel: Optional[bool] = False

class User(BaseModel):
    id: Optional[int] = None
    telegram_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    created_at: Optional[datetime] = None
    is_admin: Optional[bool] = False
    last_active: Optional[datetime] = None
    language: Optional[str] = 'uk'
    auto_notifications: Optional[bool] = False
    digest_frequency: Optional[str] = 'daily'
    safe_mode: Optional[bool] = False
    current_feed_id: Optional[int] = None
    is_premium: Optional[bool] = False
    premium_expires_at: Optional[datetime] = None
    level: Optional[int] = 1
    badges: Optional[List[str]] = []
    inviter_id: Optional[int] = None
    view_mode: Optional[str] = 'detailed'
    premium_invite_count: Optional[int] = 0
    digest_invite_count: Optional[int] = 0
    is_pro: Optional[bool] = False
    ai_requests_today: Optional[int] = 0
    ai_last_request_date: Optional[datetime] = None

class Source(BaseModel):
    id: Optional[int] = None
    user_id: Optional[int] = None
    source_name: str
    source_url: HttpUrl
    source_type: str
    status: str = 'active'
    added_at: Optional[datetime] = None
    last_parsed: Optional[datetime] = None
    parse_frequency: str = 'hourly'

MESSAGES = {
    'uk': {
        'welcome': "Привіт, {first_name}! Я ваш AI News Bot. Оберіть дію:",
        'main_menu_prompt': "Оберіть дію:",
        'help_text': ("<b>Команди:</b>\n"
                      "/start - Почати\n"
                      "/menu - Меню\n"
                      "/cancel - Скасувати\n"
                      "/my_news - Мої новини\n"
                      "/add_source - Додати джерело\n"
                      "/my_sources - Мої джерела\n"
                      "/ask_expert - Експерт\n"
                      "/invite - Запросити\n"
                      "/subscribe - Підписки\n"
                      "/donate - Донат ☕\n"
                      "<b>AI:</b> під новиною.\n"
                      "<b>AI-медіа:</b> /ai_media_menu"),
        'action_cancelled': "Скасовано. Оберіть дію:",
        'add_source_prompt': "Надішліть URL джерела:",
        'invalid_url': "Невірний URL.",
        'source_url_not_found': "URL джерела не знайдено.",
        'source_added_success': "Джерело '{source_url}' додано!",
        'add_source_error': "Помилка додавання джерела.",
        'no_new_news': "Немає нових новин.",
        'news_not_found': "Новина не знайдена.",
        'no_more_news': "Більше новин немає.",
        'first_news': "Це перша новина.",
        'error_start_menu': "Помилка. Почніть з /menu.",
        'ai_functions_prompt': "AI-функції:",
        'ai_function_premium_only': "Лише для преміум.",
        'news_title_label': "Заголовок:",
        'news_content_label': "Зміст:",
        'published_at_label': "Опубліковано:",
        'news_progress': "Новина {current_index} з {total_news}",
        'read_source_btn': "🔗 Джерело",
        'ai_functions_btn': "🧠 AI-функції",
        'prev_btn': "⬅️ Попередня",
        'next_btn': "➡️ Далі",
        'main_menu_btn': "⬅️ Меню",
        'generating_ai_summary': "Генерую AI-резюме...",
        'ai_summary_label': "AI-резюме:",
        'select_translate_language': "Оберіть мову:",
        'translating_news': "Перекладаю...",
        'translation_label': "Переклад на {language_name}:",
        'generating_audio': "Генерую аудіо...",
        'audio_news_caption': "🔊 Новина: {title}",
        'audio_error': "Помилка генерації аудіо.",
        'ask_news_ai_prompt': "Ваше запитання:",
        'processing_question': "Обробляю...",
        'ai_response_label': "Відповідь AI:",
        'ai_news_not_found': "Новина не знайдена.",
        'ask_free_ai_prompt': "Ваше запитання до AI:",
        'extracting_entities': "Витягую сутності...",
        'entities_label': "Сутності:",
        'explain_term_prompt': "Термін для пояснення:",
        'explaining_term': "Пояснюю...",
        'term_explanation_label': "Пояснення '{term}':",
        'classifying_topics': "Класифікую теми...",
        'topics_label': "Теми:",
        'checking_facts': "Перевіряю факти...",
        'fact_check_label': "Перевірка фактів:",
        'analyzing_sentiment': "Аналізую настрій...",
        'sentiment_label': "Настрій:",
        'detecting_bias': "Виявляю упередженість...",
        'bias_label': "Упередженість:",
        'generating_audience_summary': "Генерую резюме для аудиторії...",
        'audience_summary_label': "Резюме для аудиторії:",
        'searching_historical_analogues': "Шукаю аналоги...",
        'historical_analogues_label': "Історичні аналоги:",
        'analyzing_impact': "Аналізую вплив...",
        'impact_label': "Аналіз впливу:",
        'performing_monetary_analysis': "Виконую грошовий аналіз...",
        'monetary_analysis_label': "Грошовий аналіз:",
        'bookmark_added': "Новину додано до закладок!",
        'bookmark_already_exists': "Вже в закладках.",
        'bookmark_add_error': "Помилка закладок.",
        'bookmark_removed': "Новину видалено із закладок!",
        'bookmark_not_found': "Новини немає в закладках.",
        'bookmark_remove_error': "Помилка видалення закладок.",
        'no_bookmarks': "Закладок немає.",
        'your_bookmarks_label': "Ваші закладки:",
        'report_fake_news_btn': "🚩 Повідомити фейк",
        'report_already_sent': "Репорт вже надіслано.",
        'report_sent_success': "Дякуємо! Репорт надіслано.",
        'report_action_done': "Дякуємо! Оберіть дію:",
        'user_not_identified': "Користувача не ідентифіковано.",
        'no_admin_access': "Немає доступу.",
        'loading_moderation_news': "Завантажую новини...",
        'no_pending_news': "Немає новин на модерації.",
        'moderation_news_label': "Новина на модерації ({current_index} з {total_news}):",
        'source_label': "Джерело:",
        'status_label': "Статус:",
        'approve_btn': "✅ Схвалити",
        'reject_btn': "❌ Відхилити",
        'news_approved': "Новину {news_id} схвалено!",
        'news_rejected': "Новину {news_id} відхилено!",
        'all_moderation_done': "Усі новини оброблено.",
        'no_more_moderation_news': "Більше новин немає.",
        'first_moderation_news': "Це перша новина.",
        'source_stats_label': "📊 Статистика джерел (топ-10):",
        'source_stats_entry': "{idx}. {source_name}: {count} публікацій",
        'no_source_stats': "Статистика джерел відсутня.",
        'your_invite_code': "Ваш інвайт-код: <code>{invite_code}</code>\nПоділіться: {invite_link}",
        'invite_error': "Помилка генерації коду.",
        'daily_digest_header': "📰 Ваш щоденний AI-дайджест:",
        'daily_digest_entry': "<b>{idx}. {title}</b>\n{summary}\n🔗 <a href='{source_url}'>Читати</a>\n\n",
        'no_news_for_digest': "Немає новин для дайджесту.",
        'ai_rate_limit_exceeded': "Забагато AI-запитів. Використано {count}/{limit}. Спробуйте завтра або преміум.",
        'what_new_digest_header': "👋 Привіт! Ви пропустили {count} новин. Дайджест:",
        'what_new_digest_footer': "\n\nВсі новини? Натисніть '📰 Мої новини'.",
        'cancel_btn': "Скасувати",
        'toggle_notifications_btn': "🔔 Сповіщення",
        'set_digest_frequency_btn': "🔄 Частота дайджесту",
        'toggle_safe_mode_btn': "🔒 Безпечний режим",
        'set_view_mode_btn': "👁️ Режим перегляду",
        'ai_summary_btn': "📝 AI-резюме",
        'translate_btn': "🌐 Перекласти",
        'ask_ai_btn': "❓ Запитати AI",
        'extract_entities_btn': "🧑‍🤝‍🧑 Сутності",
        'explain_term_btn': "❓ Пояснити",
        'classify_topics_btn': "🏷️ Теми",
        'listen_news_btn': "🔊 Прослухати",
        'next_ai_page_btn': "➡️ Далі (AI)",
        'fact_check_btn': "✅ Факт (Преміум)",
        'sentiment_trend_analysis_btn': "📊 Настрій (Преміум)",
        'bias_detection_btn': "🔍 Упередженість (Преміум)",
        'audience_summary_btn': "📝 Резюме для аудиторії (Преміум)",
        'historical_analogues_btn': "📜 Аналоги (Преміум)",
        'impact_analysis_btn': "💥 Вплив (Преміум)",
        'monetary_impact_btn': "💰 Грошовий аналіз (Преміум)",
        'prev_ai_page_btn': "⬅️ Назад (AI)",
        'bookmark_add_btn': "❤️ Обране",
        'comments_btn': "💬 Коментарі",
        'english_lang': "🇬🇧 Англійська",
        'polish_lang': "🇵🇱 Польська",
        'german_lang': "🇩🇪 Німецька",
        'spanish_lang': "🇪🇸 Іспанська",
        'french_lang': "🇫🇷 Французька",
        'ukrainian_lang': "🇺🇦 Українська",
        'back_to_ai_btn': "⬅️ До AI",
        'ask_free_ai_btn': "💬 Запитай AI",
        'news_channel_link_error': "Невірне посилання на канал.",
        'news_channel_link_warning': "Невірний формат посилання.",
        'news_published_success': "Новина '{title}' опублікована в каналі {identifier}.",
        'news_publish_error': "Помилка публікації '{title}': {error}",
        'source_parsing_warning': "Не вдалося спарсити з джерела: {name} ({url}).",
        'source_parsing_error': "Помилка парсингу джерела {name} ({url}): {error}",
        'no_active_sources': "Немає активних джерел.",
        'news_already_exists': "Новина з URL {url} вже існує.",
        'news_added_success': "Новина '{title}' додана.",
        'news_not_added': "Новина з джерела {name} не додана.",
        'source_last_parsed_updated': "Оновлено last_parsed для джерела {name}.",
        'deleted_expired_news': "Видалено {count} прострочених..."
    }
}

async def fetch_user_by_id(user_id: int) -> Optional[User]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            user_rec = await cur.fetchone()
            return User(**user_rec) if user_rec else None

async def fetch_user_by_telegram_id(telegram_id: int) -> Optional[User]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
            user_rec = await cur.fetchone()
            return User(**user_rec) if user_rec else None

async def create_user(telegram_id: int, username: Optional[str] = None, first_name: Optional[str] = None, last_name: Optional[str] = None) -> User:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO users (telegram_id, username, first_name, last_name) VALUES (%s, %s, %s, %s) ON CONFLICT (telegram_id) DO UPDATE SET username = EXCLUDED.username, first_name = EXCLUDED.first_name, last_name = EXCLUDED.last_name, last_active = CURRENT_TIMESTAMP RETURNING *;",
                (telegram_id, username, first_name, last_name)
            )
            user_rec = await cur.fetchone()
            return User(**user_rec)

async def update_user_last_active(telegram_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE telegram_id = %s", (telegram_id,))
            await conn.commit()

async def get_user_language(telegram_id: int) -> str:
    user = await fetch_user_by_telegram_id(telegram_id)
    return user.language if user else 'uk'

async def fetch_news_by_id(news_id: int) -> Optional[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM news WHERE id = %s", (news_id,))
            news_rec = await cur.fetchone()
            return News(**news_rec) if news_rec else None

async def fetch_news_for_user(user_id: int, offset: int = 0, limit: int = 1) -> List[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT n.* FROM news n
                LEFT JOIN user_news_views unw ON n.id = unw.news_id AND unw.user_id = %s
                WHERE unw.news_id IS NULL AND n.moderation_status = 'approved' AND n.expires_at > CURRENT_TIMESTAMP
                ORDER BY n.published_at DESC
                OFFSET %s LIMIT %s;
                """,
                (user_id, offset, limit)
            )
            news_recs = await cur.fetchall()
            return [News(**rec) for rec in news_recs]

async def count_unread_news(user_id: int) -> int:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(n.id) FROM news n
                LEFT JOIN user_news_views unw ON n.id = unw.news_id AND unw.user_id = %s
                WHERE unw.news_id IS NULL AND n.moderation_status = 'approved' AND n.expires_at > CURRENT_TIMESTAMP;
                """,
                (user_id,)
            )
            count = await cur.fetchone()
            return count[0] if count else 0

async def mark_news_as_viewed(user_id: int, news_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO user_news_views (user_id, news_id) VALUES (%s, %s) ON CONFLICT (user_id, news_id) DO NOTHING;",
                (user_id, news_id)
            )
            await conn.commit()

async def add_source(user_id: int, source_url: HttpUrl, source_name: str, source_type: str = 'web', parse_frequency: str = 'hourly') -> Source:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO sources (user_id, source_name, source_url, source_type, parse_frequency) VALUES (%s, %s, %s, %s, %s) RETURNING *;",
                (user_id, source_name, str(source_url), source_type, parse_frequency)
            )
            source_rec = await cur.fetchone()
            await conn.commit()
            return Source(**source_rec)

async def fetch_user_sources(user_id: int) -> List[Source]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM sources WHERE user_id = %s ORDER BY added_at DESC", (user_id,))
            source_recs = await cur.fetchall()
            return [Source(**rec) for rec in source_recs]

async def update_source_last_parsed(source_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE sources SET last_parsed = CURRENT_TIMESTAMP WHERE id = %s", (source_id,))
            await conn.commit()

async def add_news(title: str, content: str, source_url: HttpUrl, source_id: int, image_url: Optional[HttpUrl] = None) -> News:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO news (title, content, source_url, source_id, image_url, published_at) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP) RETURNING *;",
                (title, content, str(source_url), source_id, str(image_url) if image_url else None)
            )
            news_rec = await cur.fetchone()
            await conn.commit()
            return News(**news_rec)

async def news_exists(source_url: HttpUrl) -> bool:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM news WHERE source_url = %s", (str(source_url),))
            count = await cur.fetchone()
            return count[0] > 0

async def update_news_ai_data(news_id: int, ai_summary: str, ai_classified_topics: List[str]):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE news SET ai_summary = %s, ai_classified_topics = %s WHERE id = %s;",
                (ai_summary, json.dumps(ai_classified_topics), news_id)
            )
            await conn.commit()

async def update_news_moderation_status(news_id: int, status: str):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE news SET moderation_status = %s WHERE id = %s;",
                (status, news_id)
            )
            await conn.commit()

async def fetch_news_for_moderation(offset: int = 0, limit: int = 1) -> List[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM news WHERE moderation_status = 'pending' ORDER BY published_at ASC OFFSET %s LIMIT %s;",
                (offset, limit)
            )
            news_recs = await cur.fetchall()
            return [News(**rec) for rec in news_recs]

async def count_pending_moderation_news() -> int:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM news WHERE moderation_status = 'pending';")
            count = await cur.fetchone()
            return count[0] if count else 0

async def fetch_source_by_id(source_id: int) -> Optional[Source]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM sources WHERE id = %s", (source_id,))
            source_rec = await cur.fetchone()
            return Source(**source_rec) if source_rec else None

async def fetch_all_active_sources() -> List[Source]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM sources WHERE status = 'active';")
            source_recs = await cur.fetchall()
            return [Source(**rec) for rec in source_recs]

async def add_news_to_channel_history(news_id: int, message_id: int, channel_identifier: str):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO channel_publication_history (news_id, message_id, channel_identifier) VALUES (%s, %s, %s) ON CONFLICT (news_id) DO UPDATE SET message_id = EXCLUDED.message_id, channel_identifier = EXCLUDED.channel_identifier;",
                (news_id, message_id, channel_identifier)
            )
            await conn.commit()

async def update_news_published_status(news_id: int, is_published: bool):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE news SET is_published_to_channel = %s WHERE id = %s;",
                (is_published, news_id)
            )
            await conn.commit()

async def get_source_stats(limit: int = 10) -> Dict[str, int]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT s.source_name, COUNT(n.id) AS publication_count
                FROM sources s
                JOIN news n ON s.id = n.source_id
                GROUP BY s.source_name
                ORDER BY publication_count DESC
                LIMIT %s;
                """,
                (limit,)
            )
            stats = await cur.fetchall()
            return {rec['source_name']: rec['publication_count'] for rec in stats}

async def create_invite_code(inviter_user_id: int) -> str:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            invite_code = ''.join(random.choices('0123456789ABCDEF', k=8))
            await cur.execute(
                "INSERT INTO invitations (inviter_user_id, invite_code) VALUES (%s, %s) RETURNING invite_code;",
                (inviter_user_id, invite_code)
            )
            code = await cur.fetchone()
            await conn.commit()
            return code['invite_code']

async def use_invite_code(invite_code: str, invitee_telegram_id: int) -> bool:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM invitations WHERE invite_code = %s AND used_at IS NULL;", (invite_code,))
            invite = await cur.fetchone()
            if invite:
                await cur.execute(
                    "UPDATE invitations SET used_at = CURRENT_TIMESTAMP, invitee_telegram_id = %s, status = 'accepted' WHERE id = %s;",
                    (invitee_telegram_id, invite['id'])
                )
                await cur.execute(
                    "UPDATE users SET inviter_id = %s, is_premium = TRUE, premium_expires_at = CURRENT_TIMESTAMP + INTERVAL '30 days' WHERE telegram_id = %s;",
                    (invite['inviter_user_id'], invitee_telegram_id)
                )
                await conn.commit()
                return True
            return False

async def fetch_news_for_digest(user_id: int, user_lang: str, limit: int = 5) -> List[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT n.* FROM news n
                LEFT JOIN user_news_views unw ON n.id = unw.news_id AND unw.user_id = %s
                WHERE unw.news_id IS NULL AND n.moderation_status = 'approved' AND n.expires_at > CURRENT_TIMESTAMP
                ORDER BY n.published_at DESC
                LIMIT %s;
                """,
                (user_id, limit)
            )
            news_recs = await cur.fetchall()
            return [News(**rec) for rec in news_recs]

async def increment_ai_requests_today(telegram_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET ai_requests_today = ai_requests_today + 1, ai_last_request_date = CURRENT_DATE WHERE telegram_id = %s;",
                (telegram_id,)
            )
            await conn.commit()

async def reset_ai_requests_if_new_day(telegram_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET ai_requests_today = 0, ai_last_request_date = CURRENT_DATE WHERE telegram_id = %s AND ai_last_request_date IS DISTINCT FROM CURRENT_DATE;",
                (telegram_id,)
            )
            await conn.commit()

async def check_ai_request_limit(telegram_id: int) -> bool:
    user = await fetch_user_by_telegram_id(telegram_id)
    if not user:
        return False
    await reset_ai_requests_if_new_day(telegram_id)
    if user.is_premium or user.is_pro:
        return True
    return user.ai_requests_today < AI_REQUEST_LIMIT_DAILY_FREE

async def fetch_all_users() -> List[User]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM users ORDER BY created_at DESC;")
            user_recs = await cur.fetchall()
            return [User(**rec) for rec in user_recs]

async def count_total_users() -> int:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM users;")
            count = await cur.fetchone()
            return count[0] if count else 0

async def count_total_news() -> int:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM news;")
            count = await cur.fetchone()
            return count[0] if count else 0

async def count_active_users_last_24_hours() -> int:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(DISTINCT telegram_id) FROM users WHERE last_active >= CURRENT_TIMESTAMP - INTERVAL '24 hours';")
            count = await cur.fetchone()
            return count[0] if count else 0

async def fetch_news_status_counts() -> Dict[str, int]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT moderation_status, COUNT(*) FROM news GROUP BY moderation_status;")
            counts = await cur.fetchall()
            return {rec['moderation_status']: rec['count'] for rec in counts}

async def fetch_active_users(limit: int = 10, offset: int = 0) -> List[User]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM users ORDER BY last_active DESC LIMIT %s OFFSET %s;",
                (limit, offset)
            )
            user_recs = await cur.fetchall()
            return [User(**rec) for rec in user_recs]

async def update_user_settings(telegram_id: int, setting: str, value: Any):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            set_clause = f"{setting} = %s"
            await cur.execute(f"UPDATE users SET {set_clause} WHERE telegram_id = %s;", (value, telegram_id))
            await conn.commit()

class BotStates(StatesGroup):
    MAIN_MENU = State()
    ADD_SOURCE = State()
    ASK_EXPERT = State()
    AI_NEWS_MENU = State()
    TRANSLATE_NEWS = State()
    ASK_NEWS_AI = State()
    EXPLAIN_TERM = State()
    GENERATE_AI_NEWS_PROMPT = State()
    PROCESS_YOUTUBE_URL = State()
    MODERATION_MENU = State()
    SET_DIGEST_FREQUENCY = State()
    SET_VIEW_MODE = State()
    SET_LANGUAGE = State()
    ASK_FREE_AI = State()

async def get_user_data(message_or_callback: Union[Message, CallbackQuery]) -> Dict[str, Any]:
    user_id = message_or_callback.from_user.id
    user = await fetch_user_by_telegram_id(user_id)
    if not user:
        user = await create_user(
            user_id,
            message_or_callback.from_user.username,
            message_or_callback.from_user.first_name,
            message_or_callback.from_user.last_name
        )
    else:
        await update_user_last_active(user_id)
    return user.__dict__

def get_message(user_lang: str, key: str, **kwargs) -> str:
    return MESSAGES.get(user_lang, MESSAGES['uk']).get(key, f"Translation missing for {key}")

def get_main_menu_keyboard(user_lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(user_lang, 'my_news'), callback_data="my_news")
    builder.button(text=get_message(user_lang, 'add_source'), callback_data="add_source")
    builder.button(text=get_message(user_lang, 'my_sources'), callback_data="my_sources")
    builder.button(text=get_message(user_lang, 'ask_expert'), callback_data="ask_expert")
    builder.button(text=get_message(user_lang, 'invite'), callback_data="invite")
    builder.button(text=get_message(user_lang, 'subscribe'), callback_data="subscribe")
    builder.button(text=get_message(user_lang, 'donate'), callback_data="donate")
    builder.button(text=get_message(user_lang, 'ai_media_menu'), callback_data="ai_media_menu")

    user_id = 0
    if hasattr(F, 'event_from_user') and F.event_from_user.id:
        user_id = F.event_from_user.id
    elif hasattr(F, 'from_user') and F.from_user.id:
        user_id = F.from_user.id

    if user_id in ADMIN_IDS:
        builder.button(text="⚙️ Admin Panel", callback_data="admin_panel")

    builder.adjust(2, 2, 2, 2, 1)
    return builder.as_markup()

def get_ai_news_menu_keyboard(user_lang: str, news_id: int, has_summary: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(user_lang, 'ai_summary_btn'), callback_data=f"ai_summary_{news_id}")
    builder.button(text=get_message(user_lang, 'translate_btn'), callback_data=f"translate_news_{news_id}")
    builder.button(text=get_message(user_lang, 'ask_ai_btn'), callback_data=f"ask_news_ai_{news_id}")
    builder.button(text=get_message(user_lang, 'extract_entities_btn'), callback_data=f"extract_entities_{news_id}")
    builder.button(text=get_message(user_lang, 'explain_term_btn'), callback_data=f"explain_term_{news_id}")
    builder.button(text=get_message(user_lang, 'classify_topics_btn'), callback_data=f"classify_topics_{news_id}")
    builder.button(text=get_message(user_lang, 'listen_news_btn'), callback_data=f"listen_news_{news_id}")
    builder.button(text=get_message(user_lang, 'next_ai_page_btn'), callback_data=f"ai_page_2_{news_id}")
    builder.button(text=get_message(user_lang, 'bookmark_add_btn'), callback_data=f"bookmark_add_{news_id}")
    builder.button(text=get_message(user_lang, 'report_fake_news_btn'), callback_data=f"report_fake_news_{news_id}")
    builder.button(text=get_message(user_lang, 'comments_btn'), callback_data=f"comments_{news_id}")
    builder.button(text=get_message(user_lang, 'main_menu_btn'), callback_data="main_menu")
    builder.adjust(2, 2, 2, 2, 3, 1)
    return builder.as_markup()

def get_ai_news_menu_page_2_keyboard(user_lang: str, news_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(user_lang, 'fact_check_btn'), callback_data=f"fact_check_{news_id}")
    builder.button(text=get_message(user_lang, 'sentiment_trend_analysis_btn'), callback_data=f"sentiment_trend_analysis_{news_id}")
    builder.button(text=get_message(user_lang, 'bias_detection_btn'), callback_data=f"bias_detection_{news_id}")
    builder.button(text=get_message(user_lang, 'audience_summary_btn'), callback_data=f"audience_summary_{news_id}")
    builder.button(text=get_message(user_lang, 'historical_analogues_btn'), callback_data=f"historical_analogues_{news_id}")
    builder.button(text=get_message(user_lang, 'impact_analysis_btn'), callback_data=f"impact_analysis_{news_id}")
    builder.button(text=get_message(user_lang, 'monetary_impact_btn'), callback_data=f"monetary_impact_{news_id}")
    builder.button(text=get_message(user_lang, 'prev_ai_page_btn'), callback_data=f"ai_page_1_{news_id}")
    builder.button(text=get_message(user_lang, 'main_menu_btn'), callback_data="main_menu")
    builder.adjust(2, 2, 2, 2, 1)
    return builder.as_markup()

def get_language_keyboard(user_lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(user_lang, 'english_lang'), callback_data="set_lang_en")
    builder.button(text=get_message(user_lang, 'polish_lang'), callback_data="set_lang_pl")
    builder.button(text=get_message(user_lang, 'german_lang'), callback_data="set_lang_de")
    builder.button(text=get_message(user_lang, 'spanish_lang'), callback_data="set_lang_es")
    builder.button(text=get_message(user_lang, 'french_lang'), callback_data="set_lang_fr")
    builder.button(text=get_message(user_lang, 'ukrainian_lang'), callback_data="set_lang_uk")
    builder.adjust(2)
    return builder.as_markup()

def get_ai_media_menu_keyboard(user_lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(user_lang, 'generate_ai_news'), callback_data="generate_ai_news")
    builder.button(text=get_message(user_lang, 'process_youtube_url'), callback_data="process_youtube_url")
    builder.button(text=get_message(user_lang, 'main_menu_btn'), callback_data="main_menu")
    builder.adjust(1)
    return builder.as_markup()

def get_admin_panel_keyboard(user_lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Users", callback_data="admin_users")
    builder.button(text="📰 News", callback_data="admin_news")
    builder.button(text="➕ Add Source (Admin)", callback_data="admin_add_source")
    builder.button(text="📊 Stats", callback_data="admin_stats")
    builder.button(text="✍️ Moderation", callback_data="admin_moderation")
    builder.button(text="⬅️ Menu", callback_data="main_menu")
    builder.adjust(2, 2, 2)
    return builder.as_markup()

def get_moderation_news_keyboard(user_lang: str, news_id: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(user_lang, 'approve_btn'), callback_data=f"approve_news_{news_id}")
    builder.button(text=get_message(user_lang, 'reject_btn'), callback_data=f"reject_news_{news_id}")
    if has_prev:
        builder.button(text=get_message(user_lang, 'prev_btn'), callback_data="prev_moderation_news")
    if has_next:
        builder.button(text=get_message(user_lang, 'next_btn'), callback_data="next_moderation_news")
    builder.button(text=get_message(user_lang, 'main_menu_btn'), callback_data="main_menu")
    builder.adjust(2, 2, 1)
    return builder.as_markup()

@router.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext) -> None:
    user_data = await get_user_data(message)
    user_lang = user_data.get('language', 'uk')
    await state.set_state(BotStates.MAIN_MENU)
    await message.answer(
        get_message(user_lang, 'welcome', first_name=message.from_user.first_name),
        reply_markup=get_main_menu_keyboard(user_lang)
    )

@router.message(Command("menu"))
async def command_menu_handler(message: Message, state: FSMContext) -> None:
    user_data = await get_user_data(message)
    user_lang = user_data.get('language', 'uk')
    await state.set_state(BotStates.MAIN_MENU)
    await message.answer(
        get_message(user_lang, 'main_menu_prompt'),
        reply_markup=get_main_menu_keyboard(user_lang)
    )

@router.message(Command("help"))
async def command_help_handler(message: Message) -> None:
    user_data = await get_user_data(message)
    user_lang = user_data.get('language', 'uk')
    await message.answer(get_message(user_lang, 'help_text'))

@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel", StateFilter(BotStates.ADD_SOURCE, BotStates.ASK_EXPERT, BotStates.GENERATE_AI_NEWS_PROMPT, BotStates.PROCESS_YOUTUBE_URL, BotStates.ASK_FREE_AI))
async def command_cancel_handler(update: Union[Message, CallbackQuery], state: FSMContext) -> None:
    user_data = await get_user_data(update)
    user_lang = user_data.get('language', 'uk')
    await state.clear()
    if isinstance(update, Message):
        await update.answer(get_message(user_lang, 'action_cancelled'), reply_markup=get_main_menu_keyboard(user_lang))
    elif isinstance(update, CallbackQuery):
        await update.message.answer(get_message(user_lang, 'action_cancelled'), reply_markup=get_main_menu_keyboard(user_lang))
        await update.answer()

@router.callback_query(F.data == "main_menu")
async def callback_main_menu(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    await state.set_state(BotStates.MAIN_MENU)
    await callback.message.edit_text(
        get_message(user_lang, 'main_menu_prompt'),
        reply_markup=get_main_menu_keyboard(user_lang)
    )
    await callback.answer()

user_current_news_index: Dict[int, int] = {}
user_news_ids: Dict[int, List[int]] = {}

async def send_news_to_user(chat_id: int, user_id: int, news_item: News, current_index: int, total_news: int, user_lang: str, state: FSMContext, last_message_id: Optional[int] = None):
    try:
        if last_message_id:
            try:
                await bot.delete_message(chat_id, last_message_id)
            except Exception as e:
                logger.warning(f"Failed to delete previous message {last_message_id} in chat {chat_id}: {e}")

        source_name = "Невідомо"
        if news_item.source_id:
            source = await fetch_source_by_id(news_item.source_id)
            if source:
                source_name = source.source_name

        message_text = (
            f"<b>{get_message(user_lang, 'news_title_label')}</b> {news_item.title}\n\n"
            f"<b>{get_message(user_lang, 'news_content_label')}</b> {news_item.content}\n\n"
            f"<b>{get_message(user_lang, 'published_at_label')}</b> {news_item.published_at.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"<i>{get_message(user_lang, 'news_progress').format(current_index=current_index + 1, total_news=total_news)}</i>"
        )

        keyboard = InlineKeyboardBuilder()
        keyboard.button(text=get_message(user_lang, 'read_source_btn'), url=str(news_item.source_url))
        keyboard.button(text=get_message(user_lang, 'ai_functions_btn'), callback_data=f"ai_news_menu_{news_item.id}")

        if current_index > 0:
            keyboard.button(text=get_message(user_lang, 'prev_btn'), callback_data="prev_news")
        if current_index < total_news - 1:
            keyboard.button(text=get_message(user_lang, 'next_btn'), callback_data="next_news")
        keyboard.button(text=get_message(user_lang, 'main_menu_btn'), callback_data="main_menu")

        keyboard.adjust(2, 2, 1)

        sent_message = None
        if news_item.image_url:
            try:
                await bot.send_chat_action(chat_id, ChatAction.UPLOAD_PHOTO)
                sent_message = await bot.send_photo(
                    chat_id,
                    photo=str(news_item.image_url),
                    caption=message_text,
                    reply_markup=keyboard.as_markup()
                )
            except Exception as e:
                logger.error(f"Failed to send photo for news {news_item.id}: {e}. Sending text message instead.")
                sent_message = await bot.send_message(
                    chat_id,
                    message_text + f"\n\n[Зображення недоступне: {news_item.image_url}]",
                    reply_markup=keyboard.as_markup()
                )
        else:
            sent_message = await bot.send_message(
                chat_id,
                message_text,
                reply_markup=keyboard.as_markup()
            )

        await state.update_data(last_message_id=sent_message.message_id)
        await mark_news_as_viewed(user_id, news_item.id)

    except Exception as e:
        logger.error(f"Error sending news to user {chat_id}: {e}", exc_info=True)
        await bot.send_message(chat_id, get_message(user_lang, 'error_start_menu'), reply_markup=get_main_menu_keyboard(user_lang))

@router.callback_query(F.data == "my_news")
async def callback_my_news(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_id = user_data['id']
    user_lang = user_data.get('language', 'uk')
    await state.set_state(BotStates.MAIN_MENU)

    news = await fetch_news_for_user(user_id, offset=0, limit=20)
    if not news:
        await callback.message.edit_text(get_message(user_lang, 'no_new_news'))
        await callback.answer()
        return

    user_news_ids[user_id] = [n.id for n in news]
    user_current_news_index[user_id] = 0

    current_news_item = await fetch_news_by_id(user_news_ids[user_id][user_current_news_index[user_id]])
    if current_news_item:
        await send_news_to_user(
            callback.message.chat.id,
            user_id,
            current_news_item,
            user_current_news_index[user_id],
            len(user_news_ids[user_id]),
            user_lang,
            state
        )
    else:
        await callback.message.answer(get_message(user_lang, 'news_not_found'))
    await callback.answer()

@router.callback_query(F.data == "next_news")
async def callback_next_news(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_id = user_data['id']
    user_lang = user_data.get('language', 'uk')
    last_message_id = (await state.get_data()).get('last_message_id')

    if user_id not in user_current_news_index:
        await callback.message.edit_text(get_message(user_lang, 'no_more_news'))
        await callback.answer()
        return

    user_current_news_index[user_id] += 1
    if user_current_news_index[user_id] >= len(user_news_ids[user_id]):
        await callback.message.edit_text(get_message(user_lang, 'no_more_news'))
        del user_current_news_index[user_id]
        del user_news_ids[user_id]
        await callback.answer()
        return

    current_news_item = await fetch_news_by_id(user_news_ids[user_id][user_current_news_index[user_id]])
    if current_news_item:
        await send_news_to_user(
            callback.message.chat.id,
            user_id,
            current_news_item,
            user_current_news_index[user_id],
            len(user_news_ids[user_id]),
            user_lang,
            state,
            last_message_id
        )
    else:
        await callback.message.answer(get_message(user_lang, 'news_not_found'))
    await callback.answer()

@router.callback_query(F.data == "prev_news")
async def callback_prev_news(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_id = user_data['id']
    user_lang = user_data.get('language', 'uk')
    last_message_id = (await state.get_data()).get('last_message_id')

    if user_id not in user_current_news_index:
        await callback.message.edit_text(get_message(user_lang, 'first_news'))
        await callback.answer()
        return

    user_current_news_index[user_id] -= 1
    if user_current_news_index[user_id] < 0:
        await callback.message.edit_text(get_message(user_lang, 'first_news'))
        user_current_news_index[user_id] = 0
        await callback.answer()
        return

    current_news_item = await fetch_news_by_id(user_news_ids[user_id][user_current_news_index[user_id]])
    if current_news_item:
        await send_news_to_user(
            callback.message.chat.id,
            user_id,
            current_news_item,
            user_current_news_index[user_id],
            len(user_news_ids[user_id]),
            user_lang,
            state,
            last_message_id
        )
    else:
        await callback.message.answer(get_message(user_lang, 'news_not_found'))
    await callback.answer()

@router.callback_query(F.data == "add_source")
async def callback_add_source(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    await state.set_state(BotStates.ADD_SOURCE)
    await callback.message.edit_text(get_message(user_lang, 'add_source_prompt'), reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel")).as_markup())
    await callback.answer()

@router.message(BotStates.ADD_SOURCE)
async def process_add_source(message: Message, state: FSMContext):
    user_data = await get_user_data(message)
    user_id = user_data['id']
    user_lang = user_data.get('language', 'uk')
    source_url_str = message.text.strip()
    try:
        source_url = HttpUrl(source_url_str)
    except ValueError:
        await message.answer(get_message(user_lang, 'invalid_url'))
        return

    try:
        source_name = source_url.host or "Unknown"
        await add_source(user_id, source_url, source_name)
        await message.answer(get_message(user_lang, 'source_added_success').format(source_url=source_url), reply_markup=get_main_menu_keyboard(user_lang))
        await state.clear()
    except Exception as e:
        logger.error(f"Error adding source: {e}", exc_info=True)
        await message.answer(get_message(user_lang, 'add_source_error'))

@router.callback_query(F.data == "my_sources")
async def callback_my_sources(callback: CallbackQuery):
    user_data = await get_user_data(callback)
    user_id = user_data['id']
    user_lang = user_data.get('language', 'uk')
    sources = await fetch_user_sources(user_id)
    if not sources:
        await callback.message.edit_text(get_message(user_lang, 'no_sources'))
        await callback.answer()
        return

    response_text = get_message(user_lang, 'your_sources_label') + "\n"
    for idx, source in enumerate(sources):
        response_text += f"{idx + 1}. {source.source_name} ({source.source_url})\n"
    await callback.message.edit_text(response_text)
    await callback.answer()

@router.callback_query(F.data.startswith("ai_news_menu_"))
async def callback_ai_news_menu(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    news_id = int(callback.data.split("_")[3])
    await state.update_data(current_news_id=news_id)
    await callback.message.edit_text(
        get_message(user_lang, 'ai_functions_prompt'),
        reply_markup=get_ai_news_menu_keyboard(user_lang, news_id)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("ai_page_2_"))
async def callback_ai_news_menu_page_2(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    news_id = int(callback.data.split("_")[3])
    await state.update_data(current_news_id=news_id)
    await callback.message.edit_text(
        get_message(user_lang, 'ai_functions_prompt'),
        reply_markup=get_ai_news_menu_page_2_keyboard(user_lang, news_id)
    )
    await callback.answer()

@router.callback_query(F.data.startswith("ai_page_1_"))
async def callback_ai_news_menu_page_1(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    news_id = int(callback.data.split("_")[3])
    await state.update_data(current_news_id=news_id)
    await callback.message.edit_text(
        get_message(user_lang, 'ai_functions_prompt'),
        reply_markup=get_ai_news_menu_keyboard(user_lang, news_id)
    )
    await callback.answer()

async def call_gemini_api(prompt: str, user_telegram_id: int, chat_history: Optional[List[Dict]] = None, image_data: Optional[bytes] = None) -> Optional[str]:
    if not await check_ai_request_limit(user_telegram_id):
        user = await fetch_user_by_telegram_id(user_telegram_id)
        user_lang = user.language if user else 'uk'
        await bot.send_message(user_telegram_id, get_message(user_lang, 'ai_rate_limit_exceeded', count=user.ai_requests_today, limit=AI_REQUEST_LIMIT_DAILY_FREE))
        return None

    headers = {"Content-Type": "application/json"}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
    if image_data:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro-vision:generateContent?key={GEMINI_API_KEY}"

    contents = []
    if chat_history:
        for entry in chat_history:
            contents.append({"role": entry["role"], "parts": [{"text": entry["text"]}]})

    if image_data:
        contents.append({"role": "user", "parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(image_data).decode()}}]})
    else:
        contents.append({"role": "user", "parts": [{"text": prompt}]})

    data = {"contents": contents}

    try:
        async with ClientSession() as session:
            async with session.post(url, headers=headers, json=data) as response:
                response.raise_for_status()
                response_json = await response.json()
                await increment_ai_requests_today(user_telegram_id)
                return response_json['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}", exc_info=True)
        return None

async def process_ai_request(callback: CallbackQuery, prompt_template: str, message_key: str, check_premium: bool = False, news_content: Optional[str] = None):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    telegram_id = user_data['telegram_id']

    if check_premium and not (user_data.get('is_premium') or user_data.get('is_pro')):
        await callback.message.answer(get_message(user_lang, 'ai_function_premium_only'))
        await callback.answer()
        return

    news_id = int(callback.data.split("_")[2])
    news_item = await fetch_news_by_id(news_id)
    if not news_item:
        await callback.message.answer(get_message(user_lang, 'ai_news_not_found'))
        await callback.answer()
        return

    await bot.send_chat_action(callback.message.chat.id, ChatAction.TYPING)
    await callback.message.edit_text(get_message(user_lang, message_key))
    await callback.answer()

    content_to_process = news_content if news_content else news_item.content
    prompt = prompt_template.format(content=content_to_process, title=news_item.title)
    ai_response = await call_gemini_api(prompt, telegram_id)

    if ai_response:
        response_text = f"<b>{get_message(user_lang, message_key)}</b>\n{ai_response}"
        if len(response_text) > 4096:
            for x in range(0, len(response_text), 4096):
                await callback.message.answer(response_text[x:x+4096])
        else:
            await callback.message.answer(response_text)
    else:
        await callback.message.answer(get_message(user_lang, 'ai_error'))

    current_state_data = await state.get_data()
    last_message_id = current_state_data.get('last_message_id')
    if last_message_id:
        try:
            await bot.delete_message(callback.message.chat.id, last_message_id)
        except Exception as e:
            logger.warning(f"Failed to delete message {last_message_id}: {e}")

    await callback.message.answer(
        get_message(user_lang, 'ai_functions_prompt'),
        reply_markup=get_ai_news_menu_keyboard(user_lang, news_id)
    )

@router.callback_query(F.data.startswith("ai_summary_"))
async def handle_ai_summary(callback: CallbackQuery, state: FSMContext):
    await process_ai_request(
        callback,
        "Summarize the following news article: Title: {title}\nContent: {content}",
        'generating_ai_summary'
    )

@router.callback_query(F.data.startswith("translate_news_"))
async def handle_translate_news(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    news_id = int(callback.data.split("_")[2])
    await state.update_data(current_news_id=news_id)
    await state.set_state(BotStates.TRANSLATE_NEWS)
    await callback.message.edit_text(
        get_message(user_lang, 'select_translate_language'),
        reply_markup=get_language_keyboard(user_lang)
    )
    await callback.answer()

@router.callback_query(BotStates.TRANSLATE_NEWS, F.data.startswith("set_lang_"))
async def process_translate_language(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    target_lang_code = callback.data.split("_")[2]
    lang_map = {
        'en': get_message(user_lang, 'english_lang'),
        'pl': get_message(user_lang, 'polish_lang'),
        'de': get_message(user_lang, 'german_lang'),
        'es': get_message(user_lang, 'spanish_lang'),
        'fr': get_message(user_lang, 'french_lang'),
        'uk': get_message(user_lang, 'ukrainian_lang')
    }
    language_name = lang_map.get(target_lang_code, target_lang_code)

    data = await state.get_data()
    news_id = data.get('current_news_id')
    news_item = await fetch_news_by_id(news_id)
    if not news_item:
        await callback.message.answer(get_message(user_lang, 'ai_news_not_found'))
        await callback.answer()
        await state.clear()
        return

    await bot.send_chat_action(callback.message.chat.id, ChatAction.TYPING)
    await callback.message.edit_text(get_message(user_lang, 'translating_news'))
    await callback.answer()

    prompt = f"Translate the following news article to {language_name} ({target_lang_code}): Title: {news_item.title}\nContent: {news_item.content}"
    translated_text = await call_gemini_api(prompt, user_data['telegram_id'])

    if translated_text:
        response_text = f"<b>{get_message(user_lang, 'translation_label').format(language_name=language_name)}</b>\n{translated_text}"
        if len(response_text) > 4096:
            for x in range(0, len(response_text), 4096):
                await callback.message.answer(response_text[x:x+4096])
        else:
            await callback.message.answer(response_text)
    else:
        await callback.message.answer(get_message(user_lang, 'ai_error'))

    await state.set_state(BotStates.AI_NEWS_MENU)
    await callback.message.answer(
        get_message(user_lang, 'ai_functions_prompt'),
        reply_markup=get_ai_news_menu_keyboard(user_lang, news_id)
    )

@router.callback_query(F.data.startswith("ask_news_ai_"))
async def handle_ask_news_ai(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    news_id = int(callback.data.split("_")[3])
    await state.update_data(current_news_id=news_id)
    await state.set_state(BotStates.ASK_NEWS_AI)
    await callback.message.edit_text(
        get_message(user_lang, 'ask_news_ai_prompt'),
        reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel")).as_markup()
    )
    await callback.answer()

@router.message(BotStates.ASK_NEWS_AI)
async def process_ask_news_ai(message: Message, state: FSMContext):
    user_data = await get_user_data(message)
    user_lang = user_data.get('language', 'uk')
    user_question = message.text
    data = await state.get_data()
    news_id = data.get('current_news_id')

    news_item = await fetch_news_by_id(news_id)
    if not news_item:
        await message.answer(get_message(user_lang, 'ai_news_not_found'))
        await state.clear()
        return

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await message.answer(get_message(user_lang, 'processing_question'))

    prompt = f"Given the news article: Title: {news_item.title}\nContent: {news_item.content}\n\nAnswer the following question based *only* on the provided article: {user_question}"
    ai_response = await call_gemini_api(prompt, user_data['telegram_id'])

    if ai_response:
        response_text = f"<b>{get_message(user_lang, 'ai_response_label')}</b>\n{ai_response}"
        if len(response_text) > 4096:
            for x in range(0, len(response_text), 4096):
                await message.answer(response_text[x:x+4096])
        else:
            await message.answer(response_text)
    else:
        await message.answer(get_message(user_lang, 'ai_error'))

    await state.set_state(BotStates.AI_NEWS_MENU)
    await message.answer(
        get_message(user_lang, 'ai_functions_prompt'),
        reply_markup=get_ai_news_menu_keyboard(user_lang, news_id)
    )

@router.callback_query(F.data.startswith("extract_entities_"))
async def handle_extract_entities(callback: CallbackQuery, state: FSMContext):
    await process_ai_request(
        callback,
        "Extract key entities (people, organizations, locations) from the following news article: Title: {title}\nContent: {content}",
        'extracting_entities'
    )

@router.callback_query(F.data.startswith("explain_term_"))
async def handle_explain_term(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    news_id = int(callback.data.split("_")[3])
    await state.update_data(current_news_id=news_id)
    await state.set_state(BotStates.EXPLAIN_TERM)
    await callback.message.edit_text(
        get_message(user_lang, 'explain_term_prompt'),
        reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel")).as_markup()
    )
    await callback.answer()

@router.message(BotStates.EXPLAIN_TERM)
async def process_explain_term(message: Message, state: FSMContext):
    user_data = await get_user_data(message)
    user_lang = user_data.get('language', 'uk')
    term = message.text.strip()
    data = await state.get_data()
    news_id = data.get('current_news_id')

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await message.answer(get_message(user_lang, 'explaining_term'))

    prompt = f"Explain the term '{term}' in the context of the following news article: Title: {news_item.title}\nContent: {news_item.content}" if news_id else f"Explain the term '{term}'."
    ai_response = await call_gemini_api(prompt, user_data['telegram_id'])

    if ai_response:
        response_text = f"<b>{get_message(user_lang, 'term_explanation_label').format(term=term)}</b>\n{ai_response}"
        if len(response_text) > 4096:
            for x in range(0, len(response_text), 4096):
                await message.answer(response_text[x:x+4096])
        else:
            await message.answer(response_text)
    else:
        await message.answer(get_message(user_lang, 'ai_error'))

    await state.set_state(BotStates.AI_NEWS_MENU)
    if news_id:
        await message.answer(
            get_message(user_lang, 'ai_functions_prompt'),
            reply_markup=get_ai_news_menu_keyboard(user_lang, news_id)
        )
    else:
        await message.answer(
            get_message(user_lang, 'main_menu_prompt'),
            reply_markup=get_main_menu_keyboard(user_lang)
        )

@router.callback_query(F.data.startswith("classify_topics_"))
async def handle_classify_topics(callback: CallbackQuery, state: FSMContext):
    await process_ai_request(
        callback,
        "Classify the main topics of the following news article into a list of keywords: Title: {title}\nContent: {content}",
        'classifying_topics'
    )

@router.callback_query(F.data.startswith("listen_news_"))
async def handle_listen_news(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    news_id = int(callback.data.split("_")[2])
    news_item = await fetch_news_by_id(news_id)
    if not news_item:
        await callback.message.answer(get_message(user_lang, 'ai_news_not_found'))
        await callback.answer()
        return

    await bot.send_chat_action(callback.message.chat.id, ChatAction.UPLOAD_AUDIO)
    await callback.message.edit_text(get_message(user_lang, 'generating_audio'))
    await callback.answer()

    try:
        tts = gTTS(text=news_item.content, lang=user_lang)
        audio_fp = io.BytesIO()
        tts.write_to_fp(audio_fp)
        audio_fp.seek(0)

        audio_file = BufferedInputFile(audio_fp.getvalue(), filename="news_audio.mp3")
        await bot.send_audio(
            callback.message.chat.id,
            audio=audio_file,
            caption=get_message(user_lang, 'audio_news_caption').format(title=news_item.title)
        )
    except Exception as e:
        logger.error(f"Error generating audio: {e}", exc_info=True)
        await callback.message.answer(get_message(user_lang, 'audio_error'))

    await callback.message.answer(
        get_message(user_lang, 'ai_functions_prompt'),
        reply_markup=get_ai_news_menu_keyboard(user_lang, news_id)
    )

@router.callback_query(F.data.startswith("fact_check_"))
async def handle_fact_check(callback: CallbackQuery, state: FSMContext):
    await process_ai_request(
        callback,
        "Fact-check the key claims in the following news article: Title: {title}\nContent: {content}",
        'checking_facts',
        check_premium=True
    )

@router.callback_query(F.data.startswith("sentiment_trend_analysis_"))
async def handle_sentiment_trend_analysis(callback: CallbackQuery, state: FSMContext):
    await process_ai_request(
        callback,
        "Analyze the sentiment (positive, negative, neutral) of the following news article: Title: {title}\nContent: {content}",
        'analyzing_sentiment',
        check_premium=True
    )

@router.callback_query(F.data.startswith("bias_detection_"))
async def handle_bias_detection(callback: CallbackQuery, state: FSMContext):
    await process_ai_request(
        callback,
        "Detect any potential biases in the following news article: Title: {title}\nContent: {content}",
        'detecting_bias',
        check_premium=True
    )

@router.callback_query(F.data.startswith("audience_summary_"))
async def handle_audience_summary(callback: CallbackQuery, state: FSMContext):
    await process_ai_request(
        callback,
        "Summarize the following news article for a general audience, simplifying complex terms: Title: {title}\nContent: {content}",
        'generating_audience_summary',
        check_premium=True
    )

@router.callback_query(F.data.startswith("historical_analogues_"))
async def handle_historical_analogues(callback: CallbackQuery, state: FSMContext):
    await process_ai_request(
        callback,
        "Find historical analogues or similar events related to the following news article: Title: {title}\nContent: {content}",
        'searching_historical_analogues',
        check_premium=True
    )

@router.callback_query(F.data.startswith("impact_analysis_"))
async def handle_impact_analysis(callback: CallbackQuery, state: FSMContext):
    await process_ai_request(
        callback,
        "Analyze the potential short-term and long-term impacts of the events described in the following news article: Title: {title}\nContent: {content}",
        'analyzing_impact',
        check_premium=True
    )

@router.callback_query(F.data.startswith("monetary_impact_"))
async def handle_monetary_impact(callback: CallbackQuery, state: FSMContext):
    await process_ai_request(
        callback,
        "Perform a monetary analysis or discuss the economic implications of the following news article: Title: {title}\nContent: {content}",
        'performing_monetary_analysis',
        check_premium=True
    )

@router.callback_query(F.data.startswith("bookmark_add_"))
async def handle_bookmark_add(callback: CallbackQuery):
    user_data = await get_user_data(callback)
    user_id = user_data['id']
    user_lang = user_data.get('language', 'uk')
    news_id = int(callback.data.split("_")[2])

    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "INSERT INTO user_bookmarks (user_id, news_id) VALUES (%s, %s) ON CONFLICT (user_id, news_id) DO NOTHING;",
                    (user_id, news_id)
                )
                if cur.rowcount > 0:
                    await conn.commit()
                    await callback.answer(get_message(user_lang, 'bookmark_added'))
                else:
                    await callback.answer(get_message(user_lang, 'bookmark_already_exists'))
            except Exception as e:
                logger.error(f"Error adding bookmark: {e}", exc_info=True)
                await callback.answer(get_message(user_lang, 'bookmark_add_error'))

@router.callback_query(F.data.startswith("bookmark_remove_"))
async def handle_bookmark_remove(callback: CallbackQuery):
    user_data = await get_user_data(callback)
    user_id = user_data['id']
    user_lang = user_data.get('language', 'uk')
    news_id = int(callback.data.split("_")[2])

    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "DELETE FROM user_bookmarks WHERE user_id = %s AND news_id = %s;",
                    (user_id, news_id)
                )
                if cur.rowcount > 0:
                    await conn.commit()
                    await callback.answer(get_message(user_lang, 'bookmark_removed'))
                else:
                    await callback.answer(get_message(user_lang, 'bookmark_not_found'))
            except Exception as e:
                logger.error(f"Error removing bookmark: {e}", exc_info=True)
                await callback.answer(get_message(user_lang, 'bookmark_remove_error'))

@router.callback_query(F.data == "my_bookmarks")
async def handle_my_bookmarks(callback: CallbackQuery):
    user_data = await get_user_data(callback)
    user_id = user_data['id']
    user_lang = user_data.get('language', 'uk')

    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT n.* FROM news n
                JOIN user_bookmarks ub ON n.id = ub.news_id
                WHERE ub.user_id = %s
                ORDER BY ub.created_at DESC;
                """,
                (user_id,)
            )
            bookmarked_news = await cur.fetchall()

    if not bookmarked_news:
        await callback.message.edit_text(get_message(user_lang, 'no_bookmarks'))
        await callback.answer()
        return

    response_text = get_message(user_lang, 'your_bookmarks_label') + "\n"
    for idx, news_item in enumerate(bookmarked_news):
        response_text += f"{idx + 1}. {news_item['title']} - {news_item['source_url']}\n"

    await callback.message.edit_text(response_text)
    await callback.answer()

@router.callback_query(F.data.startswith("report_fake_news_"))
async def handle_report_fake_news(callback: CallbackQuery):
    user_data = await get_user_data(callback)
    user_id = user_data['id']
    user_lang = user_data.get('language', 'uk')
    news_id = int(callback.data.split("_")[3])

    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "INSERT INTO reports (user_id, target_type, target_id, reason) VALUES (%s, 'news', %s, 'fake_news') ON CONFLICT (user_id, target_id, reason) DO NOTHING;",
                    (user_id, news_id)
                )
                if cur.rowcount > 0:
                    await conn.commit()
                    await callback.answer(get_message(user_lang, 'report_sent_success'))
                else:
                    await callback.answer(get_message(user_lang, 'report_already_sent'))
            except Exception as e:
                logger.error(f"Error reporting fake news: {e}", exc_info=True)
                await callback.answer(get_message(user_lang, 'report_error'))

    await callback.message.answer(get_message(user_lang, 'report_action_done'), reply_markup=get_main_menu_keyboard(user_lang))

@router.callback_query(F.data == "admin_panel")
async def callback_admin_panel(callback: CallbackQuery):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    if user_data['telegram_id'] not in ADMIN_IDS:
        await callback.answer(get_message(user_lang, 'no_admin_access'), show_alert=True)
        return
    await callback.message.edit_text("Адмін-панель:", reply_markup=get_admin_panel_keyboard(user_lang))
    await callback.answer()

@router.callback_query(F.data == "admin_moderation")
async def callback_admin_moderation(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    if user_data['telegram_id'] not in ADMIN_IDS:
        await callback.answer(get_message(user_lang, 'no_admin_access'), show_alert=True)
        return

    await state.set_state(BotStates.MODERATION_MENU)
    await callback.message.edit_text(get_message(user_lang, 'loading_moderation_news'))
    await callback.answer()

    await display_next_moderation_news(callback.message.chat.id, state, user_lang)

user_moderation_news_index: Dict[int, int] = {}
user_moderation_news_ids: Dict[int, List[int]] = {}

async def display_next_moderation_news(chat_id: int, state: FSMContext, user_lang: str):
    telegram_id = (await state.get_data()).get('user_telegram_id')
    user = await fetch_user_by_telegram_id(telegram_id)
    if not user:
        await bot.send_message(chat_id, get_message(user_lang, 'user_not_identified'))
        return

    news_list = await fetch_news_for_moderation(offset=user_moderation_news_index.get(user.id, 0), limit=20)
    if not news_list:
        await bot.send_message(chat_id, get_message(user_lang, 'no_pending_news'), reply_markup=get_admin_panel_keyboard(user_lang))
        if user.id in user_moderation_news_index:
            del user_moderation_news_index[user.id]
        if user.id in user_moderation_news_ids:
            del user_moderation_news_ids[user.id]
        return

    user_moderation_news_ids[user.id] = [n.id for n in news_list]
    current_index = user_moderation_news_index.get(user.id, 0)
    total_news = len(user_moderation_news_ids[user.id])

    news_item = await fetch_news_by_id(user_moderation_news_ids[user.id][current_index])
    if not news_item:
        await bot.send_message(chat_id, get_message(user_lang, 'news_not_found'))
        return

    source = await fetch_source_by_id(news_item.source_id)
    source_name = source.source_name if source else "Unknown"

    message_text = (
        f"<b>{get_message(user_lang, 'moderation_news_label').format(current_index=current_index + 1, total_news=total_news)}</b>\n\n"
        f"<b>{get_message(user_lang, 'news_title_label')}</b> {news_item.title}\n\n"
        f"<b>{get_message(user_lang, 'news_content_label')}</b> {news_item.content}\n\n"
        f"<b>{get_message(user_lang, 'source_label')}</b> {source_name} ({news_item.source_url})\n"
        f"<b>{get_message(user_lang, 'status_label')}</b> {news_item.moderation_status}"
    )

    has_prev = current_index > 0
    has_next = current_index < total_news - 1

    await bot.send_message(
        chat_id,
        message_text,
        reply_markup=get_moderation_news_keyboard(user_lang, news_item.id, has_prev, has_next)
    )

@router.callback_query(F.data.startswith("approve_news_"))
async def callback_approve_news(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    if user_data['telegram_id'] not in ADMIN_IDS:
        await callback.answer(get_message(user_lang, 'no_admin_access'), show_alert=True)
        return

    news_id = int(callback.data.split("_")[2])
    await update_news_moderation_status(news_id, 'approved')
    await callback.answer(get_message(user_lang, 'news_approved').format(news_id=news_id))

    news_item = await fetch_news_by_id(news_id)
    if news_item and not news_item.is_published_to_channel:
        await send_news_to_channel(news_item, user_lang)

    user_id = user_data['id']
    if user_id in user_moderation_news_ids and news_id in user_moderation_news_ids[user_id]:
        user_moderation_news_ids[user_id].remove(news_id)
        if user_moderation_news_index[user_id] >= len(user_moderation_news_ids[user_id]) and len(user_moderation_news_ids[user_id]) > 0:
            user_moderation_news_index[user_id] = len(user_moderation_news_ids[user_id]) - 1

    await display_next_moderation_news(callback.message.chat.id, state, user_lang)

@router.callback_query(F.data.startswith("reject_news_"))
async def callback_reject_news(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    if user_data['telegram_id'] not in ADMIN_IDS:
        await callback.answer(get_message(user_lang, 'no_admin_access'), show_alert=True)
        return

    news_id = int(callback.data.split("_")[2])
    await update_news_moderation_status(news_id, 'rejected')
    await callback.answer(get_message(user_lang, 'news_rejected').format(news_id=news_id))

    user_id = user_data['id']
    if user_id in user_moderation_news_ids and news_id in user_moderation_news_ids[user_id]:
        user_moderation_news_ids[user_id].remove(news_id)
        if user_moderation_news_index[user_id] >= len(user_moderation_news_ids[user_id]) and len(user_moderation_news_ids[user_id]) > 0:
            user_moderation_news_index[user_id] = len(user_moderation_news_ids[user_id]) - 1

    await display_next_moderation_news(callback.message.chat.id, state, user_lang)

@router.callback_query(F.data == "next_moderation_news")
async def callback_next_moderation_news(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_id = user_data['id']
    user_lang = user_data.get('language', 'uk')

    if user_id not in user_moderation_news_index:
        await callback.message.edit_text(get_message(user_lang, 'no_more_moderation_news'))
        await callback.answer()
        return

    user_moderation_news_index[user_id] += 1
    if user_moderation_news_index[user_id] >= len(user_moderation_news_ids[user_id]):
        await callback.message.edit_text(get_message(user_lang, 'all_moderation_done'))
        del user_moderation_news_index[user_id]
        del user_moderation_news_ids[user_id]
        await callback.answer()
        return

    await display_next_moderation_news(callback.message.chat.id, state, user_lang)
    await callback.answer()

@router.callback_query(F.data == "prev_moderation_news")
async def callback_prev_moderation_news(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_id = user_data['id']
    user_lang = user_data.get('language', 'uk')

    if user_id not in user_moderation_news_index:
        await callback.message.edit_text(get_message(user_lang, 'first_moderation_news'))
        await callback.answer()
        return

    user_moderation_news_index[user_id] -= 1
    if user_moderation_news_index[user_id] < 0:
        await callback.message.edit_text(get_message(user_lang, 'first_moderation_news'))
        user_moderation_news_index[user_id] = 0
        await callback.answer()
        return

    await display_next_moderation_news(callback.message.chat.id, state, user_lang)
    await callback.answer()

@router.callback_query(F.data == "admin_stats")
async def callback_admin_stats(callback: CallbackQuery):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    if user_data['telegram_id'] not in ADMIN_IDS:
        await callback.answer(get_message(user_lang, 'no_admin_access'), show_alert=True)
        return

    stats_text = "📊 <b>Статистика бота:</b>\n"
    stats_text += f"Користувачів: {await count_total_users()}\n"
    stats_text += f"Новин: {await count_total_news()}\n"
    stats_text += f"Активних за 24 год: {await count_active_users_last_24_hours()}\n\n"

    source_stats = await get_source_stats()
    if source_stats:
        stats_text += get_message(user_lang, 'source_stats_label') + "\n"
        for idx, (source_name, count) in enumerate(source_stats.items()):
            stats_text += get_message(user_lang, 'source_stats_entry').format(idx=idx + 1, source_name=source_name, count=count) + "\n"
    else:
        stats_text += get_message(user_lang, 'no_source_stats') + "\n"

    await callback.message.edit_text(stats_text, reply_markup=get_admin_panel_keyboard(user_lang))
    await callback.answer()

@router.callback_query(F.data == "invite")
async def callback_invite(callback: CallbackQuery):
    user_data = await get_user_data(callback)
    user_id = user_data['id']
    user_lang = user_data.get('language', 'uk')

    try:
        invite_code = await create_invite_code(user_id)
        invite_link = f"https://t.me/{bot.me.username}?start=invite_{invite_code}"
        await callback.message.answer(
            get_message(user_lang, 'your_invite_code').format(invite_code=invite_code, invite_link=invite_link),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Error creating invite code: {e}", exc_info=True)
        await callback.message.answer(get_message(user_lang, 'invite_error'))
    await callback.answer()

@router.message(CommandStart(deep_link=True, magic=F.args.startswith("invite_")))
async def handler_deep_link_invite(message: Message, state: FSMContext):
    invite_code = message.text.split("_")[1]
    user_telegram_id = message.from_user.id
    user_data = await get_user_data(message)
    user_lang = user_data.get('language', 'uk')

    if await use_invite_code(invite_code, user_telegram_id):
        await message.answer(get_message(user_lang, 'invite_accepted_premium_granted'))
    else:
        await message.answer(get_message(user_lang, 'invite_code_invalid_or_used'))

    await state.set_state(BotStates.MAIN_MENU)
    await message.answer(
        get_message(user_lang, 'welcome', first_name=message.from_user.first_name),
        reply_markup=get_main_menu_keyboard(user_lang)
    )

@router.callback_query(F.data == "subscribe")
async def callback_subscribe(callback: CallbackQuery):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(user_lang, 'toggle_notifications_btn'), callback_data="toggle_notifications")
    builder.button(text=get_message(user_lang, 'set_digest_frequency_btn'), callback_data="set_digest_frequency")
    builder.button(text=get_message(user_lang, 'toggle_safe_mode_btn'), callback_data="toggle_safe_mode")
    builder.button(text=get_message(user_lang, 'set_view_mode_btn'), callback_data="set_view_mode")
    builder.button(text=get_message(user_lang, 'set_language'), callback_data="set_language")
    builder.button(text=get_message(user_lang, 'main_menu_btn'), callback_data="main_menu")
    builder.adjust(2)
    await callback.message.edit_text(get_message(user_lang, 'subscribe_menu_prompt'), reply_markup=builder.as_markup())
    await callback.answer()

@router.callback_query(F.data == "toggle_notifications")
async def handle_toggle_notifications(callback: CallbackQuery):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    telegram_id = user_data['telegram_id']
    new_status = not user_data.get('auto_notifications', False)
    await update_user_settings(telegram_id, 'auto_notifications', new_status)
    await callback.answer(get_message(user_lang, 'notifications_toggled').format(status='увімкнено' if new_status else 'вимкнено'))
    await callback.message.edit_reply_markup(reply_markup=get_main_menu_keyboard(user_lang))

@router.callback_query(F.data == "set_digest_frequency")
async def handle_set_digest_frequency(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(user_lang, 'daily_digest'), callback_data="freq_daily")
    builder.button(text=get_message(user_lang, 'weekly_digest'), callback_data="freq_weekly")
    builder.button(text=get_message(user_lang, 'monthly_digest'), callback_data="freq_monthly")
    builder.button(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel")
    builder.adjust(1)
    await state.set_state(BotStates.SET_DIGEST_FREQUENCY)
    await callback.message.edit_text(get_message(user_lang, 'select_digest_frequency'), reply_markup=builder.as_markup())
    await callback.answer()

@router.callback_query(BotStates.SET_DIGEST_FREQUENCY, F.data.startswith("freq_"))
async def process_set_digest_frequency(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    telegram_id = user_data['telegram_id']
    frequency = callback.data.split("_")[1]
    await update_user_settings(telegram_id, 'digest_frequency', frequency)
    await callback.answer(get_message(user_lang, 'digest_frequency_set').format(frequency=frequency))
    await state.clear()
    await callback.message.edit_text(get_message(user_lang, 'main_menu_prompt'), reply_markup=get_main_menu_keyboard(user_lang))

@router.callback_query(F.data == "toggle_safe_mode")
async def handle_toggle_safe_mode(callback: CallbackQuery):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    telegram_id = user_data['telegram_id']
    new_status = not user_data.get('safe_mode', False)
    await update_user_settings(telegram_id, 'safe_mode', new_status)
    await callback.answer(get_message(user_lang, 'safe_mode_toggled').format(status='увімкнено' if new_status else 'вимкнено'))
    await callback.message.edit_reply_markup(reply_markup=get_main_menu_keyboard(user_lang))

@router.callback_query(F.data == "set_view_mode")
async def handle_set_view_mode(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(user_lang, 'detailed_view'), callback_data="view_detailed")
    builder.button(text=get_message(user_lang, 'compact_view'), callback_data="view_compact")
    builder.button(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel")
    builder.adjust(1)
    await state.set_state(BotStates.SET_VIEW_MODE)
    await callback.message.edit_text(get_message(user_lang, 'select_view_mode'), reply_markup=builder.as_markup())
    await callback.answer()

@router.callback_query(BotStates.SET_VIEW_MODE, F.data.startswith("view_"))
async def process_set_view_mode(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    telegram_id = user_data['telegram_id']
    view_mode = callback.data.split("_")[1]
    await update_user_settings(telegram_id, 'view_mode', view_mode)
    await callback.answer(get_message(user_lang, 'view_mode_set').format(mode=view_mode))
    await state.clear()
    await callback.message.edit_text(get_message(user_lang, 'main_menu_prompt'), reply_markup=get_main_menu_keyboard(user_lang))

@router.callback_query(F.data == "set_language")
async def handle_set_language(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    await state.set_state(BotStates.SET_LANGUAGE)
    await callback.message.edit_text(
        get_message(user_lang, 'select_translate_language'),
        reply_markup=get_language_keyboard(user_lang)
    )
    await callback.answer()

@router.callback_query(BotStates.SET_LANGUAGE, F.data.startswith("set_lang_"))
async def process_set_language(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    telegram_id = user_data['telegram_id']
    new_lang = callback.data.split("_")[2]

    await update_user_settings(telegram_id, 'language', new_lang)
    user_lang = new_lang

    await callback.answer(get_message(user_lang, 'language_set'))
    await state.clear()
    await callback.message.edit_text(get_message(user_lang, 'main_menu_prompt'), reply_markup=get_main_menu_keyboard(user_lang))

@router.callback_query(F.data == "ai_media_menu")
async def callback_ai_media_menu(callback: CallbackQuery):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    await callback.message.edit_text(
        get_message(user_lang, 'ai_media_menu_prompt'),
        reply_markup=get_ai_media_menu_keyboard(user_lang)
    )
    await callback.answer()

@router.callback_query(F.data == "generate_ai_news")
async def handle_generate_ai_news(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    await state.set_state(BotStates.GENERATE_AI_NEWS_PROMPT)
    await callback.message.edit_text(
        get_message(user_lang, 'generate_ai_news_prompt'),
        reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel")).as_markup()
    )
    await callback.answer()

@router.message(BotStates.GENERATE_AI_NEWS_PROMPT)
async def process_generate_ai_news(message: Message, state: FSMContext):
    user_data = await get_user_data(message)
    user_lang = user_data.get('language', 'uk')
    prompt_text = message.text
    telegram_id = user_data['telegram_id']

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await message.answer(get_message(user_lang, 'generating_ai_news'))

    ai_news_content = await call_gemini_api(
        f"Generate a concise news article (around 200-300 words) based on the following prompt, including a title, and provide a relevant image URL. Format as JSON: {{'title': '...', 'content': '...', 'image_url': '...'}}\n\nPrompt: {prompt_text}",
        telegram_id
    )

    if ai_news_content:
        try:
            news_data = json.loads(ai_news_content)
            title = news_data.get('title', 'AI-Generated News')
            content = news_data.get('content', 'No content generated.')
            image_url_str = news_data.get('image_url')
            image_url = HttpUrl(image_url_str) if image_url_str else None

            source_name = "AI-Generated"
            source_url = HttpUrl(f"https://ai-news.com/{int(time.time())}")

            source_record = await add_source(telegram_id, source_url, source_name)
            news_item = await add_news(title, content, source_url, source_record.id, image_url)

            message_text = (
                f"<b>{get_message(user_lang, 'news_title_label')}</b> {news_item.title}\n\n"
                f"<b>{get_message(user_lang, 'news_content_label')}</b> {news_item.content}\n\n"
                f"<b>{get_message(user_lang, 'published_at_label')}</b> {news_item.published_at.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"<i>{get_message(user_lang, 'source_label')}</i> AI-Generated"
            )

            if news_item.image_url:
                try:
                    await bot.send_photo(
                        message.chat.id,
                        photo=str(news_item.image_url),
                        caption=message_text,
                        reply_markup=get_main_menu_keyboard(user_lang)
                    )
                except Exception as e:
                    logger.error(f"Failed to send AI-generated news photo: {e}. Sending text message instead with placeholder.")
                    fallback_image_url = f"https://via.placeholder.com/600x400?text=AI+News"
                    await bot.send_photo(
                        message.chat.id,
                        photo=fallback_image_url,
                        caption=message_text + f"\n\n[Оригінальне зображення недоступне: {news_item.image_url}]",
                        reply_markup=get_main_menu_keyboard(user_lang)
                    )
            else:
                await message.answer(message_text, reply_markup=get_main_menu_keyboard(user_lang))

        except json.JSONDecodeError:
            await message.answer(get_message(user_lang, 'ai_error_format'))
            logger.error(f"AI response was not valid JSON: {ai_news_content}")
        except Exception as e:
            logger.error(f"Error processing AI generated news: {e}", exc_info=True)
            await message.answer(get_message(user_lang, 'ai_error'))
    else:
        await message.answer(get_message(user_lang, 'ai_error'))

    await state.clear()
    await message.answer(
        get_message(user_lang, 'main_menu_prompt'),
        reply_markup=get_main_menu_keyboard(user_lang)
    )

@router.callback_query(F.data == "process_youtube_url")
async def handle_process_youtube_url(callback: CallbackQuery, state: FSMContext):
    user_data = await get_user_data(callback)
    user_lang = user_data.get('language', 'uk')
    await state.set_state(BotStates.PROCESS_YOUTUBE_URL)
    await callback.message.edit_text(
        get_message(user_lang, 'process_youtube_url_prompt'),
        reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel")).as_markup()
    )
    await callback.answer()

@router.message(BotStates.PROCESS_YOUTUBE_URL)
async def process_youtube_url(message: Message, state: FSMContext):
    user_data = await get_user_data(message)
    user_lang = user_data.get('language', 'uk')
    youtube_url = message.text.strip()
    telegram_id = user_data['telegram_id']

    if "youtube.com/watch?v=" not in youtube_url and "youtu.be/" not in youtube_url:
        await message.answer(get_message(user_lang, 'invalid_youtube_url'))
        return

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await message.answer(get_message(user_lang, 'processing_youtube_url'))

    try:
        search_query = f"transcribe youtube video {youtube_url}"
        search_results = Google Search(queries=[search_query])
        transcript_text = ""
        if search_results and search_results[0].results:
            for result in search_results[0].results:
                if "transcript" in result.snippet.lower() or "subtitles" in result.snippet.lower():
                    transcript_text = result.snippet
                    break

        if not transcript_text:
            transcript_text = "No transcript found via search. Analyzing video content directly."

        ai_analysis_prompt = (
            f"Analyze the content of this YouTube video (URL: {youtube_url}). "
            f"Here is a partial transcript/summary if available: '{transcript_text}'. "
            f"Provide a summary, identify key topics, and suggest a title for a news article based on this video. "
            f"Also, suggest a relevant image URL for the news article (e.g., a YouTube thumbnail URL or a related stock photo). "
            f"Format as JSON: {{'title': '...', 'summary': '...', 'topics': ['...', '...'], 'image_url': '...'}}"
        )

        ai_response = await call_gemini_api(ai_analysis_prompt, telegram_id)

        if ai_response:
            try:
                video_data = json.loads(ai_response)
                title = video_data.get('title', 'YouTube Video Analysis')
                summary = video_data.get('summary', 'No summary generated.')
                topics = ", ".join(video_data.get('topics', []))
                image_url_str = video_data.get('image_url')
                image_url = HttpUrl(image_url_str) if image_url_str else None

                news_content = f"{summary}\n\nKey Topics: {topics}\n\nOriginal Video: {youtube_url}"
                source_name = "YouTube Analysis"
                source_url = HttpUrl(f"https://youtube.com/analysis/{int(time.time())}")

                source_record = await add_source(telegram_id, source_url, source_name)
                news_item = await add_news(title, news_content, source_url, source_record.id, image_url)

                message_text = (
                    f"<b>{get_message(user_lang, 'news_title_label')}</b> {news_item.title}\n\n"
                    f"<b>{get_message(user_lang, 'news_content_label')}</b> {summary}\n\n"
                    f"<b>{get_message(user_lang, 'topics_label')}</b> {topics}\n\n"
                    f"🔗 <a href='{youtube_url}'>{get_message(user_lang, 'view_on_youtube')}</a>"
                )

                if news_item.image_url:
                    try:
                        await bot.send_photo(
                            message.chat.id,
                            photo=str(news_item.image_url),
                            caption=message_text,
                            reply_markup=get_main_menu_keyboard(user_lang)
                        )
                    except Exception as e:
                        logger.error(f"Failed to send YouTube analysis photo: {e}. Sending text message instead with placeholder.")
                        fallback_image_url = f"https://via.placeholder.com/600x400?text=YouTube+Analysis"
                        await bot.send_photo(
                            message.chat.id,
                            photo=fallback_image_url,
                            caption=message_text + f"\n\n[Оригінальне зображення недоступне: {news_item.image_url}]",
                            reply_markup=get_main_menu_keyboard(user_lang)
                        )
                else:
                    await message.answer(message_text, reply_markup=get_main_menu_keyboard(user_lang))

            except json.JSONDecodeError:
                await message.answer(get_message(user_lang, 'ai_error_format'))
                logger.error(f"AI response was not valid JSON for YouTube: {ai_response}")
            except Exception as e:
                logger.error(f"Error processing YouTube analysis: {e}", exc_info=True)
                await message.answer(get_message(user_lang, 'ai_error'))
        else:
            await message.answer(get_message(user_lang, 'ai_error'))

    except Exception as e:
        logger.error(f"Error in YouTube URL processing: {e}", exc_info=True)
        await message.answer(get_message(user_lang, 'youtube_process_error'))

    await state.clear()
    await message.answer(
        get_message(user_lang, 'main_menu_prompt'),
        reply_markup=get_main_menu_keyboard(user_lang)
    )

async def send_news_to_channel(news_item: News, user_lang: str):
    if not CHANNEL_ID:
        logger.error("CHANNEL_ID is not set.")
        return

    try:
        news_content_for_channel = news_item.content
        if len(news_content_for_channel) > 250:
            ai_summary_for_channel = await call_gemini_api(
                f"Summarize the following news article for a short Telegram channel post (max 200 characters): Title: {news_item.title}\nContent: {news_item.content}",
                user_telegram_id=ADMIN_IDS[0] if ADMIN_IDS else 0
            )
            if ai_summary_for_channel:
                news_content_for_channel = ai_summary_for_channel
            else:
                news_content_for_channel = news_content_for_channel[:200] + "..."

        caption = (
            f"<b>{news_item.title}</b>\n\n"
            f"{news_content_for_channel}\n\n"
            f"🔗 <a href='{news_item.source_url}'>{get_message(user_lang, 'read_source_btn')}</a>"
        )

        sent_message = None
        if news_item.image_url:
            try:
                sent_message = await bot.send_photo(
                    CHANNEL_ID,
                    photo=str(news_item.image_url),
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.warning(f"Failed to send photo to channel {CHANNEL_ID} for news {news_item.id}: {e}. Sending text message instead.")
                sent_message = await bot.send_message(
                    CHANNEL_ID,
                    caption + f"\n\n[Зображення недоступне: {news_item.image_url}]",
                    parse_mode=ParseMode.HTML
                )
        else:
            sent_message = await bot.send_message(
                CHANNEL_ID,
                caption,
                parse_mode=ParseMode.HTML
            )

        if sent_message:
            await add_news_to_channel_history(news_item.id, sent_message.message_id, str(CHANNEL_ID))
            await update_news_published_status(news_item.id, True)
            logger.info(get_message(user_lang, 'news_published_success').format(title=news_item.title, identifier=CHANNEL_ID))
        else:
            logger.error(get_message(user_lang, 'news_publish_error').format(title=news_item.title, error="Failed to send message to channel."))

    except Exception as e:
        logger.error(get_message(user_lang, 'news_publish_error').format(title=news_item.title, error=e), exc_info=True)

async def parse_and_add_news():
    sources = await fetch_all_active_sources()
    if not sources:
        logger.info("No active sources found for parsing.")
        return

    for source in sources:
        try:
            parsed_articles = []
            if source.source_type == 'web':
                parsed_articles = await web_parser.parse_website(str(source.source_url))
            elif source.source_type == 'telegram':
                parsed_articles = await telegram_parser.parse_telegram_channel(str(source.source_url))
            elif source.source_type == 'rss':
                parsed_articles = await rss_parser.parse_rss_feed(str(source.source_url))
            elif source.source_type == 'social_media':
                parsed_articles = await social_media_parser.parse_social_media(str(source.source_url))

            if not parsed_articles:
                logger.warning(get_message('uk', 'source_parsing_warning').format(name=source.source_name, url=source.source_url))
                continue

            for article in parsed_articles:
                if not await news_exists(article.source_url):
                    news_item = await add_news(
                        title=article.title,
                        content=article.content,
                        source_url=article.source_url,
                        source_id=source.id,
                        image_url=article.image_url
                    )
                    logger.info(get_message('uk', 'news_added_success').format(title=news_item.title))
                else:
                    logger.info(get_message('uk', 'news_already_exists').format(url=article.source_url))
            await update_source_last_parsed(source.id)
            logger.info(get_message('uk', 'source_last_parsed_updated').format(name=source.source_name))

        except Exception as e:
            logger.error(get_message('uk', 'source_parsing_error').format(name=source.source_name, url=source.source_url, error=e), exc_info=True)

async def delete_expired_news():
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM news WHERE expires_at < CURRENT_TIMESTAMP RETURNING id;")
            deleted_count = cur.rowcount
            await conn.commit()
            if deleted_count > 0:
                logger.info(get_message('uk', 'deleted_expired_news').format(count=deleted_count))

async def send_daily_digests():
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM users WHERE auto_notifications = TRUE AND digest_frequency = 'daily';")
            daily_digest_users = await cur.fetchall()

    for user_data in daily_digest_users:
        user = User(**user_data)
        user_lang = user.language
        digest_news = await fetch_news_for_digest(user.id, user_lang, limit=5)
        if digest_news:
            digest_text = get_message(user_lang, 'daily_digest_header') + "\n\n"
            for idx, news_item in enumerate(digest_news):
                summary_for_digest = news_item.ai_summary or news_item.content[:150] + "..."
                digest_text += get_message(user_lang, 'daily_digest_entry').format(
                    idx=idx + 1,
                    title=news_item.title,
                    summary=summary_for_digest,
                    source_url=news_item.source_url
                )
                await mark_news_as_viewed(user.id, news_item.id)
            await bot.send_message(user.telegram_id, digest_text, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(user.telegram_id, get_message(user_lang, 'no_news_for_digest'))

async def scheduler():
    while True:
        try:
            await parse_and_add_news()
            await delete_expired_news()
            await send_daily_digests()
        except Exception as e:
            logger.error(f"Error in scheduler task: {e}", exc_info=True)
        await asyncio.sleep(60 * 60)

async def on_startup(dispatcher: Dispatcher, bot: Bot):
    logger.info("Bot is starting...")
    await get_db_pool()
    if WEBHOOK_URL:
        await bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"Webhook set to {WEBHOOK_URL}")
    asyncio.create_task(scheduler())
    logger.info("Scheduler task started.")

async def on_shutdown(dispatcher: Dispatcher, bot: Bot):
    logger.info("Bot is shutting down...")
    if db_pool:
        await db_pool.close()
        logger.info("DB pool closed.")
    logger.info("Bot shutdown complete.")

@app.on_event("startup")
async def startup_event():
    await on_startup(dp, bot)

@app.on_event("shutdown")
async def shutdown_event():
    await on_shutdown(dp, bot)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/dashboard", response_class=HTMLResponse)
async def read_dashboard_html():
    with open("dashboard.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/users", response_class=HTMLResponse)
async def read_users_html():
    with open("users.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/reports", response_class=HTMLResponse)
async def read_reports_html():
    with open("reports.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/api/admin/stats")
async def get_admin_stats(api_key: str = Depends(get_api_key)):
    total_users = await count_total_users()
    total_news = await count_total_news()
    active_users_count = await count_active_users_last_24_hours()
    return {
        "total_users": total_users,
        "total_news": total_news,
        "active_users_count": active_users_count
    }

@app.get("/api/admin/news/counts")
async def get_news_status_counts(api_key: str = Depends(get_api_key)):
    counts = await fetch_news_status_counts()
    return counts

@app.get("/api/admin/users")
async def get_admin_users(api_key: str = Depends(get_api_key), limit: int = 10, offset: int = 0):
    users = await fetch_active_users(limit, offset)
    return [user.__dict__ for user in users]

@app.put("/api/admin/news/{news_id}", response_model=Dict)
async def update_admin_news_api(news_id: int, news: News, api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            params = []
            updates = []
            if news.title:
                updates.append("title = %s")
                params.append(news.title)
            if news.content:
                updates.append("content = %s")
                params.append(news.content)
            if news.source_url:
                updates.append("source_url = %s")
                params.append(str(news.source_url))
            if news.image_url:
                updates.append("image_url = %s")
                params.append(str(news.image_url))
            if news.published_at:
                updates.append("published_at = %s")
                params.append(news.published_at)
            if news.ai_summary:
                updates.append("ai_summary = %s")
                params.append(news.ai_summary)
            if news.ai_classified_topics:
                updates.append("ai_classified_topics = %s")
                params.append(json.dumps(news.ai_classified_topics))
            if news.moderation_status:
                updates.append("moderation_status = %s")
                params.append(news.moderation_status)
            if news.expires_at:
                updates.append("expires_at = %s")
                params.append(news.expires_at)
            if news.is_published_to_channel is not None:
                updates.append("is_published_to_channel = %s")
                params.append(news.is_published_to_channel)

            if not updates:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update.")

            params.append(news_id)
            await cur.execute(f"""UPDATE news SET {", ".join(updates)} WHERE id = %s RETURNING *;""", tuple(params))
            updated_rec = await cur.fetchone()
            if not updated_rec:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="News not found.")
            return News(**updated_rec).__dict__

@app.delete("/api/admin/news/{news_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_admin_news_api(news_id: int, api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("DELETE FROM news WHERE id = %s", (news_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="News not found.")
            return

if __name__ == "__main__":
    import uvicorn
    if WEBHOOK_URL:
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
    else:
        asyncio.run(dp.start_polling(bot))