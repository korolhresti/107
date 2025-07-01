import asyncio
import logging
from datetime import datetime, timedelta
import json
import os
from typing import List, Optional, Dict, Any

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
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
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from gtts import gTTS # For audio generation

load_dotenv()

API_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(',') if x]
ANOTHER_BOT_CHANNEL_LINK = "https://t.me/YourOtherBotChannel"
WEBHOOK_URL = os.getenv("WEBHOOK_URL") # WEBHOOK_URL added

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="Telegram AI News Bot API", version="1.0.0")
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)

app.mount("/static", StaticFiles(directory="."), name="static")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def get_api_key(api_key: str = Depends(api_key_header)):
    if not ADMIN_API_KEY: raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="ADMIN_API_KEY not configured.")
    if api_key is None or api_key != ADMIN_API_KEY: raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key.")
    return api_key

db_pool = None

async def get_db_pool():
    global db_pool
    if db_pool is None:
        try:
            db_pool = AsyncConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, open=psycopg.AsyncConnection.connect)
            async with db_pool.connection() as conn: await conn.execute("SELECT 1")
            print("DB pool initialized.")
        except Exception as e:
            print(f"DB pool error: {e}")
            raise
    return db_pool

class User:
    def __init__(self, id: int, username: Optional[str] = None, first_name: Optional[str] = None,
                 last_name: Optional[str] = None, created_at: Optional[datetime] = None,
                 is_admin: bool = False, last_active: Optional[datetime] = None,
                 language: str = 'uk', auto_notifications: bool = False, digest_frequency: str = 'daily'):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.created_at = created_at if created_at else datetime.now()
        self.is_admin = is_admin
        self.last_active = last_active if last_active else datetime.now()
        self.language = language
        self.auto_notifications = auto_notifications
        self.digest_frequency = digest_frequency

class News:
    def __init__(self, id: int, title: str, content: str, source_url: Optional[str],
                 image_url: Optional[str], published_at: datetime, lang: str,
                 ai_summary: Optional[str] = None, ai_classified_topics: Optional[List[str]] = None,
                 moderation_status: str = 'approved', expires_at: Optional[datetime] = None):
        self.id = id
        self.title = title
        self.content = content
        self.source_url = source_url
        self.image_url = image_url
        self.published_at = published_at
        self.lang = lang
        self.ai_summary = ai_summary
        self.ai_classified_topics = ai_classified_topics
        self.moderation_status = moderation_status
        self.expires_at = expires_at if expires_at else published_at + timedelta(days=5)

class CustomFeed:
    def __init__(self, id: int, user_id: int, feed_name: str, filters: Dict[str, Any]):
        self.id = id
        self.user_id = user_id
        self.feed_name = feed_name
        self.filters = filters

class AddNews(StatesGroup):
    waiting_for_news_url = State()
    waiting_for_news_lang = State()
    confirm_news = State()

class NewsBrowse(StatesGroup):
    Browse_news = State()
    news_index = State()
    news_ids = State()
    last_message_id = State()

class AIAssistant(StatesGroup):
    waiting_for_question = State()
    waiting_for_news_id_for_question = State()
    waiting_for_term_to_explain = State()
    waiting_for_fact_to_check = State()
    fact_check_news_id = State()
    waiting_for_audience_summary_type = State()
    audience_summary_news_id = State()
    waiting_for_what_if_query = State()
    what_if_news_id = State()
    waiting_for_youtube_interview_url = State()

class FilterSetup(StatesGroup):
    waiting_for_source_selection = State()

async def create_tables():
    pool = await get_db_pool()
    async with pool.connection() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                first_name VARCHAR(255),
                last_name VARCHAR(255),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                is_admin BOOLEAN DEFAULT FALSE,
                last_active TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                language VARCHAR(10) DEFAULT 'uk',
                auto_notifications BOOLEAN DEFAULT FALSE,
                digest_frequency VARCHAR(50) DEFAULT 'daily'
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source_url TEXT,
                image_url TEXT,
                published_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                lang VARCHAR(10) NOT NULL DEFAULT 'uk',
                ai_summary TEXT,
                ai_classified_topics JSONB,
                moderation_status VARCHAR(50) DEFAULT 'approved', -- 'pending_review', 'approved', 'declined'
                expires_at TIMESTAMP WITH TIME ZONE DEFAULT (CURRENT_TIMESTAMP + INTERVAL '5 days')
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS custom_feeds (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(id),
                feed_name TEXT NOT NULL,
                filters JSONB, -- Stores JSON object with filters (e.g., {"source_ids": [1, 2, 3]})
                UNIQUE (user_id, feed_name)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                link TEXT UNIQUE NOT NULL,
                type TEXT DEFAULT 'web', -- 'web', 'telegram', 'rss', 'twitter'
                status TEXT DEFAULT 'active' -- 'active', 'inactive', 'blocked'
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_news_views (
                user_id BIGINT NOT NULL REFERENCES users(id),
                news_id INTEGER NOT NULL REFERENCES news(id),
                viewed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, news_id)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id BIGINT PRIMARY KEY REFERENCES users(id),
                viewed_news_count INT DEFAULT 0,
                last_active TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                viewed_topics JSONB DEFAULT '[]'::jsonb -- Stores a list of topics the user has viewed
            );
        """)
        logger.info("Tables checked/created.")

async def get_user(user_id: int) -> Optional[User]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            rec = await cur.fetchone()
            return User(**rec) if rec else None

async def create_or_update_user(tg_user: Any) -> User:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            user = await get_user(tg_user.id)
            if user:
                await cur.execute("UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE id = %s", (tg_user.id,))
                user.last_active = datetime.now()
                return user
            else:
                is_admin = tg_user.id in ADMIN_IDS
                await cur.execute(
                    """INSERT INTO users (id, username, first_name, last_name, is_admin, created_at, last_active, language)
                    VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s)""",
                    (tg_user.id, tg_user.username, tg_user.first_name, tg_user.last_name, is_admin, tg_user.language_code)
                )
                await cur.execute("INSERT INTO user_stats (user_id, last_active) VALUES (%s, CURRENT_TIMESTAMP)", (tg_user.id,))
                new_user = User(id=tg_user.id, username=tg_user.username, first_name=tg_user.first_name,
                                last_name=tg_user.last_name, is_admin=is_admin, language=tg_user.language_code)
                logger.info(f"New user: {new_user.username or new_user.first_name} (ID: {new_user.id})")
                return new_user

async def get_news(news_id: int) -> Optional[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM news WHERE id = %s", (news_id,))
            rec = await cur.fetchone()
            return News(**rec) if rec else None

async def add_news(news: News) -> News:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """INSERT INTO news (title, content, source_url, image_url, published_at, lang, ai_summary, ai_classified_topics, moderation_status, expires_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (news.title, news.content, news.source_url, news.image_url, news.published_at, news.lang,
                news.ai_summary, json.dumps(news.ai_classified_topics), news.moderation_status, news.expires_at)
            )
            res = await cur.fetchone()
            news.id = res['id']
            return news

async def get_user_filters(user_id: int) -> Dict[str, Any]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT filters FROM custom_feeds WHERE user_id = %s AND feed_name = 'default_feed'", (user_id,))
            feed = await cur.fetchone()
            return feed['filters'] if feed else {}

async def update_user_filters(user_id: int, filters: Dict[str, Any]):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """INSERT INTO custom_feeds (user_id, feed_name, filters)
                VALUES (%s, 'default_feed', %s::jsonb)
                ON CONFLICT (user_id, feed_name) DO UPDATE SET filters = EXCLUDED.filters""",
                (user_id, json.dumps(filters))
            )

async def get_sources() -> List[Dict[str, Any]]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id, name, link, type FROM sources ORDER BY name")
            return await cur.fetchall()

async def mark_news_as_viewed(user_id: int, news_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """INSERT INTO user_news_views (user_id, news_id) VALUES (%s, %s) ON CONFLICT (user_id, news_id) DO NOTHING""",
                (user_id, news_id)
            )
            await cur.execute(
                """INSERT INTO user_stats (user_id, viewed_news_count, last_active)
                VALUES (%s, 1, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE SET viewed_news_count = user_stats.viewed_news_count + 1, last_active = CURRENT_TIMESTAMP""",
                (user_id,)
            )

async def update_user_viewed_topics(user_id: int, topics: List[str]):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT viewed_topics FROM user_stats WHERE user_id = %s", (user_id,))
            current_topics_rec = await cur.fetchone()
            current_topics = current_topics_rec['viewed_topics'] if current_topics_rec and current_topics_rec['viewed_topics'] else []
            updated_topics = list(set(current_topics + topics))
            await cur.execute("UPDATE user_stats SET viewed_topics = %s::jsonb WHERE user_id = %s", (json.dumps(updated_topics), user_id))

async def make_gemini_request_with_history(messages: List[Dict[str, Any]]) -> str:
    if not GEMINI_API_KEY: return "AI functions unavailable. GEMINI_API_KEY not set."
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}
    data = {"contents": messages}
    async with ClientSession() as session:
        try:
            # Changed model from gemini-pro to gemini-2.0-flash-lite
            async with session.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent", params=params, headers=headers, json=data) as response:
                if response.status == 200:
                    res_json = await response.json()
                    if 'candidates' in res_json and res_json['candidates']:
                        first_candidate = res_json['candidates'][0]
                        if 'content' in first_candidate and 'parts' in first_candidate['content']:
                            for part in first_candidate['content']['parts']:
                                if 'text' in part: return part['text']
                    logger.warning(f"Gemini response missing parts: {res_json}")
                    return "Failed to get AI response."
                else:
                    err_text = await response.text()
                    logger.error(f"Gemini API error: {response.status} - {err_text}")
                    return f"AI error: {response.status}. Try later."
        except Exception as e:
            logger.error(f"Network error during Gemini request: {e}")
            return "Error contacting AI. Try later."

async def ai_summarize_news(title: str, content: str) -> Optional[str]:
    prompt = f"Зроби коротке резюме цієї новини (до 150 слів). Українською мовою.\n\nЗаголовок: {title}\n\nЗміст: {content[:2000]}..."
    return await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])

async def ai_translate_news(text: str, target_lang: str) -> Optional[str]:
    prompt = f"Переклади текст на {target_lang}. Збережи стилістику та сенс. Текст:\n{text}"
    return await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])

async def ai_answer_news_question(news_item: News, question: str, chat_history: List[Dict[str, Any]]) -> Optional[str]:
    history_for_gemini = chat_history + [{"role": "user", "parts": [{"text": f"Новина: {news_item.title}\n{news_item.content[:2000]}...\n\nМій запит: {question}"}]}]
    return await make_gemini_request_with_history(history_for_gemini)

async def ai_explain_term(term: str, news_content: str) -> Optional[str]:
    prompt = f"Поясни термін '{term}' у контексті наступної новини. Дай коротке та зрозуміле пояснення (до 100 слів) українською мовою.\n\nНовина: {news_content[:2000]}..."
    return await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])

async def ai_fact_check(fact_to_check: str, news_content: str) -> Optional[str]:
    prompt = f"Перевір факт: '{fact_to_check}'. Використай новину як контекст. Відповідь до 150 слів, українською.\n\nКонтекст новини: {news_content[:2000]}..."
    return await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])

async def ai_extract_entities(news_content: str) -> Optional[str]:
    prompt = f"Виділи ключові особи, організації, сутності з новини. Перерахуй списком (до 10) з коротким поясненням. Українською.\n\nНовина: {news_content[:2000]}..."
    return await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])

async def ai_classify_topics(news_content: str) -> Optional[List[str]]:
    prompt = f"Класифікуй новину за 3-5 основними темами/категоріями. Перерахуй теми через кому, українською.\n\nНовина: {news_content[:2000]}..."
    response = await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])
    return [t.strip() for t in response.split(',') if t.strip()] if response else None

async def ai_analyze_sentiment_trend(news_item: News, related_news_items: List[News]) -> Optional[str]:
    prompt_parts = [f"Проаналізуй новини та визнач, як змінювався настрій (позитивний, негативний, нейтральний) щодо теми. Сформулюй висновок про тренд настроїв. До 250 слів, українською.\n\n--- Основна Новина ---\nЗаголовок: {news_item.title}\nЗміст: {news_item.content[:1000]}..."]
    if news_item.ai_summary: prompt_parts.append(f"AI-резюме: {news_item.ai_summary}")
    if related_news_items:
        prompt_parts.append("\n\n--- Пов'язані Новини ---")
        for i, rn in enumerate(sorted(related_news_items, key=lambda n: n.published_at)):
            prompt_parts.append(f"\n- Новина {i+1} ({rn.published_at.strftime('%d.%m.%Y')}): {rn.title}")
            prompt_parts.append(f"  Зміст: {rn.content[:500]}...")
            if rn.ai_summary: prompt_parts.append(f"  Резюме: {rn.ai_summary}")
    return await make_gemini_request_with_history([{"role": "user", "parts": [{"text": "\n".join(prompt_parts)}]}])

async def ai_detect_bias_in_news(news_title: str, news_content: str, ai_summary: Optional[str]) -> Optional[str]:
    prompt = f"Проаналізуй новину на наявність упереджень. Виділи 1-3 потенційні упередження та поясни. Якщо немає, зазнач. До 250 слів, українською.\n\n--- Новина ---\nЗаголовок: {news_title}\nЗміст: {news_content[:2000]}..."
    if ai_summary: prompt += f"\nAI-резюме: {ai_summary}"
    return await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])

async def ai_summarize_for_audience(news_title: str, news_content: str, ai_summary: Optional[str], audience_type: str) -> Optional[str]:
    prompt = f"Узагальни новину для аудиторії: '{audience_type}'. Адаптуй мову та акценти. Резюме до 200 слів, українською.\n\n--- Новина ---\nЗаголовок: {news_title}\nЗміст: {news_content[:2000]}..."
    if ai_summary: prompt += f"\nAI-резюме: {ai_summary}"
    return await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])

async def ai_find_historical_analogues(news_title: str, news_content: str, ai_summary: Optional[str]) -> Optional[str]:
    prompt = f"Знайди 1-3 історичні події, схожі на новину. Опиши аналогію та схожість. До 300 слів, українською.\n\n--- Новина ---\nЗаголовок: {news_title}\nЗміст: {news_content[:2000]}..."
    if ai_summary: prompt += f"\nAI-резюме: {ai_summary}"
    return await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])

async def ai_analyze_impact(news_title: str, news_content: str, ai_summary: Optional[str]) -> Optional[str]:
    prompt = f"Оціни потенційний вплив новини. Розглянь короткострокові та довгострокові наслідки на різні сфери. До 300 слів, українською.\n\n--- Новина ---\nЗаголовок: {news_title}\nЗміст: {news_content[:2000]}..."
    if ai_summary: prompt += f"\nAI-резюме: {ai_summary}"
    return await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])

async def ai_generate_what_if_scenario(news_title: str, news_content: str, ai_summary: Optional[str], what_if_question: str) -> Optional[str]:
    prompt = f"Згенеруй гіпотетичний сценарій 'Що якби...' для новини, відповідаючи на питання: '{what_if_question}'. Розглянь наслідки. До 200 слів, українською.\n\n--- Новина ---\nЗаголовок: {news_title}\nЗміст: {news_content[:2000]}..."
    if ai_summary: prompt += f"\nAI-резюме: {ai_summary}"
    return await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])

async def ai_generate_news_from_youtube_interview(youtube_content_summary: str) -> Optional[str]:
    prompt = f"На основі змісту YouTube-інтерв'ю, створи коротку новинну статтю. Виділи 1-3 ключові тези/заяви. Новина об'єктивна, до 300 слів, українською. Оформи як новинну статтю з заголовком.\n\n--- Зміст YouTube-інтерв'ю ---\n{youtube_content_summary}"
    return await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])

async def ai_formulate_news_post(title: str, summary: str, source_url: Optional[str]) -> str:
    prompt = f"Сформуй короткий новинний пост для Telegram-каналу на основі заголовка та резюме. Додай емодзі, зроби його привабливим, але інформативним. Включи посилання на джерело, якщо воно є. Максимум 150 слів. Українською мовою.\n\nЗаголовок: {title}\nРезюме: {summary}"
    post = await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])
    if source_url: post += f"\n\n🔗 {hlink('Читати джерело', source_url)}"
    return post

async def ai_check_news_for_fakes(title: str, content: str) -> str:
    prompt = f"Проаналізуй новину на предмет потенційних ознак дезінформації, фейків або маніпуляцій. Зверни увагу на джерела, тон, наявність емоційно забарвлених висловлювань, неперевірені факти. Надай короткий висновок: 'Ймовірно, фейк', 'Потребує перевірки', 'Схоже на правду'. Поясни своє рішення 1-2 реченнями. Українською мовою.\n\nЗаголовок: {title}\nЗміст: {content[:2000]}..."
    return await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])

async def ai_filter_interesting_news(news_title: str, news_content: str, user_interests: List[str]) -> bool:
    interests_str = ", ".join(user_interests) if user_interests else "технологіями, економікою, політикою"
    prompt = f"Новина: '{news_title}'. Зміст: '{news_content[:500]}...'\nЧи є ця новина 'цікавою' для користувача, який цікавиться {interests_str}? Відповідай лише 'Так' або 'Ні'."
    response = await make_gemini_request_with_history([{"role": "user", "parts": [{"text": prompt}]}])
    return "Так" in response

def get_main_menu_keyboard():
    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="📰 Мої новини", callback_data="my_news"))
    kb.add(InlineKeyboardButton(text="➕ Додати новину", callback_data="add_news"))
    kb.add(InlineKeyboardButton(text="🧠 AI-функції (Новини)", callback_data="ai_news_functions_menu"))
    kb.add(InlineKeyboardButton(text="⚙️ Налаштування", callback_data="settings_menu"))
    kb.add(InlineKeyboardButton(text="❓ Допомога", callback_data="help_menu"))
    kb.add(InlineKeyboardButton(text="🤝 Допоможи продати", url=ANOTHER_BOT_CHANNEL_LINK))
    kb.add(InlineKeyboardButton(text="🛍️ Допоможи купити", url=ANOTHER_BOT_CHANNEL_LINK))
    kb.adjust(2)
    return kb.as_markup()

def get_ai_news_functions_menu():
    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="🗞️ Новина з YouTube-інтерв'ю", callback_data="news_from_youtube_interview"))
    kb.add(InlineKeyboardButton(text="⬅️ Назад до головного", callback_data="main_menu"))
    kb.adjust(1)
    return kb.as_markup()

def get_settings_menu_keyboard():
    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="🔍 Фільтри новин", callback_data="news_filters_menu"))
    kb.add(InlineKeyboardButton(text="🔔 Сповіщення про новини", callback_data="toggle_auto_notifications"))
    kb.add(InlineKeyboardButton(text="⬅️ Назад до головного", callback_data="main_menu"))
    kb.adjust(1)
    return kb.as_markup()

def get_news_filters_menu_keyboard():
    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="Джерела новин", callback_data="set_news_sources_filter"))
    kb.add(InlineKeyboardButton(text="Скинути фільтри", callback_data="reset_all_filters"))
    kb.add(InlineKeyboardButton(text="⬅️ Назад до налаштувань", callback_data="settings_menu"))
    kb.adjust(1)
    return kb.as_markup()

def get_news_keyboard(news_id: int):
    buttons = [
        [InlineKeyboardButton(text="📝 AI-резюме", callback_data=f"ai_summary_{news_id}"),
         InlineKeyboardButton(text="🌐 Перекласти", callback_data=f"translate_{news_id}"),
         InlineKeyboardButton(text="❓ Запитати AI", callback_data=f"ask_news_ai_{news_id}")],
        [InlineKeyboardButton(text="🧑‍🤝‍🧑 Ключові сутності", callback_data=f"extract_entities_{news_id}"),
         InlineKeyboardButton(text="❓ Пояснити термін", callback_data=f"explain_term_{news_id}"),
         InlineKeyboardButton(text="🏷️ Класифікувати за темами", callback_data=f"classify_topics_{news_id}")],
        [InlineKeyboardButton(text="✅ Перевірити факт", callback_data=f"fact_check_news_{news_id}"),
         InlineKeyboardButton(text="📊 Аналіз тренду настроїв", callback_data=f"sentiment_trend_analysis_{news_id}"),
         InlineKeyboardButton(text="🔍 Виявлення упередженості", callback_data=f"bias_detection_{news_id}")],
        [InlineKeyboardButton(text="📝 Резюме для аудиторії", callback_data=f"audience_summary_{news_id}"),
         InlineKeyboardButton(text="📜 Історичні аналоги", callback_data=f"historical_analogues_{news_id}"),
         InlineKeyboardButton(text="💥 Аналіз впливу", callback_data=f"impact_analysis_{news_id}")],
        [InlineKeyboardButton(text="🤔 Сценарії 'Що якби...'", callback_data=f"what_if_scenario_{news_id}"),
         InlineKeyboardButton(text="➡️ Далі", callback_data=f"next_news")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def send_news_to_user(chat_id: int, news_id: int, current_index: int, total_count: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id, title, content, source_url, image_url, published_at, lang, ai_summary, ai_classified_topics FROM news WHERE id = %s", (news_id,))
            news_record = await cur.fetchone()
            if not news_record:
                await bot.send_message(chat_id, "News not found.")
                return

            news_obj = News(id=news_record['id'], title=news_record['title'], content=news_record['content'],
                            source_url=news_record['source_url'], image_url=news_record['image_url'],
                            published_at=news_record['published_at'], lang=news_record['lang'],
                            ai_summary=news_record['ai_summary'], ai_classified_topics=news_record['ai_classified_topics'])

            message_text = (
                f"<b>{news_obj.title}</b>\n\n"
                f"{news_obj.content[:1000]}...\n\n"
                f"<i>Published: {news_obj.published_at.strftime('%d.%m.%Y %H:%M')}</i>\n"
                f"<i>News {current_index + 1} of {total_count}</i>"
            )
            if news_obj.source_url: message_text += f"\n\n🔗 {hlink('Read source', news_obj.source_url)}"
            
            reply_markup = get_news_keyboard(news_obj.id)
            
            if news_obj.image_url:
                try: msg = await bot.send_photo(chat_id, photo=news_obj.image_url, caption=message_text, reply_markup=reply_markup, disable_notification=True)
                except Exception as e:
                    logger.warning(f"Failed to send photo for news {news_id}: {e}. Sending without photo.")
                    msg = await bot.send_message(chat_id, message_text, reply_markup=reply_markup, disable_web_page_preview=True)
            else:
                msg = await bot.send_message(chat_id, message_text, reply_markup=reply_markup, disable_web_page_preview=True)
            
            await dp.fsm.get_context(chat_id, chat_id).update_data(last_message_id=msg.message_id)
            await mark_news_as_viewed(chat_id, news_id)
            if news_obj.ai_classified_topics: await update_user_viewed_topics(chat_id, news_obj.ai_classified_topics)

@router.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await create_or_update_user(message.from_user)
    await message.answer(f"Hello, {hbold(message.from_user.full_name)}! 👋\n\nI am your personal news assistant with AI functions. Choose an action:", reply_markup=get_main_menu_keyboard())

@router.message(Command("menu"))
async def command_menu_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Choose an action:", reply_markup=get_main_menu_keyboard())

@router.message(Command("cancel"))
@router.message(StateFilter(
    AddNews.waiting_for_news_url, AddNews.waiting_for_news_lang, AddNews.confirm_news,
    AIAssistant.waiting_for_question, AIAssistant.waiting_for_news_id_for_question,
    AIAssistant.waiting_for_term_to_explain, AIAssistant.waiting_for_fact_to_check,
    AIAssistant.waiting_for_audience_summary_type, AIAssistant.waiting_for_what_if_query,
    AIAssistant.waiting_for_youtube_interview_url, FilterSetup.waiting_for_source_selection
))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("No active actions to cancel.")
        return
    await state.clear()
    await message.answer("Action canceled. Choose next action:", reply_markup=get_main_menu_keyboard())

@router.message(Command("myprofile"))
async def handle_my_profile_command(message: Message):
    user_id = message.from_user.id
    user_record = await get_user(user_id)
    if not user_record:
        await message.answer("Your profile was not found. Try /start.")
        return
    username = user_record.username or user_record.first_name
    is_admin_str = "Yes" if user_record.is_admin else "No"
    created_at_str = user_record.created_at.strftime("%d.%m.%Y %H:%M")
    profile_text = (
        f"👤 <b>Your Profile:</b>\n"
        f"Name: {username}\n"
        f"ID: <code>{user_id}</code>\n"
        f"Registered: {created_at_str}\n"
        f"Admin: {is_admin_str}\n"
        f"Auto-notifications: {'Enabled' if user_record.auto_notifications else 'Disabled'}\n"
        f"Digest frequency: {user_record.digest_frequency}\n\n"
        f"<i>This bot is for news and AI functions. Selling/buying functionality is available in the channel: {ANOTHER_BOT_CHANNEL_LINK}</i>"
    )
    await message.answer(profile_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

@router.callback_query(F.data == "main_menu")
async def process_main_menu_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Choose an action:", reply_markup=get_main_menu_keyboard())
    await callback.answer()

@router.callback_query(F.data == "ai_news_functions_menu")
async def process_ai_news_functions_menu(callback: CallbackQuery):
    await callback.message.edit_text("🧠 *AI News Functions:*\nChoose the desired function:", reply_markup=get_ai_news_functions_menu(), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

@router.callback_query(F.data == "settings_menu")
async def process_settings_menu(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.message.answer("Please start with /start.")
        await callback.answer()
        return
    
    toggle_btn_text = "🔔 Disable auto-notifications" if user.auto_notifications else "🔕 Enable auto-notifications"
    
    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="🔍 News Filters", callback_data="news_filters_menu"))
    kb.add(InlineKeyboardButton(text=toggle_btn_text, callback_data="toggle_auto_notifications"))
    kb.add(InlineKeyboardButton(text="⬅️ Back to main", callback_data="main_menu"))
    kb.adjust(1)
    await callback.message.edit_text("⚙️ *Settings:*", reply_markup=kb.as_markup(), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

@router.callback_query(F.data == "news_filters_menu")
async def process_news_filters_menu(callback: CallbackQuery):
    await callback.message.edit_text("🔍 *News Filters:*\nChoose an action:", reply_markup=get_news_filters_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

@router.callback_query(F.data == "toggle_auto_notifications")
async def toggle_auto_notifications(callback: CallbackQuery):
    user_id = callback.from_user.id
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT auto_notifications FROM users WHERE id = %s", (user_id,))
            user_record = await cur.fetchone()
            if not user_record:
                await callback.message.answer("User not found.")
                await callback.answer()
                return
            new_status = not user_record['auto_notifications']
            await cur.execute("UPDATE users SET auto_notifications = %s WHERE id = %s", (new_status, user_id))
            
            status_text = "enabled" if new_status else "disabled"
            await callback.message.answer(f"🔔 Automatic news notifications {status_text}.")
            
            user = await get_user(user_id)
            toggle_btn_text = "🔔 Disable auto-notifications" if user.auto_notifications else "🔕 Enable auto-notifications"
            kb = InlineKeyboardBuilder()
            kb.add(InlineKeyboardButton(text="🔍 News Filters", callback_data="news_filters_menu"))
            kb.add(InlineKeyboardButton(text=toggle_btn_text, callback_data="toggle_auto_notifications"))
            kb.add(InlineKeyboardButton(text="⬅️ Back to main", callback_data="main_menu"))
            kb.adjust(1)
            await callback.message.edit_reply_markup(reply_markup=kb.as_markup())
    await callback.answer()

@router.callback_query(F.data == "set_news_sources_filter")
async def set_news_sources_filter(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    sources = await get_sources()
    if not sources:
        await callback.message.answer("No sources available for selection at the moment.")
        await callback.answer()
        return

    user_filters = await get_user_filters(user_id)
    selected_source_ids = user_filters.get('source_ids', [])

    kb = InlineKeyboardBuilder()
    for source in sources:
        is_selected = source['id'] in selected_source_ids
        kb.button(text=f"{'✅ ' if is_selected else ''}{source['name']}", callback_data=f"toggle_source_filter_{source['id']}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="Save and Apply", callback_data="save_source_filters"))
    kb.row(InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_filter_setup"))

    await callback.message.edit_text("Select sources from which you want to receive news:", reply_markup=kb.as_markup())
    await state.set_state(FilterSetup.waiting_for_source_selection)
    await callback.answer()

@router.callback_query(FilterSetup.waiting_for_source_selection, F.data.startswith("toggle_source_filter_"))
async def toggle_source_filter(callback: CallbackQuery, state: FSMContext):
    source_id = int(callback.data.split('_')[3])
    user_id = callback.from_user.id
    user_filters = await get_user_filters(user_id)
    selected_source_ids = user_filters.get('source_ids', [])

    if source_id in selected_source_ids: selected_source_ids.remove(source_id)
    else: selected_source_ids.append(source_id)
    user_filters['source_ids'] = selected_source_ids
    await update_user_filters(user_id, user_filters)

    sources = await get_sources()
    kb = InlineKeyboardBuilder()
    for source in sources:
        is_selected = source['id'] in selected_source_ids
        kb.button(text=f"{'✅ ' if is_selected else ''}{source['name']}", callback_data=f"toggle_source_filter_{source['id']}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="Save and Apply", callback_data="save_source_filters"))
    kb.row(InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_filter_setup"))
    await callback.message.edit_reply_markup(reply_markup=kb.as_markup())
    await callback.answer()

@router.callback_query(FilterSetup.waiting_for_source_selection, F.data == "save_source_filters")
async def save_source_filters(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user_filters = await get_user_filters(user_id)
    selected_source_ids = user_filters.get('source_ids', [])
    
    if selected_source_ids:
        pool = await get_db_pool()
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT name FROM sources WHERE id = ANY(%s)", (selected_source_ids,))
                sources_data = await cur.fetchall()
                selected_names = [s['name'] for s in sources_data]
        await callback.message.edit_text(f"Your source filters have been saved: {', '.join(selected_names)}.\nYou can view news using /my_news.")
    else:
        await callback.message.edit_text("You have not selected any sources. News will be displayed without source filtering.")
    await state.clear()
    await callback.answer("Filters saved!")

@router.callback_query(FilterSetup.waiting_for_source_selection, F.data == "cancel_filter_setup")
async def cancel_filter_setup(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Filter setup canceled.", reply_markup=get_settings_menu_keyboard())
    await callback.answer()

@router.callback_query(F.data == "reset_all_filters")
async def reset_all_filters(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    await update_user_filters(user_id, {})
    await callback.message.edit_text("✅ All your news filters have been reset.", reply_markup=get_news_filters_menu_keyboard())
    await callback.answer()

@router.callback_query(F.data == "news_from_youtube_interview")
async def handle_news_from_youtube_interview(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AIAssistant.waiting_for_youtube_interview_url)
    await callback.message.edit_text("🗞️ Send a link to a YouTube video. (AI will simulate analysis)", parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

@router.message(AIAssistant.waiting_for_youtube_interview_url, F.text.regexp(r"(https?://)?(www\.)?(youtube|youtu|m\.youtube)\.(com|be)/(watch\?v=|embed/|v/|)([\w-]{11})(?:\S+)?"))
async def process_youtube_interview_url(message: Message, state: FSMContext):
    youtube_url = message.text
    await message.answer("⏳ Analyzing interview and generating news...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    mock_content_prompt = f"Imagine you watched a YouTube interview at the link: {youtube_url}. Formulate a short imaginary content of this interview so that I can create news. Include hypothetical main topics and key quotes. The content should be realistic for news generation. Up to 500 words, content only. In Ukrainian."
    simulated_content = await make_gemini_request_with_history([{"role": "user", "parts": [{"text": mock_content_prompt}]}])
    if not simulated_content or "Failed to get AI response." in simulated_content:
        await message.answer("❌ Failed to get interview content for analysis. Try another link.")
        await state.clear()
        return
    generated_news_text = await ai_generate_news_from_youtube_interview(simulated_content)
    if generated_news_text and "Failed to get AI response." not in generated_news_text:
        await message.answer(f"✅ **Your news from YouTube interview:**\n\n{generated_news_text}", parse_mode=ParseMode.MARKDOWN)
    else:
        await message.answer("❌ Failed to create news from the provided interview.")
    await state.clear()
    await message.answer("Choose next action:", reply_markup=get_main_menu_keyboard())

@router.message(AIAssistant.waiting_for_youtube_interview_url)
async def process_youtube_interview_url_invalid(message: Message):
    await message.answer("Please send a valid YouTube video link, or enter /cancel.")

@router.callback_query(F.data.startswith("ai_summary_"))
async def handle_ai_summary_callback(callback: CallbackQuery):
    news_id = int(callback.data.split('_')[2])
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT title, content FROM news WHERE id = %s", (news_id,))
            news_item = await cur.fetchone()
            if not news_item:
                await callback.message.answer("❌ News not found.")
                await callback.answer()
                return
            await callback.message.answer("⏳ Generating summary using AI...")
            await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            summary = await ai_summarize_news(news_item['title'], news_item['content'])
            if summary:
                await cur.execute("UPDATE news SET ai_summary = %s WHERE id = %s", (summary, news_id))
                await callback.message.answer(f"📝 <b>AI Summary of News (ID: {news_id}):</b>\n\n{summary}")
            else:
                await callback.message.answer("❌ Failed to generate summary.")
    await callback.answer()

@router.callback_query(F.data.startswith("translate_"))
async def handle_translate_callback(callback: CallbackQuery):
    news_id = int(callback.data.split('_')[1])
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT title, content, lang FROM news WHERE id = %s", (news_id,))
            news_item = await cur.fetchone()
            if not news_item:
                await callback.message.answer("❌ News not found.")
                await callback.answer()
                return
            target_lang = 'en' if news_item['lang'] == 'uk' else 'uk'
            await callback.message.answer(f"⏳ Translating news to {target_lang.upper()} using AI...")
            await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            translated_title = await ai_translate_news(news_item['title'], target_lang)
            translated_content = await ai_translate_news(news_item['content'], target_lang)
            if translated_title and translated_content:
                await callback.message.answer(
                    f"🌐 <b>News Translation (ID: {news_id}) to {target_lang.upper()}:</b>\n\n"
                    f"<b>{translated_title}</b>\n\n{translated_content}"
                )
            else:
                await callback.message.answer("❌ Failed to translate news.")
    await callback.answer()

@router.callback_query(F.data.startswith("ask_news_ai_"))
async def handle_ask_news_ai_callback(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[3])
    await state.update_data(waiting_for_news_id_for_question=news_id)
    await state.set_state(AIAssistant.waiting_for_question)
    await callback.message.answer("❓ Ask your question about the news.")
    await callback.answer()

@router.message(AIAssistant.waiting_for_question, F.text)
async def process_news_question(message: Message, state: FSMContext):
    data = await state.get_data()
    news_id = data.get('waiting_for_news_id_for_question')
    question = message.text
    if not news_id:
        await message.answer("News context lost. Try again via /my_news.")
        await state.clear()
        return
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT title, content, lang FROM news WHERE id = %s", (news_id,))
            news_item_data = await cur.fetchone()
            if not news_item_data:
                await message.answer("News not found.")
                await state.clear()
                return
            news_item = News(id=news_id, title=news_item_data['title'], content=news_item_data['content'],
                             lang=news_item_data['lang'], published_at=datetime.now())
            chat_history = data.get('ask_news_ai_history', [])
            chat_history.append({"role": "user", "parts": [{"text": question}]})
            await message.answer("⏳ Processing your question using AI...")
            await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
            ai_response = await ai_answer_news_question(news_item, question, chat_history)
            if ai_response:
                await message.answer(f"<b>AI answers:</b>\n\n{ai_response}")
                chat_history.append({"role": "model", "parts": [{"text": ai_response}]})
                await state.update_data(ask_news_ai_history=chat_history)
            else:
                await message.answer("❌ Failed to answer your question.")
    await message.answer("Continue asking questions or enter /cancel to end the dialogue.")

@router.callback_query(F.data.startswith("extract_entities_"))
async def handle_extract_entities_callback(callback: CallbackQuery):
    news_id = int(callback.data.split('_')[2])
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT content FROM news WHERE id = %s", (news_id,))
            news_item = await cur.fetchone()
            if not news_item:
                await callback.message.answer("❌ News not found.")
                await callback.answer()
                return
            await callback.message.answer("⏳ Extracting key entities using AI...")
            await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            entities = await ai_extract_entities(news_item['content'])
            if entities:
                await callback.message.answer(f"🧑‍🤝‍🧑 <b>Key persons/entities in news (ID: {news_id}):</b>\n\n{entities}")
            else:
                await callback.message.answer("❌ Failed to extract entities.")
    await callback.answer()

@router.callback_query(F.data.startswith("classify_topics_"))
async def handle_classify_topics_callback(callback: CallbackQuery):
    news_id = int(callback.data.split('_')[2])
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT content, ai_classified_topics FROM news WHERE id = %s", (news_id,))
            news_item_record = await cur.fetchone()
            if not news_item_record:
                await callback.message.answer("❌ News not found.")
                await callback.answer()
                return
            topics = news_item_record['ai_classified_topics']
            if not topics:
                await callback.message.answer("⏳ Classifying news by topics using AI...")
                await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
                topics = await ai_classify_topics(news_item_record['content'])
                if topics: await cur.execute("UPDATE news SET ai_classified_topics = %s WHERE id = %s", (json.dumps(topics), news_id))
                else: topics = ["Failed to determine topics."]
            if topics:
                topics_str = ", ".join(topics)
                await callback.message.answer(f"🏷️ <b>Topic classification for news (ID: {news_id}):</b>\n\n{topics_str}")
            else:
                await callback.message.answer("❌ Failed to classify news by topics.")
    await callback.answer()

@router.callback_query(F.data.startswith("explain_term_"))
async def handle_explain_term_callback(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[2])
    await state.update_data(waiting_for_news_id_for_question=news_id)
    await state.set_state(AIAssistant.waiting_for_term_to_explain)
    await callback.message.answer("❓ Enter the term you want AI to explain in the context of this news.")
    await callback.answer()

@router.message(AIAssistant.waiting_for_term_to_explain, F.text)
async def process_explain_term_query(message: Message, state: FSMContext):
    data = await state.get_data()
    news_id = data.get('waiting_for_news_id_for_question')
    term = message.text.strip()
    if not news_id:
        await message.answer("News context lost. Try again via /my_news.")
        await state.clear()
        return
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT content FROM news WHERE id = %s", (news_id,))
            news_item = await cur.fetchone()
            if not news_item:
                await message.answer("News not found.")
                await state.clear()
                return
            await message.answer(f"⏳ Explaining term '{term}' using AI...")
            await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
            explanation = await ai_explain_term(term, news_item['content'])
            if explanation:
                await message.answer(f"❓ <b>Explanation of term '{term}' (News ID: {news_id}):</b>\n\n{explanation}")
            else:
                await message.answer("❌ Failed to explain term.")
    await state.clear()

@router.callback_query(F.data.startswith("fact_check_news_"))
async def handle_fact_check_news_callback(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[3])
    await state.update_data(fact_check_news_id=news_id)
    await state.set_state(AIAssistant.waiting_for_fact_to_check)
    await callback.message.answer("✅ Enter the fact you want to check in the context of this news.")
    await callback.answer()

@router.message(AIAssistant.waiting_for_fact_to_check, F.text)
async def process_fact_to_check(message: Message, state: FSMContext):
    data = await state.get_data()
    news_id = data.get('fact_check_news_id')
    fact_to_check = message.text.strip()
    if not news_id:
        await message.answer("News context lost. Try again.")
        await state.clear()
        return
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT content FROM news WHERE id = %s", (news_id,))
            news_item = await cur.fetchone()
            if not news_item:
                await message.answer("News not found.")
                await state.clear()
                return
            await message.answer(f"⏳ Checking fact: '{fact_to_check}' using AI...")
            await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
            fact_check_result = await ai_fact_check(fact_to_check, news_item['content'])
            if fact_check_result:
                await message.answer(f"✅ <b>Fact check for news (ID: {news_id}):</b>\n\n{fact_check_result}")
            else:
                await message.answer("❌ Failed to check fact.")
    await state.clear()

@router.callback_query(F.data.startswith("sentiment_trend_analysis_"))
async def handle_sentiment_trend_analysis_callback(callback: CallbackQuery):
    news_id = int(callback.data.split('_')[3])
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id, title, content, ai_summary, ai_classified_topics, lang, published_at FROM news WHERE id = %s", (news_id,))
            main_news_record = await cur.fetchone()
            if not main_news_record:
                await callback.message.answer("❌ News for analysis not found.")
                await callback.answer()
                return
            main_news_obj = News(id=main_news_record['id'], title=main_news_record['title'], content=main_news_record['content'], lang=main_news_record['lang'], published_at=main_news_record['published_at'], ai_summary=main_news_record['ai_summary'], ai_classified_topics=main_news_record['ai_classified_topics'])
            await callback.message.answer("⏳ Analyzing sentiment trend using AI...")
            await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            related_news_items = []
            if main_news_obj.ai_classified_topics:
                topic_conditions = [f"ai_classified_topics @> '[\"{t}\"]'::jsonb" for t in main_news_obj.ai_classified_topics]
                await cur.execute(f"""SELECT id, title, content, ai_summary, lang, published_at FROM news WHERE id != %s AND moderation_status = 'approved' AND expires_at > NOW() AND published_at >= NOW() - INTERVAL '30 days' AND ({' OR '.join(topic_conditions)}) ORDER BY published_at ASC LIMIT 5""", (news_id,))
                related_news_records = await cur.fetchall()
                related_news_items = [News(id=r['id'], title=r['title'], content=r['content'], lang=r['lang'], published_at=r['published_at'], ai_summary=r['ai_summary']) for r in related_news_records]
            ai_sentiment_trend = await ai_analyze_sentiment_trend(main_news_obj, related_news_items)
            await callback.message.answer(f"📊 <b>Sentiment Trend Analysis for News (ID: {news_id}):</b>\n\n{ai_sentiment_trend}", parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data.startswith("bias_detection_"))
async def handle_bias_detection_callback(callback: CallbackQuery):
    news_id = int(callback.data.split('_')[2])
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT title, content, ai_summary FROM news WHERE id = %s", (news_id,))
            news_item = await cur.fetchone()
            if not news_item:
                await callback.message.answer("❌ News for analysis not found.")
                await callback.answer()
                return
            await callback.message.answer("⏳ Analyzing news for bias using AI...")
            await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            ai_bias_analysis = await ai_detect_bias_in_news(news_item['title'], news_item['content'], news_item['ai_summary'])
            await callback.message.answer(f"🔍 <b>Bias Analysis for News (ID: {news_id}):</b>\n\n{ai_bias_analysis}", parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data.startswith("audience_summary_"))
async def handle_audience_summary_callback(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[2])
    await state.update_data(audience_summary_news_id=news_id)
    await state.set_state(AIAssistant.waiting_for_audience_summary_type)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Child", callback_data="audience_type_child"), InlineKeyboardButton(text="Expert", callback_data="audience_type_expert")],
        [InlineKeyboardButton(text="Politician", callback_data="audience_type_politician"), InlineKeyboardButton(text="Technologist", callback_data="audience_type_technologist")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_audience_summary")]
    ])
    await callback.message.edit_text("📝 For which audience do you want to get a summary of this news?", reply_markup=keyboard)
    await callback.answer()

@router.callback_query(AIAssistant.waiting_for_audience_summary_type, F.data.startswith("audience_type_"))
async def process_audience_type_selection(callback: CallbackQuery, state: FSMContext):
    audience_type_key = callback.data.split('_')[2]
    audience_map = {'child': 'child', 'expert': 'expert', 'politician': 'politician', 'technologist': 'technologist'}
    selected_audience = audience_map.get(audience_type_key, 'general audience')
    data = await state.get_data()
    news_id = data.get('audience_summary_news_id')
    if not news_id:
        await callback.message.answer("News context lost. Try again.")
        await state.clear()
        await callback.answer()
        return
    await callback.message.edit_text(f"⏳ Generating summary for audience: <b>{selected_audience}</b>...", parse_mode=ParseMode.HTML)
    await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT title, content, ai_summary FROM news WHERE id = %s", (news_id,))
            news_item = await cur.fetchone()
            if not news_item:
                await callback.message.answer("❌ News for summary not found.")
                await state.clear()
                await callback.answer()
                return
            ai_summary_for_audience = await ai_summarize_for_audience(news_item['title'], news_item['content'], news_item['ai_summary'], selected_audience)
            await callback.message.answer(f"📝 <b>Summary for audience: {selected_audience} (News ID: {news_id}):</b>\n\n{ai_summary_for_audience}", parse_mode=ParseMode.HTML)
    await state.clear()
    await callback.answer()

@router.callback_query(AIAssistant.waiting_for_audience_summary_type, F.data == "cancel_audience_summary")
async def cancel_audience_summary_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("✅ Audience summary generation canceled.")
    await state.clear()
    await callback.answer()

@router.callback_query(F.data.startswith("historical_analogues_"))
async def handle_historical_analogues_callback(callback: CallbackQuery):
    news_id = int(callback.data.split('_')[2])
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT title, content, ai_summary FROM news WHERE id = %s", (news_id,))
            news_item = await cur.fetchone()
            if not news_item:
                await callback.message.answer("❌ News for analysis not found.")
                await callback.answer()
                return
            await callback.message.answer("⏳ Searching for historical analogues using AI...")
            await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            ai_historical_analogues = await ai_find_historical_analogues(news_item['title'], news_item['content'], news_item['ai_summary'])
            await callback.message.answer(f"📜 <b>Historical Analogues for News (ID: {news_id}):</b>\n\n{ai_historical_analogues}", parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data.startswith("impact_analysis_"))
async def handle_impact_analysis_callback(callback: CallbackQuery):
    news_id = int(callback.data.split('_')[2])
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT title, content, ai_summary FROM news WHERE id = %s", (news_id,))
            news_item = await cur.fetchone()
            if not news_item:
                await callback.message.answer("❌ News for impact analysis not found.")
                await callback.answer()
                return
            await callback.message.answer("⏳ Analyzing potential news impact using AI...")
            await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            ai_impact_analysis = await ai_analyze_impact(news_item['title'], news_item['content'], news_item['ai_summary'])
            await callback.message.answer(f"💥 <b>News Impact Analysis (ID: {news_id}):</b>\n\n{ai_impact_analysis}", parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data.startswith("what_if_scenario_"))
async def handle_what_if_scenario_callback(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[3])
    await state.update_data(what_if_news_id=news_id)
    await state.set_state(AIAssistant.waiting_for_what_if_query)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"🤔 Enter your 'What if...' question for the news (ID: {news_id}). For example: 'What if the meeting ended without an agreement?'")
    await callback.answer()

@router.message(AIAssistant.waiting_for_what_if_query, F.text)
async def process_what_if_query(message: Message, state: FSMContext):
    what_if_question = message.text.strip()
    if not what_if_question:
        await message.answer("Please enter your 'What if...' question.")
        return
    data = await state.get_data()
    news_id_for_context = data.get('what_if_news_id')
    if not news_id_for_context:
        await message.answer("News context lost. Try again via /my_news.")
        await state.clear()
        return
    await message.answer("⏳ Generating 'What if...' scenario using AI...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT title, content, ai_summary FROM news WHERE id = %s", (news_id_for_context,))
            news_item = await cur.fetchone()
            if not news_item:
                await message.answer("❌ News not found. Try with another news item.")
                await state.clear()
                return
            ai_what_if_scenario = await ai_generate_what_if_scenario(news_item['title'], news_item['content'], news_item['ai_summary'], what_if_question)
            await message.answer(f"🤔 <b>'What if...' Scenario for News (ID: {news_id_for_context}):</b>\n\n{ai_what_if_scenario}", parse_mode=ParseMode.HTML)
    await state.clear()

@router.callback_query(F.data == "my_news")
async def handle_my_news_command(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    user_filters = await get_user_filters(user_id)
    source_ids = user_filters.get('source_ids', [])
    
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            query = "SELECT id FROM news WHERE moderation_status = 'approved' AND expires_at > NOW()"
            params = []
            if source_ids:
                # Get source links by their IDs
                await cur.execute("SELECT link FROM sources WHERE id = ANY(%s)", (source_ids,))
                source_links_data = await cur.fetchall()
                source_links = [s['link'] for s in source_links_data]
                if source_links:
                    query += " AND source_url = ANY(%s)" # Use ANY for array of links
                    params.append(source_links)
                else: # If selected source IDs do not match any links, there will be no news
                    await callback.message.answer("There are currently no news available for your filters. Try changing filters or check back later.")
                    await callback.answer()
                    return

            query += " ORDER BY published_at DESC"
            
            # Execute the query with parameters
            if params:
                await cur.execute(query, (params[0],) if len(params) == 1 else tuple(params))
            else:
                await cur.execute(query)

            news_records = await cur.fetchall()


            if not news_records:
                await callback.message.answer("There are currently no news available for your filters. Try changing filters or check back later.")
                await callback.answer()
                return
            news_ids = [r['id'] for r in news_records]
            await state.update_data(news_ids=news_ids, news_index=0)
            await state.set_state(NewsBrowse.Browse_news)
            current_news_id = news_ids[0]
            await callback.message.edit_text("Loading news...")
            await send_news_to_user(callback.message.chat.id, current_news_id, 0, len(news_ids))
            await callback.answer()

@router.callback_query(NewsBrowse.Browse_news, F.data == "next_news")
async def process_next_news(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    news_ids = data.get('news_ids', [])
    current_index = data.get('news_index', 0)
    if current_index < len(news_ids) - 1:
        new_index = current_index + 1
        await state.update_data(news_index=new_index)
        await callback.message.delete()
        await send_news_to_user(callback.message.chat.id, news_ids[new_index], new_index, len(news_ids))
    else:
        await callback.answer("This is the last news item.", show_alert=True)
    await callback.answer()

@router.callback_query(F.data == "add_news")
async def add_news_command(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddNews.waiting_for_news_url)
    await callback.message.answer("Please send a link to a news article.")
    await callback.answer()

@router.message(AddNews.waiting_for_news_url, F.text.regexp(r"https?://[^\s]+"))
async def process_news_url(message: Message, state: FSMContext):
    news_url = message.text
    mock_title = f"News from {news_url.split('/')[2]}"
    mock_content = f"This is an imaginary content of a news article from the link: {news_url}. It talks about important world events, the impact of technology on society, and new discoveries in science. Details are left out as this is just a simulation of parsing a real news item. More information can be found at the link."
    mock_image_url = "https://placehold.co/600x400/ADE8F4/000000?text=News+Image"
    
    user_id = message.from_user.id
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT viewed_topics FROM user_stats WHERE user_id = %s", (user_id,))
            user_stats_rec = await cur.fetchone()
            user_interests = user_stats_rec['viewed_topics'] if user_stats_rec else []

    is_interesting = await ai_filter_interesting_news(mock_title, mock_content, user_interests)

    if not is_interesting:
        await message.answer("This news does not seem interesting enough for our feed. Try another link.")
        await state.clear()
        return

    await state.update_data(news_url=news_url, title=mock_title, content=mock_content, image_url=mock_image_url)
    await state.set_state(AddNews.waiting_for_news_lang)
    lang_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ukrainian", callback_data="lang_uk")],
        [InlineKeyboardButton(text="English", callback_data="lang_en")]
    ])
    await message.answer("Please choose the language of the news:", reply_markup=lang_keyboard)

@router.message(AddNews.waiting_for_news_url)
async def process_news_url_invalid(message: Message):
    await message.answer("Please send a valid article link, or enter /cancel.")

@router.callback_query(AddNews.waiting_for_news_lang, F.data.startswith("lang_"))
async def process_news_lang(callback: CallbackQuery, state: FSMContext):
    lang = callback.data.split('_')[1]
    data = await state.get_data()
    title = data['title']
    content = data['content']
    news_url = data['news_url']
    image_url = data['image_url']

    await callback.message.edit_text("⏳ Analyzing news and generating post using AI...")
    await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)

    fake_check_result = await ai_check_news_for_fakes(title, content)
    if "Ймовірно, фейк" in fake_check_result: # "Ймовірно, фейк" is Ukrainian for "Likely fake"
        await callback.message.answer(f"⚠️ **Attention!** AI check revealed: {fake_check_result}\nNews will not be published due to possible misinformation.")
        await state.clear()
        await callback.message.answer("Choose next action:", reply_markup=get_main_menu_keyboard())
        await callback.answer()
        return

    ai_summary = await ai_summarize_news(title, content)
    ai_topics = await ai_classify_topics(content)
    post_text = await ai_formulate_news_post(title, ai_summary or content, news_url)

    new_news = News(id=0, title=title, content=content, source_url=news_url, image_url=image_url,
                    published_at=datetime.now(), lang=lang, ai_summary=ai_summary,
                    ai_classified_topics=ai_topics, moderation_status='approved')
    await add_news(new_news)
    logger.info(f"News {new_news.id} added and auto-approved.")

    await callback.message.answer(f"✅ News successfully added and AI-post generated:\n\n{post_text}", parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    await callback.message.answer("Choose next action:", reply_markup=get_main_menu_keyboard())
    await state.clear()
    await callback.answer()

@router.message(AddNews.waiting_for_news_lang)
async def process_news_lang_invalid(message: Message):
    await message.answer("Please choose news language using buttons, or enter /cancel.")

@router.callback_query(F.data == "help_menu")
async def handle_help_menu(callback: CallbackQuery):
    help_text = (
        "<b>Available commands:</b>\n"
        "/start - Start working with the bot\n"
        "/menu - Main menu\n"
        "/cancel - Cancel current action\n"
        "/myprofile - View your profile\n"
        "/my_news - View news selection\n"
        "/add_news - Add new news (for publication)\n"
        "/setfiltersources - Configure news sources\n"
        "/resetfilters - Reset all news filters\n"
        "\n<b>AI functions:</b>\n"
        "Available after selecting news (buttons under the news).\n"
        "\n<b>Marketplace integration:</b>\n"
        "Buttons 'Help sell' and 'Help buy' redirect to another bot/channel."
    )
    await callback.message.edit_text(help_text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
    await callback.answer()

async def news_repost_task():
    repost_interval = 300 # Increased interval to 300 seconds (5 minutes)
    while True:
        try:
            mock_title = f"AI News Update {datetime.now().strftime('%H:%M:%S')}"
            mock_content = f"This is an automatically generated news item about the latest events in the world of AI and technology. AI continues to integrate into everyday life, changing the way people interact with information. New advances in machine learning allow for the creation of more personalized and adaptive systems. Experts predict further growth in the impact of AI on the economy and society."
            mock_source_url = "https://example.com/ai-news"
            mock_image_url = "https://placehold.co/600x400/ADE8F4/000000?text=AI+News"
            mock_lang = 'uk'

            pool = await get_db_pool()
            async with pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("SELECT viewed_topics FROM user_stats LIMIT 1")
                    user_stats_rec = await cur.fetchone()
                    user_interests = user_stats_rec['viewed_topics'] if user_stats_rec else []

            is_interesting = await ai_filter_interesting_news(mock_title, mock_content, user_interests)

            if is_interesting:
                ai_summary = await ai_summarize_news(mock_title, mock_content)
                ai_topics = await ai_classify_topics(mock_content)
                new_news = News(id=0, title=mock_title, content=mock_content, source_url=mock_source_url,
                                image_url=mock_image_url, published_at=datetime.now(), lang=mock_lang,
                                ai_summary=ai_summary, ai_classified_topics=ai_topics, moderation_status='approved')
                await add_news(new_news)
                logger.info(f"Auto-reposted and approved news: {new_news.id} - '{new_news.title}'")
            else:
                logger.info(f"Skipped reposting news (not interesting): '{mock_title}'")
        except Exception as e:
            logger.error(f"Error in news repost task: {e}")
        await asyncio.sleep(repost_interval)

async def news_digest_task():
    while True:
        now = datetime.now()
        next_run_time = datetime(now.year, now.month, now.day, 9, 0, 0) # Daily at 9 AM
        if now >= next_run_time: next_run_time += timedelta(days=1)
        
        sleep_seconds = (next_run_time - now).total_seconds()
        if sleep_seconds < 0: sleep_seconds = 0 # Should not happen if logic is correct for next day
        
        logger.info(f"Next digest run in {sleep_seconds:.0f} seconds.")
        await asyncio.sleep(sleep_seconds)

        try:
            pool = await get_db_pool()
            async with pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute("SELECT id, language, auto_notifications FROM users WHERE auto_notifications = TRUE AND digest_frequency = 'daily'")
                    users = await cur.fetchall()
                    for user_data in users:
                        user_id = user_data['id']
                        user_lang = user_data['language']
                        user_filters = await get_user_filters(user_id) # This function already uses a cursor
                        source_ids = user_filters.get('source_ids', [])
                        
                        query = "SELECT id, title, content, source_url, image_url, published_at, ai_summary FROM news WHERE moderation_status = 'approved' AND expires_at > NOW()"
                        params = []
                        if source_ids:
                            # Get source links by their IDs for filtering
                            await cur.execute("SELECT link FROM sources WHERE id = ANY(%s)", (source_ids,))
                            source_links_data = await cur.fetchall()
                            source_links = [s['link'] for s in source_links_data]
                            if source_links:
                                query += " AND source_url = ANY(%s)"
                                params.append(source_links)
                            else: # If selected source IDs do not match any links, there will be no news for this user
                                continue # Proceed to the next user
                        
                        # Add filtering by viewed news
                        query += f" AND id NOT IN (SELECT news_id FROM user_news_views WHERE user_id = {user_id}) ORDER BY published_at DESC LIMIT 5"
                        
                        # Execute the query with parameters
                        if params:
                            await cur.execute(query, (params[0],) if len(params) == 1 else tuple(params))
                        else:
                            await cur.execute(query)

                        news_items_data = await cur.fetchall()
                        
                        if news_items_data:
                            digest_text = f"📰 <b>Your daily news digest ({now.strftime('%d.%m.%Y')}):</b>\n\n"
                            for news_rec in news_items_data:
                                news_obj = News(id=news_rec['id'], title=news_rec['title'], content=news_rec['content'],
                                                source_url=news_rec['source_url'], image_url=news_rec['image_url'],
                                                published_at=news_rec['published_at'], lang=user_lang, ai_summary=news_rec['ai_summary'])
                                
                                summary_to_use = news_obj.ai_summary or news_obj.content[:200] + "..."
                                digest_text += f"• <b>{news_obj.title}</b>\n{summary_to_use}\n"
                                if news_obj.source_url: digest_text += f"🔗 {hlink('Read', news_obj.source_url)}\n\n"
                                
                                await mark_news_as_viewed(user_id, news_obj.id)
                            
                            try:
                                await bot.send_message(user_id, digest_text, disable_web_page_preview=True)
                                logger.info(f"Digest sent to user {user_id}.")
                            except Exception as e:
                                logger.error(f"Failed to send digest to user {user_id}: {e}")
        except Exception as e:
            logger.error(f"Error in news digest task: {e}")

@app.on_event("startup")
async def startup_event():
    await get_db_pool()
    await create_tables()
    
    if WEBHOOK_URL and API_TOKEN:
        # Use a fixed path for the webhook
        webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/telegram_webhook"
        logger.info(f"Attempting to set webhook to: {webhook_full_url}") # Added logging
        try:
            await asyncio.sleep(5) # Added a 5-second delay
            await bot.set_webhook(url=webhook_full_url)
            logger.info(f"Webhook successfully set to {webhook_full_url}")
        except Exception as e:
            logger.error(f"Failed to set webhook: {e}")
            # Important: If the webhook cannot be set, the application will not be able to receive updates.
            # Therefore, it is better to let it crash so that the problem is noticed.
            raise # Re-raise the exception to stop startup if webhook fails to set
    else:
        logger.warning("WEBHOOK_URL or BOT_TOKEN not set. Webhook will not be configured.")
        # If WEBHOOK_URL or BOT_TOKEN are not set, the application will not work correctly with webhooks.
        # In this case, if you want to use polling as a fallback, it needs to be enabled here.
        # However, for production on Render, webhooks are recommended.
        # asyncio.create_task(dp.start_polling(bot)) # Commented out to avoid conflict if webhook is not configured.

    asyncio.create_task(news_repost_task())
    asyncio.create_task(news_digest_task())
    logger.info("FastAPI app started.")

@app.on_event("shutdown")
async def shutdown_event():
    global db_pool
    if db_pool: await db_pool.close()
    # When the service shuts down, it is advisable to delete the webhook
    if API_TOKEN:
        try:
            await bot.delete_webhook()
            logger.info("Webhook deleted.")
        except Exception as e:
            logger.error(f"Failed to delete webhook: {e}")
    logger.info("FastAPI app shut down.")

@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(get_api_key)])
async def get_dashboard():
    with open("dashboard.html", "r", encoding="utf-8") as f: return HTMLResponse(content=f.read())

@app.get("/users", response_class=HTMLResponse, dependencies=[Depends(get_api_key)])
async def get_users_page():
    with open("users.html", "r", encoding="utf-8") as f: return HTMLResponse(content=f.read())

@app.get("/reports", response_class=HTMLResponse, dependencies=[Depends(get_api_key)])
async def get_reports_page():
    with open("reports.html", "r", encoding="utf-8") as f: return HTMLResponse(content=f.read())

@app.get("/api/admin/stats")
async def get_admin_stats_api(api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT COUNT(*) FROM users")
            total_users = (await cur.fetchone())['count']
            await cur.execute("SELECT COUNT(*) FROM news")
            total_news = (await cur.fetchone())['count']
            await cur.execute("SELECT COUNT(DISTINCT id) FROM users WHERE last_active >= NOW() - INTERVAL '7 days'")
            active_users_count = (await cur.fetchone())['count']
            return {
                "total_users": total_users,
                "total_news": total_news,
                "total_products": 0, # Removed product functionality
                "total_transactions": 0, # Removed product functionality
                "total_reviews": 0, # Removed product functionality
                "active_users_count": active_users_count
            }

@app.get("/api/admin/users")
async def get_admin_users_api(limit: int = 20, offset: int = 0, api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id, username, first_name, last_name, created_at, is_admin, last_active, language, auto_notifications, digest_frequency FROM users ORDER BY created_at DESC LIMIT %s OFFSET %s", (limit, offset))
            users_data = await cur.fetchall()
            await cur.execute("SELECT COUNT(*) FROM users")
            total_count = (await cur.fetchone())['count']
            return {"users": [User(**u).__dict__ for u in users_data], "total_count": total_count}

@app.get("/api/admin/news")
async def get_admin_news_api(limit: int = 20, offset: int = 0, api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id, title, content, source_url, image_url, published_at, lang, ai_summary, ai_classified_topics, moderation_status, expires_at FROM news ORDER BY published_at DESC LIMIT %s OFFSET %s", (limit, offset))
            news_data = await cur.fetchall()
            await cur.execute("SELECT COUNT(*) FROM news")
            total_count = (await cur.fetchone())['count']
            return {"news": [News(**n).__dict__ for n in news_data], "total_count": total_count}

@app.post("/api/admin/news")
async def create_admin_news_api(news_data: Dict[str, Any], api_key: str = Depends(get_api_key)):
    news_obj = News(id=0, **news_data)
    news_obj.ai_summary = await ai_summarize_news(news_obj.title, news_obj.content)
    news_obj.ai_classified_topics = await ai_classify_topics(news_obj.content)
    new_news = await add_news(news_obj)
    return new_news.__dict__

@app.put("/api/admin/news/{news_id}")
async def update_admin_news_api(news_id: int, news_data: Dict[str, Any], api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            set_clauses = []
            params = []
            for k, v in news_data.items():
                if k in ['title', 'content', 'source_url', 'image_url', 'lang', 'moderation_status', 'expires_at']:
                    set_clauses.append(f"{k} = %s")
                    params.append(v)
                elif k == 'ai_classified_topics':
                    set_clauses.append(f"{k} = %s::jsonb")
                    params.append(json.dumps(v))
            if not set_clauses: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update.")
            params.append(news_id)
            await cur.execute(f"UPDATE news SET {', '.join(set_clauses)} WHERE id = %s RETURNING *", tuple(params))
            updated_rec = await cur.fetchone()
            if not updated_rec: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="News not found.")
            return News(**updated_rec).__dict__

@app.delete("/api/admin/news/{news_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_admin_news_api(news_id: int, api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("DELETE FROM news WHERE id = %s", (news_id,))
            # Check if any row was deleted
            if cur.rowcount == 0: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="News not found.")
            return

@app.post("/telegram_webhook") # Changed webhook path
async def telegram_webhook(request: Request):
    update = await request.json()
    await dp.feed_update(bot, types.Update.model_validate(update, context={"bot": bot}))
    return {"ok": True}

@router.message()
async def echo_handler(message: types.Message) -> None:
    await message.answer("Command not understood. Use /menu.")

# Removed main function and asyncio.run(main()) as FastAPI manages application lifecycle
# async def main():
#     await get_db_pool()
#     await create_tables()
#     await dp.start_polling(bot)

# if __name__ == "__main__":
#     asyncio.run(main())
