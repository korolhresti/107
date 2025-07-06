import asyncio
import logging
import logging.handlers
from datetime import datetime, timedelta, timezone, date
import json
import os
from typing import List, Optional, Dict, Any, Union
import random
import io
import base64
import time
import re

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
from pydantic import BaseModel, HttpUrl

import web_parser
import telegram_parser
import rss_parser
import social_media_parser

load_dotenv()

API_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
NEWS_FETCH_INTERVAL_SECONDS = int(os.getenv("NEWS_FETCH_INTERVAL_SECONDS", 200))
NEWS_CHANNEL_ID = os.getenv("NEWS_CHANNEL_ID")
NEWS_CHANNEL_LINK = os.getenv("NEWS_CHANNEL_LINK")
DAILY_DIGEST_CRON = os.getenv("DAILY_DIGEST_CRON", "0 9 * * *")
AI_NEWS_GEN_CRON = os.getenv("AI_NEWS_GEN_CRON", "0 */2 * * *")
MAX_AI_REQUESTS_PER_DAY = int(os.getenv("MAX_AI_REQUESTS_PER_DAY", 10))

bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

db_pool: Optional[AsyncConnectionPool] = None
BOT_USERNAME: Optional[str] = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

file_handler = logging.handlers.RotatingFileHandler(
    'bot_activity.log', maxBytes=10485760, backupCount=5
)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)

async def get_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = AsyncConnectionPool(conninfo=DATABASE_URL, open=False)
        await db_pool.open()
        logger.info("DB pool initialized successfully.")
    return db_pool

app = FastAPI()

# Ensure the 'static' directory exists
if not os.path.exists("static"):
    os.makedirs("static")
    logger.info("Created 'static' directory.")

app.mount("/static", StaticFiles(directory="static"), name="static")

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
    ai_last_request_date: Optional[date] = None

class Source(BaseModel):
    id: Optional[int] = None
    user_id: Optional[int] = None
    name: str
    url: HttpUrl
    type: str
    is_active: Optional[bool] = True
    created_at: Optional[datetime] = None
    last_parsed: Optional[datetime] = None
    parse_interval_minutes: Optional[int] = 30
    language: Optional[str] = 'uk'
    is_verified: Optional[bool] = False
    category: Optional[str] = 'general'
    priority: Optional[int] = 1

class News(BaseModel):
    id: Optional[int] = None
    source_id: int
    title: str
    content: str
    source_url: HttpUrl
    image_url: Optional[HttpUrl] = None
    published_at: datetime
    ai_summary: Optional[str] = None
    ai_classified_topics: Optional[List[str]] = []
    moderation_status: Optional[str] = 'pending'
    expires_at: Optional[datetime] = None
    is_published_to_channel: Optional[bool] = False

class Comment(BaseModel):
    id: Optional[int] = None
    news_id: int
    user_id: int
    text: str
    created_at: Optional[datetime] = None
    status: Optional[str] = 'active'

class Reaction(BaseModel):
    id: Optional[int] = None
    news_id: int
    user_id: int
    type: str
    created_at: Optional[datetime] = None

class Report(BaseModel):
    id: Optional[int] = None
    user_id: int
    target_type: str
    target_id: int
    reason: Optional[str] = None
    created_at: Optional[datetime] = None
    status: Optional[str] = 'pending'

class Invitation(BaseModel):
    id: Optional[int] = None
    inviter_user_id: Optional[int] = None
    invite_code: str
    created_at: Optional[datetime] = None
    used_at: Optional[datetime] = None
    status: Optional[str] = 'pending'
    invitee_telegram_id: Optional[int] = None

MESSAGES = {
    'uk': {
        'start_welcome': "Вітаю! Я ваш персональний новинний бот. Обери, що тебе цікавить:",
        'main_menu': "Головне меню:",
        'my_news': "Мої новини 📰",
        'ai_media': "AI Медіа 🤖",
        'settings': "Налаштування ⚙️",
        'admin_panel': "Адмін-панель 👑",
        'back': "⬅️ Назад",
        'cancel': "❌ Скасувати",
        'next_news': "➡️ Наступна",
        'prev_news': "⬅️ Попередня",
        'no_more_news': "Більше новин немає.",
        'source_added': "Джерело '{name}' успішно додано.",
        'source_exists': "Джерело '{name}' вже існує.",
        'invalid_url': "Будь ласка, введіть коректний URL.",
        'enter_source_url': "Будь ласка, введіть URL нового джерела:",
        'select_source_type': "Оберіть тип джерела:",
        'web_type': "🌐 Веб-сайт",
        'rss_type': "📡 RSS-стрічка",
        'telegram_type': "✈️ Telegram-канал",
        'social_media_type': "📱 Соціальна мережа",
        'source_type_selected': "Тип джерела '{source_type}' обрано. Тепер введіть URL.",
        'source_deleted': "Джерело успішно видалено.",
        'source_not_found': "Джерело не знайдено.",
        'enter_source_id_to_delete': "Введіть ID джерела, яке хочете видалити:",
        'error': "Виникла помилка: {e}",
        'ai_summary': "📝 AI Резюме",
        'ai_topics': "🏷️ AI Теми",
        'ai_sentiment': "📊 AI Настрій",
        'ai_fact_check': "✅ AI Факт-чек (Преміум)",
        'ai_audience_summary': "👥 AI Аудиторія (Преміум)",
        'ai_historical_analogues': "🕰️ AI Історичні Аналоги (Преміум)",
        'ai_impact_analysis': "💥 AI Аналіз Впливу (Преміум)",
        'ai_monetary_analysis': "💰 AI Фінансовий Аналіз (Преміум)",
        'premium_only': "Ця функція доступна лише для преміум користувачів. Оформіть підписку, щоб отримати доступ!",
        'ai_price_analysis': "💲 AI Аналіз Цін",
        'enter_product_name': "Будь ласка, введіть назву продукту для аналізу цін:",
        'price_analysis_result': "Аналіз цін для '{product_name}':\n{analysis}",
        'no_price_analysis_data': "Не вдалося знайти дані для аналізу цін по '{product_name}'.",
        'ai_news_generation': "✍️ AI Генерація Новин",
        'ai_news_generation_prompt': "Введіть тему для генерації AI новини:",
        'ai_news_generated': "Ваша AI новина готова!",
        'generating_ai_news': "Генерую AI новину, це може зайняти деякий час...",
        'comments_btn': "💬 Коментарі",
        'like_btn': "👍",
        'dislike_btn': "👎",
        'bookmark_btn': "🔖",
        'no_news_found': "Наразі немає новин за вашими критеріями.",
        'select_language': "Оберіть мову:",
        'language_set': "Мову змінено на українську.",
        'notifications_on': "Автоматичні сповіщення увімкнено.",
        'notifications_off': "Автоматичні сповіщення вимкнено.",
        'digest_frequency_set': "Частота дайджесту встановлена на {frequency}.",
        'select_digest_frequency': "Оберіть частоту дайджесту:",
        'daily': "Щоденно",
        'weekly': "Щотижнево",
        'monthly': "Щомісячно",
        'off': "Вимкнено",
        'safe_mode_on': "Безпечний режим увімкнено. Контент 18+ буде приховано.",
        'safe_mode_off': "Безпечний режим вимкнено. Може відображатися контент 18+.",
        'view_mode_set': "Режим перегляду встановлено на '{mode}'.",
        'select_view_mode': "Оберіть режим перегляду:",
        'detailed_view': "Детальний",
        'compact_view': "Компактний",
        'invite_code_generated': "Ваш код запрошення: `{code}`\nПоділіться ним: {link}",
        'invite_code_used': "Код запрошення `{code}` успішно активовано!",
        'invite_code_invalid': "Невірний або використаний код запрошення.",
        'ai_requests_limit_reached': "Ви досягли ліміту AI запитів на сьогодні ({limit}). Спробуйте завтра або оформіть преміум підписку.",
        'ask_expert': "🧑‍🔬 Запитати експерта",
        'my_subscriptions': "🔔 Мої підписки",
        'help_sell': "💰 Допоможи продати",
        'help_buy': "🛒 Допоможи купити",
    },
    'en': {
        'start_welcome': "Hello! I am your personal news bot. Choose what interests you:",
        'main_menu': "Main Menu:",
        'my_news': "My News 📰",
        'ai_media': "AI Media 🤖",
        'settings': "Settings ⚙️",
        'admin_panel': "Admin Panel 👑",
        'back': "⬅️ Back",
        'cancel': "❌ Cancel",
        'next_news': "➡️ Next",
        'prev_news': "⬅️ Previous",
        'no_more_news': "No more news available.",
        'source_added': "Source '{name}' successfully added.",
        'source_exists': "Source '{name}' already exists.",
        'invalid_url': "Please enter a valid URL.",
        'enter_source_url': "Please enter the URL of the new source:",
        'select_source_type': "Select source type:",
        'web_type': "🌐 Website",
        'rss_type': "📡 RSS Feed",
        'telegram_type': "✈️ Telegram Channel",
        'social_media_type': "📱 Social Media",
        'source_type_selected': "Source type '{source_type}' selected. Now enter the URL.",
        'source_deleted': "Source successfully deleted.",
        'source_not_found': "Source not found.",
        'enter_source_id_to_delete': "Enter the ID of the source you want to delete:",
        'error': "An error occurred: {e}",
        'ai_summary': "📝 AI Summary",
        'ai_topics': "🏷️ AI Topics",
        'ai_sentiment': "📊 AI Sentiment",
        'ai_fact_check': "✅ AI Fact Check (Premium)",
        'ai_audience_summary': "👥 AI Audience (Premium)",
        'ai_historical_analogues': "🕰️ AI Historical Analogues (Premium)",
        'ai_impact_analysis': "💥 AI Impact Analysis (Premium)",
        'ai_monetary_analysis': "💰 AI Monetary Analysis (Premium)",
        'premium_only': "This feature is for premium users only. Subscribe to get access!",
        'ai_price_analysis': "💲 AI Price Analysis",
        'enter_product_name': "Please enter the product name for price analysis:",
        'price_analysis_result': "Price analysis for '{product_name}':\n{analysis}",
        'no_price_analysis_data': "Could not find data for price analysis of '{product_name}'.",
        'ai_news_generation': "✍️ AI News Generation",
        'ai_news_generation_prompt': "Enter a topic for AI news generation:",
        'ai_news_generated': "Your AI news is ready!",
        'generating_ai_news': "Generating AI news, this may take some time...",
        'comments_btn': "💬 Comments",
        'like_btn': "👍",
        'dislike_btn': "👎",
        'bookmark_btn': "🔖",
        'no_news_found': "No news found for your criteria.",
        'select_language': "Select language:",
        'language_set': "Language set to English.",
        'notifications_on': "Automatic notifications enabled.",
        'notifications_off': "Automatic notifications disabled.",
        'digest_frequency_set': "Digest frequency set to {frequency}.",
        'select_digest_frequency': "Select digest frequency:",
        'daily': "Daily",
        'weekly': "Weekly",
        'monthly': "Monthly",
        'off': "Off",
        'safe_mode_on': "Safe mode enabled. 18+ content will be hidden.",
        'safe_mode_off': "Safe mode disabled. 18+ content may be displayed.",
        'view_mode_set': "View mode set to '{mode}'.",
        'select_view_mode': "Select view mode:",
        'detailed_view': "Detailed",
        'compact_view': "Compact",
        'invite_code_generated': "Your invite code: `{code}`\nShare it: {link}",
        'invite_code_used': "Invite code `{code}` successfully activated!",
        'invite_code_invalid': "Invalid or used invite code.",
        'ai_requests_limit_reached': "You have reached your daily AI request limit ({limit}). Try again tomorrow or subscribe to premium.",
        'ask_expert': "🧑‍🔬 Ask an Expert",
        'my_subscriptions': "🔔 My Subscriptions",
        'help_sell': "💰 Help Sell",
        'help_buy': "🛒 Help Buy",
    }
}

def get_message(lang: str, key: str, **kwargs) -> str:
    return MESSAGES.get(lang, MESSAGES['uk']).get(key, f"MISSING TRANSLATION: {key}").format(**kwargs)

class Form(StatesGroup):
    add_source_url = State()
    add_source_type = State()
    delete_source_id = State()
    ai_price_analysis_product = State()
    ai_news_generation_topic = State()
    ask_expert_query = State()
    help_sell_product = State()
    help_buy_product = State()

async def get_user(telegram_id: int) -> Optional[User]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
            user_data = await cur.fetchone()
            return User(**user_data) if user_data else None

async def create_user(telegram_id: int, username: Optional[str] = None, first_name: Optional[str] = None, last_name: Optional[str] = None, inviter_id: Optional[int] = None) -> User:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO users (telegram_id, username, first_name, last_name, inviter_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *;
                """,
                (telegram_id, username, first_name, last_name, inviter_id)
            )
            user_data = await cur.fetchone()
            return User(**user_data)

async def update_user(user_id: int, **kwargs) -> Optional[User]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            set_clauses = [f"{key} = %s" for key in kwargs]
            params = list(kwargs.values())
            params.append(user_id)
            await cur.execute(
                f"UPDATE users SET {', '.join(set_clauses)}, last_active = CURRENT_TIMESTAMP WHERE id = %s RETURNING *;",
                tuple(params)
            )
            user_data = await cur.fetchone()
            return User(**user_data) if user_data else None

async def update_user_ai_request_count(user_id: int) -> Optional[User]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT ai_requests_today, ai_last_request_date FROM users WHERE id = %s;", (user_id,))
            user_data = await cur.fetchone()
            
            current_date = date.today()
            requests_today = user_data['ai_requests_today'] if user_data else 0
            last_request_date = user_data['ai_last_request_date'] if user_data else None

            if last_request_date != current_date:
                requests_today = 1
            else:
                requests_today += 1

            await cur.execute(
                """
                UPDATE users SET ai_requests_today = %s, ai_last_request_date = %s, last_active = CURRENT_TIMESTAMP
                WHERE id = %s RETURNING *;
                """,
                (requests_today, current_date, user_id)
            )
            user_data = await cur.fetchone()
            return User(**user_data) if user_data else None

async def check_ai_request_limit(user_id: int) -> bool:
    user = await get_user_by_id(user_id)
    if not user:
        return False
    if user.is_premium or user.is_pro:
        return True
    
    current_date = date.today()
    if user.ai_last_request_date != current_date:
        await update_user(user.id, ai_requests_today=0, ai_last_request_date=current_date)
        return True
    
    return user.ai_requests_today < MAX_AI_REQUESTS_PER_DAY

async def get_user_by_id(user_id: int) -> Optional[User]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            user_data = await cur.fetchone()
            return User(**user_data) if user_data else None

async def get_source_by_url(url: str) -> Optional[Source]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM sources WHERE url = %s", (url,))
            source_data = await cur.fetchone()
            return Source(**source_data) if source_data else None

async def add_source(user_id: Optional[int], name: str, url: str, source_type: str) -> Source:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO sources (user_id, name, url, type)
                VALUES (%s, %s, %s, %s)
                RETURNING *;
                """,
                (user_id, name, url, source_type)
            )
            source_data = await cur.fetchone()
            return Source(**source_data)

async def get_all_sources() -> List[Source]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM sources WHERE is_active = TRUE;")
            sources_data = await cur.fetchall()
            return [Source(**s) for s in sources_data]

async def delete_source_by_id(source_id: int) -> bool:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("DELETE FROM sources WHERE id = %s", (source_id,))
            return cur.rowcount > 0

async def add_news(news_item: News) -> News:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO news (source_id, title, content, source_url, image_url, published_at, ai_summary, ai_classified_topics, moderation_status, expires_at, is_published_to_channel)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *;
                """,
                (
                    news_item.source_id, news_item.title, news_item.content,
                    str(news_item.source_url), str(news_item.image_url) if news_item.image_url else None,
                    news_item.published_at, news_item.ai_summary,
                    json.dumps(news_item.ai_classified_topics) if news_item.ai_classified_topics else '[]',
                    news_item.moderation_status, news_item.expires_at, news_item.is_published_to_channel
                )
            )
            news_data = await cur.fetchone()
            return News(**news_data)

async def is_news_already_exists(source_url: str, title: str) -> bool:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id FROM news WHERE source_url = %s AND title = %s;", (source_url, title))
            return await cur.fetchone() is not None

async def get_news_by_id(news_id: int) -> Optional[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM news WHERE id = %s", (news_id,))
            news_data = await cur.fetchone()
            if news_data:
                if news_data['ai_classified_topics']:
                    news_data['ai_classified_topics'] = json.loads(news_data['ai_classified_topics'])
                return News(**news_data)
            return None

async def get_news_for_user(user_id: int, offset: int = 0, limit: int = 1) -> List[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            user = await get_user_by_id(user_id)
            if not user:
                return []

            await cur.execute("SELECT topic FROM user_subscriptions WHERE user_id = %s;", (user_id,))
            subscribed_topics = [row['topic'] for row in await cur.fetchall()]

            query = """
            SELECT n.*, s.name as source_name
            FROM news n
            JOIN sources s ON n.source_id = s.id
            WHERE n.moderation_status = 'approved'
            AND n.published_at >= NOW() - INTERVAL '1 day'
            """
            params = []

            if subscribed_topics:
                topic_conditions = [f"n.ai_classified_topics @> '[\"{topic}\"]'" for topic in subscribed_topics]
                query += f" AND ({' OR '.join(topic_conditions)})"

            query += " ORDER BY n.published_at DESC OFFSET %s LIMIT %s;"
            params.extend([offset, limit])

            await cur.execute(query, tuple(params))
            news_data = await cur.fetchall()
            
            result = []
            for n_data in news_data:
                if n_data['ai_classified_topics']:
                    n_data['ai_classified_topics'] = json.loads(n_data['ai_classified_topics'])
                result.append(News(**n_data))
            return result

async def get_all_news_ids_for_user(user_id: int) -> List[int]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            user = await get_user_by_id(user_id)
            if not user:
                return []

            await cur.execute("SELECT topic FROM user_subscriptions WHERE user_id = %s;", (user_id,))
            subscribed_topics = [row['topic'] for row in await cur.fetchall()]

            query = """
            SELECT n.id
            FROM news n
            JOIN sources s ON n.source_id = s.id
            WHERE n.moderation_status = 'approved'
            AND n.published_at >= NOW() - INTERVAL '1 day'
            """
            params = []

            if subscribed_topics:
                topic_conditions = [f"n.ai_classified_topics @> '[\"{topic}\"]'" for topic in subscribed_topics]
                query += f" AND ({' OR '.join(topic_conditions)})"

            query += " ORDER BY n.published_at DESC;"

            await cur.execute(query, tuple(params))
            news_ids = [row['id'] for row in await cur.fetchall()]
            return news_ids

async def update_source_last_parsed(source_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("UPDATE sources SET last_parsed = CURRENT_TIMESTAMP WHERE id = %s", (source_id,))

async def get_news_counts_by_status() -> Dict[str, int]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT moderation_status, COUNT(*) FROM news GROUP BY moderation_status;")
            counts = await cur.fetchall()
            return {row['moderation_status']: row['count'] for row in counts}

async def update_news_moderation_status(news_id: int, status: str) -> Optional[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE news SET moderation_status = %s WHERE id = %s RETURNING *;",
                (status, news_id)
            )
            news_data = await cur.fetchone()
            if news_data:
                if news_data['ai_classified_topics']:
                    news_data['ai_classified_topics'] = json.loads(news_data['ai_classified_topics'])
                return News(**news_data)
            return None

async def delete_expired_news():
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("DELETE FROM news WHERE expires_at IS NOT NULL AND expires_at < CURRENT_TIMESTAMP;")
            logger.info(f"Deleted {cur.rowcount} expired news items.")

async def add_reaction(news_id: int, user_id: int, reaction_type: str) -> Reaction:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM reactions WHERE news_id = %s AND user_id = %s;", (news_id, user_id))
            existing_reaction = await cur.fetchone()

            if existing_reaction:
                if existing_reaction['type'] == reaction_type:
                    await cur.execute("DELETE FROM reactions WHERE news_id = %s AND user_id = %s;", (news_id, user_id))
                    logger.info(f"Reaction '{reaction_type}' removed for news {news_id} by user {user_id}")
                    return None
                else:
                    await cur.execute(
                        "UPDATE reactions SET type = %s, created_at = CURRENT_TIMESTAMP WHERE news_id = %s AND user_id = %s RETURNING *;",
                        (reaction_type, news_id, user_id)
                    )
                    reaction_data = await cur.fetchone()
                    logger.info(f"Reaction updated to '{reaction_type}' for news {news_id} by user {user_id}")
                    return Reaction(**reaction_data)
            else:
                await cur.execute(
                    """
                    INSERT INTO reactions (news_id, user_id, type)
                    VALUES (%s, %s, %s)
                    RETURNING *;
                    """,
                    (news_id, user_id, reaction_type)
                )
                reaction_data = await cur.fetchone()
                logger.info(f"Reaction '{reaction_type}' added for news {news_id} by user {user_id}")
                return Reaction(**reaction_data)

async def get_reaction(news_id: int, user_id: int) -> Optional[Reaction]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM reactions WHERE news_id = %s AND user_id = %s;", (news_id, user_id))
            reaction_data = await cur.fetchone()
            return Reaction(**reaction_data) if reaction_data else None

async def add_user_subscription(user_id: int, topic: str):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO user_subscriptions (user_id, topic) VALUES (%s, %s) ON CONFLICT (user_id, topic) DO NOTHING;",
                (user_id, topic)
            )

async def remove_user_subscription(user_id: int, topic: str):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "DELETE FROM user_subscriptions WHERE user_id = %s AND topic = %s;",
                (user_id, topic)
            )

async def get_user_subscriptions(user_id: int) -> List[str]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT topic FROM user_subscriptions WHERE user_id = %s;", (user_id,))
            return [row['topic'] for row in await cur.fetchall()]

async def create_invite_code(inviter_user_id: Optional[int] = None) -> Invitation:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            while True:
                code = ''.join(random.choices('0123456789ABCDEF', k=8))
                try:
                    await cur.execute(
                        "INSERT INTO invitations (inviter_user_id, invite_code) VALUES (%s, %s) RETURNING *;",
                        (inviter_user_id, code)
                    )
                    invite_data = await cur.fetchone()
                    return Invitation(**invite_data)
                except psycopg.IntegrityError:
                    continue

async def get_invite_code(code: str) -> Optional[Invitation]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM invitations WHERE invite_code = %s AND status = 'pending';", (code,))
            invite_data = await cur.fetchone()
            return Invitation(**invite_data) if invite_data else None

async def use_invite_code(invite_id: int, new_user_telegram_id: int) -> bool:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE invitations SET used_at = CURRENT_TIMESTAMP, status = 'accepted', invitee_telegram_id = %s WHERE id = %s;
                """,
                (new_user_telegram_id, invite_id)
            )
            return cur.rowcount > 0

async def call_gemini_api(prompt: str, user_telegram_id: Optional[int] = None) -> Optional[str]:
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY is not set.")
        return "API key for AI not configured."

    if user_telegram_id:
        user = await get_user(user_telegram_id)
        if user and not await check_ai_request_limit(user.id):
            return get_message(user.language, 'ai_requests_limit_reached', limit=MAX_AI_REQUESTS_PER_DAY)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "topK": 40,
            "topP": 0.95,
            "maxOutputTokens": 1024,
        }
    }

    async with ClientSession() as session:
        try:
            async with session.post(url, headers=headers, json=payload) as response:
                response.raise_for_status()
                result = await response.json()
                
                if result.get("candidates") and result["candidates"][0].get("content") and result["candidates"][0]["content"].get("parts"):
                    if user_telegram_id:
                        user = await get_user(user_telegram_id)
                        if user:
                            await update_user_ai_request_count(user.id)
                    return result["candidates"][0]["content"]["parts"][0]["text"]
                else:
                    logger.warning(f"Unexpected Gemini API response structure: {result}")
                    return "Не вдалося отримати відповідь від AI."
        except Exception as e:
            logger.error(f"Error calling Gemini API: {e}")
            return f"Помилка при зверненні до AI: {e}"

def get_main_menu_keyboard(lang: str, is_admin: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(lang, 'my_news'), callback_data="my_news")
    builder.button(text=get_message(lang, 'ai_media'), callback_data="ai_media")
    builder.button(text=get_message(lang, 'settings'), callback_data="settings")
    if is_admin:
        builder.button(text=get_message(lang, 'admin_panel'), callback_data="admin_panel")
    builder.adjust(2)
    return builder.as_markup()

def get_admin_panel_keyboard(lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Додати джерело", callback_data="admin_add_source")
    builder.button(text="🗑️ Видалити джерело", callback_data="admin_delete_source")
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="📰 Усі новини", callback_data="admin_all_news")
    builder.button(text=get_message(lang, 'back'), callback_data="main_menu")
    builder.adjust(2)
    return builder.as_markup()

def get_source_type_keyboard(lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(lang, 'web_type'), callback_data="source_type_web")
    builder.button(text=get_message(lang, 'rss_type'), callback_data="source_type_rss")
    builder.button(text=get_message(lang, 'telegram_type'), callback_data="source_type_telegram")
    builder.button(text=get_message(lang, 'social_media_type'), callback_data="source_type_social_media")
    builder.button(text=get_message(lang, 'back'), callback_data="admin_panel")
    builder.adjust(2)
    return builder.as_markup()

def get_news_navigation_keyboard(lang: str, current_index: int, total_news: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if current_index > 0:
        builder.button(text=get_message(lang, 'prev_news'), callback_data=f"news_nav_prev_{current_index - 1}")
    if current_index < total_news - 1:
        builder.button(text=get_message(lang, 'next_news'), callback_data=f"news_nav_next_{current_index + 1}")
    builder.button(text=get_message(lang, 'back'), callback_data="main_menu")
    builder.adjust(2)
    return builder.as_markup()

def get_news_reactions_keyboard(news_id: int, lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(lang, 'like_btn'), callback_data=f"react_like_{news_id}")
    builder.button(text=get_message(lang, 'dislike_btn'), callback_data=f"react_dislike_{news_id}")
    builder.button(text=get_message(lang, 'bookmark_btn'), callback_data=f"react_bookmark_{news_id}")
    builder.adjust(3)
    return builder.as_markup()

def get_ai_news_functions_keyboard(lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(lang, 'ai_summary'), callback_data="ai_summary")
    builder.button(text=get_message(lang, 'ai_topics'), callback_data="ai_topics")
    builder.button(text=get_message(lang, 'ai_sentiment'), callback_data="ai_sentiment")
    builder.button(text=get_message(lang, 'ai_price_analysis'), callback_data="ai_price_analysis")
    builder.button(text=get_message(lang, 'ai_news_generation'), callback_data="ai_news_generation")
    builder.button(text=get_message(lang, 'ask_expert'), callback_data="ask_expert")
    builder.button(text=get_message(lang, 'my_subscriptions'), callback_data="my_subscriptions")
    builder.button(text=get_message(lang, 'help_sell'), callback_data="help_sell")
    builder.button(text=get_message(lang, 'help_buy'), callback_data="help_buy")
    builder.button(text=get_message(lang, 'comments_btn'), callback_data="comments")
    builder.button(text=get_message(lang, 'back'), callback_data="main_menu")
    builder.adjust(2)
    return builder.as_markup()

def get_settings_keyboard(lang: str, user: User) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"🌐 {get_message(lang, 'select_language')}", callback_data="settings_language")
    notif_status = get_message(lang, 'notifications_on') if user.auto_notifications else get_message(lang, 'notifications_off')
    builder.button(text=f"🔔 {notif_status}", callback_data="settings_notifications")
    digest_freq = get_message(lang, user.digest_frequency)
    builder.button(text=f"⏰ Дайджест: {digest_freq}", callback_data="settings_digest_frequency")
    safe_mode_status = get_message(lang, 'safe_mode_on') if user.safe_mode else get_message(lang, 'safe_mode_off')
    builder.button(text=f"🔞 {safe_mode_status}", callback_data="settings_safe_mode")
    view_mode_text = get_message(lang, 'detailed_view') if user.view_mode == 'detailed' else get_message(lang, 'compact_view')
    builder.button(text=f"👁️ Режим перегляду: {view_mode_text}", callback_data="settings_view_mode")
    builder.button(text="🔗 Запросити друзів", callback_data="invite_friends")
    builder.button(text=get_message(lang, 'back'), callback_data="main_menu")
    builder.adjust(2)
    return builder.as_markup()

def get_language_keyboard(lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🇺🇦 Українська", callback_data="set_lang_uk")
    builder.button(text="🇬🇧 English", callback_data="set_lang_en")
    builder.button(text=get_message(lang, 'back'), callback_data="settings")
    builder.adjust(2)
    return builder.as_markup()

def get_digest_frequency_keyboard(lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(lang, 'daily'), callback_data="set_digest_daily")
    builder.button(text=get_message(lang, 'weekly'), callback_data="set_digest_weekly")
    builder.button(text=get_message(lang, 'monthly'), callback_data="set_digest_monthly")
    builder.button(text=get_message(lang, 'off'), callback_data="set_digest_off")
    builder.button(text=get_message(lang, 'back'), callback_data="settings")
    builder.adjust(2)
    return builder.as_markup()

def get_view_mode_keyboard(lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=get_message(lang, 'detailed_view'), callback_data="set_view_detailed")
    builder.button(text=get_message(lang, 'compact_view'), callback_data="set_view_compact")
    builder.button(text=get_message(lang, 'back'), callback_data="settings")
    builder.adjust(2)
    return builder.as_markup()

@dp.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext) -> None:
    user_telegram_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name
    
    user = await get_user(user_telegram_id)
    
    invite_code = None
    if message.text and len(message.text.split()) > 1:
        start_payload = message.text.split(maxsplit=1)[1]
        invite_code = start_payload

    if not user:
        inviter_user_id = None
        if invite_code:
            invite = await get_invite_code(invite_code)
            if invite:
                inviter_user_id = invite.inviter_user_id
                
        user = await create_user(user_telegram_id, username, first_name, last_name, inviter_user_id)
        
        if invite_code and invite:
            await use_invite_code(invite.id, user_telegram_id)
            await message.answer(get_message(user.language, 'invite_code_used', code=invite_code))
            if inviter_user_id:
                inviter = await get_user_by_id(inviter_user_id)
                if inviter:
                    try:
                        await bot.send_message(inviter.telegram_id, f"🎉 Ваш друг {user.first_name or user.username} використав ваш код запрошення!")
                    except Exception as e:
                        logger.error(f"Could not notify inviter {inviter.telegram_id}: {e}")

    await state.clear()
    await message.answer(
        get_message(user.language, 'start_welcome'),
        reply_markup=get_main_menu_keyboard(user.language, user.is_admin)
    )

@dp.callback_query(F.data == "main_menu")
async def show_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    user = await get_user(callback.from_user.id)
    await callback.message.edit_text(
        get_message(user.language, 'main_menu'),
        reply_markup=get_main_menu_keyboard(user.language, user.is_admin)
    )
    await callback.answer()

@dp.callback_query(F.data == "my_news")
async def handle_my_news_command(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    all_news_ids = await get_all_news_ids_for_user(user.id)
    
    if not all_news_ids:
        await callback.message.edit_text(get_message(user.language, 'no_news_found'))
        await callback.message.edit_reply_markup(reply_markup=get_main_menu_keyboard(user.language, user.is_admin))
        await callback.answer()
        return

    await state.update_data(all_news_ids=all_news_ids, current_news_index=0)
    
    await send_news_to_user(callback.message.chat.id, all_news_ids[0], 0, len(all_news_ids), state)
    await callback.answer()

async def send_news_to_user(chat_id: int, news_id: int, current_index: int, total_news: int, state: FSMContext):
    user = await get_user(chat_id)
    news_item = await get_news_by_id(news_id)
    
    if not news_item:
        await bot.send_message(chat_id, get_message(user.language, 'no_more_news'))
        return

    source = await get_source_by_url(str(news_item.source_url))
    source_name = source.name if source else "Невідоме джерело"

    text = f"<b>{news_item.title}</b>\n\n"
    if user.view_mode == 'detailed':
        text += f"{news_item.content}\n\n"
    elif news_item.ai_summary:
        text += f"{news_item.ai_summary}\n\n"
    
    text += f"Джерело: {hlink(source_name, str(news_item.source_url))}\n"
    text += f"Опубліковано: {news_item.published_at.strftime('%Y-%m-%d %H:%M')}\n"

    keyboard_builder = InlineKeyboardBuilder()
    
    nav_keyboard = get_news_navigation_keyboard(user.language, current_index, total_news)
    for row in nav_keyboard.inline_keyboard:
        keyboard_builder.row(*row)

    reaction_keyboard = get_news_reactions_keyboard(news_item.id, user.language)
    for row in reaction_keyboard.inline_keyboard:
        keyboard_builder.row(*row)

    ai_functions_keyboard = get_ai_news_functions_keyboard(user.language)
    for button_row in ai_functions_keyboard.inline_keyboard:
        if len(button_row) == 1 and button_row[0].callback_data == "main_menu":
            continue
        keyboard_builder.row(*button_row)

    try:
        if news_item.image_url:
            try:
                await bot.send_photo(chat_id=chat_id, photo=str(news_item.image_url), caption=text, parse_mode=ParseMode.HTML, reply_markup=keyboard_builder.as_markup())
            except Exception as e:
                logger.error(f"Помилка відправки фото для новини '{news_item.title}': {e}. Спроба відправити як текст.")
                text_with_image_link = text
                if news_item.image_url:
                    text_with_image_link += f"\n\n{hlink('Зображення', str(news_item.image_url))}"
                await bot.send_message(chat_id=chat_id, text=text_with_image_link, parse_mode=ParseMode.HTML, reply_markup=keyboard_builder.as_markup(), disable_web_page_preview=False)
        else:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard_builder.as_markup(), disable_web_page_preview=False)
    except Exception as e:
        logger.error(f"Помилка публікації '{news_item.title}': {e}")
        await bot.send_message(chat_id=chat_id, text=f"Не вдалося відобразити новину: {news_item.title}. {e}", reply_markup=keyboard_builder.as_markup())

@dp.callback_query(F.data.startswith("news_nav_"))
async def handle_news_navigation(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    all_news_ids = data.get("all_news_ids")
    current_index = int(callback.data.split('_')[-1])

    if not all_news_ids or current_index < 0 or current_index >= len(all_news_ids):
        user = await get_user(callback.from_user.id)
        await callback.message.edit_text(get_message(user.language, 'no_more_news'))
        await callback.answer()
        return

    await state.update_data(current_news_index=current_index)
    
    try:
        await callback.message.delete()
    except Exception as e:
        logger.warning(f"Could not delete previous message: {e}")

    await send_news_to_user(callback.message.chat.id, all_news_ids[current_index], current_index, len(all_news_ids), state)
    await callback.answer()

@dp.callback_query(F.data.startswith("react_"))
async def handle_reaction(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    parts = callback.data.split('_')
    reaction_type = parts[1]
    news_id = int(parts[2])

    reaction_result = await add_reaction(news_id, user.id, reaction_type)
    
    if reaction_result:
        await callback.answer(f"Ви поставили {reaction_type} на новину {news_id}!")
    else:
        await callback.answer(f"Ви прибрали {reaction_type} з новини {news_id}!")

@dp.callback_query(F.data == "ai_media")
async def handle_ai_media_command(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    await callback.message.edit_text(
        get_message(user.language, 'ai_media'),
        reply_markup=get_ai_news_functions_keyboard(user.language)
    )
    await callback.answer()

@dp.callback_query(F.data == "ai_summary")
async def handle_ai_summary(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    data = await state.get_data()
    current_news_index = data.get("current_news_index")
    all_news_ids = data.get("all_news_ids")

    if all_news_ids is None or current_news_index is None:
        await callback.answer("Будь ласка, спочатку оберіть новину.", show_alert=True)
        return

    news_id = all_news_ids[current_news_index]
    news_item = await get_news_by_id(news_id)

    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return

    if not await check_ai_request_limit(user.id):
        await callback.answer(get_message(user.language, 'ai_requests_limit_reached', limit=MAX_AI_REQUESTS_PER_DAY), show_alert=True)
        return

    await callback.message.edit_text("Генерую резюме...")
    prompt = f"Зроби коротке резюме цієї новини (до 250 символів) українською мовою: {news_item.content}"
    summary = await call_gemini_api(prompt, user.telegram_id)
    
    if summary:
        text = f"<b>AI Резюме:</b>\n{summary}\n\n{hlink('Читати повну новину', str(news_item.source_url))}"
        await callback.message.edit_text(text, reply_markup=get_ai_news_functions_keyboard(user.language))
    else:
        await callback.message.edit_text(get_message(user.language, 'error', e="Не вдалося згенерувати резюме."), reply_markup=get_ai_news_functions_keyboard(user.language))
    await callback.answer()

@dp.callback_query(F.data == "ai_topics")
async def handle_ai_topics(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    data = await state.get_data()
    current_news_index = data.get("current_news_index")
    all_news_ids = data.get("all_news_ids")

    if all_news_ids is None or current_news_index is None:
        await callback.answer("Будь ласка, спочатку оберіть новину.", show_alert=True)
        return

    news_id = all_news_ids[current_news_index]
    news_item = await get_news_by_id(news_id)

    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return

    if not await check_ai_request_limit(user.id):
        await callback.answer(get_message(user.language, 'ai_requests_limit_reached', limit=MAX_AI_REQUESTS_PER_DAY), show_alert=True)
        return

    await callback.message.edit_text("Аналізую теми...")
    prompt = f"Визнач 3-5 ключових тем цієї новини у вигляді списку, українською мовою: {news_item.content}"
    topics_str = await call_gemini_api(prompt, user.telegram_id)
    
    if topics_str:
        text = f"<b>AI Теми:</b>\n{topics_str}\n\n{hlink('Читати повну новину', str(news_item.source_url))}"
        await callback.message.edit_text(text, reply_markup=get_ai_news_functions_keyboard(user.language))
    else:
        await callback.message.edit_text(get_message(user.language, 'error', e="Не вдалося визначити теми."), reply_markup=get_ai_news_functions_keyboard(user.language))
    await callback.answer()

@dp.callback_query(F.data == "ai_sentiment")
async def handle_ai_sentiment(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    data = await state.get_data()
    current_news_index = data.get("current_news_index")
    all_news_ids = data.get("all_news_ids")

    if all_news_ids is None or current_news_index is None:
        await callback.answer("Будь ласка, спочатку оберіть новину.", show_alert=True)
        return

    news_id = all_news_ids[current_news_index]
    news_item = await get_news_by_id(news_id)

    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return

    if not await check_ai_request_limit(user.id):
        await callback.answer(get_message(user.language, 'ai_requests_limit_reached', limit=MAX_AI_REQUESTS_PER_DAY), show_alert=True)
        return

    await callback.message.edit_text("Аналізую настрій...")
    prompt = f"Визнач загальний настрій (позитивний, негативний, нейтральний) цієї новини та коротко обґрунтуй, українською мовою: {news_item.content}"
    sentiment = await call_gemini_api(prompt, user.telegram_id)
    
    if sentiment:
        text = f"<b>AI Настрій:</b>\n{sentiment}\n\n{hlink('Читати повну новину', str(news_item.source_url))}"
        await callback.message.edit_text(text, reply_markup=get_ai_news_functions_keyboard(user.language))
    else:
        await callback.message.edit_text(get_message(user.language, 'error', e="Не вдалося визначити настрій."), reply_markup=get_ai_news_functions_keyboard(user.language))
    await callback.answer()

@dp.callback_query(F.data == "ai_price_analysis")
async def handle_ai_price_analysis_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not await check_ai_request_limit(user.id):
        await callback.answer(get_message(user.language, 'ai_requests_limit_reached', limit=MAX_AI_REQUESTS_PER_DAY), show_alert=True)
        return

    await state.set_state(Form.ai_price_analysis_product)
    await callback.message.edit_text(get_message(user.language, 'enter_product_name'), reply_markup=InlineKeyboardBuilder().button(text=get_message(user.language, 'back'), callback_data="ai_media").as_markup())
    await callback.answer()

@dp.message(Form.ai_price_analysis_product)
async def process_price_analysis_input(message: Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    product_name = message.text
    if not product_name:
        await message.answer(get_message(user.language, 'enter_product_name'))
        return

    await message.answer(get_message(user.language, 'generating_ai_news'))
    
    search_query = f"ціна {product_name} Україна"
    try:
        search_results_raw = await asyncio.to_thread(web_parser.parse_website, search_query)
        
        snippets = [res.get('content', '') for res in [search_results_raw] if res and res.get('content')]
        
        if not snippets:
            await message.answer(get_message(user.language, 'no_price_analysis_data', product_name=product_name), reply_markup=get_ai_news_functions_keyboard(user.language))
            await state.clear()
            return

        context_for_ai = "\n".join(snippets[:5])
        prompt = f"Проаналізуй ціни на '{product_name}' на основі наступних даних з інтернету і зроби висновок, українською мовою:\n\n{context_for_ai}"
        
        analysis = await call_gemini_api(prompt, user.telegram_id)
        
        if analysis:
            await message.answer(get_message(user.language, 'price_analysis_result', product_name=product_name, analysis=analysis), reply_markup=get_ai_news_functions_keyboard(user.language))
        else:
            await message.answer(get_message(user.language, 'error', e="Не вдалося згенерувати аналіз цін."), reply_markup=get_ai_news_functions_keyboard(user.language))
    except Exception as e:
        logger.error(f"Error during price analysis for {product_name}: {e}")
        await message.answer(get_message(user.language, 'error', e=f"Помилка при аналізі цін: {e}"), reply_markup=get_ai_news_functions_keyboard(user.language))
    
    await state.clear()

@dp.callback_query(F.data == "ai_news_generation")
async def handle_ai_news_generation_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not await check_ai_request_limit(user.id):
        await callback.answer(get_message(user.language, 'ai_requests_limit_reached', limit=MAX_AI_REQUESTS_PER_DAY), show_alert=True)
        return

    await state.set_state(Form.ai_news_generation_topic)
    await callback.message.edit_text(get_message(user.language, 'ai_news_generation_prompt'), reply_markup=InlineKeyboardBuilder().button(text=get_message(user.language, 'back'), callback_data="ai_media").as_markup())
    await callback.answer()

@dp.message(Form.ai_news_generation_topic)
async def process_ai_news_generation_input(message: Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    topic = message.text
    if not topic:
        await message.answer(get_message(user.language, 'ai_news_generation_prompt'))
        return

    await message.answer(get_message(user.language, 'generating_ai_news'))
    
    prompt = f"Напиши коротку новину (200-300 слів) на тему '{topic}' українською мовою. Додай заголовок."
    generated_news = await call_gemini_api(prompt, user.telegram_id)
    
    if generated_news:
        await message.answer(get_message(user.language, 'ai_news_generated'))
        await message.answer(generated_news, reply_markup=get_ai_news_functions_keyboard(user.language))
    else:
        await message.answer(get_message(user.language, 'error', e="Не вдалося згенерувати новину."), reply_markup=get_ai_news_functions_keyboard(user.language))
    
    await state.clear()

@dp.callback_query(F.data == "ask_expert")
async def handle_ask_expert(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not await check_ai_request_limit(user.id):
        await callback.answer(get_message(user.language, 'ai_requests_limit_reached', limit=MAX_AI_REQUESTS_PER_DAY), show_alert=True)
        return

    await state.set_state(Form.ask_expert_query)
    await callback.message.edit_text("Введіть ваше питання до експерта:", reply_markup=InlineKeyboardBuilder().button(text=get_message(user.language, 'back'), callback_data="ai_media").as_markup())
    await callback.answer()

@dp.message(Form.ask_expert_query)
async def process_ask_expert_query(message: Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    query_text = message.text
    if not query_text:
        await message.answer("Будь ласка, введіть питання.")
        return

    await message.answer("Запитую експерта, це може зайняти деякий час...")
    prompt = f"Виступіть у ролі експерта з новин та аналітики. Дайте розгорнуту відповідь на питання: '{query_text}'"
    response = await call_gemini_api(prompt, user.telegram_id)

    if response:
        await message.answer(f"<b>Відповідь експерта:</b>\n{response}", reply_markup=get_ai_news_functions_keyboard(user.language))
    else:
        await message.answer(get_message(user.language, 'error', e="Не вдалося отримати відповідь від експерта."), reply_markup=get_ai_news_functions_keyboard(user.language))
    await state.clear()

@dp.callback_query(F.data == "my_subscriptions")
async def handle_my_subscriptions(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    subscriptions = await get_user_subscriptions(user.id)
    
    if subscriptions:
        subs_text = "\n".join([f"- {topic}" for topic in subscriptions])
        await callback.message.edit_text(f"Ваші підписки на теми:\n{subs_text}", reply_markup=get_ai_news_functions_keyboard(user.language))
    else:
        await callback.message.edit_text("У вас поки немає підписок на теми.", reply_markup=get_ai_news_functions_keyboard(user.language))
    
    await callback.answer()

@dp.callback_query(F.data == "help_sell")
async def handle_help_sell_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not await check_ai_request_limit(user.id):
        await callback.answer(get_message(user.language, 'ai_requests_limit_reached', limit=MAX_AI_REQUESTS_PER_DAY), show_alert=True)
        return

    await state.set_state(Form.help_sell_product)
    await callback.message.edit_text("Опишіть товар або послугу, яку ви хочете продати:", reply_markup=InlineKeyboardBuilder().button(text=get_message(user.language, 'back'), callback_data="ai_media").as_markup())
    await callback.answer()

@dp.message(Form.help_sell_product)
async def process_help_sell_product(message: Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    product_description = message.text
    if not product_description:
        await message.answer("Будь ласка, опишіть товар.")
        return

    await message.answer("Генерую поради для продажу...")
    prompt = f"Виступіть у ролі маркетолога. Дайте поради, як найкраще продати наступний товар/послугу: '{product_description}'. Включіть цільову аудиторію, ключові переваги та канали продажу."
    response = await call_gemini_api(prompt, user.telegram_id)

    if response:
        await message.answer(f"<b>Поради для продажу:</b>\n{response}", reply_markup=get_ai_news_functions_keyboard(user.language))
    else:
        await message.answer(get_message(user.language, 'error', e="Не вдалося згенерувати поради для продажу."), reply_markup=get_ai_news_functions_keyboard(user.language))
    await state.clear()

@dp.callback_query(F.data == "help_buy")
async def handle_help_buy_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not await check_ai_request_limit(user.id):
        await callback.answer(get_message(user.language, 'ai_requests_limit_reached', limit=MAX_AI_REQUESTS_PER_DAY), show_alert=True)
        return

    await state.set_state(Form.help_buy_product)
    await callback.message.edit_text("Опишіть товар або послугу, яку ви хочете купити, та ваші критерії вибору:", reply_markup=InlineKeyboardBuilder().button(text=get_message(user.language, 'back'), callback_data="ai_media").as_markup())
    await callback.answer()

@dp.message(Form.help_buy_product)
async def process_help_buy_product(message: Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    product_criteria = message.text
    if not product_criteria:
        await message.answer("Будь ласка, опишіть товар та критерії.")
        return

    await message.answer("Генерую поради для покупки...")
    prompt = f"Виступіть у ролі консультанта з покупок. Дайте поради, як найкраще купити товар/послугу, виходячи з наступних критеріїв: '{product_criteria}'. Включіть рекомендації щодо вибору, на що звернути увагу та де шукати."
    response = await call_gemini_api(prompt, user.telegram_id)

    if response:
        await message.answer(f"<b>Поради для покупки:</b>\n{response}", reply_markup=get_ai_news_functions_keyboard(user.language))
    else:
        await message.answer(get_message(user.language, 'error', e="Не вдалося згенерувати поради для покупки."), reply_markup=get_ai_news_functions_keyboard(user.language))
    await state.clear()

@dp.callback_query(F.data == "settings")
async def handle_settings_command(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    await callback.message.edit_text(
        get_message(user.language, 'settings'),
        reply_markup=get_settings_keyboard(user.language, user)
    )
    await callback.answer()

@dp.callback_query(F.data == "settings_language")
async def handle_settings_language(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    await callback.message.edit_text(
        get_message(user.language, 'select_language'),
        reply_markup=get_language_keyboard(user.language) # Pass user.language here
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("set_lang_"))
async def set_language(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    new_lang = callback.data.split('_')[2]
    await update_user(user.id, language=new_lang)
    user.language = new_lang
    await callback.message.edit_text(get_message(new_lang, 'language_set'))
    await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(new_lang, user))
    await callback.answer()

@dp.callback_query(F.data == "settings_notifications")
async def toggle_notifications(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    new_status = not user.auto_notifications
    await update_user(user.id, auto_notifications=new_status)
    user.auto_notifications = new_status
    message_key = 'notifications_on' if new_status else 'notifications_off'
    await callback.message.edit_text(get_message(user.language, message_key))
    await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(user.language, user))
    await callback.answer()

@dp.callback_query(F.data == "settings_digest_frequency")
async def handle_settings_digest_frequency(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    await callback.message.edit_text(
        get_message(user.language, 'select_digest_frequency'),
        reply_markup=get_digest_frequency_keyboard(user.language)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("set_digest_"))
async def set_digest_frequency(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    new_frequency = callback.data.split('_')[2]
    await update_user(user.id, digest_frequency=new_frequency)
    user.digest_frequency = new_frequency
    await callback.message.edit_text(get_message(user.language, 'digest_frequency_set', frequency=get_message(user.language, new_frequency)))
    await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(user.language, user))
    await callback.answer()

@dp.callback_query(F.data == "settings_safe_mode")
async def toggle_safe_mode(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    new_status = not user.safe_mode
    await update_user(user.id, safe_mode=new_status)
    user.safe_mode = new_status
    message_key = 'safe_mode_on' if new_status else 'safe_mode_off'
    await callback.message.edit_text(get_message(user.language, message_key))
    await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(user.language, user))
    await callback.answer()

@dp.callback_query(F.data == "settings_view_mode")
async def handle_settings_view_mode(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    await callback.message.edit_text(
        get_message(user.language, 'select_view_mode'),
        reply_markup=get_view_mode_keyboard(user.language)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("set_view_"))
async def set_view_mode(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    new_mode = callback.data.split('_')[2]
    await update_user(user.id, view_mode=new_mode)
    user.view_mode = new_mode
    await callback.message.edit_text(get_message(user.language, 'view_mode_set', mode=get_message(user.language, f'{new_mode}_view')))
    await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(user.language, user))
    await callback.answer()

@dp.callback_query(F.data == "invite_friends")
async def command_invite_handler(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    invite = await create_invite_code(user.id)
    
    global BOT_USERNAME
    if not BOT_USERNAME:
        bot_info = await bot.get_me()
        BOT_USERNAME = bot_info.username

    invite_link = f"https://t.me/{BOT_USERNAME}?start={invite.invite_code}"
    await callback.message.answer(get_message(user.language, 'invite_code_generated', code=invite.invite_code, link=invite_link), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

@dp.callback_query(F.data == "admin_panel")
async def handle_admin_panel(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("У вас немає доступу до адмін-панелі.", show_alert=True)
        return
    await callback.message.edit_text(
        "Адмін-панель:",
        reply_markup=get_admin_panel_keyboard(user.language)
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_add_source")
async def handle_add_source_command(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("У вас немає доступу до цієї функції.", show_alert=True)
        return
    await state.set_state(Form.add_source_url)
    await callback.message.edit_text(get_message(user.language, 'enter_source_url'), reply_markup=InlineKeyboardBuilder().button(text=get_message(user.language, 'back'), callback_data="admin_panel").as_markup())
    await callback.answer()

@dp.message(Form.add_source_url)
async def process_source_url(message: Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    source_url = message.text
    if not source_url or not (source_url.startswith("http://") or source_url.startswith("https://")):
        await message.answer(get_message(user.language, 'invalid_url'))
        return

    existing_source = await get_source_by_url(source_url)
    if existing_source:
        await message.answer(get_message(user.language, 'source_exists', name=existing_source.name), reply_markup=get_admin_panel_keyboard(user.language))
        await state.clear()
        return

    await state.update_data(source_url=source_url)
    await state.set_state(Form.add_source_type)
    await message.answer(get_message(user.language, 'select_source_type'), reply_markup=get_source_type_keyboard(user.language))

@dp.callback_query(F.data.startswith("source_type_"), StateFilter(Form.add_source_type))
async def process_source_type(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    source_type = callback.data.replace("source_type_", "")
    data = await state.get_data()
    source_url = data.get('source_url')

    if not source_url:
        await callback.message.edit_text(get_message(user.language, 'error', e="URL джерела не знайдено. Спробуйте знову."), reply_markup=get_admin_panel_keyboard(user.language))
        await state.clear()
        await callback.answer()
        return

    try:
        source_name = source_url.replace("http://", "").replace("https://", "").split('/')[0]
        new_source = await add_source(user.id, source_name, source_url, source_type)
        await callback.message.edit_text(get_message(user.language, 'source_added', name=new_source.name), reply_markup=get_admin_panel_keyboard(user.language))
    except Exception as e:
        logger.error(f"Error adding source: {e}")
        await callback.message.edit_text(get_message(user.language, 'error', e=e), reply_markup=get_admin_panel_keyboard(user.language))
    
    await state.clear()
    await callback.answer()

@dp.callback_query(F.data == "admin_delete_source")
async def handle_delete_source_command(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("У вас немає доступу до цієї функції.", show_alert=True)
        return
    
    sources = await get_all_sources()
    if not sources:
        await callback.message.edit_text("Наразі немає доданих джерел.", reply_markup=get_admin_panel_keyboard(user.language))
        await callback.answer()
        return

    sources_list = "\n".join([f"ID: {s.id}, Назва: {s.name}, URL: {s.url}" for s in sources])
    await state.set_state(Form.delete_source_id)
    await callback.message.edit_text(f"{get_message(user.language, 'enter_source_id_to_delete')}\n\n{sources_list}", reply_markup=InlineKeyboardBuilder().button(text=get_message(user.language, 'back'), callback_data="admin_panel").as_markup())
    await callback.answer()

@dp.message(Form.delete_source_id)
async def process_delete_source_id(message: Message, state: FSMContext) -> None:
    user = await get_user(message.from_user.id)
    try:
        source_id = int(message.text)
        deleted = await delete_source_by_id(source_id)
        if deleted:
            await message.answer(get_message(user.language, 'source_deleted'), reply_markup=get_admin_panel_keyboard(user.language))
        else:
            await message.answer(get_message(user.language, 'source_not_found'), reply_markup=get_admin_panel_keyboard(user.language))
    except ValueError:
        await message.answer("Будь ласка, введіть дійсний числовий ID.", reply_markup=get_admin_panel_keyboard(user.language))
    except Exception as e:
        logger.error(f"Error deleting source: {e}")
        await message.answer(get_message(user.language, 'error', e=e), reply_markup=get_admin_panel_keyboard(user.language))
    
    await state.clear()

@dp.callback_query(F.data == "admin_stats")
async def handle_admin_stats(callback: CallbackQuery) -> None:
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("У вас немає доступу до цієї функції.", show_alert=True)
        return
    
    sources = await get_all_sources()
    news_counts = await get_news_counts_by_status()

    stats_text = "<b>Статистика:</b>\n\n"
    stats_text += "<u>Джерела:</u>\n"
    if sources:
        for s in sources:
            stats_text += f"ID: {s.id}, Назва: {s.name}, Тип: {s.type}, Активне: {s.is_active}, Останній парсинг: {s.last_parsed.strftime('%Y-%m-%d %H:%M') if s.last_parsed else 'N/A'}\n"
    else:
        stats_text += "Немає доданих джерел.\n"
    
    stats_text += "\n<u>Статус новин:</u>\n"
    if news_counts:
        for status, count in news_counts.items():
            stats_text += f"{status.capitalize()}: {count}\n"
    else:
        stats_text += "Немає новин.\n"

    await callback.message.edit_text(stats_text, parse_mode=ParseMode.HTML, reply_markup=get_admin_panel_keyboard(user.language))
    await callback.answer()

@dp.callback_query(F.data == "admin_all_news")
async def handle_admin_all_news(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("У вас немає доступу до цієї функції.", show_alert=True)
        return
    
    all_news_ids = await get_all_news_ids_for_user(user.id)
    
    if not all_news_ids:
        await callback.message.edit_text(get_message(user.language, 'no_news_found'), reply_markup=get_admin_panel_keyboard(user.language))
        await callback.answer()
        return

    await state.update_data(all_news_ids=all_news_ids, current_news_index=0)
    await send_news_to_user(callback.message.chat.id, all_news_ids[0], 0, len(all_news_ids), state)
    await callback.answer()

async def fetch_news_from_source(source: Source) -> List[News]:
    news_items = []
    try:
        if source.type == 'web':
            parsed_data = await web_parser.parse_website(str(source.url))
            if parsed_data:
                parsed_data = [parsed_data] # Wrap single item in a list for consistency
        elif source.type == 'rss':
            parsed_data = await rss_parser.parse_rss_feed(str(source.url))
            if parsed_data:
                parsed_data = [parsed_data] # Wrap single item in a list for consistency
        elif source.type == 'telegram':
            parsed_data = await telegram_parser.get_telegram_channel_posts(str(source.url))
            if parsed_data:
                parsed_data = [parsed_data] # Wrap single item in a list for consistency
        elif source.type == 'social_media':
            # Social media parser needs platform type, assuming it's part of source.name or can be inferred
            platform_type = source.name.lower().split(' ')[0] # Example: "Instagram User" -> "instagram"
            parsed_data = await social_media_parser.get_social_media_posts(str(source.url), platform_type)
            if parsed_data:
                parsed_data = [parsed_data] # Wrap single item in a list for consistency
        else:
            logger.warning(f"Unknown source type: {source.type}")
            return []

        if not parsed_data:
            logger.info(f"No new news found from source {source.url}.")
            return []

        for item in parsed_data:
            if not await is_news_already_exists(item['source_url'], item['title']):
                ai_summary = None
                ai_topics = []
                try:
                    summary_prompt = f"Зроби коротке резюме цієї новини (до 250 символів) українською мовою: {item['content']}"
                    ai_summary = await call_gemini_api(summary_prompt)
                    
                    topics_prompt = f"Визнач 3-5 ключових тем цієї новини у вигляді JSON-масиву рядків (наприклад, ['Політика', 'Економіка']), українською мовою: {item['content']}"
                    topics_response = await call_gemini_api(topics_prompt)
                    if topics_response:
                        try:
                            ai_topics = json.loads(topics_response)
                            if not isinstance(ai_topics, list):
                                ai_topics = []
                        except json.JSONDecodeError:
                            logger.warning(f"Could not parse AI topics as JSON: {topics_response}")
                            ai_topics = [t.strip() for t in topics_response.split(',') if t.strip()]
                except Exception as ai_e:
                    logger.error(f"Error generating AI data for news: {ai_e}")

                news_items.append(News(
                    source_id=source.id,
                    title=item['title'],
                    content=item['content'],
                    source_url=item['source_url'],
                    image_url=item.get('image_url'),
                    published_at=item['published_at'],
                    ai_summary=ai_summary,
                    ai_classified_topics=ai_topics,
                    moderation_status='pending'
                ))
            else:
                logger.info(f"Новина з джерела {source.url} вже існує.")

    except Exception as e:
        logger.error(f"Помилка парсингу джерела {source.url}: {e}")
    finally:
        await update_source_last_parsed(source.id)
        logger.info(f"Оновлено last_parsed для джерела {source.url}.")
    return news_items

async def send_news_to_channel(news_item: News):
    global NEWS_CHANNEL_ID
    if not NEWS_CHANNEL_ID:
        logger.warning("NEWS_CHANNEL_ID is not set. Skipping channel publication.")
        return

    channel_identifier = NEWS_CHANNEL_ID
    # Attempt to convert to int if it looks like a numeric channel ID
    if channel_identifier.startswith("-100") or channel_identifier.isdigit():
        try:
            channel_identifier = int(channel_identifier)
        except ValueError:
            logger.error(f"Invalid NEWS_CHANNEL_ID: {NEWS_CHANNEL_ID}. Must be integer for channel ID or @username.")
            return

    text = f"<b>{news_item.title}</b>\n\n"
    if news_item.ai_summary:
        text += f"{news_item.ai_summary}\n\n"
    else:
        short_content = news_item.content[:250] + "..." if len(news_item.content) > 250 else news_item.content
        text += f"{short_content}\n\n"

    text += f"Джерело: {hlink('Читати далі', str(news_item.source_url))}"

    try:
        if news_item.image_url:
            try:
                await bot.send_photo(chat_id=channel_identifier, photo=str(news_item.image_url), caption=text, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.error(f"Помилка публікації '{news_item.title}' з фото: {e}. Спроба відправити як текст.")
                text_with_image_link = text
                if news_item.image_url:
                    text_with_image_link += f"\n\n{hlink('Зображення', str(news_item.image_url))}"
                await bot.send_message(chat_id=channel_identifier, text=text_with_image_link, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        else:
            await bot.send_message(chat_id=channel_identifier, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        
        pool = await get_db_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE news SET is_published_to_channel = TRUE WHERE id = %s;", (news_item.id,))
        logger.info(f"Новина '{news_item.title}' успішно опублікована в канал.")

    except Exception as e:
        logger.error(f"Помилка публікації '{news_item.title}': {e}")

async def fetch_and_post_news_task():
    logger.info("Running fetch_and_post_news_task.")
    sources = await get_all_sources()
    
    for source in sources:
        new_news_items = await fetch_news_from_source(source)
        for news_item in new_news_items:
            try:
                added_news = await add_news(news_item)
                logger.info(f"Додано нову новину: {added_news.title}")
                if added_news.moderation_status == 'approved' or "AI News" in added_news.title:
                    await send_news_to_channel(added_news)
            except Exception as e:
                logger.error(f"Помилка при додаванні або публікації новини: {e}")
    logger.info("fetch_and_post_news_task finished.")

async def send_daily_digest():
    logger.info("Running send_daily_digest.")
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM users WHERE auto_notifications = TRUE AND digest_frequency = 'daily';")
            users_for_digest = await cur.fetchall()

            for user_data in users_for_digest:
                user = User(**user_data)
                news_for_digest = await get_news_for_user(user.id, limit=5)
                if news_for_digest:
                    digest_text = f"<b>Ваш щоденний дайджест новин ({datetime.now().strftime('%Y-%m-%d')}):</b>\n\n"
                    for i, news_item in enumerate(news_for_digest):
                        digest_text += f"{i+1}. {hbold(news_item.title)}\n"
                        if news_item.ai_summary:
                            digest_text += f"{news_item.ai_summary}\n"
                        digest_text += f"{hlink('Читати далі', str(news_item.source_url))}\n\n"
                    
                    try:
                        await bot.send_message(user.telegram_id, digest_text, parse_mode=ParseMode.HTML)
                        logger.info(f"Daily digest sent to user {user.telegram_id}")
                    except Exception as e:
                        logger.error(f"Error sending daily digest to user {user.telegram_id}: {e}")
                else:
                    logger.info(f"No news for daily digest for user {user.telegram_id}")
    logger.info("send_daily_digest finished.")

async def generate_ai_news_task():
    logger.info("Running generate_ai_news_task.")
    pool = await get_db_pool()
    ai_source_id = None
    try:
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                # Check if AI source exists
                await cur.execute("SELECT id FROM sources WHERE name = 'AI Generated News' AND type = 'ai';")
                ai_source = await cur.fetchone()
                if ai_source:
                    ai_source_id = ai_source['id']
                else:
                    # Create AI source if it doesn't exist
                    await cur.execute(
                        """
                        INSERT INTO sources (name, url, type, is_active, is_verified, category)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING id;
                        """,
                        ('AI Generated News', 'https://ai-generated.news', 'ai', True, True, 'ai_generated')
                    )
                    ai_source_id = (await cur.fetchone())['id']
                    logger.info(f"Created AI news source with ID: {ai_source_id}")

        prompt = "Згенеруй цікаву, коротку (150-200 слів) новину на випадкову тему, яка може бути цікавою для широкої аудиторії в Україні. Додай заголовок. Назви новину 'AI News Update HH:MM:SS'."
        generated_content = await call_gemini_api(prompt)
        
        if generated_content:
            title_match = re.search(r"^(.*?)\n\n", generated_content)
            title = title_match.group(1).strip() if title_match else f"AI News Update {datetime.now().strftime('%H:%M:%S')}"
            content = generated_content
            
            news_item = News(
                source_id=ai_source_id,
                title=title,
                content=content,
                source_url=HttpUrl("https://ai-generated.news"),
                image_url=None,
                published_at=datetime.now(timezone.utc),
                ai_summary=content[:250] + "..." if len(content) > 250 else content,
                ai_classified_topics=["AI Generated"],
                moderation_status='approved',
                expires_at=datetime.now(timezone.utc) + timedelta(days=7)
            )
            added_news = await add_news(news_item)
            logger.info(f"Згенеровано та додано AI новину: {added_news.title}")
            await send_news_to_channel(added_news)
        else:
            logger.warning("Не вдалося згенерувати AI новину.")
    except Exception as e:
        logger.error(f"Помилка при генерації AI новини: {e}")
    logger.info("generate_ai_news_task finished.")

async def scheduler():
    await get_db_pool()
    
    global BOT_USERNAME
    if not BOT_USERNAME:
        bot_info = await bot.get_me()
        BOT_USERNAME = bot_info.username
        logger.info(f"Bot username: @{BOT_USERNAME}")

    next_fetch_post_time = datetime.now(timezone.utc)
    next_delete_expired_time = datetime.now(timezone.utc)
    next_daily_digest_time = croniter(DAILY_DIGEST_CRON, datetime.now(timezone.utc)).get_next(datetime)
    next_ai_news_gen_time = croniter(AI_NEWS_GEN_CRON, datetime.now(timezone.utc)).get_next(datetime)

    logger.info(f"Next fetch_and_post_news_task at {next_fetch_post_time.strftime('%Y-%m-%d %H:%M:%S')}.")
    logger.info(f"Next delete_expired_news_task at {next_delete_expired_time.strftime('%Y-%m-%d %H:%M:%S')}.")
    logger.info(f"Next send_daily_digest at {next_daily_digest_time.strftime('%Y-%m-%d %H:%M:%S')}.")
    logger.info(f"Next generate_ai_news_task at {next_ai_news_gen_time.strftime('%Y-%m-%d %H:%M:%S')}.")

    while True:
        now = datetime.now(timezone.utc)
        
        if now >= next_fetch_post_time:
            asyncio.create_task(fetch_and_post_news_task())
            next_fetch_post_time = now + timedelta(seconds=NEWS_FETCH_INTERVAL_SECONDS)
            logger.info(f"Next fetch_and_post_news_task in {int((next_fetch_post_time - now).total_seconds())}s.")

        if now >= next_delete_expired_time:
            asyncio.create_task(delete_expired_news())
            next_delete_expired_time = now + timedelta(minutes=20)
            logger.info(f"Next delete_expired_news_task in {int((next_delete_expired_time - now).total_seconds())}s.")

        if now >= next_daily_digest_time:
            asyncio.create_task(send_daily_digest())
            next_daily_digest_time = croniter(DAILY_DIGEST_CRON, now).get_next(datetime)
            logger.info(f"Next send_daily_digest in {int((next_daily_digest_time - now).total_seconds())}s.")

        if now >= next_ai_news_gen_time:
            asyncio.create_task(generate_ai_news_task())
            next_ai_news_gen_time = croniter(AI_NEWS_GEN_CRON, now).get_next(datetime)
            logger.info(f"Next generate_ai_news_task in {int((next_ai_news_gen_time - now).total_seconds())}s.")

        delays = [
            (next_fetch_post_time - now).total_seconds(),
            (next_delete_expired_time - now).total_seconds(),
            (next_daily_digest_time - now).total_seconds(),
            (next_ai_news_gen_time - now).total_seconds()
        ]
        min_delay = min(d for d in delays if d > 0) if any(d > 0 for d in delays) else 10

        logger.info(f"Bot sleeping for {int(min_delay)}s.")
        await asyncio.sleep(max(1, int(min_delay)))

@app.on_event("startup")
async def on_startup():
    logger.info("FastAPI app startup.")
    asyncio.create_task(scheduler())

@app.on_event("shutdown")
async def on_shutdown():
    logger.info("FastAPI app shutdown.")
    if db_pool:
        await db_pool.close()
        logger.info("DB pool closed.")

@app.post("/")
async def root_webhook(request: Request):
    try:
        update = await request.json()
        aiogram_update = types.Update(**update)
        await dp.feed_update(bot, aiogram_update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Error processing Telegram webhook at root path: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

api_key_header = APIKeyHeader(name="X-API-Key")

def get_api_key(api_key: str = Depends(api_key_header)):
    if api_key == ADMIN_API_KEY:
        return api_key
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API Key")

@app.get("/api/admin/sources", response_model=List[Source])
async def get_admin_sources_api(api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM sources;")
            sources_data = await cur.fetchall()
            return [Source(**s) for s in sources_data]

@app.post("/api/admin/sources", response_model=Source, status_code=status.HTTP_201_CREATED)
async def add_admin_source_api(source: Source, api_key: str = Depends(get_api_key)):
    existing_source = await get_source_by_url(str(source.url))
    if existing_source:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Source with this URL already exists.")
    
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO sources (user_id, name, url, type, is_active, parse_interval_minutes, language, is_verified, category, priority)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *;
                """,
                (
                    source.user_id, source.name, str(source.url), source.type, source.is_active,
                    source.parse_interval_minutes, source.language, source.is_verified, source.category, source.priority
                )
            )
            source_data = await cur.fetchone()
            return Source(**source_data)

@app.put("/api/admin/sources/{source_id}", response_model=Source)
async def update_admin_source_api(source_id: int, source: Source, api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            set_clauses = []
            params = []
            for field, value in source.dict(exclude_unset=True).items():
                if field == 'url':
                    value = str(value)
                set_clauses.append(f"{field} = %s")
                params.append(value)
            
            if not set_clauses:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update.")

            params.append(source_id)
            
            await cur.execute(
                f"UPDATE sources SET {', '.join(set_clauses)} WHERE id = %s RETURNING *;",
                tuple(params)
            )
            updated_source = await cur.fetchone()
            if not updated_source:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found.")
            return Source(**updated_source)

@app.delete("/api/admin/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_admin_source_api(source_id: int, api_key: str = Depends(get_api_key)):
    deleted = await delete_source_by_id(source_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found.")
    return

@app.get("/api/admin/news/counts_by_status")
async def get_admin_news_counts_by_status_api(api_key: str = Depends(get_api_key)):
    return await get_news_counts_by_status()

@app.get("/api/admin/news", response_model=List[News])
async def get_admin_news_api(
    status_filter: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    api_key: str = Depends(get_api_key)
):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            query = "SELECT * FROM news"
            params = []
            if status_filter:
                query += " WHERE moderation_status = %s"
                params.append(status_filter)
            query += " ORDER BY published_at DESC LIMIT %s OFFSET %s;"
            params.extend([limit, offset])
            
            await cur.execute(query, tuple(params))
            news_data = await cur.fetchall()
            
            result = []
            for n_data in news_data:
                if n_data['ai_classified_topics']:
                    n_data['ai_classified_topics'] = json.loads(n_data['ai_classified_topics'])
                result.append(News(**n_data))
            return result

@app.put("/api/admin/news/{news_id}", response_model=News)
async def update_admin_news_api(news_id: int, news: News, api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            set_clauses = []
            params = []
            for field, value in news.dict(exclude_unset=True).items():
                if field in ['source_url', 'image_url'] and value is not None:
                    value = str(value)
                elif field == 'ai_classified_topics' and value is not None:
                    value = json.dumps(value)
                set_clauses.append(f"{field} = %s")
                params.append(value)
            
            if not set_clauses:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update.")

            params.append(news_id)
            
            await cur.execute(
                f"UPDATE news SET {', '.join(set_clauses)} WHERE id = %s RETURNING *;",
                tuple(params)
            )
            updated_rec = await cur.fetchone()
            if not updated_rec:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="News not found.")
            
            if updated_rec['ai_classified_topics']:
                updated_rec['ai_classified_topics'] = json.loads(updated_rec['ai_classified_topics'])
            return News(**updated_rec)

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
    if not WEBHOOK_URL:
        logger.info("WEBHOOK_URL not set, running in polling mode.")
        async def main():
            await get_db_pool()
            asyncio.create_task(scheduler())
            await dp.start_polling(bot)
        asyncio.run(main())
    else:
        logger.info(f"Running in webhook mode. Webhook URL: {WEBHOOK_URL}")
        async def set_webhook():
            await bot.set_webhook(url=WEBHOOK_URL)
            logger.info(f"Webhook set to {WEBHOOK_URL}")
        asyncio.run(set_webhook())
        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

