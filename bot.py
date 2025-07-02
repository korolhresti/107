import asyncio
import logging
from datetime import datetime, timedelta
import json
import os
from typing import List, Optional, Dict, Any, Union
import random # Додано для випадкового вибору джерела

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
from gtts import gTTS # Для генерації аудіо

load_dotenv()

API_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(',') if x]
# Оновлені посилання на Telegram-канали
ANOTHER_BOT_CHANNEL_LINK_SELL = "https://t.me/BigmoneycreateBot"
ANOTHER_BOT_CHANNEL_LINK_BUY = "https://t.me/+eZEMW4FMEWQxMjYy"
NEWS_CHANNEL_LINK = os.getenv("NEWS_CHANNEL_LINK", "https://t.me/newsone234") # Канал для публікації новин
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

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
    if not ADMIN_API_KEY: raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="ADMIN_API_KEY не налаштовано.")
    if api_key is None or api_key != ADMIN_API_KEY: raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Недійсний або відсутній ключ API.")
    return api_key

db_pool = None

async def get_db_pool():
    global db_pool
    if db_pool is None:
        try:
            db_pool = AsyncConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, open=psycopg.AsyncConnection.connect)
            async with db_pool.connection() as conn: await conn.execute("SELECT 1")
            logger.info("Пул БД ініціалізовано.")
        except Exception as e:
            logger.error(f"Помилка пулу БД: {e}")
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

class AddSource(StatesGroup):
    waiting_for_source_link = State()
    waiting_for_source_name = State()
    waiting_for_source_type = State()

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

class LanguageSelection(StatesGroup):
    waiting_for_language = State()

async def create_tables():
    pool = await get_db_pool()
    async with pool.connection() as conn:
        # Оновлено CREATE TABLE users для включення всіх полів
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
                digest_frequency VARCHAR(50) DEFAULT 'daily',
                safe_mode BOOLEAN DEFAULT FALSE,
                current_feed_id INT,
                is_premium BOOLEAN DEFAULT FALSE,
                premium_expires_at TIMESTAMP,
                level INT DEFAULT 1,
                badges TEXT[] DEFAULT ARRAY[]::TEXT[],
                inviter_id INT,
                email TEXT UNIQUE,
                view_mode TEXT DEFAULT 'manual',
                telegram_id BIGINT
            );
        """)
        # Додавання відсутніх стовпців до users, якщо вони не були додані раніше
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(255);")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name VARCHAR(255);")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name VARCHAR(255);")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS language VARCHAR(10) DEFAULT 'uk';")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_notifications BOOLEAN DEFAULT FALSE;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS digest_frequency VARCHAR(50) DEFAULT 'daily';")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS safe_mode BOOLEAN DEFAULT FALSE;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS current_feed_id INT;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_premium BOOLEAN DEFAULT FALSE;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_expires_at TIMESTAMP;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS level INT DEFAULT 1;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS badges TEXT[] DEFAULT ARRAY[]::TEXT[];")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS inviter_id INT;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT UNIQUE;")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS view_mode TEXT DEFAULT 'manual';")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_id BIGINT;")

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
                moderation_status VARCHAR(50) DEFAULT 'approved',
                expires_at TIMESTAMP WITH TIME ZONE DEFAULT (CURRENT_TIMESTAMP + INTERVAL '5 days')
            );
        """)
        await conn.execute("ALTER TABLE news ADD COLUMN IF NOT EXISTS source_url TEXT;")
        await conn.execute("ALTER TABLE news ADD COLUMN IF NOT EXISTS image_url TEXT;")
        await conn.execute("ALTER TABLE news ADD COLUMN IF NOT EXISTS ai_summary TEXT;")
        await conn.execute("ALTER TABLE news ADD COLUMN IF NOT EXISTS ai_classified_topics JSONB;")
        await conn.execute("ALTER TABLE news ADD COLUMN IF NOT EXISTS moderation_status VARCHAR(50) DEFAULT 'approved';")
        await conn.execute("ALTER TABLE news ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP WITH TIME ZONE DEFAULT (CURRENT_TIMESTAMP + INTERVAL '5 days');")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS custom_feeds (
                id SERIAL PRIMARY KEY,
                user_id BIGINT REFERENCES users(id),
                feed_name TEXT NOT NULL,
                filters JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, feed_name)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                id SERIAL PRIMARY KEY,
                name TEXT UNIQUE NOT NULL,
                link TEXT UNIQUE NOT NULL,
                type TEXT DEFAULT 'web',
                status TEXT DEFAULT 'active'
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
                viewed_topics JSONB DEFAULT '[]'::jsonb
            );
        """)
        await conn.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS viewed_topics JSONB DEFAULT '[]'::jsonb;")

        # Додавання інших таблиць, якщо їх немає
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id SERIAL PRIMARY KEY,
                news_id INT REFERENCES news(id),
                user_id INT REFERENCES users(id),
                comment_text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id SERIAL PRIMARY KEY,
                user_id INT REFERENCES users(id),
                report_type TEXT NOT NULL,
                target_id INT,
                details JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS target_id INT;")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                user_id INT REFERENCES users(id),
                feedback_text TEXT NOT NULL,
                rating INT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id SERIAL PRIMARY KEY,
                news_id INT REFERENCES news(id),
                summary_text TEXT NOT NULL,
                summary_type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                id SERIAL PRIMARY KEY,
                user_id INT REFERENCES users(id),
                block_type TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, block_type, value)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bookmarks (
                id SERIAL PRIMARY KEY,
                user_id INT REFERENCES users(id),
                news_id INT REFERENCES news(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (user_id, news_id)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS invites (
                id SERIAL PRIMARY KEY,
                inviter_id INT REFERENCES users(id),
                invite_code TEXT UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                accepted_at TIMESTAMP
            );
        """)
        await conn.execute("ALTER TABLE invites ADD COLUMN IF NOT EXISTS inviter_id INT REFERENCES users(id);")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS admin_actions (
                id SERIAL PRIMARY KEY,
                admin_user_id INT,
                action_type TEXT NOT NULL,
                target_id INT,
                details JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS source_stats (
                id SERIAL PRIMARY KEY,
                source_id INT REFERENCES sources(id) UNIQUE,
                publication_count INT DEFAULT 0,
                avg_rating REAL DEFAULT 0.0,
                report_count INT DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Створення або перестворення індексів
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_news_published_expires_moderation ON news (published_at DESC, expires_at, moderation_status);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_blocks_user_type_value ON blocks (user_id, block_type, value);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_bookmarks_user_id ON bookmarks (user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_stats_user_id ON user_stats (user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_news_id ON comments (news_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_news_views_user_news_id ON user_news_views (user_id, news_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_reports_user_id_target_id ON reports (user_id, target_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_feedback_user_id ON feedback (user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_invites_inviter_id ON invites (inviter_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_actions_admin_user_id ON admin_actions (admin_user_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_source_stats_source_id ON source_stats (source_id);")

        logger.info("Таблиці перевірено/створено.")

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
                # Ensure that username, first_name, last_name are not None before passing to DB
                username = getattr(tg_user, 'username', None)
                first_name = getattr(tg_user, 'first_name', None)
                last_name = getattr(tg_user, 'last_name', None)
                language_code = getattr(tg_user, 'language_code', 'uk')

                await cur.execute(
                    """INSERT INTO users (id, username, first_name, last_name, is_admin, created_at, last_active, language)
                    VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s)""",
                    (tg_user.id, username, first_name, last_name, is_admin, language_code)
                )
                await cur.execute("INSERT INTO user_stats (user_id, last_active) VALUES (%s, CURRENT_TIMESTAMP) ON CONFLICT (user_id) DO NOTHING", (tg_user.id,))
                new_user = User(id=tg_user.id, username=username, first_name=first_name,
                                last_name=last_name, is_admin=is_admin, language=language_code)
                logger.info(f"Новий користувач: {new_user.username or new_user.first_name} (ID: {new_user.id})")
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
                news.ai_summary, news.ai_classified_topics, news.moderation_status, news.expires_at)
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
                (user_id, json.dumps(filters)) # Використовуємо json.dumps для JSONB
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

async def update_user_language(user_id: int, lang_code: str):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("UPDATE users SET language = %s WHERE id = %s", (lang_code, user_id))

async def make_gemini_request_with_history(messages: List[Dict[str, Any]]) -> str:
    if not GEMINI_API_KEY: return "Функції AI недоступні. GEMINI_API_KEY не встановлено."
    headers = {"Content-Type": "application/json"}
    params = {"key": GEMINI_API_KEY}
    data = {"contents": messages}
    async with ClientSession() as session:
        try:
            async with session.post("https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent", params=params, headers=headers, json=data) as response:
                if response.status == 200:
                    res_json = await response.json()
                    if 'candidates' in res_json and res_json['candidates']:
                        first_candidate = res_json['candidates'][0]
                        if 'content' in first_candidate and 'parts' in first_candidate['content']:
                            for part in first_candidate['content']['parts']:
                                if 'text' in part: return part['text']
                    logger.warning(f"Відповідь Gemini відсутня: {res_json}")
                    return "Не вдалося отримати відповідь AI."
                else:
                    err_text = await response.text()
                    logger.error(f"Помилка API Gemini: {response.status} - {err_text}")
                    return f"Помилка AI: {response.status}. Спробуйте пізніше."
        except Exception as e:
            logger.error(f"Помилка мережі під час запиту Gemini: {e}")
            return "Помилка зв'язку з AI. Спробуйте пізніше."

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
    if response:
        return [t.strip() for t in response.split(',') if t.strip()]
    return None

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
    kb.add(InlineKeyboardButton(text="➕ Додати джерело", callback_data="add_source"))
    kb.add(InlineKeyboardButton(text="🧠 AI-функції (Новини)", callback_data="ai_news_functions_menu"))
    kb.add(InlineKeyboardButton(text="⚙️ Налаштування", callback_data="settings_menu"))
    kb.add(InlineKeyboardButton(text="❓ Допомога", callback_data="help_menu"))
    kb.add(InlineKeyboardButton(text="🤝 Допоможи продати", url=ANOTHER_BOT_CHANNEL_LINK_SELL))
    kb.add(InlineKeyboardButton(text="🛍️ Допоможи купити", url=ANOTHER_BOT_CHANNEL_LINK_BUY))
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
    kb.add(InlineKeyboardButton(text="🌐 Мова", callback_data="language_selection_menu")) # Додано кнопку "Мова"
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

def get_language_selection_keyboard():
    kb = InlineKeyboardBuilder()
    languages = {
        "uk": "Українська 🇺🇦", "en": "English 🇬🇧", "de": "Deutsch 🇩🇪", "fr": "Français 🇫🇷",
        "es": "Español 🇪🇸", "pl": "Polski 🇵🇱", "it": "Italiano 🇮🇹", "pt": "Português 🇵🇹",
        "zh": "中文 🇨🇳", "ja": "日本語 🇯🇵"
    }
    for code, name in languages.items():
        kb.button(text=name, callback_data=f"set_lang_{code}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="⬅️ Назад до налаштувань", callback_data="settings_menu"))
    return kb.as_markup()

def get_news_keyboard(news_id: int, current_index: int, total_count: int):
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
    ]
    # Додаємо кнопки навігації "Назад" та "Далі"
    nav_buttons = []
    if current_index > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"prev_news"))
    if current_index < total_count - 1:
        nav_buttons.append(InlineKeyboardButton(text="➡️ Далі", callback_data=f"next_news"))
    
    if nav_buttons:
        buttons.append(nav_buttons)

    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def send_news_to_user(chat_id: int, news_id: int, current_index: int, total_count: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id, title, content, source_url, image_url, published_at, lang, ai_summary, ai_classified_topics FROM news WHERE id = %s", (news_id,))
            news_record = await cur.fetchone()
            if not news_record:
                await bot.send_message(chat_id, "Новина не знайдена.")
                return

            news_obj = News(id=news_record['id'], title=news_record['title'], content=news_record['content'],
                            source_url=news_record['source_url'], image_url=news_record['image_url'],
                            published_at=news_record['published_at'], lang=news_record['lang'],
                            ai_summary=news_record['ai_summary'], ai_classified_topics=news_record['ai_classified_topics'])

            message_text = (
                f"<b>{news_obj.title}</b>\n\n"
                f"{news_obj.content[:1000]}...\n\n"
                f"<i>Опубліковано: {news_obj.published_at.strftime('%d.%m.%Y %H:%M')}</i>\n"
                f"<i>Новина {current_index + 1} з {total_count}</i>"
            )
            
            if news_obj.source_url: message_text += f"\n\n🔗 {hlink('Читати джерело', news_obj.source_url)}"
            if news_obj.image_url: message_text += f"\n\n[Зображення новини]({news_obj.image_url})"

            reply_markup = get_news_keyboard(news_obj.id, current_index, total_count) # Передаємо індекс та загальну кількість
            
            msg = await bot.send_message(chat_id, message_text, reply_markup=reply_markup, disable_web_page_preview=False)
            
            # Correct way to get FSM context outside of a handler
            state_context = FSMContext(storage=dp.storage, key=types.Chat(id=chat_id).model_copy(deep=True), bot=bot)
            await state_context.update_data(last_message_id=msg.message_id)
            
            await mark_news_as_viewed(chat_id, news_id)
            if news_obj.ai_classified_topics: await update_user_viewed_topics(chat_id, news_obj.ai_classified_topics)

@router.message(CommandStart())
@router.message(Command("begin")) # Додано команду /begin
async def command_begin_handler(message: Message, state: FSMContext) -> None:
    logger.info(f"Команда /begin (або /start) отримана та обробляється для користувача: {message.from_user.id} ({message.from_user.full_name})")
    await state.clear()
    await create_or_update_user(message.from_user)
    await message.answer(f"Привіт, {hbold(message.from_user.full_name)}! 👋\n\nЯ ваш особистий новинний помічник з AI-функціями. Оберіть дію:", reply_markup=get_main_menu_keyboard())

@router.message(Command("menu"))
async def command_menu_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Оберіть дію:", reply_markup=get_main_menu_keyboard())

@router.message(Command("cancel"))
@router.message(StateFilter(
    AddSource.waiting_for_source_link, AddSource.waiting_for_source_name, AddSource.waiting_for_source_type,
    AIAssistant.waiting_for_question, AIAssistant.waiting_for_news_id_for_question,
    AIAssistant.waiting_for_term_to_explain, AIAssistant.waiting_for_fact_to_check,
    AIAssistant.waiting_for_audience_summary_type, AIAssistant.waiting_for_what_if_query,
    AIAssistant.waiting_for_youtube_interview_url, FilterSetup.waiting_for_source_selection,
    LanguageSelection.waiting_for_language
))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        await message.answer("Немає активних дій для скасування.")
        return
    await state.clear()
    await message.answer("Дію скасовано. Оберіть наступну дію:", reply_markup=get_main_menu_keyboard())

@router.message(Command("myprofile"))
async def handle_my_profile_command(message: Message):
    user_id = message.from_user.id
    user_record = await get_user(user_id)
    if not user_record:
        await message.answer("Ваш профіль не знайдено. Спробуйте /begin.") # Оновлено
        return
    username = user_record.username or user_record.first_name
    is_admin_str = "Так" if user_record.is_admin else "Ні"
    created_at_str = user_record.created_at.strftime("%d.%m.%Y %H:%M")
    profile_text = (
        f"👤 <b>Ваш Профіль:</b>\n"
        f"Ім'я: {username}\n"
        f"ID: <code>{user_id}</code>\n"
        f"Зареєстрований: {created_at_str}\n"
        f"Адмін: {is_admin_str}\n"
        f"Автосповіщення: {'Увімкнено' if user_record.auto_notifications else 'Вимкнено'}\n"
        f"Частота дайджестів: {user_record.digest_frequency}\n\n"
        f"<i>Цей бот призначений для новин та AI-функцій. Функціонал продажу/купівлі доступний у каналах:</i>\n"
        f"<i>Продати: {ANOTHER_BOT_CHANNEL_LINK_SELL}</i>\n"
        f"<i>Купити: {ANOTHER_BOT_CHANNEL_LINK_BUY}</i>"
    )
    await message.answer(profile_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

@router.callback_query(F.data == "main_menu")
async def process_main_menu_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Оберіть дію:", reply_markup=get_main_menu_keyboard())
    await callback.answer()

@router.callback_query(F.data == "ai_news_functions_menu")
async def process_ai_news_functions_menu(callback: CallbackQuery):
    await callback.message.edit_text("🧠 *AI-функції для новин:*\nОберіть бажану функцію:", reply_markup=get_ai_news_functions_menu(), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

@router.callback_query(F.data == "settings_menu")
async def process_settings_menu(callback: CallbackQuery):
    user = await get_user(callback.from_user.id)
    if not user:
        await callback.message.answer("Будь ласка, почніть з /begin.") # Оновлено
        await callback.answer()
        return
    
    toggle_btn_text = "🔔 Вимкнути автосповіщення" if user.auto_notifications else "🔕 Увімкнути автосповіщення"
    
    kb = InlineKeyboardBuilder()
    kb.add(InlineKeyboardButton(text="🔍 Фільтри новин", callback_data="news_filters_menu"))
    kb.add(InlineKeyboardButton(text=toggle_btn_text, callback_data="toggle_auto_notifications"))
    kb.add(InlineKeyboardButton(text="🌐 Мова", callback_data="language_selection_menu")) # Додано кнопку "Мова"
    kb.add(InlineKeyboardButton(text="⬅️ Назад до головного", callback_data="main_menu"))
    kb.adjust(1)
    await callback.message.edit_text("⚙️ *Налаштування:*", reply_markup=kb.as_markup(), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

@router.callback_query(F.data == "news_filters_menu")
async def process_news_filters_menu(callback: CallbackQuery):
    await callback.message.edit_text("🔍 *Фільтри новин:*\nОберіть дію:", reply_markup=get_news_filters_menu_keyboard(), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

@router.callback_query(F.data == "language_selection_menu")
async def process_language_selection_menu(callback: CallbackQuery, state: FSMContext):
    await state.set_state(LanguageSelection.waiting_for_language)
    await callback.message.edit_text("🌐 *Оберіть мову для перекладу новин:*\n(Це вплине на мову перекладу AI-резюме та інших функцій, якщо мова новини відрізняється)", reply_markup=get_language_selection_keyboard(), parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

@router.callback_query(LanguageSelection.waiting_for_language, F.data.startswith("set_lang_"))
async def process_set_language(callback: CallbackQuery, state: FSMContext):
    lang_code = callback.data.split('_')[2]
    user_id = callback.from_user.id
    await update_user_language(user_id, lang_code)
    await callback.message.edit_text(f"✅ Мову встановлено на: <b>{lang_code.upper()}</b>.", parse_mode=ParseMode.HTML)
    await state.clear()
    await callback.answer()
    await callback.message.answer("Оберіть наступну дію:", reply_markup=get_settings_menu_keyboard())

@router.callback_query(F.data == "toggle_auto_notifications")
async def toggle_auto_notifications(callback: CallbackQuery):
    user_id = callback.from_user.id
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT auto_notifications FROM users WHERE id = %s", (user_id,))
            user_record = await cur.fetchone()
            if not user_record:
                await callback.message.answer("Користувача не знайдено.")
                await callback.answer()
                return
            new_status = not user_record['auto_notifications']
            await cur.execute("UPDATE users SET auto_notifications = %s WHERE id = %s", (new_status, user_id))
            
            status_text = "увімкнено" if new_status else "вимкнено"
            await callback.message.answer(f"🔔 Автоматичні сповіщення про новини {status_text}.")
            
            user = await get_user(user_id)
            toggle_btn_text = "🔔 Вимкнути автосповіщення" if user.auto_notifications else "🔕 Увімкнути автосповіщення"
            kb = InlineKeyboardBuilder()
            kb.add(InlineKeyboardButton(text="🔍 Фільтри новин", callback_data="news_filters_menu"))
            kb.add(InlineKeyboardButton(text=toggle_btn_text, callback_data="toggle_auto_notifications"))
            kb.add(InlineKeyboardButton(text="🌐 Мова", callback_data="language_selection_menu"))
            kb.add(InlineKeyboardButton(text="⬅️ Назад до головного", callback_data="main_menu"))
            kb.adjust(1)
            await callback.message.edit_reply_markup(reply_markup=kb.as_markup())
    await callback.answer()

@router.callback_query(F.data == "set_news_sources_filter")
async def set_news_sources_filter(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    sources = await get_sources()
    if not sources:
        await callback.message.answer("Наразі немає доступних джерел для вибору.")
        await callback.answer()
        return

    user_filters = await get_user_filters(user_id)
    selected_source_ids = user_filters.get('source_ids', [])

    kb = InlineKeyboardBuilder()
    for source in sources:
        is_selected = source['id'] in selected_source_ids
        kb.button(text=f"{'✅ ' if is_selected else ''}{source['name']}", callback_data=f"toggle_source_filter_{source['id']}")
    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="Зберегти та застосувати", callback_data="save_source_filters"))
    kb.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_filter_setup"))

    await callback.message.edit_text("Оберіть джерела, з яких ви хочете отримувати новини:", reply_markup=kb.as_markup())
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
    kb.row(InlineKeyboardButton(text="Зберегти та застосувати", callback_data="save_source_filters"))
    kb.row(InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_filter_setup"))
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
        await callback.message.edit_text(f"Ваші фільтри джерел збережено: {', '.join(selected_names)}.\nВи можете переглянути новини за допомогою /my_news.")
    else:
        await callback.message.edit_text("Ви не обрали жодного джерела. Новини будуть відображатися без фільтрації за джерелами.")
    await state.clear()
    await callback.answer("Фільтри збережено!")

@router.callback_query(FilterSetup.waiting_for_source_selection, F.data == "cancel_filter_setup")
async def cancel_filter_setup(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Налаштування фільтрів скасовано.", reply_markup=get_settings_menu_keyboard())
    await callback.answer()

@router.callback_query(F.data == "reset_all_filters")
async def reset_all_filters(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    await update_user_filters(user_id, {})
    await callback.message.edit_text("✅ Всі ваші фільтри новин були скинуті.", reply_markup=get_news_filters_menu_keyboard())
    await callback.answer()

@router.callback_query(F.data == "news_from_youtube_interview")
async def handle_news_from_youtube_interview(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AIAssistant.waiting_for_youtube_interview_url)
    await callback.message.edit_text("🗞️ Надішліть посилання на YouTube-відео. (AI імітуватиме аналіз)", parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

@router.message(AIAssistant.waiting_for_youtube_interview_url, F.text.regexp(r"(https?://)?(www\.)?(youtube|youtu|m\.youtube)\.(com|be)/(watch\?v=|embed/|v/|)([\w-]{11})(?:\S+)?"))
async def process_youtube_interview_url(message: Message, state: FSMContext):
    youtube_url = message.text
    await message.answer("⏳ Аналізую інтерв'ю та генерую новину...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    mock_content_prompt = f"Уяви, що ти переглянув YouTube-інтерв'ю за посиланням: {youtube_url}. Сформуй короткий уявний зміст цього інтерв'ю, щоб я міг створити новину. Включи гіпотетичні основні теми та ключові цитати. Зміст має бути реалістичним для генерації новини. До 500 слів, тільки зміст. Українською."
    simulated_content = await make_gemini_request_with_history([{"role": "user", "parts": [{"text": mock_content_prompt}]}])
    if not simulated_content or "Не вдалося отримати відповідь від AI." in simulated_content:
        await message.answer("❌ Не вдалося отримати зміст інтерв'ю для аналізу. Спробуйте інше посилання.")
        await state.clear()
        return
    generated_news_text = await ai_generate_news_from_youtube_interview(simulated_content)
    if generated_news_text and "Не вдалося отримати відповідь від AI." not in generated_news_text:
        await message.answer(f"✅ **Ваша новина з YouTube-інтерв'ю:**\n\n{generated_news_text}", parse_mode=ParseMode.MARKDOWN)
    else:
        await message.answer("❌ Не вдалося створити новину з наданого інтерв'ю.")
    await state.clear()
    await message.answer("Оберіть наступну дію:", reply_markup=get_main_menu_keyboard())

@router.message(AIAssistant.waiting_for_youtube_interview_url)
async def process_youtube_interview_url_invalid(message: Message):
    await message.answer("Будь ласка, надішліть дійсне посилання на YouTube-відео, або введіть /cancel.")

@router.callback_query(F.data.startswith("ai_summary_"))
async def handle_ai_summary_callback(callback: CallbackQuery):
    news_id = int(callback.data.split('_')[2])
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT title, content FROM news WHERE id = %s", (news_id,))
            news_item = await cur.fetchone()
            if not news_item:
                await callback.message.answer("❌ Новину не знайдено.")
                await callback.answer()
                return
            await callback.message.answer("⏳ Генерую резюме за допомогою AI...")
            await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            summary = await ai_summarize_news(news_item['title'], news_item['content'])
            if summary:
                await cur.execute("UPDATE news SET ai_summary = %s WHERE id = %s", (summary, news_id))
                await callback.message.answer(f"📝 <b>AI-резюме новини (ID: {news_id}):</b>\n\n{summary}")
            else:
                await callback.message.answer("❌ Не вдалося згенерувати резюме.")
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
                await callback.message.answer("❌ Новину не знайдено.")
                await callback.answer()
                return
            
            user_lang_record = await get_user(callback.from_user.id)
            user_target_lang = user_lang_record.language if user_lang_record else 'uk'

            # Визначаємо мову перекладу: якщо мова новини співпадає з мовою користувача,
            # перекладаємо на англійську, інакше - на мову користувача.
            target_lang = 'en' if news_item['lang'] == user_target_lang else user_target_lang
            
            await callback.message.answer(f"⏳ Перекладаю новину на {target_lang.upper()} за допомогою AI...")
            await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            translated_title = await ai_translate_news(news_item['title'], target_lang)
            translated_content = await ai_translate_news(news_item['content'], target_lang)
            if translated_title and translated_content:
                await callback.message.answer(
                    f"🌐 <b>Переклад новини (ID: {news_id}) на {target_lang.upper()}:</b>\n\n"
                    f"<b>{translated_title}</b>\n\n{translated_content}"
                )
            else:
                await callback.message.answer("❌ Не вдалося перекласти новину.")
    await callback.answer()

@router.callback_query(F.data.startswith("ask_news_ai_"))
async def handle_ask_news_ai_callback(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[3])
    await state.update_data(waiting_for_news_id_for_question=news_id)
    await state.set_state(AIAssistant.waiting_for_question)
    await callback.message.answer("❓ Задайте ваше питання про новину.")
    await callback.answer()

@router.message(AIAssistant.waiting_for_question, F.text)
async def process_news_question(message: Message, state: FSMContext):
    data = await state.get_data()
    news_id = data.get('waiting_for_news_id_for_question')
    question = message.text
    if not news_id:
        await message.answer("Контекст новини втрачено. Спробуйте ще раз через /my_news.")
        await state.clear()
        return
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT title, content, lang FROM news WHERE id = %s", (news_id,))
            news_item_data = await cur.fetchone()
            if not news_item_data:
                await message.answer("Новину не знайдено.")
                await state.clear()
                return
            news_item = News(id=news_id, title=news_item_data['title'], content=news_item_data['content'],
                             lang=news_item_data['lang'], published_at=datetime.now())
            chat_history = data.get('ask_news_ai_history', [])
            chat_history.append({"role": "user", "parts": [{"text": question}]})
            await message.answer("⏳ Обробляю ваше питання за допомогою AI...")
            await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
            ai_response = await ai_answer_news_question(news_item, question, chat_history)
            if ai_response:
                await message.answer(f"<b>AI відповідає:</b>\n\n{ai_response}")
                chat_history.append({"role": "model", "parts": [{"text": ai_response}]})
                await state.update_data(ask_news_ai_history=chat_history)
            else:
                await message.answer("❌ Не вдалося відповісти на ваше питання.")
    await message.answer("Продовжуйте ставити питання або введіть /cancel для завершення діалогу.")

@router.callback_query(F.data.startswith("extract_entities_"))
async def handle_extract_entities_callback(callback: CallbackQuery):
    news_id = int(callback.data.split('_')[2])
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT content FROM news WHERE id = %s", (news_id,))
            news_item = await cur.fetchone()
            if not news_item:
                await callback.message.answer("❌ Новину не знайдено.")
                await callback.answer()
                return
            await callback.message.answer("⏳ Витягую ключові сутності за допомогою AI...")
            await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            entities = await ai_extract_entities(news_item['content'])
            if entities:
                await callback.message.answer(f"🧑‍🤝‍🧑 <b>Ключові особи/сутності в новині (ID: {news_id}):</b>\n\n{entities}")
            else:
                await callback.message.answer("❌ Не вдалося витягнути сутності.")
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
                await callback.message.answer("❌ Новину не знайдено.")
                await callback.answer()
                return
            topics = news_item_record['ai_classified_topics']
            if not topics:
                await callback.message.answer("⏳ Класифікую новину за темами за допомогою AI...")
                await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
                topics = await ai_classify_topics(news_item_record['content'])
                if topics: 
                    await cur.execute("UPDATE news SET ai_classified_topics = %s::jsonb WHERE id = %s", (json.dumps(topics), news_id))
                else: 
                    topics = ["Не вдалося визначити теми."]
            if topics:
                topics_str = ", ".join(topics)
                await callback.message.answer(f"🏷️ <b>Класифікація за темами для новини (ID: {news_id}):</b>\n\n{topics_str}")
            else:
                await callback.message.answer("❌ Не вдалося класифікувати новину за темами.")
    await callback.answer()

@router.callback_query(F.data.startswith("explain_term_"))
async def handle_explain_term_callback(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[2])
    await state.update_data(waiting_for_news_id_for_question=news_id)
    await state.set_state(AIAssistant.waiting_for_term_to_explain)
    await callback.message.answer("❓ Введіть термін, який ви хочете, щоб AI пояснив у контексті цієї новини.")
    await callback.answer()

@router.message(AIAssistant.waiting_for_term_to_explain, F.text)
async def process_explain_term_query(message: Message, state: FSMContext):
    data = await state.get_data()
    news_id = data.get('waiting_for_news_id_for_question')
    term = message.text.strip()
    if not news_id:
        await message.answer("Контекст новини втрачено. Спробуйте ще раз через /my_news.")
        await state.clear()
        return
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT content FROM news WHERE id = %s", (news_id,))
            news_item = await cur.fetchone()
            if not news_item:
                await message.answer("Новину не знайдено.")
                await state.clear()
                return
            await message.answer(f"⏳ Пояснюю термін '{term}' за допомогою AI...")
            await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
            explanation = await ai_explain_term(term, news_item['content'])
            if explanation:
                await message.answer(f"❓ <b>Пояснення терміну '{term}' (Новина ID: {news_id}):</b>\n\n{explanation}")
            else:
                await message.answer("❌ Не вдалося пояснити термін.")
    await state.clear()

@router.callback_query(F.data.startswith("fact_check_news_"))
async def handle_fact_check_news_callback(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[3])
    await state.update_data(fact_check_news_id=news_id)
    await state.set_state(AIAssistant.waiting_for_fact_to_check)
    await callback.message.answer("✅ Введіть факт, який ви хочете перевірити в контексті цієї новини.")
    await callback.answer()

@router.message(AIAssistant.waiting_for_fact_to_check, F.text)
async def process_fact_to_check(message: Message, state: FSMContext):
    data = await state.get_data()
    news_id = data.get('fact_check_news_id')
    fact_to_check = message.text.strip()
    if not news_id:
        await message.answer("Контекст новини втрачено. Спробуйте ще раз.")
        await state.clear()
        return
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT content FROM news WHERE id = %s", (news_id,))
            news_item = await cur.fetchone()
            if not news_item:
                await message.answer("Новину не знайдено.")
                await state.clear()
                return
            await message.answer(f"⏳ Перевіряю факт: '{fact_to_check}' за допомогою AI...")
            await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
            fact_check_result = await ai_fact_check(fact_to_check, news_item['content'])
            if fact_check_result:
                await message.answer(f"✅ <b>Перевірка факту для новини (ID: {news_id}):</b>\n\n{fact_check_result}")
            else:
                await message.answer("❌ Не вдалося перевірити факт.")
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
                await callback.message.answer("❌ Новину для аналізу не знайдено.")
                await callback.answer()
                return
            main_news_obj = News(id=main_news_record['id'], title=main_news_record['title'], content=main_news_record['content'], lang=main_news_record['lang'], published_at=main_news_record['published_at'], ai_summary=main_news_record['ai_summary'], ai_classified_topics=main_news_record['ai_classified_topics'])
            await callback.message.answer("⏳ Аналізую тренд настроїв за допомогою AI...")
            await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            related_news_items = []
            if main_news_obj.ai_classified_topics:
                await cur.execute(f"""
                    SELECT id, title, content, ai_summary, lang, published_at
                    FROM news
                    WHERE id != %s
                    AND moderation_status = 'approved'
                    AND expires_at > NOW()
                    AND published_at >= NOW() - INTERVAL '30 days'
                    AND ai_classified_topics ?| %s
                    ORDER BY published_at ASC LIMIT 5
                """, (news_id, main_news_obj.ai_classified_topics))
                related_news_records = await cur.fetchall()
                related_news_items = [News(id=r['id'], title=r['title'], content=r['content'], lang=r['lang'], published_at=r['published_at'], ai_summary=r['ai_summary']) for r in related_news_records]
            ai_sentiment_trend = await ai_analyze_sentiment_trend(main_news_obj, related_news_items)
            await callback.message.answer(f"📊 <b>Аналіз тренду настроїв для новини (ID: {news_id}):</b>\n\n{ai_sentiment_trend}", parse_mode=ParseMode.HTML)
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
                await callback.message.answer("❌ Новину для аналізу не знайдено.")
                await callback.answer()
                return
            await callback.message.answer("⏳ Аналізую новину на наявність упереджень за допомогою AI...")
            await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            ai_bias_analysis = await ai_detect_bias_in_news(news_item['title'], news_item['content'], news_item['ai_summary'])
            await callback.message.answer(f"🔍 <b>Аналіз на упередженість для новини (ID: {news_id}):</b>\n\n{ai_bias_analysis}", parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data.startswith("audience_summary_"))
async def handle_audience_summary_callback(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[2])
    await state.update_data(audience_summary_news_id=news_id)
    await state.set_state(AIAssistant.waiting_for_audience_summary_type)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Дитина", callback_data="audience_type_child"), InlineKeyboardButton(text="Експерт", callback_data="audience_type_expert")],
        [InlineKeyboardButton(text="Політик", callback_data="audience_type_politician"), InlineKeyboardButton(text="Технолог", callback_data="audience_type_technologist")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_audience_summary")]
    ])
    await callback.message.edit_text("📝 Для якої аудиторії ви хочете отримати резюме цієї новини?", reply_markup=keyboard)
    await callback.answer()

@router.callback_query(AIAssistant.waiting_for_audience_summary_type, F.data.startswith("audience_type_"))
async def process_audience_type_selection(callback: CallbackQuery, state: FSMContext):
    audience_type_key = callback.data.split('_')[2]
    audience_map = {'child': 'дитини', 'expert': 'експерта', 'politician': 'політика', 'technologist': 'технолога'}
    selected_audience = audience_map.get(audience_type_key, 'загальної аудиторії')
    data = await state.get_data()
    news_id = data.get('audience_summary_news_id')
    if not news_id:
        await callback.message.answer("Контекст новини втрачено. Спробуйте знову.")
        await state.clear()
        await callback.answer()
        return
    await callback.message.edit_text(f"⏳ Генерую резюме для аудиторії: <b>{selected_audience}</b>...", parse_mode=ParseMode.HTML)
    await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT title, content, ai_summary FROM news WHERE id = %s", (news_id,))
            news_item = await cur.fetchone()
            if not news_item:
                await callback.message.answer("❌ Новину для резюме не знайдено.")
                await state.clear()
                await callback.answer()
                return
            ai_summary_for_audience = await ai_summarize_for_audience(news_item['title'], news_item['content'], news_item['ai_summary'], selected_audience)
            await callback.message.answer(f"📝 <b>Резюме для аудиторії: {selected_audience} (Новина ID: {news_id}):</b>\n\n{ai_summary_for_audience}", parse_mode=ParseMode.HTML)
    await state.clear()
    await callback.answer()

@router.callback_query(AIAssistant.waiting_for_audience_summary_type, F.data == "cancel_audience_summary")
async def cancel_audience_summary_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("✅ Генерацію резюме для аудиторії скасовано.")
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
                await callback.message.answer("❌ Новину для аналізу не знайдено.")
                await callback.answer()
                return
            await callback.message.answer("⏳ Шукаю історичні аналоги за допомогою AI...")
            await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            ai_historical_analogues = await ai_find_historical_analogues(news_item['title'], news_item['content'], news_item['ai_summary'])
            await callback.message.answer(f"📜 <b>Історичні аналоги для новини (ID: {news_id}):</b>\n\n{ai_historical_analogues}", parse_mode=ParseMode.HTML)
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
                await callback.message.answer("❌ Новину для аналізу впливу не знайдено.")
                await callback.answer()
                return
            await callback.message.answer("⏳ Аналізую потенційний вплив новини за допомогою AI...")
            await callback.bot.send_chat_action(chat_id=callback.message.chat.id, action=ChatAction.TYPING)
            ai_impact_analysis = await ai_analyze_impact(news_item['title'], news_item['content'], news_item['ai_summary'])
            await callback.message.answer(f"💥 <b>Аналіз впливу новини (ID: {news_id}):</b>\n\n{ai_impact_analysis}", parse_mode=ParseMode.HTML)
    await callback.answer()

@router.callback_query(F.data.startswith("what_if_scenario_"))
async def handle_what_if_scenario_callback(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[3])
    await state.update_data(what_if_news_id=news_id)
    await state.set_state(AIAssistant.waiting_for_what_if_query)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"🤔 Введіть ваше питання у форматі 'Що якби...' для новини (ID: {news_id}). Наприклад: 'Що якби зустріч завершилася без угоди?'")
    await callback.answer()

@router.message(AIAssistant.waiting_for_what_if_query, F.text)
async def process_what_if_query(message: Message, state: FSMContext):
    what_if_question = message.text.strip()
    if not what_if_question:
        await message.answer("Будь ласка, введіть ваше питання у форматі 'Що якби...'.")
        return
    data = await state.get_data()
    news_id_for_context = data.get('what_if_news_id')
    if not news_id_for_context:
        await message.answer("Контекст новини втрачено. Спробуйте ще раз через /my_news.")
        await state.clear()
        return
    await message.answer("⏳ Генерую сценарій 'Що якби...' за допомогою AI...")
    await message.bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT title, content, ai_summary FROM news WHERE id = %s", (news_id_for_context,))
            news_item = await cur.fetchone()
            if not news_item:
                await message.answer("❌ Новину не знайдено. Спробуйте з іншою новиною.")
                await state.clear()
                return
            ai_what_if_scenario = await ai_generate_what_if_scenario(news_item['title'], news_item['content'], news_item['ai_summary'], what_if_question)
            await message.answer(f"🤔 <b>Сценарій 'Що якби...' для новини (ID: {news_id_for_context}):</b>\n\n{ai_what_if_scenario}", parse_mode=ParseMode.HTML)
    await state.clear()

# --- New handlers for adding sources ---
@router.callback_query(F.data == "add_source")
async def add_source_command(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddSource.waiting_for_source_link)
    await callback.message.answer("Будь ласка, надішліть посилання на джерело (наприклад, RSS-стрічку, Telegram-канал, веб-сайт). Або введіть /cancel для скасування.")
    await callback.answer()

@router.message(AddSource.waiting_for_source_link, F.text.regexp(r"https?://[^\s]+"))
async def process_source_link(message: Message, state: FSMContext):
    source_link = message.text
    await state.update_data(source_link=source_link)
    await state.set_state(AddSource.waiting_for_source_name)
    await message.answer("Будь ласка, введіть назву для цього джерела (наприклад, 'BBC News', 'Мій Telegram Канал'). Або введіть /cancel для скасування.")

@router.message(AddSource.waiting_for_source_link)
async def process_source_link_invalid(message: Message):
    await message.answer("Будь ласка, надішліть дійсне посилання, або введіть /cancel.")

@router.message(AddSource.waiting_for_source_name, F.text)
async def process_source_name(message: Message, state: FSMContext):
    source_name = message.text.strip()
    if not source_name:
        await message.answer("Назва джерела не може бути порожньою. Будь ласка, введіть назву. Або введіть /cancel для скасування.")
        return
    await state.update_data(source_name=source_name)
    await state.set_state(AddSource.waiting_for_source_type)
    source_type_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Веб-сайт", callback_data="source_type_web")],
        [InlineKeyboardButton(text="Telegram-канал", callback_data="source_type_telegram")],
        [InlineKeyboardButton(text="RSS-стрічка", callback_data="source_type_rss")],
        [InlineKeyboardButton(text="Twitter", callback_data="source_type_twitter")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="cancel_add_source")] # Додано кнопку скасування
    ])
    await message.answer("Оберіть тип джерела:", reply_markup=source_type_keyboard)

@router.callback_query(AddSource.waiting_for_source_type, F.data.startswith("source_type_"))
async def process_source_type(callback: CallbackQuery, state: FSMContext):
    source_type = callback.data.split('_')[2]
    data = await state.get_data()
    source_link = data['source_link']
    source_name = data['source_name']

    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    """INSERT INTO sources (name, link, type, status) VALUES (%s, %s, %s, 'active') RETURNING id""",
                    (source_name, source_link, source_type)
                )
                new_source_id = (await cur.fetchone())['id']
                await callback.message.edit_text(f"✅ Джерело '{source_name}' (ID: {new_source_id}) успішно додано! Наразі бот автоматично генерує мок-новини, використовуючи ваші додані джерела для демонстрації. Для повноцінного парсингу потрібна додаткова розробка.")
                logger.info(f"Нове джерело додано: {source_name} ({source_link}, Type: {source_type})")
            except psycopg.errors.UniqueViolation:
                await callback.message.edit_text("❌ Це джерело вже існує в базі даних.")
            except Exception as e:
                logger.error(f"Помилка при додаванні джерела: {e}")
                await callback.message.edit_text("❌ Сталася помилка при додаванні джерела. Спробуйте пізніше.")
    await state.clear()
    await callback.answer()
    await callback.message.answer("Оберіть наступну дію:", reply_markup=get_main_menu_keyboard())

@router.callback_query(AddSource.waiting_for_source_type, F.data == "cancel_add_source")
async def cancel_add_source_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("✅ Додавання джерела скасовано.", reply_markup=get_main_menu_keyboard())
    await state.clear()
    await callback.answer()

@router.message(AddSource.waiting_for_source_name)
@router.message(AddSource.waiting_for_source_type)
async def process_source_type_invalid(message: Message):
    await message.answer("Будь ласка, оберіть тип джерела за допомогою кнопок, або введіть /cancel.")

# --- End new handlers for adding sources ---

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
                await cur.execute("SELECT link FROM sources WHERE id = ANY(%s)", (source_ids,))
                source_links_data = await cur.fetchall()
                source_links = [s['link'] for s in source_links_data]
                if source_links:
                    query += " AND source_url = ANY(%s)"
                    params.append(source_links)
                else:
                    await callback.message.answer("Наразі немає доступних новин за вашими фільтрами. Спробуйте змінити фільтри або зайдіть пізніше.")
                    await callback.answer()
                    return

            query += " ORDER BY published_at DESC"
            
            if params:
                await cur.execute(query, (params[0],) if len(params) == 1 else tuple(params))
            else:
                await cur.execute(query)

            news_records = await cur.fetchall()

            if not news_records:
                await callback.message.answer("Наразі немає доступних новин за вашими фільтрами. Спробуйте змінити фільтри або зайдіть пізніше.")
                await callback.answer()
                return
            news_ids = [r['id'] for r in news_records]
            await state.update_data(news_ids=news_ids, news_index=0)
            await state.set_state(NewsBrowse.Browse_news)
            current_news_id = news_ids[0]
            await callback.message.edit_text("Завантажую новину...")
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
        await callback.answer("Це остання новина.", show_alert=True)
    await callback.answer()

@router.callback_query(NewsBrowse.Browse_news, F.data == "prev_news") # Новий обробник для кнопки "Назад"
async def process_prev_news(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    news_ids = data.get('news_ids', [])
    current_index = data.get('news_index', 0)
    if current_index > 0:
        new_index = current_index - 1
        await state.update_data(news_index=new_index)
        await callback.message.delete()
        await send_news_to_user(callback.message.chat.id, news_ids[new_index], new_index, len(news_ids))
    else:
        await callback.answer("Це перша новина.", show_alert=True)
    await callback.answer()


@router.callback_query(F.data == "help_menu")
async def handle_help_menu(callback: CallbackQuery):
    help_text = (
        "<b>Доступні команди:</b>\n"
        "/begin - Почати роботу з ботом / Головне меню\n" # Оновлено
        "/menu - Головне меню\n"
        "/cancel - Скасувати поточну дію\n"
        "/myprofile - Переглянути ваш профіль\n"
        "/my_news - Переглянути добірку новин\n"
        "/add_source - Додати нове джерело новин\n"
        "/setfiltersources - Налаштувати джерела новин\n"
        "/resetfilters - Скинути всі фільтри новин\n"
        "\n<b>AI-функції:</b>\n"
        "Доступні після вибору новини (кнопки під новиною).\n"
        "\n<b>Інтеграція з маркетплейсом:</b>\n"
        f"Кнопки 'Допоможи продати' та 'Допоможи купити' перенаправляють на канали:\n"
        f"Продати: {ANOTHER_BOT_CHANNEL_LINK_SELL}\n"
        f"Купити: {ANOTHER_BOT_CHANNEL_LINK_BUY}"
    )
    await callback.message.edit_text(help_text, parse_mode=ParseMode.HTML, reply_markup=get_main_menu_keyboard())
    await callback.answer()

async def news_repost_task():
    repost_interval = 300 # Збільшено інтервал до 300 секунд (5 хвилин)
    while True:
        try:
            pool = await get_db_pool()
            async with pool.connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    # Отримуємо всі активні джерела
                    await cur.execute("SELECT id, name, link, type FROM sources WHERE status = 'active'")
                    available_sources = await cur.fetchall()

                    selected_source = None
                    if available_sources:
                        selected_source = random.choice(available_sources)
                        mock_source_url = selected_source['link']
                        mock_source_name = selected_source['name']
                    else:
                        mock_source_url = "https://example.com/ai-news"
                        mock_source_name = "AI News (Default)"

                    mock_title = f"Оновлення новин AI {datetime.now().strftime('%H:%M:%S')} від {mock_source_name}"
                    mock_content = f"Це автоматично згенерована новина про останні події у світі AI та технологій. AI продовжує інтегруватися в повсякденне життя, змінюючи спосіб взаємодії людей з інформацією. Нові досягнення в машинному навчанні дозволяють створювати більш персоналізовані та адаптивні системи. Експерти прогнозують подальше зростання впливу AI на економіку та суспільство. Джерело: {mock_source_name}."
                    mock_image_url = "https://placehold.co/600x400/ADE8F4/000000?text=AI+News"
                    mock_lang = 'uk'

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
                logger.info(f"Автоматично репостнуто та схвалено новину: {new_news.id} - '{new_news.title}'")
                
                # Спробуємо опублікувати новину в канал
                if NEWS_CHANNEL_LINK:
                    try:
                        post_text = await ai_formulate_news_post(new_news.title, new_news.ai_summary or new_news.content, new_news.source_url)
                        
                        channel_identifier = NEWS_CHANNEL_LINK
                        if channel_identifier.startswith("https://t.me/"):
                            channel_identifier = "@" + channel_identifier.split('/')[-1]
                        elif not channel_identifier.startswith("@"):
                            channel_identifier = "@" + channel_identifier

                        await bot.send_message(chat_id=channel_identifier, text=post_text, disable_web_page_preview=False)
                        logger.info(f"Новину {new_news.id} опубліковано в канал {NEWS_CHANNEL_LINK}.")
                    except Exception as e:
                        logger.error(f"Помилка публікації новини в канал {NEWS_CHANNEL_LINK}: {e}")
            else:
                logger.info(f"Пропущено репост новини (нецікаво): '{mock_title}'")
        except Exception as e:
            logger.error(f"Помилка в завданні репосту новин: {e}")
        await asyncio.sleep(repost_interval)

async def news_digest_task():
    while True:
        now = datetime.now()
        next_run_time = datetime(now.year, now.month, now.day, 9, 0, 0) # Щодня о 9 ранку
        if now >= next_run_time: next_run_time += timedelta(days=1)
        
        sleep_seconds = (next_run_time - now).total_seconds()
        if sleep_seconds < 0: sleep_seconds = 0 # Не повинно відбуватися, якщо логіка для наступного дня коректна
        
        logger.info(f"Наступний запуск дайджесту через {sleep_seconds:.0f} секунд.")
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
                        user_filters = await get_user_filters(user_id)
                        source_ids = user_filters.get('source_ids', [])
                        
                        query = "SELECT id, title, content, source_url, image_url, published_at, ai_summary FROM news WHERE moderation_status = 'approved' AND expires_at > NOW()"
                        params = []
                        if source_ids:
                            await cur.execute("SELECT link FROM sources WHERE id = ANY(%s)", (source_ids,))
                            source_links_data = await cur.fetchall()
                            source_links = [s['link'] for s in source_links_data]
                            if source_links:
                                query += " AND source_url = ANY(%s)"
                                params.append(source_links)
                            else:
                                continue
                        
                        query += f" AND id NOT IN (SELECT news_id FROM user_news_views WHERE user_id = {user_id}) ORDER BY published_at DESC LIMIT 5"
                        
                        if params:
                            await cur.execute(query, (params[0],) if len(params) == 1 else tuple(params))
                        else:
                            await cur.execute(query)

                        news_items_data = await cur.fetchall()
                        
                        if news_items_data:
                            digest_text = f"📰 <b>Ваш щоденний дайджест новин ({now.strftime('%d.%m.%Y')}):</b>\n\n"
                            for news_rec in news_items_data:
                                news_obj = News(id=news_rec['id'], title=news_rec['title'], content=news_rec['content'],
                                                source_url=news_rec['source_url'], image_url=news_rec['image_url'],
                                                published_at=news_rec['published_at'], lang=user_lang, ai_summary=news_rec['ai_summary'])
                                
                                summary_to_use = news_obj.ai_summary or news_obj.content[:200] + "..."
                                digest_text += f"• <b>{news_obj.title}</b>\n{summary_to_use}\n"
                                if news_obj.source_url: digest_text += f"🔗 {hlink('Читати', news_obj.source_url)}\n\n"
                                
                                await mark_news_as_viewed(user_id, news_obj.id)
                            
                            try:
                                await bot.send_message(user_id, digest_text, disable_web_page_preview=True)
                                logger.info(f"Дайджест надіслано користувачу {user_id}.")
                            except Exception as e:
                                logger.error(f"Не вдалося надіслати дайджест користувачу {user_id}: {e}")
        except Exception as e:
            logger.error(f"Помилка в завданні дайджесту новин: {e}")

@app.on_event("startup")
async def startup_event():
    await get_db_pool()
    await create_tables()
    
    if WEBHOOK_URL and API_TOKEN:
        webhook_full_url = f"{WEBHOOK_URL.rstrip('/')}/telegram_webhook"
        logger.info(f"Спроба встановити вебхук на: {webhook_full_url}")
        try:
            await asyncio.sleep(5)
            await bot.set_webhook(url=webhook_full_url)
            logger.info(f"Вебхук успішно встановлено на {webhook_full_url}")
        except Exception as e:
            logger.error(f"Не вдалося встановити вебхук: {e}")
            raise
    else:
        logger.warning("WEBHOOK_URL або BOT_TOKEN не встановлено. Вебхук не буде налаштовано.")

    asyncio.create_task(news_repost_task())
    asyncio.create_task(news_digest_task())
    logger.info("Додаток FastAPI запущено.")

@app.on_event("shutdown")
async def shutdown_event():
    global db_pool
    if db_pool: await db_pool.close()
    if API_TOKEN:
        try:
            await bot.delete_webhook()
            logger.info("Вебхук видалено.")
        except Exception as e:
            logger.error(f"Не вдалося видалити вебхук: {e}")
    logger.info("Додаток FastAPI вимкнено.")

@app.get("/health")
async def health_check():
    logger.info("Виклик ендпоінту перевірки стану.")
    return {"status": "OK"}

@app.post("/telegram_webhook")
async def telegram_webhook(request: Request):
    logger.info("Отримано запит на /telegram_webhook")
    try:
        update = await request.json()
        logger.info(f"Отримано оновлення Telegram: {update}")
        await dp.feed_update(bot, types.Update.model_validate(update, context={"bot": bot}))
        logger.info("Успішно оброблено оновлення Telegram.")
    except Exception as e:
        logger.error(f"Помилка обробки вебхука Telegram: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}
    return {"ok": True}

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
            total_products = 0 
            total_transactions = 0
            total_reviews = 0 

            await cur.execute("SELECT COUNT(DISTINCT id) FROM users WHERE last_active >= NOW() - INTERVAL '7 days'")
            active_users_count = (await cur.fetchone())['count']
            return {
                "total_users": total_users,
                "total_news": total_news,
                "total_products": total_products,
                "total_transactions": total_transactions,
                "total_reviews": total_reviews,
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
                    params.append(json.dumps(v)) # Використовуємо json.dumps для JSONB
            if not set_clauses: raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Немає полів для оновлення.")
            params.append(news_id)
            await cur.execute(f"UPDATE news SET {', '.join(set_clauses)} WHERE id = %s RETURNING *", tuple(params))
            updated_rec = await cur.fetchone()
            if not updated_rec: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Новину не знайдено.")
            return News(**updated_rec).__dict__

@app.delete("/api/admin/news/{news_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_admin_news_api(news_id: int, api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("DELETE FROM news WHERE id = %s", (news_id,))
            if cur.rowcount == 0: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Новину не знайдено.")
            return

@app.post("/telegram_webhook")
async def telegram_webhook(request: Request):
    logger.info("Отримано запит на /telegram_webhook")
    try:
        update = await request.json()
        logger.info(f"Отримано оновлення Telegram: {update}")
        await dp.feed_update(bot, types.Update.model_validate(update, context={"bot": bot}))
        logger.info("Успішно оброблено оновлення Telegram.")
    except Exception as e:
        logger.error(f"Помилка обробки вебхука Telegram: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}
    return {"ok": True}

@router.message()
async def echo_handler(message: types.Message) -> None:
    await message.answer("Команду не зрозуміло. Скористайтеся /menu.")
