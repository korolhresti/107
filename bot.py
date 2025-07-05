import asyncio
import logging
from datetime import datetime, timedelta, timezone
import json
import os
from typing import List, Optional, Dict, Any, Union
import random
import io # Для роботи з аудіо в пам'яті

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
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from gtts import gTTS # Для генерації голосових повідомлень
from croniter import croniter

# Імпортуємо парсери безпосередньо, оскільки вони знаходяться в тій же директорії
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
ANOTHER_BOT_CHANNEL_LINK_SELL = "https://t.me/BigmoneycreateBot"
ANOTHER_BOT_CHANNEL_LINK_BUY = "https://t.me/+eZEMW4FMEWQxMjYy"
NEWS_CHANNEL_LINK = os.getenv("NEWS_CHANNEL_LINK", "https://t.me/newsone234")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Налаштування логування
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Ініціалізація FastAPI
app = FastAPI(title="Telegram AI News Bot API", version="1.0.0")

# Статичні файли для адмін-панелі
app.mount("/static", StaticFiles(directory="."), name="static")

# Залежність для перевірки API ключа адміністратора
api_key_header = APIKeyHeader(name="X-API-Key")

async def get_api_key(api_key: str = Depends(api_key_header)):
    if not ADMIN_API_KEY: raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="ADMIN_API_KEY не налаштовано.")
    if api_key is None or api_key != ADMIN_API_KEY: raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Недійсний або відсутній ключ API.")
    return api_key

# Ініціалізація бота та диспетчера
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router() # Додано Router
dp.include_router(router) # Включено Router до Dispatcher

# Пул з'єднань до БД
db_pool: Optional[AsyncConnectionPool] = None

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

# Моделі Pydantic для даних
from pydantic import BaseModel, HttpUrl

class News(BaseModel):
    id: Optional[int] = None
    source_id: Optional[int] = None # Зроблено Optional, оскільки може бути невідомим при створенні
    title: str
    content: str
    source_url: HttpUrl
    image_url: Optional[HttpUrl] = None
    ai_summary: Optional[str] = None
    ai_classified_topics: Optional[List[str]] = None
    published_at: datetime
    moderation_status: str = 'pending'
    expires_at: Optional[datetime] = None

class User(BaseModel):
    id: Optional[int] = None # Це ID з вашої БД, не Telegram ID
    telegram_id: int # Telegram ID користувача
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
    email: Optional[str] = None
    view_mode: Optional[str] = 'detailed'

class Source(BaseModel):
    id: Optional[int] = None
    user_id: Optional[int] = None
    source_name: str
    source_url: HttpUrl
    source_type: str
    status: str = 'active'
    added_at: Optional[datetime] = None # Включено added_at в модель Pydantic
    last_parsed: Optional[datetime] = None
    parse_frequency: str = 'hourly'


# Функції для взаємодії з БД
async def create_or_update_user(user_data: types.User):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            telegram_id = user_data.id
            username = user_data.username
            first_name = user_data.first_name
            last_name = user_data.last_name
            
            # Перевірка, чи існує користувач
            await cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
            user_record = await cur.fetchone()

            if user_record:
                # Оновлення існуючого користувача
                await cur.execute(
                    """
                    UPDATE users SET
                        username = %s,
                        first_name = %s,
                        last_name = %s,
                        last_active = CURRENT_TIMESTAMP
                    WHERE telegram_id = %s
                    RETURNING *;
                    """,
                    (username, first_name, last_name, telegram_id)
                )
                updated_user = await cur.fetchone()
                logger.info(f"Користувача оновлено: {updated_user['telegram_id']}")
                return User(**updated_user)
            else:
                # Створення нового користувача
                await cur.execute(
                    """
                    INSERT INTO users (telegram_id, username, first_name, last_name, created_at, last_active)
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    RETURNING *;
                    """,
                    (telegram_id, username, first_name, last_name)
                )
                new_user = await cur.fetchone()
                logger.info(f"Нового користувача створено: {new_user['telegram_id']}")
                return User(**new_user)

async def get_user_by_telegram_id(telegram_id: int) -> Optional[User]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
            user_record = await cur.fetchone()
            if user_record:
                return User(**user_record)
            return None

async def get_user_by_db_id(user_db_id: int) -> Optional[User]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM users WHERE id = %s", (user_db_id,))
            user_record = await cur.fetchone()
            if user_record:
                return User(**user_record)
            return None

async def add_news_to_db(news_data: Dict[str, Any]) -> Optional[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # Перевірка, чи існує джерело за URL
            # Змінено: використовуємо source_url та source_name з news_data
            await cur.execute("SELECT id FROM sources WHERE source_url = %s", (str(news_data['source_url']),))
            source_record = await cur.fetchone()
            source_id = None
            if source_record:
                source_id = source_record['id']
            else:
                # Якщо джерела немає, додаємо його
                # user_id тут може бути None, якщо новина додається автоматично планувальником
                user_id_for_source = news_data.get('user_id_for_source') # Додаємо це поле в news_data при парсингу
                
                # Визначаємо source_name з URL
                parsed_url = HttpUrl(news_data['source_url'])
                source_name = parsed_url.host if parsed_url.host else 'Невідоме джерело'

                await cur.execute(
                    """
                    INSERT INTO sources (user_id, source_name, source_url, source_type, added_at)
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (source_url) DO UPDATE SET
                        source_name = EXCLUDED.source_name,
                        source_type = EXCLUDED.source_type,
                        status = 'active', -- Активація, якщо було неактивним
                        last_parsed = NULL -- Скинути, щоб перепарсити
                    RETURNING id;
                    """,
                    (user_id_for_source, source_name, str(news_data['source_url']), news_data.get('source_type', 'web'))
                )
                source_id = await cur.fetchone()['id']
                logger.info(f"Нове джерело додано: {news_data['source_url']}")

            # Перевірка, чи новина вже існує
            await cur.execute("SELECT id FROM news WHERE source_url = %s", (str(news_data['source_url']),))
            news_exists = await cur.fetchone()
            if news_exists:
                logger.info(f"Новина з URL {news_data['source_url']} вже існує. Пропускаю.")
                return None

            # Визначаємо статус модерації: 'approved' для автоматично спарсених, 'pending' для доданих користувачем
            moderation_status = 'approved' if news_data.get('user_id_for_source') is None else 'pending'

            # Додавання новини
            await cur.execute(
                """
                INSERT INTO news (source_id, title, content, source_url, image_url, published_at, ai_summary, ai_classified_topics, moderation_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *;
                """,
                (
                    source_id,
                    news_data['title'],
                    news_data['content'],
                    str(news_data['source_url']),
                    str(news_data['image_url']) if news_data.get('image_url') else None,
                    news_data['published_at'],
                    news_data.get('ai_summary'),
                    json.dumps(news_data.get('ai_classified_topics')) if news_data.get('ai_classified_topics') else None,
                    moderation_status
                )
            )
            new_news = await cur.fetchone()
            logger.info(f"Новина додана: {new_news['title']}")
            return News(**new_news)

async def get_news_for_user(user_id: int, limit: int = 10, offset: int = 0) -> List[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # Отримуємо ID новин, які користувач вже бачив
            await cur.execute("SELECT news_id FROM user_news_views WHERE user_id = %s", (user_id,))
            viewed_news_ids = [row['news_id'] for row in await cur.fetchall()]

            # Отримуємо новини, які не були переглянуті користувачем, відсортовані за датою публікації
            query = """
            SELECT * FROM news
            WHERE id NOT IN (SELECT news_id FROM user_news_views WHERE user_id = %s)
            AND moderation_status = 'approved' -- Тільки схвалені новини
            AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP) -- Не прострочені
            ORDER BY published_at DESC
            LIMIT %s OFFSET %s;
            """
            await cur.execute(query, (user_id, limit, offset))
            news_records = await cur.fetchall()
            return [News(**record) for record in news_records]

async def mark_news_as_viewed(user_id: int, news_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO user_news_views (user_id, news_id, viewed_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id, news_id) DO NOTHING;
                """,
                (user_id, news_id)
            )
            await conn.commit()

async def get_news_by_id(news_id: int) -> Optional[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM news WHERE id = %s", (news_id,))
            news_record = await cur.fetchone()
            if news_record:
                return News(**news_record)
            return None

async def get_source_by_id(source_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # Змінено: використовуємо source_name та source_url
            await cur.execute("SELECT id, source_name, source_url, source_type, status, added_at FROM sources WHERE id = %s", (source_id,))
            return await cur.fetchone()

# FSM States
class AddSourceStates(StatesGroup):
    waiting_for_url = State()
    waiting_for_type = State()

class NewsBrowse(StatesGroup):
    Browse_news = State()
    news_index = State()
    news_ids = State()
    last_message_id = State() # Додано для відстеження останнього повідомлення

class AIAssistant(StatesGroup):
    waiting_for_question = State()
    waiting_for_news_id_for_ai = State() # Оновлено назву для ясності
    waiting_for_term_to_explain = State()
    waiting_for_fact_to_check = State()
    fact_check_news_id = State()
    waiting_for_audience_summary_type = State()
    audience_summary_news_id = State()
    waiting_for_what_if_query = State()
    what_if_news_id = State()
    waiting_for_youtube_interview_url = State()
    waiting_for_translate_language = State() # Новий стан для вибору мови перекладу
    waiting_for_free_question = State() # NEW: State for general AI chat

class MonetaryImpactAnalysis(StatesGroup):
    waiting_for_monetary_impact_news_id = State()

class ReportFakeNews(StatesGroup):
    waiting_for_report_details = State()
    news_id_to_report = State()

class FilterSetup(StatesGroup):
    waiting_for_source_selection = State()

class LanguageSelection(StatesGroup):
    waiting_for_language = State()

class ModerationStates(StatesGroup): # NEW: State for moderation
    browsing_pending_news = State()


# Inline Keyboards
def get_main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📰 Мої новини", callback_data="my_news"),
        InlineKeyboardButton(text="➕ Додати джерело", callback_data="add_source")
    )
    builder.row(
        InlineKeyboardButton(text="⚙️ Налаштування", callback_data="settings_menu"),
        InlineKeyboardButton(text="💬 Запитай AI", callback_data="ask_free_ai") # NEW: AI chat button
    )
    builder.row(
        InlineKeyboardButton(text="❓ Допомога", callback_data="help_menu"),
        InlineKeyboardButton(text="🌐 Мова", callback_data="language_selection_menu")
    )
    builder.row(
        InlineKeyboardButton(text="🛍️ Допоможи купити", url="https://t.me/+eZEMW4FMEWQxMjYy"),
        InlineKeyboardButton(text="🤝 Допоможи продати", url="https://t.me/BigmoneycreateBot")
    )
    return builder.as_markup()

def get_ai_news_functions_keyboard(news_id: int, page: int = 0):
    builder = InlineKeyboardBuilder()
    
    # Кнопки AI функцій (розбиті на сторінки)
    # Сторінка 0: Базові функції
    if page == 0:
        builder.row(
            InlineKeyboardButton(text="📝 AI-резюме", callback_data=f"ai_summary_{news_id}"),
            InlineKeyboardButton(text="🌐 Перекласти", callback_data=f"translate_select_lang_{news_id}") # Змінено callback
        )
        builder.row(
            InlineKeyboardButton(text="❓ Запитати AI", callback_data=f"ask_news_ai_{news_id}"),
            InlineKeyboardButton(text="🧑‍🤝‍🧑 Ключові сутності", callback_data=f"extract_entities_{news_id}")
        )
        builder.row(
            InlineKeyboardButton(text="❓ Пояснити термін", callback_data=f"explain_term_{news_id}"),
            InlineKeyboardButton(text="🏷️ Класифікувати за темами", callback_data=f"classify_topics_{news_id}")
        )
        builder.row(
            InlineKeyboardButton(text="🔊 Прослухати новину", callback_data=f"listen_news_{news_id}") # NEW: Listen to news button
        )
        builder.row(
            InlineKeyboardButton(text="➡️ Далі (AI функції)", callback_data=f"ai_functions_page_1_{news_id}")
        )
    # Сторінка 1: Розширені функції (платні/преміум)
    elif page == 1:
        builder.row(
            InlineKeyboardButton(text="✅ Перевірити факт (Преміум)", callback_data=f"fact_check_news_{news_id}"),
            InlineKeyboardButton(text="📊 Аналіз тренду настроїв (Преміум)", callback_data=f"sentiment_trend_analysis_{news_id}")
        )
        builder.row(
            InlineKeyboardButton(text="🔍 Виявлення упередженості (Преміум)", callback_data=f"bias_detection_{news_id}"),
            InlineKeyboardButton(text="📝 Резюме для аудиторії (Преміум)", callback_data=f"audience_summary_{news_id}")
        )
        builder.row(
            InlineKeyboardButton(text="📜 Історичні аналоги (Преміум)", callback_data=f"historical_analogues_{news_id}"),
            InlineKeyboardButton(text="💥 Аналіз впливу (Преміум)", callback_data=f"impact_analysis_{news_id}")
        )
        builder.row(
            InlineKeyboardButton(text="💰 Грошовий аналіз (Преміум)", callback_data=f"monetary_impact_{news_id}") # Нова кнопка
        )
        builder.row(
            InlineKeyboardButton(text="⬅️ Назад (AI функції)", callback_data=f"ai_functions_page_0_{news_id}")
        )
    
    builder.row(
        InlineKeyboardButton(text="🚩 Повідомити про фейк", callback_data=f"report_fake_news_{news_id}")
    )
    builder.row(
        InlineKeyboardButton(text="❤️ В обране", callback_data=f"bookmark_news_add_{news_id}"), # Нова кнопка
        InlineKeyboardButton(text="💬 Коментарі", callback_data=f"view_comments_{news_id}") # Нова кнопка
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ До головного меню", callback_data="main_menu")
    )
    return builder.as_markup()

def get_translate_language_keyboard(news_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🇬🇧 Англійська", callback_data=f"translate_to_en_{news_id}"),
        InlineKeyboardButton(text="🇵🇱 Польська", callback_data=f"translate_to_pl_{news_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🇩🇪 Німецька", callback_data=f"translate_to_de_{news_id}"),
        InlineKeyboardButton(text="🇪🇸 Іспанська", callback_data=f"translate_to_es_{news_id}")
    )
    builder.row(
        InlineKeyboardButton(text="🇫🇷 Французька", callback_data=f"translate_to_fr_{news_id}"),
        InlineKeyboardButton(text="🇺🇦 Українська", callback_data=f"translate_to_uk_{news_id}")
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Назад до AI функцій", callback_data=f"ai_news_functions_menu_{news_id}")
    )
    return builder.as_markup()


def get_settings_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔔 Сповіщення", callback_data="toggle_notifications"),
        InlineKeyboardButton(text="🔄 Частота дайджесту", callback_data="set_digest_frequency")
    )
    builder.row(
        InlineKeyboardButton(text="🔒 Безпечний режим", callback_data="toggle_safe_mode"),
        InlineKeyboardButton(text="👁️ Режим перегляду", callback_data="set_view_mode")
    )
    builder.row(
        InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")
    )
    return builder.as_markup()

def get_source_type_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Веб-сайт", callback_data="source_type_web"),
        InlineKeyboardButton(text="RSS", callback_data="source_type_rss")
    )
    builder.row(
        InlineKeyboardButton(text="Telegram", callback_data="source_type_telegram"),
        InlineKeyboardButton(text="Соц. мережі", callback_data="source_type_social_media")
    )
    builder.row(
        InlineKeyboardButton(text="Скасувати", callback_data="cancel_action")
    )
    return builder.as_markup()

# Handlers
@router.message(CommandStart()) # Обробник для команди /start
async def command_start_handler(message: Message, state: FSMContext):
    logger.info(f"Отримано команду /start від користувача {message.from_user.id}")
    await state.clear()
    user = await create_or_update_user(message.from_user) # Переконаємося, що користувач існує
    
    # Обробка інвайт-коду, якщо він є
    if message.text and len(message.text.split()) > 1:
        invite_code = message.text.split()[1]
        await handle_invite_code(user.id, invite_code) # Передаємо user.id (з БД)
    
    await message.answer(f"Привіт, {message.from_user.first_name}! Я ваш AI News Bot. Оберіть дію:", reply_markup=get_main_menu_keyboard())

@router.message(Command("menu"))
async def command_menu_handler(message: Message, state: FSMContext):
    logger.info(f"Отримано команду /menu від користувача {message.from_user.id}")
    await state.clear()
    user = await create_or_update_user(message.from_user) # Переконаємося, що користувач існує
    await message.answer("Оберіть дію:", reply_markup=get_main_menu_keyboard())

@router.message(Command("cancel"))
async def command_cancel_handler(message: Message, state: FSMContext):
    logger.info(f"Отримано команду /cancel від користувача {message.from_user.id}")
    await state.clear()
    await message.answer("Дію скасовано. Оберіть наступну дію:", reply_markup=get_main_menu_keyboard())
    await callback.answer() # Додано для коректної обробки callback

@router.message(Command("test")) # Нова команда для тестування відгуку бота
async def command_test_handler(message: Message):
    logger.info(f"Отримано команду /test від користувача {message.from_user.id}")
    await message.answer("Я працюю! ✅")

@router.callback_query(F.data == "main_menu")
async def callback_main_menu(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'main_menu' від користувача {callback.from_user.id}")
    await state.clear()
    await callback.message.edit_text("Оберіть дію:", reply_markup=get_main_menu_keyboard())
    await callback.answer()

@router.callback_query(F.data == "help_menu")
async def callback_help_menu(callback: CallbackQuery):
    logger.info(f"Отримано callback 'help_menu' від користувача {callback.from_user.id}")
    help_text = (
        "<b>Доступні команди:</b>\n"
        "/start - Почати роботу з ботом та перейти до головного меню\n"
        "/menu - Головне меню\n"
        "/cancel - Скасувати поточну дію\n"
        "/myprofile - Переглянути ваш профіль\n"
        "/my_news - Переглянути добірку новин\n"
        "/add_source - Додати нове джерело новин\n"
        "/setfiltersources - Налаштувати джерела новин\n"
        "/resetfilters - Скинути всі фільтри новин\n\n"
        "<b>AI-функції:</b>\n"
        "Доступні після вибору новини (кнопки під новиною).\n\n"
        "<b>Інтеграція з маркетплейсом:</b>\n"
        "Кнопки 'Допоможи продати' та 'Допоможи купити' перенаправляють на канали:\n"
        "Продати: https://t.me/BigmoneycreateBot\n"
        "Купити: https://t.me/+eZEMW4FMEWQxMjYy"
    )
    await callback.message.edit_text(help_text, reply_markup=get_main_menu_keyboard(), disable_web_page_preview=True)
    await callback.answer()

@router.callback_query(F.data == "cancel_action")
async def callback_cancel_action(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'cancel_action' від користувача {callback.from_user.id}")
    await state.clear()
    await callback.message.edit_text("Дію скасовано. Оберіть наступну дію:", reply_markup=get_main_menu_keyboard())
    await callback.answer()

@router.callback_query(F.data == "add_source")
async def callback_add_source(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'add_source' від користувача {callback.from_user.id}")
    await callback.message.edit_text("Будь ласка, надішліть URL джерела (наприклад, веб-сайт, RSS-стрічка, посилання на Telegram-канал або профіль соцмережі):", reply_markup=get_main_menu_keyboard())
    await state.set_state(AddSourceStates.waiting_for_url)
    await callback.answer()

@router.message(AddSourceStates.waiting_for_url)
async def process_source_url(message: Message, state: FSMContext):
    logger.info(f"Отримано URL джерела від користувача {message.from_user.id}: {message.text}")
    source_url = message.text
    # Проста валідація URL
    if not (source_url.startswith("http://") or source_url.startswith("https://")):
        await message.answer("Будь ласка, введіть дійсний URL, що починається з http:// або https://.")
        return
    
    await state.update_data(source_url=source_url)
    await message.answer("Будь ласка, оберіть тип джерела:", reply_markup=get_source_type_keyboard())
    await state.set_state(AddSourceStates.waiting_for_type)

@router.callback_query(AddSourceStates.waiting_for_type, F.data.startswith("source_type_"))
async def process_source_type(callback: CallbackQuery, state: FSMContext):
    source_type = callback.data.replace("source_type_", "")
    logger.info(f"Отримано тип джерела '{source_type}' від користувача {callback.from_user.id}")
    user_data = await state.get_data()
    source_url = user_data.get("source_url")
    user_telegram_id = callback.from_user.id

    if not source_url:
        logger.error(f"URL джерела не знайдено в FSM для користувача {user_telegram_id}")
        await callback.message.edit_text("Вибачте, URL джерела не знайдено. Будь ласка, спробуйте знову.", reply_markup=get_main_menu_keyboard())
        await state.clear()
        return

    # Отримуємо user_db_id
    user_in_db = await get_user_by_telegram_id(user_telegram_id)
    if not user_in_db:
        user_in_db = await create_or_update_user(callback.from_user) # Створити, якщо не існує
    user_db_id = user_in_db.id

    try:
        pool = await get_db_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                # Визначаємо source_name з URL
                parsed_url = HttpUrl(source_url)
                source_name = parsed_url.host if parsed_url.host else 'Невідоме джерело'

                await cur.execute(
                    """
                    INSERT INTO sources (user_id, source_name, source_url, source_type, added_at)
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (source_url) DO UPDATE SET
                        source_name = EXCLUDED.source_name,
                        source_type = EXCLUDED.source_type,
                        status = 'active', -- Активація, якщо було неактивним
                        last_parsed = NULL -- Скинути, щоб перепарсити
                    RETURNING id;
                    """,
                    (user_db_id, source_name, source_url, source_type)
                )
                source_id = await cur.fetchone()[0]
                await conn.commit()
        logger.info(f"Джерело '{source_url}' типу '{source_type}' успішно додано до БД (ID: {source_id})")
        await callback.message.edit_text(f"Джерело '{source_url}' типу '{source_type}' успішно додано!", reply_markup=get_main_menu_keyboard())
    except Exception as e:
        logger.error(f"Помилка при додаванні джерела '{source_url}': {e}", exc_info=True)
        await callback.message.edit_text("Виникла помилка при додаванні джерела. Будь ласка, спробуйте пізніше.", reply_markup=get_main_menu_keyboard())
    
    await state.clear()
    await callback.answer()

@router.callback_query(F.data == "my_news")
async def handle_my_news_command(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'my_news' від користувача {callback.from_user.id}")
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user:
        user = await create_or_update_user(callback.from_user) # Створити, якщо не існує

    news_items = await get_news_for_user(user.id, limit=1)
    
    if not news_items:
        await callback.message.edit_text("Наразі немає нових новин для вас. Спробуйте додати більше джерел або завітайте пізніше!", reply_markup=get_main_menu_keyboard())
        await callback.answer()
        return

    # Зберігаємо список ID новин та поточний індекс у FSM контексті
    all_news_ids = [n.id for n in await get_news_for_user(user.id, limit=100, offset=0)] # Отримуємо більше новин для навігації
    
    # Видаляємо попереднє повідомлення, якщо воно було
    current_state_data = await state.get_data()
    last_message_id = current_state_data.get('last_message_id')
    if last_message_id:
        try:
            await bot.delete_message(chat_id=callback.message.chat.id, message_id=last_message_id)
            logger.info(f"Видалено попереднє повідомлення {last_message_id} для користувача {callback.from_user.id}")
        except Exception as e:
            logger.warning(f"Не вдалося видалити попереднє повідомлення {last_message_id} для користувача {callback.from_user.id}: {e}")

    await state.update_data(current_news_index=0, news_ids=all_news_ids)
    await state.set_state(NewsBrowse.Browse_news) # Встановлюємо стан для навігації

    current_news_id = news_items[0].id
    await send_news_to_user(callback.message.chat.id, current_news_id, 0, len(all_news_ids), state) # Передаємо state
    await callback.answer()

@router.callback_query(NewsBrowse.Browse_news, F.data == "next_news")
async def handle_next_news_command(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'next_news' від користувача {callback.from_user.id}")
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user:
        await callback.message.edit_text("Сталася помилка. Будь ласка, почніть з /menu.", reply_markup=get_main_menu_keyboard())
        await callback.answer()
        return

    user_data = await state.get_data()
    news_ids = user_data.get("news_ids", [])
    current_index = user_data.get("current_news_index", 0)
    last_message_id = user_data.get('last_message_id')

    if not news_ids or current_index + 1 >= len(news_ids):
        await callback.message.edit_text("Більше новин немає. Спробуйте пізніше або змініть фільтри.", reply_markup=get_main_menu_keyboard())
        await callback.answer()
        return

    next_index = current_index + 1
    next_news_id = news_ids[next_index]
    await state.update_data(current_news_index=next_index)

    if last_message_id:
        try:
            await bot.delete_message(chat_id=callback.message.chat.id, message_id=last_message_id)
            logger.info(f"Видалено попереднє повідомлення {last_message_id} для користувача {callback.from_user.id}")
        except Exception as e:
            logger.warning(f"Не вдалося видалити попереднє повідомлення {last_message_id} для користувача {callback.from_user.id}: {e}")

    await send_news_to_user(callback.message.chat.id, next_news_id, next_index, len(news_ids), state) # Передаємо state
    await callback.answer()

@router.callback_query(NewsBrowse.Browse_news, F.data == "prev_news")
async def handle_prev_news_command(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'prev_news' від користувача {callback.from_user.id}")
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user:
        await callback.message.edit_text("Сталася помилка. Будь ласка, почніть з /menu.", reply_markup=get_main_menu_keyboard())
        await callback.answer()
        return

    user_data = await state.get_data()
    news_ids = user_data.get("news_ids", [])
    current_index = user_data.get("current_news_index", 0)
    last_message_id = user_data.get('last_message_id')

    if not news_ids or current_index <= 0:
        await callback.message.edit_text("Це перша новина. Більше новин немає.", reply_markup=get_main_menu_keyboard())
        await callback.answer()
        return

    prev_index = current_index - 1
    prev_news_id = news_ids[prev_index]
    await state.update_data(current_news_index=prev_index)

    if last_message_id:
        try:
            await bot.delete_message(chat_id=callback.message.chat.id, message_id=last_message_id)
            logger.info(f"Видалено попереднє повідомлення {last_message_id} для користувача {callback.from_user.id}")
        except Exception as e:
            logger.warning(f"Не вдалося видалити попереднє повідомлення {last_message_id} для користувача {callback.from_user.id}: {e}")

    await send_news_to_user(callback.message.chat.id, prev_news_id, prev_index, len(news_ids), state) # Передаємо state
    await callback.answer()

@router.callback_query(F.data.startswith("ai_news_functions_menu"))
async def handle_ai_news_functions_menu(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'ai_news_functions_menu' від користувача {callback.from_user.id}")
    logger.debug(f"Callback data for AI functions menu: {callback.data}") # Додано логування
    
    # Очікуваний формат: ai_news_functions_menu_{news_id}
    parts = callback.data.split('_')
    news_id = None
    # Перевіряємо, чи достатньо частин і чи остання частина є числом (news_id)
    if len(parts) > 3 and parts[3].isdigit(): # news_id має бути четвертим елементом (індекс 3)
        news_id = int(parts[3])
    
    if not news_id:
        logger.warning(f"Не знайдено news_id для AI функцій для користувача {callback.from_user.id}. Callback data: {callback.data}")
        await callback.answer("Будь ласка, спочатку оберіть новину.", show_alert=True)
        return

    # Зберігаємо news_id для подальших AI-операцій
    await state.update_data(current_news_id_for_ai_menu=news_id)
    
    # Завжди починаємо з першої сторінки AI функцій при першому відкритті меню
    page = 0 

    await callback.message.edit_text("Оберіть AI-функцію:", reply_markup=get_ai_news_functions_keyboard(news_id, page))
    await callback.answer()

# Обробники для навігації по сторінках AI функцій
@router.callback_query(F.data.startswith("ai_functions_page_"))
async def handle_ai_functions_pagination(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'ai_functions_page_' від користувача {callback.from_user.id}")
    logger.debug(f"Callback data for AI functions pagination: {callback.data}") # Додано логування
    
    # Очікуваний формат: ai_functions_page_{page}_{news_id}
    parts = callback.data.split('_')
    page = int(parts[3]) # Page number is at index 3
    news_id = int(parts[4]) # News ID is at index 4

    user_data = await state.get_data()
    user_telegram_id = callback.from_user.id
    user_in_db = await get_user_by_telegram_id(user_telegram_id)

    # Перевірка преміум доступу для сторінки 1
    if page == 1 and (not user_in_db or not user_in_db.is_premium):
        await callback.answer("Ці функції доступні лише для преміум-користувачів.", show_alert=True)
        return

    await callback.message.edit_text("Оберіть AI-функцію:", reply_markup=get_ai_news_functions_keyboard(news_id, page))
    await callback.answer()


async def send_news_to_user(chat_id: int, news_id: int, current_index: int, total_news: int, state: FSMContext):
    logger.info(f"Надсилання новини {news_id} користувачу {chat_id}")
    news_item = await get_news_by_id(news_id)
    if not news_item:
        logger.warning(f"Новина {news_id} не знайдена для надсилання користувачу {chat_id}")
        await bot.send_message(chat_id, "На жаль, ця новина не знайдена.")
        return

    source_info = await get_source_by_id(news_item.source_id)
    source_name = source_info['source_name'] if source_info else "Невідоме джерело"

    # Форматування тексту новини
    text = (
        f"<b>Заголовок:</b> {news_item.title}\n\n"
        f"<b>Зміст:</b>\n{news_item.content}\n\n"
        f"Опубліковано: {news_item.published_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"Новина {current_index + 1} з {total_news}\n\n"
    )

    keyboard_builder = InlineKeyboardBuilder()
    keyboard_builder.row(InlineKeyboardButton(text="🔗 Читати джерело", url=str(news_item.source_url)))
    
    # Кнопка для виклику меню AI-функцій
    keyboard_builder.row(
        InlineKeyboardButton(text="🧠 AI-функції", callback_data=f"ai_news_functions_menu_{news_item.id}")
    )

    # Додаємо навігаційні кнопки
    nav_buttons = []
    if current_index > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Попередня", callback_data="prev_news"))
    if current_index < total_news - 1:
        nav_buttons.append(InlineKeyboardButton(text="➡️ Далі", callback_data="next_news"))
    
    if nav_buttons:
        keyboard_builder.row(*nav_buttons)

    keyboard_builder.row(InlineKeyboardButton(text="⬅️ До головного меню", callback_data="main_menu"))

    # Надсилаємо новину
    msg = None
    if news_item.image_url:
        try:
            msg = await bot.send_photo(
                chat_id=chat_id,
                photo=str(news_item.image_url),
                caption=text,
                reply_markup=keyboard_builder.as_markup(),
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Новина {news_id} надіслана з фото користувачу {chat_id}.")
        except Exception as e:
            logger.warning(f"Не вдалося надіслати фото для новини {news_id} користувачу {chat_id}: {e}. Надсилаю новину без фото.")
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text + f"\n[Зображення новини]({news_item.image_url})", # Додаємо посилання на зображення
                reply_markup=keyboard_builder.as_markup(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False # Дозволити попередній перегляд посилання на зображення
            )
            logger.info(f"Новина {news_id} надіслана без фото користувачу {chat_id}.")
    else:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard_builder.as_markup(),
            parse_mode=ParseMode.HTML
        )
        logger.info(f"Новина {news_id} надіслана без фото користувачу {chat_id}.")
    
    # Зберігаємо ID останнього надісланого повідомлення в FSM контексті
    if msg:
        await state.update_data(last_message_id=msg.message_id)
        logger.info(f"Збережено message_id {msg.message_id} для новини {news_id} користувача {chat_id}.")

    # Позначаємо новину як переглянуту
    user = await get_user_by_telegram_id(chat_id)
    if user:
        await mark_news_as_viewed(user.id, news_item.id)
        logger.info(f"Новина {news_id} позначена як переглянута для користувача {user.id}.")


# AI-функції
async def call_gemini_api(prompt: str) -> Optional[str]:
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY не встановлено.")
        return "Вибачте, функціонал AI наразі недоступний через відсутність API ключа."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "topK": 40,
            "topP": 0.95,
            "maxOutputTokens": 1000,
        }
    }

    try:
        async with ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status == 429: # Too Many Requests
                    logger.warning("Отримано 429 Too Many Requests від Gemini API. Спробую пізніше.")
                    return "Вибачте, зараз забагато запитів до AI. Будь ласка, спробуйте через хвилину."
                response.raise_for_status()
                data = await response.json()
                if data and data.get("candidates"):
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                return "Не вдалося отримати відповідь від AI."
    except Exception as e:
        logger.error(f"Помилка виклику Gemini API: {e}")
        return "Виникла помилка при зверненні до AI."

async def check_premium_access(user_telegram_id: int) -> bool:
    user = await get_user_by_telegram_id(user_telegram_id)
    return user and user.is_premium

@router.callback_query(F.data.startswith("ai_summary_"))
async def handle_ai_summary(callback: CallbackQuery):
    logger.info(f"Отримано callback 'ai_summary_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return

    await callback.message.edit_text("Генерую AI-резюме, зачекайте...")
    summary = await call_gemini_api(f"Зроби коротке резюме новини українською мовою: {news_item.content}")
    await callback.message.edit_text(f"<b>AI-резюме:</b>\n{summary}", reply_markup=get_ai_news_functions_keyboard(news_id))
    await callback.answer()

@router.callback_query(F.data.startswith("translate_select_lang_"))
async def handle_translate_select_language(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'translate_select_lang_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[3])
    await state.update_data(news_id_for_translate=news_id)
    await callback.message.edit_text("Оберіть мову для перекладу:", reply_markup=get_translate_language_keyboard(news_id))
    await state.set_state(AIAssistant.waiting_for_translate_language)
    await callback.answer()

@router.callback_query(AIAssistant.waiting_for_translate_language, F.data.startswith("translate_to_"))
async def handle_translate_to_language(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'translate_to_' від користувача {callback.from_user.id}")
    parts = callback.data.split('_')
    lang_code = parts[2] # наприклад, 'en', 'pl'
    news_id = int(parts[3])

    language_names = {
        "en": "англійську", "pl": "польську", "de": "німецьку",
        "es": "іспанську", "fr": "французьку", "uk": "українську"
    }
    language_name = language_names.get(lang_code, "обрану")

    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        await state.clear()
        return

    await callback.message.edit_text(f"Перекладаю новину на {language_name} мову, зачекайте...")
    translation = await call_gemini_api(f"Переклади цю новину на {language_name} мовою:\n\n{news_item.content}")
    await callback.message.edit_text(f"<b>Переклад на {language_name} мову:</b>\n{translation}", reply_markup=get_ai_news_functions_keyboard(news_id))
    await state.clear()
    await callback.answer()

@router.callback_query(F.data.startswith("listen_news_"))
async def handle_listen_news(callback: CallbackQuery):
    logger.info(f"Отримано callback 'listen_news_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return

    await callback.message.edit_text("Генерую аудіо, зачекайте...")
    
    try:
        # Combine title and content for TTS
        text_to_speak = f"{news_item.title}. {news_item.content}"
        
        # gTTS supports Ukrainian ('uk')
        tts = gTTS(text=text_to_speak, lang='uk')
        
        # Save to a BytesIO object (in-memory file)
        audio_buffer = io.BytesIO()
        await asyncio.to_thread(tts.write_to_fp, audio_buffer)
        audio_buffer.seek(0) # Reset buffer position to the beginning

        # Send the audio as a voice message
        await bot.send_voice(
            chat_id=callback.message.chat.id,
            voice=BufferedInputFile(audio_buffer.getvalue(), filename=f"news_{news_id}.mp3"),
            caption=f"🔊 Новина: {news_item.title}",
            reply_markup=get_ai_news_functions_keyboard(news_id)
        )
        await callback.message.delete() # Delete the "Generating audio..." message
        logger.info(f"Аудіо для новини {news_id} успішно надіслано користувачу {callback.from_user.id}.")
    except Exception as e:
        logger.error(f"Помилка генерації або надсилання аудіо для новини {news_id}: {e}", exc_info=True)
        await callback.message.edit_text("Виникла помилка при генерації аудіо. Будь ласка, спробуйте пізніше.",
                                         reply_markup=get_ai_news_functions_keyboard(news_id))
    await callback.answer()


@router.callback_query(F.data.startswith("ask_news_ai_"))
async def handle_ask_news_ai(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'ask_news_ai_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[3])
    await state.update_data(waiting_for_news_id_for_ai=news_id) # Оновлено назву
    await callback.message.edit_text("Надішліть ваше запитання щодо новини:", reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text="Скасувати", callback_data="cancel_action")).as_markup())
    await state.set_state(AIAssistant.waiting_for_question)
    await callback.answer()

@router.message(AIAssistant.waiting_for_question)
async def process_ai_question(message: Message, state: FSMContext):
    logger.info(f"Отримано запитання AI від користувача {message.from_user.id}: {message.text}")
    user_question = message.text
    user_data = await state.get_data()
    news_id = user_data.get("waiting_for_news_id_for_ai") # Оновлено назву

    if not news_id:
        logger.error(f"Новина для запитання AI не знайдена в FSM для користувача {message.from_user.id}")
        await message.answer("Вибачте, новина для запитання не знайдена. Будь ласка, спробуйте знову.", reply_markup=get_main_menu_keyboard())
        await state.clear()
        return

    news_item = await get_news_by_id(news_id)
    if not news_item:
        logger.error(f"Новина {news_id} не знайдена в БД для запитання AI від користувача {message.from_user.id}")
        await message.answer("Новина не знайдена.", reply_markup=get_main_menu_keyboard())
        await state.clear()
        return

    await message.answer("Обробляю ваше запитання, зачекайте...")
    prompt = f"Новина: {news_item.content}\n\nЗапитання: {user_question}\n\nВідповідь:"
    ai_response = await call_gemini_api(prompt)
    await message.answer(f"<b>Відповідь AI:</b>\n{ai_response}", reply_markup=get_ai_news_functions_keyboard(news_id))
    await state.clear()
    logger.info(f"Відповідь AI надіслана користувачу {message.from_user.id} для новини {news_id}.")

# NEW: Handler for general AI chat
@router.callback_query(F.data == "ask_free_ai")
async def handle_ask_free_ai(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'ask_free_ai' від користувача {callback.from_user.id}")
    await callback.message.edit_text("Надішліть ваше запитання до AI (наприклад, 'Що сталося з гривнею?', 'Як це вплине на ціни?'):",
                                     reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text="Скасувати", callback_data="cancel_action")).as_markup())
    await state.set_state(AIAssistant.waiting_for_free_question)
    await callback.answer()

@router.message(AIAssistant.waiting_for_free_question)
async def process_free_ai_question(message: Message, state: FSMContext):
    logger.info(f"Отримано вільне запитання AI від користувача {message.from_user.id}: {message.text}")
    user_question = message.text

    await message.answer("Обробляю ваше запитання, зачекайте...")
    # Тут можна додати контекст, наприклад, останні новини, які читав користувач
    # Для простоти, поки що просто передаємо запитання
    ai_response = await call_gemini_api(f"Відповідай як новинний аналітик на запитання: {user_question}")
    await message.answer(f"<b>Відповідь AI:</b>\n{ai_response}", reply_markup=get_main_menu_keyboard())
    await state.clear()
    logger.info(f"Вільна відповідь AI надіслана користувачу {message.from_user.id}.")


@router.callback_query(F.data.startswith("extract_entities_"))
async def handle_extract_entities(callback: CallbackQuery):
    logger.info(f"Отримано callback 'extract_entities_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return
    
    # Перевірка преміум доступу
    if not await check_premium_access(callback.from_user.id):
        await callback.answer("Ця функція доступна лише для преміум-користувачів.", show_alert=True)
        return

    await callback.message.edit_text("Витягую ключові сутності, зачекайте...")
    # Змінено промпт для витягування імен, прізвищ, прізвиськ без змін
    entities = await call_gemini_api(f"Витягни ключові сутності (імена людей, прізвища, прізвиська, організації, місця, дати) з наступної новини українською мовою. Перелічи їх через кому, зберігаючи оригінальний вигляд: {news_item.content}")
    await callback.message.edit_text(f"<b>Ключові сутності:</b>\n{entities}", reply_markup=get_ai_news_functions_keyboard(news_id))
    await callback.answer()

@router.callback_query(F.data.startswith("explain_term_"))
async def handle_explain_term(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'explain_term_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return

    # Перевірка преміум доступу
    if not await check_premium_access(callback.from_user.id):
        await callback.answer("Ця функція доступна лише для преміум-користувачів.", show_alert=True)
        return

    await state.update_data(waiting_for_news_id_for_ai=news_id) # Оновлено назву
    await callback.message.edit_text("Надішліть термін, який потрібно пояснити:", reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text="Скасувати", callback_data="cancel_action")).as_markup())
    await state.set_state(AIAssistant.waiting_for_term_to_explain)
    await callback.answer()

@router.message(AIAssistant.waiting_for_term_to_explain)
async def process_term_explanation(message: Message, state: FSMContext):
    logger.info(f"Отримано термін для пояснення від користувача {message.from_user.id}: {message.text}")
    term = message.text
    user_data = await state.get_data()
    news_id = user_data.get("waiting_for_news_id_for_ai") # Оновлено назву

    if not news_id:
        logger.error(f"Новина для пояснення терміну не знайдена в FSM для користувача {message.from_user.id}")
        await message.answer("Вибачте, новина для пояснення терміну не знайдена. Будь ласка, спробуйте знову.", reply_markup=get_main_menu_keyboard())
        await state.clear()
        return

    news_item = await get_news_by_id(news_id)
    if not news_item:
        logger.error(f"Новина {news_id} не знайдена в БД для пояснення терміну від користувача {message.from_user.id}")
        await message.answer("Новина не знайдена.", reply_markup=get_main_menu_keyboard())
        await state.clear()
        return

    await message.answer("Пояснюю термін, зачекайте...")
    explanation = await call_gemini_api(f"Поясни термін '{term}' у контексті наступної новини українською мовою: {news_item.content}")
    await message.answer(f"<b>Пояснення терміну '{term}':</b>\n{explanation}", reply_markup=get_ai_news_functions_keyboard(news_id))
    await state.clear()
    logger.info(f"Пояснення терміну надіслано користувачу {message.from_user.id} для новини {news_id}.")

@router.callback_query(F.data.startswith("classify_topics_"))
async def handle_classify_topics(callback: CallbackQuery):
    logger.info(f"Отримано callback 'classify_topics_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return
    
    # Перевірка преміум доступу
    if not await check_premium_access(callback.from_user.id):
        await callback.answer("Ця функція доступна лише для преміум-користувачів.", show_alert=True)
        return

    await callback.message.edit_text("Класифікую за темами, зачекайте...")
    topics = await call_gemini_api(f"Класифікуй наступну новину за 3-5 основними темами/категоріями українською мовою, перелічи їх через кому: {news_item.content}")
    await callback.message.edit_text(f"<b>Теми:</b>\n{topics}", reply_markup=get_ai_news_functions_keyboard(news_id))
    await callback.answer()

@router.callback_query(F.data.startswith("fact_check_news_"))
async def handle_fact_check_news(callback: CallbackQuery):
    logger.info(f"Отримано callback 'fact_check_news_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[3])
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return
    
    # Перевірка преміум доступу
    if not await check_premium_access(callback.from_user.id):
        await callback.answer("Ця функція доступна лише для преміум-користувачів.", show_alert=True)
        return

    await callback.message.edit_text("Перевіряю факти, зачекайте...")
    fact_check = await call_gemini_api(f"Виконай швидку перевірку фактів для наступної новини українською мовою. Вкажи, чи є в ній очевидні неточності або маніпуляції. Якщо є, наведи джерела: {news_item.content}")
    await callback.message.edit_text(f"<b>Перевірка фактів:</b>\n{fact_check}", reply_markup=get_ai_news_functions_keyboard(news_id))
    await callback.answer()

@router.callback_query(F.data.startswith("sentiment_trend_analysis_"))
async def handle_sentiment_trend_analysis(callback: CallbackQuery):
    logger.info(f"Отримано callback 'sentiment_trend_analysis_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[3])
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return
    
    # Перевірка преміум доступу
    if not await check_premium_access(callback.from_user.id):
        await callback.answer("Ця функція доступна лише для преміум-користувачів.", show_alert=True)
        return

    await callback.message.edit_text("Аналізую настрій, зачекайте...")
    sentiment = await call_gemini_api(f"Виконай аналіз настрою (позитивний, негативний, нейтральний) для наступної новини українською мовою та поясни чому: {news_item.content}")
    await callback.message.edit_text(f"<b>Аналіз настрою:</b>\n{sentiment}", reply_markup=get_ai_news_functions_keyboard(news_id))
    await callback.answer()

@router.callback_query(F.data.startswith("bias_detection_"))
async def handle_bias_detection(callback: CallbackQuery):
    logger.info(f"Отримано callback 'bias_detection_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return
    
    # Перевірка преміум доступу
    if not await check_premium_access(callback.from_user.id):
        await callback.answer("Ця функція доступна лише для преміум-користувачів.", show_alert=True)
        return

    await callback.message.edit_text("Виявляю упередженість, зачекайте...")
    bias = await call_gemini_api(f"Вияви потенційну упередженість або маніпуляції в наступній новині українською мовою: {news_item.content}")
    await callback.message.edit_text(f"<b>Виявлення упередженості:</b>\n{bias}", reply_markup=get_ai_news_functions_keyboard(news_id))
    await callback.answer()

@router.callback_query(F.data.startswith("audience_summary_"))
async def handle_audience_summary(callback: CallbackQuery):
    logger.info(f"Отримано callback 'audience_summary_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return
    
    # Перевірка преміум доступу
    if not await check_premium_access(callback.from_user.id):
        await callback.answer("Ця функція доступна лише для преміум-користувачів.", show_alert=True)
        return

    await callback.message.edit_text("Генерую резюме для аудиторії, зачекайте...")
    summary = await call_gemini_api(f"Зроби резюме наступної новини українською мовою, адаптований для широкої аудиторії (простою мовою, без складних термінів): {news_item.content}")
    await callback.message.edit_text(f"<b>Резюме для аудиторії:</b>\n{summary}", reply_markup=get_ai_news_functions_keyboard(news_id))
    await callback.answer()

@router.callback_query(F.data.startswith("historical_analogues_"))
async def handle_historical_analogues(callback: CallbackQuery):
    logger.info(f"Отримано callback 'historical_analogues_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return
    
    # Перевірка преміум доступу
    if not await check_premium_access(callback.from_user.id):
        await callback.answer("Ця функція доступна лише для преміум-користувачів.", show_alert=True)
        return

    await callback.message.edit_text("Шукаю історичні аналоги, зачекайте...")
    analogues = await call_gemini_api(f"Знайди історичні аналоги або схожі події для ситуації, описаної в наступній новині українською мовою. Опиши їх наслідки та вплив, включаючи умовні підрахунки (наприклад, 'це призвело до втрат у розмірі X мільйонів доларів' або 'змінило економічний ландер на Y%'): {news_item.content}")
    await callback.message.edit_text(f"<b>Історичні аналоги:</b>\n{analogues}", reply_markup=get_ai_news_functions_keyboard(news_id))
    await callback.answer()

@router.callback_query(F.data.startswith("impact_analysis_"))
async def handle_impact_analysis(callback: CallbackQuery):
    logger.info(f"Отримано callback 'impact_analysis_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return
    
    # Перевірка преміум доступу
    if not await check_premium_access(callback.from_user.id):
        await callback.answer("Ця функція доступна лише для преміум-користувачів.", show_alert=True)
        return

    await callback.message.edit_text("Аналізую вплив, зачекайте...")
    impact = await call_gemini_api(f"Виконай аналіз потенційного впливу наступної новини на різні аспекти (економіка, суспільство, політика) українською мовою, включаючи умовні підрахунки (наприклад, 'це може призвести до зростання цін на X%'): {news_item.content}")
    await callback.message.edit_text(f"<b>Аналіз впливу:</b>\n{impact}", reply_markup=get_ai_news_functions_keyboard(news_id))
    await callback.answer()

@router.callback_query(F.data.startswith("monetary_impact_"))
async def handle_monetary_impact(callback: CallbackQuery):
    logger.info(f"Отримано callback 'monetary_impact_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer("Новина не знайдена.", show_alert=True)
        return

    # Перевірка преміум доступу
    if not await check_premium_access(callback.from_user.id):
        await callback.answer("Ця функція доступна лише для преміум-користувачів.", show_alert=True)
        return

    await callback.message.edit_text("Виконую грошовий аналіз, зачекайте...")
    # Додаємо запит до AI для грошового аналізу
    monetary_analysis = await call_gemini_api(f"Проаналізуй потенційний грошовий вплив подій, описаних у новині, на поточний момент та на протязі року. Вкажи умовні підрахунки та можливі фінансові наслідки українською мовою: {news_item.content}")
    await callback.message.edit_text(f"<b>Грошовий аналіз:</b>\n{monetary_analysis}", reply_markup=get_ai_news_functions_keyboard(news_id))
    await callback.answer()

# Нові обробники для закладок (bookmarks)
@router.callback_query(F.data.startswith("bookmark_news_"))
async def handle_bookmark_news(callback: CallbackQuery):
    logger.info(f"Отримано callback 'bookmark_news_' від користувача {callback.from_user.id}")
    parts = callback.data.split('_')
    action = parts[2] # 'add' або 'remove'
    news_id = int(parts[3])

    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user:
        await callback.answer("Помилка: користувач не знайдений.", show_alert=True)
        return

    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if action == 'add':
                try:
                    await cur.execute(
                        """
                        INSERT INTO bookmarks (user_id, news_id, bookmarked_at)
                        VALUES (%s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (user_id, news_id) DO NOTHING;
                        """,
                        (user.id, news_id)
                    )
                    if cur.rowcount > 0:
                        await callback.answer("Новину додано до закладок!", show_alert=True)
                        logger.info(f"Новину {news_id} додано до закладок користувачем {user.id}")
                    else:
                        await callback.answer("Ця новина вже у ваших закладках.", show_alert=True)
                except Exception as e:
                    logger.error(f"Помилка додавання закладки: {e}", exc_info=True)
                    await callback.answer("Помилка при додаванні до закладок.", show_alert=True)
            elif action == 'remove':
                try:
                    await cur.execute(
                        "DELETE FROM bookmarks WHERE user_id = %s AND news_id = %s;",
                        (user.id, news_id)
                    )
                    if cur.rowcount > 0:
                        await callback.answer("Новину видалено із закладок!", show_alert=True)
                        logger.info(f"Новину {news_id} видалено із закладок користувачем {user.id}")
                    else:
                        await callback.answer("Цієї новини немає у ваших закладках.", show_alert=True)
                except Exception as e:
                    logger.error(f"Помилка видалення закладки: {e}", exc_info=True)
                    await callback.answer("Помилка при видаленні із закладок.", show_alert=True)
            await conn.commit()
    
    # Оновлюємо клавіатуру, щоб відобразити актуальний статус закладки
    # Для цього потрібно отримати новину знову і перерендерити меню AI функцій
    current_state_data = await state.get_data()
    last_message_id = current_state_data.get('last_message_id')
    if last_message_id:
        # Можна відредагувати повідомлення, щоб оновити кнопки
        # Але для простоти зараз просто повертаємось до меню AI-функцій
        await callback.message.edit_text("Оберіть AI-функцію:", reply_markup=get_ai_news_functions_keyboard(news_id))
    else:
        await callback.message.edit_text("Дію виконано. Оберіть наступну дію:", reply_markup=get_main_menu_keyboard())
    await callback.answer()

@router.message(Command("bookmarks"))
async def command_bookmarks_handler(message: Message, state: FSMContext):
    logger.info(f"Отримано команду /bookmarks від користувача {message.from_user.id}")
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user:
        user = await create_or_update_user(message.from_user)

    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT n.* FROM news n
                JOIN bookmarks b ON n.id = b.news_id
                WHERE b.user_id = %s
                ORDER BY b.bookmarked_at DESC
                LIMIT 10;
                """,
                (user.id,)
            )
            bookmarked_news = await cur.fetchall()
            
    if not bookmarked_news:
        await message.answer("У вас поки немає закладок.", reply_markup=get_main_menu_keyboard())
        return

    # Відправляємо кожну новину окремо або формуємо список
    response_text = "<b>Ваші закладки:</b>\n\n"
    for idx, news_item_data in enumerate(bookmarked_news):
        news_item = News(**news_item_data)
        response_text += f"{idx+1}. <a href='{news_item.source_url}'>{news_item.title}</a>\n"
        response_text += f"   <i>{news_item.published_at.strftime('%d.%m.%Y %H:%M')}</i>\n\n"
    
    # Додаємо кнопку для перегляду закладок з можливістю видалення
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ До головного меню", callback_data="main_menu"))
    # Можна додати кнопки для перегляду/видалення конкретних закладок, якщо список великий
    
    await message.answer(response_text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    logger.info(f"Надіслано закладки користувачу {user.id}")


@router.callback_query(F.data.startswith("report_fake_news_"))
async def handle_report_fake_news(callback: CallbackQuery):
    logger.info(f"Отримано callback 'report_fake_news_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[3])
    user_telegram_id = callback.from_user.id
    user_in_db = await get_user_by_telegram_id(user_telegram_id)
    user_db_id = user_in_db.id if user_in_db else None

    if not user_db_id:
        logger.error(f"Не вдалося ідентифікувати користувача {user_telegram_id} для повідомлення про фейк.")
        await callback.answer("Не вдалося ідентифікувати вашого користувача в системі.", show_alert=True)
        return

    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Перевірка на дублікат репорту
            await cur.execute(
                "SELECT 1 FROM reports WHERE user_id = %s AND target_type = 'news' AND target_id = %s;",
                (user_db_id, news_id)
            )
            if await cur.fetchone():
                await callback.answer("Ви вже надсилали репорт на цю новину.", show_alert=True)
                return

            await cur.execute(
                """
                INSERT INTO reports (user_id, target_type, target_id, reason, created_at, status)
                VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, 'pending');
                """,
                (user_db_id, 'news', news_id, 'Повідомлення про фейкову новину')
            )
            await conn.commit()
    logger.info(f"Повідомлення про фейкову новину {news_id} від користувача {user_telegram_id} додано до БД.")
    await callback.answer("Дякуємо! Ваше повідомлення про фейкову новину було надіслано на модерацію.", show_alert=True)
    await callback.message.edit_text("Дякуємо за ваш внесок! Оберіть наступну дію:", reply_markup=get_ai_news_functions_keyboard(news_id))

async def send_news_to_channel(news_item: News):
    """
    Надсилає новину до Telegram-каналу.
    """
    if not NEWS_CHANNEL_LINK:
        logger.warning("NEWS_CHANNEL_LINK не налаштовано. Новина не буде опублікована в каналі.")
        return

    channel_identifier = NEWS_CHANNEL_LINK
    if 't.me/' in NEWS_CHANNEL_LINK:
        channel_identifier = NEWS_CHANNEL_LINK.split('/')[-1]
    
    # Check if it's a numeric channel ID (starts with -100)
    # and ensure it's purely numeric after the initial '-'
    if channel_identifier.startswith('-100') and channel_identifier[1:].isdigit():
        # It's a numeric ID, use as is
        pass
    elif channel_identifier.startswith('+'):
        logger.error(f"NEWS_CHANNEL_LINK '{NEWS_CHANNEL_LINK}' виглядає як посилання-запрошення. Для публікації потрібен @username каналу або його числовий ID (наприклад, -1001234567890).")
        return
    elif not channel_identifier.startswith('@'):
        # If it's not a numeric ID and not starting with @, assume it's a username and prepend @
        channel_identifier = '@' + channel_identifier
        logger.warning(f"NEWS_CHANNEL_LINK '{NEWS_CHANNEL_LINK}' не містить '@' або '-' на початку. Спробую використати '{channel_identifier}'.")


    # Скорочуємо контент для каналу, щоб уникнути помилки "message caption is too long"
    # Додаємо "..." якщо контент обрізано
    display_content = news_item.content
    if len(display_content) > 250:
        display_content = display_content[:247] + "..." # 247 + 3 dots = 250

    text = (
        f"<b>Нова новина:</b> {news_item.title}\n\n"
        f"{display_content}\n\n"
        f"🔗 <a href='{news_item.source_url}'>Читати повністю</a>\n"
        f"Опубліковано: {news_item.published_at.strftime('%d.%m.%Y %H:%M')}"
    )

    try:
        if news_item.image_url:
            await bot.send_photo(
                chat_id=channel_identifier,
                photo=str(news_item.image_url),
                caption=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False
            )
        else:
            await bot.send_message(
                chat_id=channel_identifier,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False
            )
        logger.info(f"Новина '{news_item.title}' успішно опублікована в каналі {channel_identifier}.")
    except Exception as e:
        logger.error(f"Помилка публікації новини '{news_item.title}' в каналі {channel_identifier}: {e}", exc_info=True)


# Автоматичне виставлення новин (фонове завдання)
async def fetch_and_post_news_task():
    logger.info("Запущено фонове завдання: fetch_and_post_news_task")
    # Отримуємо всі активні джерела
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM sources WHERE status = 'active'")
            sources = await cur.fetchall()
    
    if not sources:
        logger.info("Немає активних джерел для парсингу. Пропускаю fetch_and_post_news_task.")
        return

    for source in sources:
        logger.info(f"Обробка джерела: {source.get('source_name', 'N/A')} ({source.get('source_url', 'N/A')})")
        # Покращена перевірка на наявність та не-None значення ключів
        if not source.get('source_type') or not source.get('source_url') or not source.get('source_name'):
            logger.warning(f"Джерело з ID {source.get('id', 'N/A')} має відсутні або порожні значення для source_type, source_url або source_name. Пропускаю.")
            continue

        news_data = None
        try:
            if source['source_type'] == 'rss':
                news_data = await rss_parser.parse_rss_feed(source['source_url'])
            elif source['source_type'] == 'web':
                parsed_data = await web_parser.parse_website(source['source_url'])
                if parsed_data:
                    news_data = parsed_data
                else:
                    logger.warning(f"Не вдалося спарсити контент з джерела: {source['source_name']} ({source['source_url']}). Пропускаю.")
                    continue
            elif source['source_type'] == 'telegram':
                news_data = await telegram_parser.get_telegram_channel_posts(source['source_url'])
            elif source['source_type'] == 'social_media':
                news_data = await social_media_parser.get_social_media_posts(source['source_url'], "general")
            
            if news_data:
                logger.info(f"Дані новини спарсені для {source['source_name']}: {news_data.get('title', 'Без заголовка')}")
                news_data['source_id'] = source['id']
                news_data['source_name'] = source['source_name']
                news_data['source_type'] = source['source_type']
                news_data['user_id_for_source'] = None # Explicitly set to None for auto-fetched news, so it gets 'approved' status
                
                added_news_item = await add_news_to_db(news_data)
                if added_news_item:
                    logger.info(f"Новина '{added_news_item.title}' успішно додана до БД.")
                    # Send to news channel if successfully added and approved (which it should be for auto-parsed)
                    await send_news_to_channel(added_news_item)
                    # Оновлення статистики джерела
                    await update_source_stats_publication_count(source['id'])
                else:
                    logger.info(f"Новина з джерела {source['source_name']} не була додана до БД (можливо, вже існує).")
                
                async with pool.connection() as conn_update:
                    async with conn_update.cursor() as cur_update:
                        await cur_update.execute("UPDATE sources SET last_parsed = CURRENT_TIMESTAMP WHERE id = %s", (source['id'],))
                        await conn_update.commit()
                        logger.info(f"Оновлено last_parsed для джерела {source['source_name']}.")
            else:
                logger.warning(f"Не вдалося спарсити контент з джерела: {source['source_name']} ({source['source_url']}). Пропускаю.")
        except Exception as e:
            source_name_log = source.get('source_name', 'N/A')
            source_url_log = source.get('source_url', 'N/A')
            logger.error(f"Критична помилка парсингу джерела {source_name_log} ({source_url_log}): {e}", exc_info=True) # Змінено на error для кращої видимості критичних помилок

async def delete_expired_news_task():
    logger.info("Запущено фонове завдання: delete_expired_news_task")
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Видаляємо новини, у яких expires_at менше поточної дати
            await cur.execute("DELETE FROM news WHERE expires_at IS NOT NULL AND expires_at < CURRENT_TIMESTAMP;")
            deleted_count = cur.rowcount
            await conn.commit()
            if deleted_count > 0:
                logger.info(f"Видалено {deleted_count} прострочених новин.")
            else:
                logger.info("Немає прострочених новин для видалення.")

# NEW: Function to send daily digest
async def send_daily_digest():
    logger.info("Запущено фонове завдання: send_daily_digest")
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # Отримуємо користувачів, які хочуть щоденний дайджест
            # Перевіряємо, що auto_notifications увімкнено
            await cur.execute("SELECT id, telegram_id, language FROM users WHERE auto_notifications = TRUE AND digest_frequency = 'daily';")
            users_for_digest = await cur.fetchall()

    if not users_for_digest:
        logger.info("Немає користувачів для щоденного дайджесту.")
        return

    for user_data in users_for_digest:
        user_db_id = user_data['id']
        user_telegram_id = user_data['telegram_id']
        user_lang = user_data['language']

        logger.info(f"Надсилаю щоденний дайджест користувачу {user_telegram_id}")

        # Отримуємо 3-5 новин, які користувач ще не бачив
        news_items = await get_news_for_user(user_db_id, limit=5)

        if not news_items:
            logger.info(f"Для користувача {user_telegram_id} немає нових новин для дайджесту.")
            continue

        digest_text = "<b>📰 Ваш щоденний AI-дайджест новин:</b>\n\n"
        for i, news_item in enumerate(news_items):
            summary = await call_gemini_api(f"Зроби коротке резюме новини українською мовою: {news_item.content}")
            digest_text += (
                f"<b>{i+1}. {news_item.title}</b>\n"
                f"{summary}\n"
                f"🔗 <a href='{news_item.source_url}'>Читати повністю</a>\n\n"
            )
            # Позначаємо новину як переглянуту після додавання до дайджесту
            await mark_news_as_viewed(user_db_id, news_item.id)

        try:
            await bot.send_message(
                chat_id=user_telegram_id,
                text=digest_text,
                reply_markup=get_main_menu_keyboard(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            logger.info(f"Щоденний дайджест успішно надіслано користувачу {user_telegram_id}.")
        except Exception as e:
            logger.error(f"Помилка надсилання дайджесту користувачу {user_telegram_id}: {e}", exc_info=True)


# Функції для роботи з запрошеннями
async def generate_invite_code() -> str:
    return ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=8))

async def create_invite(inviter_user_db_id: int) -> Optional[str]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            invite_code = await generate_invite_code()
            try:
                await cur.execute(
                    """
                    INSERT INTO invitations (inviter_user_id, invite_code, created_at, status)
                    VALUES (%s, %s, CURRENT_TIMESTAMP, 'pending')
                    RETURNING invite_code;
                    """,
                    (inviter_user_db_id, invite_code)
                )
                await conn.commit()
                return invite_code
            except Exception as e:
                logger.error(f"Помилка створення запрошення для користувача {inviter_user_db_id}: {e}")
                return None

async def handle_invite_code(new_user_db_id: int, invite_code: str):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, inviter_user_id FROM invitations
                WHERE invite_code = %s AND status = 'pending' AND used_at IS NULL;
                """,
                (invite_code,)
            )
            invite_record = await cur.fetchone()

            if invite_record:
                invite_id, inviter_user_db_id = invite_record
                await cur.execute(
                    """
                    UPDATE invitations SET
                        used_at = CURRENT_TIMESTAMP,
                        status = 'accepted',
                        invitee_telegram_id = %s -- Зберігаємо Telegram ID нового користувача
                    WHERE id = %s;
                    """,
                    (new_user_db_id, invite_id)
                )
                await cur.execute(
                    "UPDATE users SET inviter_id = %s WHERE id = %s;",
                    (inviter_user_db_id, new_user_db_id)
                )
                # Тут можна додати логіку нагородження запрошувача, наприклад, преміум-статус
                # await cur.execute("UPDATE users SET is_premium = TRUE, premium_expires_at = NOW() + INTERVAL '30 days' WHERE id = %s;", (inviter_user_db_id,))
                logger.info(f"Запрошення {invite_code} використано користувачем {new_user_db_id}. Запрошувач: {inviter_user_db_id}")
                await conn.commit()
            else:
                logger.warning(f"Недійсний або використаний інвайт-код: {invite_code} для користувача {new_user_db_id}")

@router.message(Command("invite"))
async def command_invite_handler(message: Message):
    logger.info(f"Отримано команду /invite від користувача {message.from_user.id}")
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user:
        user = await create_or_update_user(message.from_user)
    
    invite_code = await create_invite(user.id)
    if invite_code:
        invite_link = f"https://t.me/{bot.me.username}?start={invite_code}"
        await message.answer(f"Ваш персональний інвайт-код: <code>{invite_code}</code>\n"
                             f"Поділіться цим посиланням, щоб запросити друзів: {hlink('Посилання для запрошення', invite_link)}",
                             parse_mode=ParseMode.HTML, disable_web_page_preview=False)
    else:
        await message.answer("Не вдалося згенерувати інвайт-код. Спробуйте пізніше.")


# Функції для статистики джерел
async def update_source_stats_publication_count(source_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO source_stats (source_id, publication_count, last_updated)
                VALUES (%s, 1, CURRENT_TIMESTAMP)
                ON CONFLICT (source_id) DO UPDATE SET
                    publication_count = source_stats.publication_count + 1,
                    last_updated = CURRENT_TIMESTAMP;
                """,
                (source_id,)
            )
            await conn.commit()
            logger.info(f"Оновлено статистику публікацій для джерела {source_id}")

@router.callback_query(F.data == "source_stats_menu") # Припустимо, що це буде кнопка в меню
async def handle_source_stats_menu(callback: CallbackQuery):
    logger.info(f"Отримано callback 'source_stats_menu' від користувача {callback.from_user.id}")
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT s.source_name, ss.publication_count
                FROM source_stats ss
                JOIN sources s ON ss.source_id = s.id
                ORDER BY ss.publication_count DESC
                LIMIT 10;
                """
            )
            stats = await cur.fetchall()
    
    if not stats:
        await callback.message.edit_text("Статистика джерел відсутня.", reply_markup=get_main_menu_keyboard())
        await callback.answer()
        return

    response_text = "<b>📊 Статистика джерел (топ-10 за публікаціями):</b>\n\n"
    for idx, stat in enumerate(stats):
        response_text += f"{idx+1}. {stat['source_name']}: {stat['publication_count']} публікацій\n"
    
    await callback.message.edit_text(response_text, reply_markup=get_main_menu_keyboard(), parse_mode=ParseMode.HTML)
    await callback.answer()

# NEW: Admin commands
@router.message(Command("moderation"))
async def command_moderation_handler(message: Message, state: FSMContext):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user or not user.is_admin:
        await message.answer("У вас немає доступу до цієї команди.", reply_markup=get_main_menu_keyboard())
        return

    logger.info(f"Адміністратор {message.from_user.id} викликав команду /moderation.")
    await message.answer("Завантажую новини на модерацію, зачекайте...")

    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id FROM news WHERE moderation_status = 'pending' ORDER BY published_at ASC;")
            pending_news_ids = [row['id'] for row in await cur.fetchall()]

    if not pending_news_ids:
        await message.answer("Немає новин на модерації.", reply_markup=get_main_menu_keyboard())
        await state.clear()
        return

    await state.update_data(moderation_news_ids=pending_news_ids, current_moderation_index=0)
    await state.set_state(ModerationStates.browsing_pending_news)

    await send_moderation_news_to_admin(message.chat.id, pending_news_ids[0], 0, len(pending_news_ids))


async def send_moderation_news_to_admin(chat_id: int, news_id: int, current_index: int, total_news: int):
    news_item = await get_news_by_id(news_id)
    if not news_item:
        logger.warning(f"Новина {news_id} не знайдена для модерації.")
        await bot.send_message(chat_id, "На жаль, ця новина не знайдена для модерації.")
        return

    source_info = await get_source_by_id(news_item.source_id)
    source_name = source_info['source_name'] if source_info else "Невідоме джерело"

    text = (
        f"<b>Новина на модерації ({current_index + 1} з {total_news}):</b>\n\n"
        f"<b>Заголовок:</b> {news_item.title}\n\n"
        f"<b>Зміст:</b>\n{news_item.content}\n\n"
        f"Джерело: {source_name} ({news_item.source_url})\n"
        f"Опубліковано: {news_item.published_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"Статус: {news_item.moderation_status}"
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Схвалити", callback_data=f"moderate_news_approve_{news_item.id}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"moderate_news_reject_{news_item.id}")
    )
    
    nav_buttons = []
    if current_index > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Попередня", callback_data="moderation_prev_news"))
    if current_index < total_news - 1:
        nav_buttons.append(InlineKeyboardButton(text="➡️ Наступна", callback_data="moderation_next_news"))
    
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(InlineKeyboardButton(text="⬅️ До головного меню", callback_data="main_menu"))

    if news_item.image_url:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=str(news_item.image_url),
                caption=text,
                reply_markup=builder.as_markup(),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.warning(f"Не вдалося надіслати фото для новини {news_id} для модерації: {e}. Надсилаю новину без фото.")
            await bot.send_message(
                chat_id=chat_id,
                text=text + f"\n[Зображення новини]({news_item.image_url})",
                reply_markup=builder.as_markup(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False
            )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )

@router.callback_query(ModerationStates.browsing_pending_news, F.data.startswith("moderate_news_"))
async def handle_moderation_action(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("У вас немає доступу до цієї дії.", show_alert=True)
        return

    parts = callback.data.split('_')
    action = parts[2] # 'approve' or 'reject'
    news_id = int(parts[3])

    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            new_status = 'approved' if action == 'approve' else 'rejected'
            await cur.execute(
                "UPDATE news SET moderation_status = %s WHERE id = %s RETURNING *;",
                (new_status, news_id)
            )
            updated_news = await cur.fetchone()
            await conn.commit()

            if updated_news:
                await callback.answer(f"Новину {news_id} {new_status}!", show_alert=True)
                logger.info(f"Новину {news_id} {new_status} адміністратором {user.telegram_id}.")
                
                # Якщо новина схвалена, відправляємо її в канал
                if new_status == 'approved':
                    await send_news_to_channel(News(**updated_news))

                # Видаляємо новину з поточного списку модерації
                user_data = await state.get_data()
                pending_news_ids = user_data.get("moderation_news_ids", [])
                current_index = user_data.get("current_moderation_index", 0)

                if news_id in pending_news_ids:
                    pending_news_ids.remove(news_id)
                    await state.update_data(moderation_news_ids=pending_news_ids)

                # Переходимо до наступної новини або завершуємо модерацію
                if pending_news_ids:
                    # Якщо поточний індекс вийшов за межі нового списку, ставимо його на останній елемент
                    if current_index >= len(pending_news_ids):
                        current_index = len(pending_news_ids) - 1
                    
                    await state.update_data(current_moderation_index=current_index)
                    await callback.message.delete() # Видаляємо попереднє повідомлення з новиною
                    await send_moderation_news_to_admin(callback.message.chat.id, pending_news_ids[current_index], current_index, len(pending_news_ids))
                else:
                    await callback.message.edit_text("Усі новини на модерації оброблено.", reply_markup=get_main_menu_keyboard())
                    await state.clear()
            else:
                await callback.answer("Помилка: новина не знайдена.", show_alert=True)
    await callback.answer()

@router.callback_query(ModerationStates.browsing_pending_news, F.data == "moderation_next_news")
async def handle_moderation_next_news(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("У вас немає доступу до цієї дії.", show_alert=True)
        return

    user_data = await state.get_data()
    pending_news_ids = user_data.get("moderation_news_ids", [])
    current_index = user_data.get("current_moderation_index", 0)

    if not pending_news_ids or current_index + 1 >= len(pending_news_ids):
        await callback.answer("Більше новин на модерації немає.", show_alert=True)
        return

    next_index = current_index + 1
    await state.update_data(current_moderation_index=next_index)
    await callback.message.delete() # Видаляємо попереднє повідомлення
    await send_moderation_news_to_admin(callback.message.chat.id, pending_news_ids[next_index], next_index, len(pending_news_ids))
    await callback.answer()

@router.callback_query(ModerationStates.browsing_pending_news, F.data == "moderation_prev_news")
async def handle_moderation_prev_news(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user or not user.is_admin:
        await callback.answer("У вас немає доступу до цієї дії.", show_alert=True)
        return

    user_data = await state.get_data()
    pending_news_ids = user_data.get("moderation_news_ids", [])
    current_index = user_data.get("current_moderation_index", 0)

    if not pending_news_ids or current_index <= 0:
        await callback.answer("Це перша новина на модерації.", show_alert=True)
        return

    prev_index = current_index - 1
    await state.update_data(current_moderation_index=prev_index)
    await callback.message.delete() # Видаляємо попереднє повідомлення
    await send_moderation_news_to_admin(callback.message.chat.id, pending_news_ids[prev_index], prev_index, len(pending_news_ids))
    await callback.answer()

# Планувальник завдань
async def scheduler():
    # Плануємо наступні запуски
    # Розклад для fetch_and_post_news_task: кожні 5 хвилин
    fetch_schedule_expression = '*/5 * * * *'
    # Розклад для delete_expired_news_task: кожні 5 годин (наприклад, о 0, 5, 10, 15, 20 годинах)
    delete_schedule_expression = '0 */5 * * *' 
    # Розклад для щоденного дайджесту (наприклад, о 9:00 UTC щодня)
    daily_digest_schedule_expression = '0 9 * * *'
    
    while True:
        now = datetime.now(timezone.utc)
        
        # Планування fetch_and_post_news_task
        fetch_itr = croniter(fetch_schedule_expression, now)
        next_fetch_run = fetch_itr.get_next(datetime)
        fetch_delay_seconds = (next_fetch_run - now).total_seconds()

        # Планування delete_expired_news_task
        delete_itr = croniter(delete_schedule_expression, now)
        next_delete_run = delete_itr.get_next(datetime)
        delete_delay_seconds = (next_delete_run - now).total_seconds()

        # Планування send_daily_digest
        daily_digest_itr = croniter(daily_digest_schedule_expression, now)
        next_daily_digest_run = daily_digest_itr.get_next(datetime)
        daily_digest_delay_seconds = (next_daily_digest_run - now).total_seconds()

        # Обираємо найменшу затримку до наступного запуску
        min_delay = min(fetch_delay_seconds, delete_delay_seconds, daily_digest_delay_seconds)
        
        logger.info(f"Наступний запуск fetch_and_post_news_task через {int(fetch_delay_seconds)} секунд.")
        logger.info(f"Наступний запуск delete_expired_news_task через {int(delete_delay_seconds)} секунд.")
        logger.info(f"Наступний запуск send_daily_digest через {int(daily_digest_delay_seconds)} секунд.")
        logger.info(f"Бот засинає на {int(min_delay)} секунд.")

        await asyncio.sleep(min_delay)

        # Виконуємо завдання, які мають бути запущені
        # Використовуємо невеликий допуск, щоб уникнути проблем з точністю часу
        # Запускаємо завдання, якщо поточний час >= запланованого часу
        current_utc_time = datetime.now(timezone.utc)
        if (current_utc_time - next_fetch_run).total_seconds() >= -1:
            await fetch_and_post_news_task()
        if (current_utc_time - next_delete_run).total_seconds() >= -1:
            await delete_expired_news_task()
        if (current_utc_time - next_daily_digest_run).total_seconds() >= -1:
            await send_daily_digest()


# FastAPI endpoints
@app.on_event("startup")
async def on_startup():
    # Встановлення вебхука Telegram
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        # Якщо WEBHOOK_URL не встановлено, використовуємо URL Render
        render_external_url = os.getenv("RENDER_EXTERNAL_HOSTNAME")
        if render_external_url:
            webhook_url = f"https://{render_external_url}/telegram_webhook"
        else:
            logger.error("Не вдалося визначити WEBHOOK_URL. Вебхук не буде встановлено.")
            return

    try:
        logger.info(f"Спроба встановити вебхук на: {webhook_url}")
        await bot.set_webhook(url=webhook_url)
        logger.info(f"Вебхук успішно встановлено на {webhook_url}")
    except Exception as e:
        logger.error(f"Помилка встановлення вебхука: {e}")

    # Запускаємо планувальник завдань у фоновому режимі
    asyncio.create_task(scheduler())
    logger.info("Додаток FastAPI запущено.")


@app.on_event("shutdown")
async def on_shutdown():
    if db_pool:
        await db_pool.close()
    await bot.session.close()
    logger.info("Додаток FastAPI вимкнено.")

@app.post("/telegram_webhook")
async def telegram_webhook(request: Request):
    logger.info("Отримано запит на /telegram_webhook")
    try:
        update = await request.json()
        logger.debug(f"Отримано оновлення Telegram (повний об'єкт): {update}") # Детальне логування
        aiogram_update = types.Update.model_validate(update, context={"bot": bot})
        logger.debug(f"Оновлення Telegram валідовано Aiogram: {aiogram_update}") # Детальне логування
        await dp.feed_update(bot, aiogram_update)
        logger.info("Успішно оброблено оновлення Telegram.")
    except Exception as e:
        logger.error(f"Помилка обробки вебхука Telegram: {e}", exc_info=True)
        # Важливо повертати 200 OK, навіть якщо сталася помилка, щоб Telegram не намагався повторно надсилати оновлення
    return {"ok": True}

# Додано обробник POST запитів для кореневого шляху
@app.post("/")
async def root_webhook(request: Request):
    logger.info("Отримано запит на / (root webhook)")
    try:
        update = await request.json()
        logger.debug(f"Отримано оновлення Telegram (повний об'єкт) на кореневому шляху: {update}")
        aiogram_update = types.Update.model_validate(update, context={"bot": bot})
        await dp.feed_update(bot, aiogram_update)
        logger.info("Успішно оброблено оновлення Telegram на кореневому шляху.")
    except Exception as e:
        logger.error(f"Помилка обробки вебхука Telegram на кореневому шляху: {e}", exc_info=True)
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/dashboard", response_class=HTMLResponse)
async def read_dashboard(api_key: str = Depends(get_api_key)):
    with open("dashboard.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/users", response_class=HTMLResponse)
async def read_users(api_key: str = Depends(get_api_key), limit: int = 10, offset: int = 0):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id, telegram_id, username, first_name, last_name, created_at, is_admin, last_active, language, auto_notifications, digest_frequency, safe_mode, current_feed_id, is_premium, premium_expires_at, level, badges, inviter_id, email, view_mode FROM users ORDER BY created_at DESC LIMIT %s OFFSET %s;", (limit, offset))
            users = await cur.fetchall()
            return {"users": users} # Обгортаємо у словник для відповідності очікуванням JS на фронтенді

@app.get("/reports", response_class=HTMLResponse)
async def read_reports(api_key: str = Depends(get_api_key), limit: int = 10, offset: int = 0):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            query = "SELECT r.*, u.username, u.first_name, n.title as news_title, n.source_url as news_source_url FROM reports r LEFT JOIN users u ON r.user_id = u.id LEFT JOIN news n ON r.target_id = n.id WHERE r.target_type = 'news'"
            params = []
            # if status: # Якщо буде потреба фільтрувати звіти за статусом
            #     query += " WHERE r.status = %s"
            #     params.append(status)
            query += " ORDER BY r.created_at DESC LIMIT %s OFFSET %s;"
            params.extend([limit, offset])
            await cur.execute(query, tuple(params))
            reports = await cur.fetchall()
            return reports

@app.get("/api/admin/sources")
async def get_admin_sources(api_key: str = Depends(get_api_key), limit: int = 100, offset: int = 0):
    """
    Отримує список усіх джерел з бази даних.
    """
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # Включаємо added_at у запит
            await cur.execute("SELECT id, user_id, source_name, source_url, source_type, status, added_at, last_parsed, parse_frequency FROM sources ORDER BY added_at DESC LIMIT %s OFFSET %s;", (limit, offset))
            sources = await cur.fetchall()
            return [Source(**s) for s in sources]


@app.get("/api/admin/stats")
async def get_admin_stats(api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT COUNT(*) AS total_users FROM users;")
            total_users = (await cur.fetchone())['total_users']

            await cur.execute("SELECT COUNT(*) AS total_news FROM news;")
            total_news = (await cur.fetchone())['total_news']

            # Активні користувачі за останні 24 години (оновлено з 7 днів)
            await cur.execute("SELECT COUNT(DISTINCT telegram_id) AS active_users_count FROM users WHERE last_active >= NOW() - INTERVAL '24 hours';")
            active_users_count = (await cur.fetchone())['active_users_count']

            return {
                "total_users": total_users,
                "total_news": total_news,
                "active_users_count": active_users_count
            }

@app.get("/api/admin/news")
async def get_admin_news(api_key: str = Depends(get_api_key), limit: int = 10, offset: int = 0, status: Optional[str] = None):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            query = "SELECT n.*, s.source_name FROM news n JOIN sources s ON n.source_id = s.id"
            params = []
            if status:
                query += " WHERE n.moderation_status = %s"
                params.append(status)
            query += " ORDER BY n.published_at DESC LIMIT %s OFFSET %s;"
            params.extend([limit, offset])
            await cur.execute(query, tuple(params))
            news = await cur.fetchall()
            return news

@app.get("/api/admin/news/counts_by_status")
async def get_news_counts_by_status(api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT moderation_status, COUNT(*) FROM news GROUP BY moderation_status;")
            counts = {row['moderation_status']: row['count'] for row in await cur.fetchall()}
            return counts

@app.put("/api/admin/news/{news_id}")
async def update_admin_news(news_id: int, news: News, api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            params = [
                news.source_id, news.title, news.content, str(news.source_url),
                str(news.image_url) if news.image_url else None, news.published_at,
                news.ai_summary, json.dumps(news.ai_classified_topics) if news.ai_classified_topics else None,
                news.moderation_status, news.expires_at, news_id
            ]
            await cur.execute(
                """
                UPDATE news SET
                    source_id = %s, title = %s, content = %s, source_url = %s,
                    image_url = %s, published_at = %s, ai_summary = %s,
                    ai_classified_topics = %s, moderation_status = %s, expires_at = %s
                WHERE id = %s
                RETURNING *;
                """,
                tuple(params)
            )
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

# Додано для локального запуску Uvicorn
if __name__ == "__main__":
    import uvicorn
    # Запускаємо вебхук у фоновому режимі, якщо WEBHOOK_URL не встановлено
    # У продакшні WEBHOOK_URL буде встановлено через Render ENV
    if not os.getenv("WEBHOOK_URL"):
        # Для локального тестування, якщо не використовується ngrok/аналог
        # Можна запустити polling для бота замість вебхука
        # Або налаштувати ngrok і вказати WEBHOOK_URL
        logger.info("WEBHOOK_URL не встановлено. Запускаю бота в режимі polling.")
        async def start_polling():
            await dp.start_polling(bot)
        asyncio.run(start_polling())
    
    # Запускаємо FastAPI
    uvicorn.run(app, host="0.0.0.0", port=8000)
