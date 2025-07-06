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
import re # Додано для перевірки URL зображень

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, InputFile
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
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")

# Налаштування логування
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Ініціалізація FastAPI
app = FastAPI()

# Налаштування статичних файлів
app.mount("/static", StaticFiles(directory="static"), name="static")

# Залежність для перевірки API ключа
api_key_header = APIKeyHeader(name="X-API-Key")

async def get_api_key(api_key: str = Depends(api_key_header)):
    if api_key == ADMIN_API_KEY:
        return api_key
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API Key")

# Модель для новин
class News(BaseModel):
    id: Optional[int] = None
    source_id: str
    title: str
    content: str
    source_url: str
    image_url: Optional[str] = None
    published_at: datetime
    ai_summary: Optional[str] = None
    ai_classified_topics: Optional[List[str]] = None
    moderation_status: str = "pending"
    expires_at: Optional[datetime] = None
    is_published_to_channel: bool = False

# Модель для користувачів
class User(BaseModel):
    id: Optional[int] = None
    telegram_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    language_code: Optional[str] = None
    is_premium: Optional[bool] = False
    last_activity: datetime = Field(default_factory=datetime.now)
    state: Optional[str] = None
    last_news_id: Optional[int] = None
    last_news_index: Optional[int] = None
    selected_sources: Optional[List[str]] = None
    selected_topics: Optional[List[str]] = None
    ai_requests_today: int = 0 # Додано для відстеження запитів до AI
    ai_last_request_date: Optional[datetime] = None # Додано для відстеження дати останнього запиту до AI

# Модель для підписок
class Subscription(BaseModel):
    id: Optional[int] = None
    user_id: int
    source_id: str
    subscribed_at: datetime = Field(default_factory=datetime.now)

# Модель для реакцій
class Reaction(BaseModel):
    id: Optional[int] = None
    user_id: int
    news_id: int
    reaction_type: str
    created_at: datetime = Field(default_factory=datetime.now)

# Модель для AI запитів
class AIRequest(BaseModel):
    id: Optional[int] = None
    user_id: int
    request_text: str
    response_text: str
    created_at: datetime = Field(default_factory=datetime.now)

# Налаштування пулу з'єднань з базою даних
db_pool: Optional[AsyncConnectionPool] = None

async def get_db_pool():
    global db_pool
    if db_pool is None:
        db_pool = AsyncConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10)
        await db_pool.wait()
        logger.info("Database connection pool initialized.")
    return db_pool

# Ініціалізація бота
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()

# Стан для FSM
class Gen(StatesGroup):
    choosing_source = State()
    choosing_topic = State()
    waiting_for_ai_prompt = State()
    waiting_for_expert_question = State() # Новий стан для запитання експерта
    waiting_for_sell_prompt = State() # Новий стан для допомоги продати
    waiting_for_buy_prompt = State() # Новий стан для допомоги купити

# --- Допоміжні функції для роботи з базою даних ---

async def get_user(telegram_id: int) -> Optional[User]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
            user_data = await cur.fetchone()
            return User(**user_data) if user_data else None

async def create_or_update_user(message: Message) -> User:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            telegram_id = message.from_user.id
            username = message.from_user.username
            first_name = message.from_user.first_name
            last_name = message.from_user.last_name
            language_code = message.from_user.language_code
            is_premium = message.from_user.is_premium if message.from_user.is_premium is not None else False

            user = await get_user(telegram_id)
            if user:
                params = [username, first_name, last_name, language_code, is_premium, datetime.now(), telegram_id]
                await cur.execute(
                    """
                    UPDATE users SET username = %s, first_name = %s, last_name = %s,
                    language_code = %s, is_premium = %s, last_activity = %s
                    WHERE telegram_id = %s RETURNING *;
                    """,
                    tuple(params)
                )
            else:
                params = [telegram_id, username, first_name, last_name, language_code, is_premium, datetime.now()]
                await cur.execute(
                    """
                    INSERT INTO users (telegram_id, username, first_name, last_name, language_code, is_premium, last_activity)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *;
                    """,
                    tuple(params)
                )
            updated_user_data = await cur.fetchone()
            return User(**updated_user_data)

async def update_user_state(telegram_id: int, state: Optional[str], last_news_id: Optional[int] = None, last_news_index: Optional[int] = None):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            params = [state, datetime.now(), telegram_id]
            query = "UPDATE users SET state = %s, last_activity = %s"
            if last_news_id is not None:
                query += ", last_news_id = %s"
                params.insert(-1, last_news_id) # Вставити перед telegram_id
            if last_news_index is not None:
                query += ", last_news_index = %s"
                params.insert(-1, last_news_index) # Вставити перед telegram_id
            query += " WHERE telegram_id = %s;"
            await cur.execute(query, tuple(params))

async def update_user_ai_requests_today(telegram_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # Отримати поточну дату
            today = datetime.now(timezone.utc).date()
            
            # Отримати користувача
            user = await get_user(telegram_id)
            if user:
                # Перевірити, чи останній запит був не сьогодні
                if user.ai_last_request_date and user.ai_last_request_date.date() == today:
                    new_requests_today = user.ai_requests_today + 1
                else:
                    new_requests_today = 1 # Скинути лічильник, якщо новий день

                await cur.execute(
                    """
                    UPDATE users SET ai_requests_today = %s, ai_last_request_date = %s
                    WHERE telegram_id = %s;
                    """,
                    (new_requests_today, today, telegram_id)
                )
                logger.info(f"User {telegram_id} AI requests today updated to {new_requests_today}")
            else:
                logger.warning(f"User {telegram_id} not found for AI request update.")


async def get_news_by_id(news_id: int) -> Optional[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM news WHERE id = %s", (news_id,))
            news_data = await cur.fetchone()
            return News(**news_data) if news_data else None

async def get_all_news_ids() -> List[int]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id FROM news ORDER BY published_at DESC")
            return [row['id'] for row in await cur.fetchall()]

async def get_news_for_channel_posting() -> List[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # Отримати новини, опубліковані сьогодні, які ще не були опубліковані в каналі
            today = datetime.now(timezone.utc).date()
            await cur.execute(
                """
                SELECT * FROM news
                WHERE is_published_to_channel = FALSE
                AND published_at::date = %s
                ORDER BY published_at ASC;
                """,
                (today,)
            )
            news_data = await cur.fetchall()
            return [News(**data) for data in news_data]

async def mark_news_as_published(news_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE news SET is_published_to_channel = TRUE WHERE id = %s;",
                (news_id,)
            )

async def get_user_subscriptions(user_id: int) -> List[Subscription]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM subscriptions WHERE user_id = %s", (user_id,))
            subs_data = await cur.fetchall()
            return [Subscription(**data) for data in subs_data]

async def add_subscription(user_id: int, source_id: str):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO subscriptions (user_id, source_id) VALUES (%s, %s) ON CONFLICT (user_id, source_id) DO NOTHING;",
                (user_id, source_id)
            )

async def remove_subscription(user_id: int, source_id: str):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "DELETE FROM subscriptions WHERE user_id = %s AND source_id = %s;",
                (user_id, source_id)
            )

async def get_news_reactions(news_id: int) -> List[Reaction]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM reactions WHERE news_id = %s", (news_id,))
            reactions_data = await cur.fetchall()
            return [Reaction(**data) for data in reactions_data]

async def add_reaction(user_id: int, news_id: int, reaction_type: str):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO reactions (user_id, news_id, reaction_type) VALUES (%s, %s, %s) ON CONFLICT (user_id, news_id) DO UPDATE SET reaction_type = EXCLUDED.reaction_type;",
                (user_id, news_id, reaction_type)
            )

async def add_ai_request(user_id: int, request_text: str, response_text: str):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "INSERT INTO ai_requests (user_id, request_text, response_text) VALUES (%s, %s, %s);",
                (user_id, request_text, response_text)
            )

# --- Клавіатури ---

MAIN_MENU_BUTTON_TEXT = {
    'uk': "Головне меню 🏠",
    'en': "Main Menu 🏠"
}

AI_MEDIA_BUTTON_TEXT = {
    'uk': "AI Медіа 🤖",
    'en': "AI Media 🤖"
}

MY_NEWS_BUTTON_TEXT = {
    'uk': "Мої новини 📰",
    'en': "My News 📰"
}

SETTINGS_BUTTON_TEXT = {
    'uk': "Налаштування ⚙️",
    'en': "Settings ⚙️"
}

HELP_BUTTON_TEXT = {
    'uk': "Допомога ❓",
    'en': "Help ❓"
}

# Нові кнопки для AI Media
ASK_EXPERT_BUTTON_TEXT = {
    'uk': "Запитати експерта 🧑‍🏫",
    'en': "Ask an Expert 🧑‍🏫"
}

MY_SUBSCRIPTIONS_BUTTON_TEXT = {
    'uk': "Мої підписки 🔔",
    'en': "My Subscriptions 🔔"
}

HELP_SELL_BUTTON_TEXT = {
    'uk': "Допоможи продати 💰",
    'en': "Help Sell 💰"
}

HELP_BUY_BUTTON_TEXT = {
    'uk': "Допоможи купити 🛍️",
    'en': "Help Buy 🛍️"
}

BACK_BUTTON_TEXT = {
    'uk': "⬅️ Назад",
    'en': "⬅️ Back"
}

def get_main_menu_keyboard(lang: str = 'uk') -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text=AI_MEDIA_BUTTON_TEXT.get(lang, "AI Media 🤖"), callback_data="ai_media"))
    builder.add(InlineKeyboardButton(text=MY_NEWS_BUTTON_TEXT.get(lang, "My News 📰"), callback_data="my_news"))
    builder.add(InlineKeyboardButton(text=SETTINGS_BUTTON_TEXT.get(lang, "Settings ⚙️"), callback_data="settings"))
    builder.add(InlineKeyboardButton(text=HELP_BUTTON_TEXT.get(lang, "Help ❓"), callback_data="help"))
    builder.adjust(2) # Використовуємо adjust замість row_width
    return builder.as_markup()

def get_ai_media_keyboard(lang: str = 'uk') -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text=ASK_EXPERT_BUTTON_TEXT.get(lang, "Ask an Expert 🧑‍🏫"), callback_data="ask_expert"))
    builder.add(InlineKeyboardButton(text=MY_SUBSCRIPTIONS_BUTTON_TEXT.get(lang, "My Subscriptions 🔔"), callback_data="my_subscriptions"))
    builder.add(InlineKeyboardButton(text=HELP_SELL_BUTTON_TEXT.get(lang, "Help Sell 💰"), callback_data="help_sell"))
    builder.add(InlineKeyboardButton(text=HELP_BUY_BUTTON_TEXT.get(lang, "Help Buy 🛍️"), callback_data="help_buy"))
    builder.add(InlineKeyboardButton(text=BACK_BUTTON_TEXT.get(lang, "⬅️ Back"), callback_data="back_to_main_menu"))
    builder.adjust(2, 2, 1) # Використовуємо adjust для гнучкого розташування кнопок
    return builder.as_markup()

def get_settings_keyboard(lang: str = 'uk') -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="Вибрати джерела 🌐", callback_data="choose_sources"))
    builder.add(InlineKeyboardButton(text="Вибрати теми 🏷️", callback_data="choose_topics"))
    builder.add(InlineKeyboardButton(text=BACK_BUTTON_TEXT.get(lang, "⬅️ Back"), callback_data="back_to_main_menu"))
    builder.adjust(2)
    return builder.as_markup()

def get_sources_keyboard(lang: str = 'uk', selected_sources: List[str] = None) -> InlineKeyboardMarkup:
    if selected_sources is None:
        selected_sources = []
    builder = InlineKeyboardBuilder()
    sources = ["ukrinform.ua", "pravda.com.ua", "nv.ua"] # Приклад джерел
    for source in sources:
        text = f"{source} {'✅' if source in selected_sources else ''}"
        builder.add(InlineKeyboardButton(text=text, callback_data=f"select_source_{source}"))
    builder.add(InlineKeyboardButton(text=BACK_BUTTON_TEXT.get(lang, "⬅️ Back"), callback_data="back_to_settings"))
    builder.adjust(1)
    return builder.as_markup()

def get_topics_keyboard(lang: str = 'uk', selected_topics: List[str] = None) -> InlineKeyboardMarkup:
    if selected_topics is None:
        selected_topics = []
    builder = InlineKeyboardBuilder()
    topics = ["Політика", "Економіка", "Спорт", "Технології", "Культура"] # Приклад тем
    for topic in topics:
        text = f"{topic} {'✅' if topic in selected_topics else ''}"
        builder.add(InlineKeyboardButton(text=text, callback_data=f"select_topic_{topic}"))
    builder.add(InlineKeyboardButton(text=BACK_BUTTON_TEXT.get(lang, "⬅️ Back"), callback_data="back_to_settings"))
    builder.adjust(2)
    return builder.as_markup()

def get_news_navigation_keyboard(news_id: int, current_index: int, total_news: int, lang: str = 'uk') -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if current_index > 0:
        builder.add(InlineKeyboardButton(text="⬅️ Попередня", callback_data=f"prev_news_{news_id}"))
    builder.add(InlineKeyboardButton(text=f"{current_index + 1}/{total_news}", callback_data="current_news_index"))
    if current_index < total_news - 1:
        builder.add(InlineKeyboardButton(text="Наступна ➡️", callback_data=f"next_news_{news_id}"))
    builder.add(InlineKeyboardButton(text=BACK_BUTTON_TEXT.get(lang, "⬅️ Back"), callback_data="back_to_main_menu"))
    builder.adjust(3, 1) # Використовуємо adjust
    return builder.as_markup()

def get_news_reactions_keyboard(news_id: int, lang: str = 'uk') -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="👍", callback_data=f"react_like_{news_id}"))
    builder.add(InlineKeyboardButton(text="👎", callback_data=f"react_dislike_{news_id}"))
    builder.add(InlineKeyboardButton(text="❤️", callback_data=f"react_heart_{news_id}"))
    builder.add(InlineKeyboardButton(text="🤔", callback_data=f"react_think_{news_id}"))
    return builder.as_markup()

# --- Обробники команд та колбеків ---

@router.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext) -> None:
    user = await create_or_update_user(message)
    lang = user.language_code if user.language_code else 'uk'
    await state.clear()
    await message.answer(
        text=f"Привіт, {hbold(message.from_user.full_name)}! 👋\n"
             f"Я ваш особистий AI-асистент для новин та медіа. Виберіть опцію нижче:",
        reply_markup=get_main_menu_keyboard(lang)
    )
    logger.info(f"User {message.from_user.id} started bot.")

@router.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu_callback(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    await state.clear()
    await callback.message.edit_text(
        text="Ви повернулись до головного меню 🏠. Виберіть опцію:",
        reply_markup=get_main_menu_keyboard(lang)
    )
    await callback.answer()

@router.callback_query(F.data == "ai_media")
async def ai_media_callback(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    await state.clear()
    await callback.message.edit_text(
        text="Ласкаво просимо до AI Медіа! 🤖\n"
             "Тут ви можете взаємодіяти з AI для отримання інформації.",
        reply_markup=get_ai_media_keyboard(lang)
    )
    await callback.answer()

@router.callback_query(F.data == "my_news")
async def handle_my_news_command(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    all_news_ids = await get_all_news_ids()
    if not all_news_ids:
        await callback.message.edit_text("Наразі немає доступних новин. 😔")
        await callback.answer()
        return

    # Спробувати відновити останню новину, яку переглядав користувач
    last_news_id = user.last_news_id
    last_news_index = user.last_news_index

    news_to_show_id = None
    news_to_show_index = 0

    if last_news_id and last_news_id in all_news_ids:
        try:
            news_to_show_index = all_news_ids.index(last_news_id)
            news_to_show_id = last_news_id
        except ValueError:
            # Новина не знайдена, почнемо з першої
            news_to_show_id = all_news_ids[0]
            news_to_show_index = 0
    else:
        news_to_show_id = all_news_ids[0]
        news_to_show_index = 0

    await send_news_to_user(callback.message.chat.id, news_to_show_id, news_to_show_index, len(all_news_ids), state)
    await callback.answer()

async def send_news_to_user(chat_id: int, news_id: int, current_index: int, total_news: int, state: FSMContext):
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await bot.send_message(chat_id, "Новина не знайдена. 😥")
        return

    user = await get_user(chat_id)
    lang = user.language_code if user.language_code else 'uk'

    text = (
        f"<b>{news_item.title}</b>\n\n"
        f"{news_item.content}\n\n"
        f"Джерело: {hlink(news_item.source_id, news_item.source_url)}\n"
        f"Опубліковано: {news_item.published_at.strftime('%Y-%m-%d %H:%M')}"
    )
    if news_item.ai_summary:
        text += f"\n\n<b>AI Резюме:</b> {news_item.ai_summary}"
    if news_item.ai_classified_topics:
        text += f"\n<b>Теми:</b> {', '.join(news_item.ai_classified_topics)}"

    # Оновлення стану користувача для навігації
    await update_user_state(chat_id, state.get_current(), news_id, current_index)

    keyboard_builder = InlineKeyboardBuilder()
    
    # Додаємо кнопки реакцій
    reactions_keyboard = get_news_reactions_keyboard(news_item.id, lang).inline_keyboard[0]
    keyboard_builder.row(*reactions_keyboard) # Додаємо кнопки реакцій в один ряд

    # Додаємо кнопки навігації
    nav_keyboard = get_news_navigation_keyboard(news_item.id, current_index, total_news, lang).inline_keyboard
    for row in nav_keyboard:
        keyboard_builder.row(*row) # Додаємо ряди навігації

    try:
        if news_item.image_url and re.match(r'^https?://.*\.(jpeg|jpg|png|gif|bmp)$', news_item.image_url, re.IGNORECASE):
            await bot.send_photo(chat_id=chat_id, photo=news_item.image_url, caption=text, parse_mode=ParseMode.HTML, reply_markup=keyboard_builder.as_markup())
        else:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard_builder.as_markup())
            logger.warning(f"Invalid image URL for news_id {news_id}: {news_item.image_url}. Sending message without photo.")
    except Exception as e:
        logger.error(f"Помилка відправки новини {news_id} користувачу {chat_id}: {e}")
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard_builder.as_markup())


@router.callback_query(F.data.startswith("prev_news_"))
async def handle_prev_news_callback(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[2])
    all_news_ids = await get_all_news_ids()
    try:
        current_index = all_news_ids.index(news_id)
        if current_index > 0:
            prev_news_id = all_news_ids[current_index - 1]
            await send_news_to_user(callback.message.chat.id, prev_news_id, current_index - 1, len(all_news_ids), state)
        else:
            await callback.answer("Це перша новина. 🤷‍♀️")
    except ValueError:
        await callback.answer("Новина не знайдена. 😥")
    await callback.answer()

@router.callback_query(F.data.startswith("next_news_"))
async def handle_next_news_callback(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[2])
    all_news_ids = await get_all_news_ids()
    try:
        current_index = all_news_ids.index(news_id)
        if current_index < len(all_news_ids) - 1:
            next_news_id = all_news_ids[current_index + 1]
            await send_news_to_user(callback.message.chat.id, next_news_id, current_index + 1, len(all_news_ids), state)
        else:
            await callback.answer("Це остання новина. 🤷‍♀️")
    except ValueError:
        await callback.answer("Новина не знайдена. 😥")
    await callback.answer()

@router.callback_query(F.data.startswith("react_"))
async def handle_reaction_callback(callback: CallbackQuery):
    parts = callback.data.split('_')
    reaction_type = parts[1]
    news_id = int(parts[2])
    user_id = callback.from_user.id
    await add_reaction(user_id, news_id, reaction_type)
    await callback.answer(f"Ви поставили реакцію: {reaction_type}!")

@router.callback_query(F.data == "settings")
async def settings_callback(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    await state.clear()
    await callback.message.edit_text(
        text="Налаштування ⚙️. Виберіть, що бажаєте налаштувати:",
        reply_markup=get_settings_keyboard(lang)
    )
    await callback.answer()

@router.callback_query(F.data == "back_to_settings")
async def back_to_settings_callback(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    await state.clear()
    await callback.message.edit_text(
        text="Ви повернулись до налаштувань ⚙️. Виберіть, що бажаєте налаштувати:",
        reply_markup=get_settings_keyboard(lang)
    )
    await callback.answer()

@router.callback_query(F.data == "choose_sources")
async def choose_sources_callback(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    await state.set_state(Gen.choosing_source)
    await callback.message.edit_text(
        text="Виберіть джерела новин 🌐:",
        reply_markup=get_sources_keyboard(lang, user.selected_sources if user else [])
    )
    await callback.answer()

@router.callback_query(F.data.startswith("select_source_"))
async def select_source_callback(callback: CallbackQuery, state: FSMContext):
    source_id = callback.data.split('_')[2]
    user = await get_user(callback.from_user.id)
    lang = user.language_code if user.language_code else 'uk'

    if user:
        current_sources = user.selected_sources if user.selected_sources else []
        if source_id in current_sources:
            current_sources.remove(source_id)
            await remove_subscription(user.id, source_id)
            await callback.answer(f"Ви відписались від {source_id} ❌")
        else:
            current_sources.append(source_id)
            await add_subscription(user.id, source_id)
            await callback.answer(f"Ви підписались на {source_id} ✅")
        
        # Оновлюємо selected_sources у об'єкті User
        user.selected_sources = current_sources
        
        await callback.message.edit_reply_markup(
            reply_markup=get_sources_keyboard(lang, current_sources)
        )
    else:
        await callback.answer("Користувача не знайдено.")

@router.callback_query(F.data == "choose_topics")
async def choose_topics_callback(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    await state.set_state(Gen.choosing_topic)
    await callback.message.edit_text(
        text="Виберіть теми новин 🏷️:",
        reply_markup=get_topics_keyboard(lang, user.selected_topics if user else [])
    )
    await callback.answer()

@router.callback_query(F.data.startswith("select_topic_"))
async def select_topic_callback(callback: CallbackQuery, state: FSMContext):
    topic = callback.data.split('_')[2]
    user = await get_user(callback.from_user.id)
    lang = user.language_code if user.language_code else 'uk'

    if user:
        current_topics = user.selected_topics if user.selected_topics else []
        if topic in current_topics:
            current_topics.remove(topic)
            await callback.answer(f"Тема '{topic}' видалена ❌")
        else:
            current_topics.append(topic)
            await callback.answer(f"Тема '{topic}' додана ✅")
        
        # Оновлюємо selected_topics у об'єкті User
        user.selected_topics = current_topics
        
        await callback.message.edit_reply_markup(
            reply_markup=get_topics_keyboard(lang, current_topics)
        )
    else:
        await callback.answer("Користувача не знайдено.")

@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    await state.clear()
    await callback.message.edit_text(
        text="Це розділ допомоги. ❓\n"
             "Тут буде інформація про використання бота.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=BACK_BUTTON_TEXT.get(lang, "⬅️ Back"), callback_data="back_to_main_menu")]
        ])
    )
    await callback.answer()

# --- Нові обробники для AI Media ---

@router.callback_query(F.data == "ask_expert")
async def handle_ask_expert(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    await state.set_state(Gen.waiting_for_expert_question)
    await callback.message.edit_text(
        text="Задайте своє питання експерту 🧑‍🏫. Я постараюся надати вам вичерпну відповідь.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=BACK_BUTTON_TEXT.get(lang, "⬅️ Back"), callback_data="ai_media")]
        ])
    )
    await callback.answer()

@router.message(Gen.waiting_for_expert_question)
async def process_expert_question(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    await message.answer("Дякую за ваше питання! ⏳ Обробляю запит...")
    
    # Тут має бути логіка виклику AI моделі для відповіді на питання експерта
    # Приклад:
    # response_from_ai = await call_gemini_api(message.text)
    response_from_ai = f"Я експерт AI і можу відповісти на ваше питання: '{message.text}'. На жаль, ця функція ще в розробці. 😅"
    
    await add_ai_request(user.id, message.text, response_from_ai)
    await update_user_ai_requests_today(user.telegram_id) # Оновлення лічильника запитів

    await message.answer(
        text=response_from_ai,
        reply_markup=get_ai_media_keyboard(lang)
    )
    await state.clear()

@router.callback_query(F.data == "my_subscriptions")
async def handle_my_subscriptions(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    subscriptions = await get_user_subscriptions(user.id)
    
    if subscriptions:
        subs_text = "Ваші поточні підписки 🔔:\n" + "\n".join([f"- {s.source_id}" for s in subscriptions])
    else:
        subs_text = "У вас поки немає підписок. 😞"

    await callback.message.edit_text(
        text=subs_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=BACK_BUTTON_TEXT.get(lang, "⬅️ Back"), callback_data="ai_media")]
        ])
    )
    await callback.answer()

@router.callback_query(F.data == "help_sell")
async def handle_help_sell(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    await state.set_state(Gen.waiting_for_sell_prompt)
    await callback.message.edit_text(
        text="Я можу допомогти вам продати щось! 💰 Опишіть товар/послугу, яку ви хочете продати, і я запропоную текст оголошення або стратегію.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=BACK_BUTTON_TEXT.get(lang, "⬅️ Back"), callback_data="ai_media")]
        ])
    )
    await callback.answer()

@router.message(Gen.waiting_for_sell_prompt)
async def process_sell_prompt(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    await message.answer("Дякую! ⏳ Генерую пропозиції для продажу...")
    
    # Тут має бути логіка виклику AI моделі для генерації тексту для продажу
    # Приклад:
    # response_from_ai = await call_gemini_api(f"Створи текст для продажу: {message.text}")
    response_from_ai = f"Для продажу '{message.text}' я б запропонував:\n\n*Заголовок:* [Привабливий заголовок]\n*Опис:* [Детальний опис з перевагами]\n*Заклик до дії:* [Купити зараз!] \n\nЦя функція ще в розробці. 😅"
    
    await add_ai_request(user.id, message.text, response_from_ai)
    await update_user_ai_requests_today(user.telegram_id)

    await message.answer(
        text=response_from_ai,
        reply_markup=get_ai_media_keyboard(lang)
    )
    await state.clear()

@router.callback_query(F.data == "help_buy")
async def handle_help_buy(callback: CallbackQuery, state: FSMContext):
    user = await get_user(callback.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    await state.set_state(Gen.waiting_for_buy_prompt)
    await callback.message.edit_text(
        text="Я можу допомогти вам купити щось! 🛍️ Опишіть, що ви шукаєте, і я спробую знайти найкращі варіанти або поради.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=BACK_BUTTON_TEXT.get(lang, "⬅️ Back"), callback_data="ai_media")]
        ])
    )
    await callback.answer()

@router.message(Gen.waiting_for_buy_prompt)
async def process_buy_prompt(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    lang = user.language_code if user.language_code else 'uk'
    await message.answer("Дякую! ⏳ Шукаю найкращі варіанти для покупки...")
    
    # Тут має бути логіка виклику AI моделі для допомоги з покупкою
    # Приклад:
    # response_from_ai = await call_gemini_api(f"Допоможи знайти: {message.text}")
    response_from_ai = f"Для покупки '{message.text}' я б рекомендував:\n\n*Варіант 1:* [Назва товару/послуги] - [Де опис]\n*Варіант 2:* [Назва товару/послуги] - [Де опис]\n\nЦя функція ще в розробці. 😅"
    
    await add_ai_request(user.id, message.text, response_from_ai)
    await update_user_ai_requests_today(user.telegram_id)

    await message.answer(
        text=response_from_ai,
        reply_markup=get_ai_media_keyboard(lang)
    )
    await state.clear()

# --- Функції для AI генерації (заглушки) ---

async def call_gemini_api(prompt: str) -> str:
    # Це заглушка. Тут має бути реальний виклик Gemini API.
    # Для реального використання потрібно буде налаштувати автентифікацію та запити.
    # Приклад:
    # headers = {"Content-Type": "application/json"}
    # payload = {"contents": [{"parts": [{"text": prompt}]}]}
    # async with ClientSession() as session:
    #     async with session.post(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}",
    #                             headers=headers, json=payload) as response:
    #         if response.status == 200:
    #             result = await response.json()
    #             return result['candidates'][0]['content']['parts'][0]['text']
    #         else:
    #             logger.error(f"Gemini API error: {response.status} - {await response.text()}")
    #             return "На жаль, не вдалося отримати відповідь від AI. Спробуйте пізніше."
    return f"AI відповідь на запит: '{prompt}' (ця функція в розробці)."

# --- Планувальник завдань ---

news_posting_queue = asyncio.Queue() # Черга для новин, які потрібно опублікувати
posted_news_ids = set() # Зберігаємо ID вже опублікованих новин, щоб уникнути дублікатів

async def fetch_and_post_news_task():
    logger.info("Запускаю fetch_and_post_news_task.")
    while True:
        try:
            # Очищаємо чергу та список опублікованих новин, щоб брати свіжі новини за сьогодні
            # Якщо потрібно зберігати історію опублікованих новин за сесію, цю частину можна змінити
            while not news_posting_queue.empty():
                await news_posting_queue.get()
            posted_news_ids.clear()

            news_to_post = await get_news_for_channel_posting()
            if not news_to_post:
                logger.info("Немає нових новин для публікації за сьогодні. Чекаю...")
                # Чекаємо 200 секунд, якщо новин немає, і спробуємо знову
                await asyncio.sleep(200)
                continue

            for news_item in news_to_post:
                if news_item.id not in posted_news_ids:
                    await news_posting_queue.put(news_item)
                    posted_news_ids.add(news_item.id)

            while not news_posting_queue.empty():
                news_item = await news_posting_queue.get()
                try:
                    await send_news_to_channel(news_item)
                    await mark_news_as_published(news_item.id)
                    logger.info(f"Новина '{news_item.title}' опублікована в каналі.")
                except Exception as e:
                    logger.error(f"Помилка публікації '{news_item.title}': {e}")
                await asyncio.sleep(200) # Чекаємо 200 секунд між публікаціями

            logger.info("Всі доступні новини за сьогодні опубліковані. Чекаю на нові.")
            await asyncio.sleep(200) # Чекаємо 200 секунд, якщо черга спорожніла
        except Exception as e:
            logger.error(f"Помилка у fetch_and_post_news_task: {e}")
            await asyncio.sleep(60) # Чекаємо хвилину перед повторною спробою у випадку помилки

async def send_news_to_channel(news_item: News):
    channel_identifier = CHANNEL_ID
    if not channel_identifier:
        logger.error("CHANNEL_ID не встановлено. Новини не можуть бути опубліковані.")
        return

    text = (
        f"<b>{news_item.title}</b>\n\n"
        f"{news_item.content}\n\n"
        f"Джерело: {hlink(news_item.source_id, news_item.source_url)}\n"
        f"Опубліковано: {news_item.published_at.strftime('%Y-%m-%d %H:%M')}"
    )
    if news_item.ai_summary:
        text += f"\n\n<b>AI Резюме:</b> {news_item.ai_summary}"
    if news_item.ai_classified_topics:
        text += f"\n<b>Теми:</b> {', '.join(news_item.ai_classified_topics)}"

    try:
        if news_item.image_url and re.match(r'^https?://.*\.(jpeg|jpg|png|gif|bmp)$', news_item.image_url, re.IGNORECASE):
            await bot.send_photo(chat_id=channel_identifier, photo=news_item.image_url, caption=text, parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(chat_id=channel_identifier, text=text, parse_mode=ParseMode.HTML)
            logger.warning(f"Invalid image URL for news_id {news_item.id}: {news_item.image_url}. Sending message without photo to channel.")
    except Exception as e:
        logger.error(f"Помилка публікації новини '{news_item.title}' в канал: {e}")
        # Спроба відправити без фото, якщо з фото не вдалося
        try:
            await bot.send_message(chat_id=channel_identifier, text=text, parse_mode=ParseMode.HTML)
        except Exception as e_fallback:
            logger.error(f"Помилка публікації новини '{news_item.title}' в канал навіть без фото: {e_fallback}")


async def delete_expired_news_task():
    logger.info("Запускаю delete_expired_news_task.")
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            while True:
                try:
                    now = datetime.now(timezone.utc)
                    await cur.execute("DELETE FROM news WHERE expires_at IS NOT NULL AND expires_at < %s;", (now,))
                    deleted_count = cur.rowcount
                    if deleted_count > 0:
                        logger.info(f"Видалено {deleted_count} прострочених новин.")
                except Exception as e:
                    logger.error(f"Помилка у delete_expired_news_task: {e}")
                await asyncio.sleep(3600) # Перевіряти щогодини

async def send_daily_digest():
    logger.info("Запускаю send_daily_digest.")
    # Реалізація щоденного дайджесту новин
    await asyncio.sleep(86400) # Раз на добу

async def generate_ai_news_task():
    logger.info("Запускаю generate_ai_news_task.")
    # Реалізація генерації AI новин
    await asyncio.sleep(43200) # Раз на 12 годин

# --- Вебхуки FastAPI ---

@app.on_event("startup")
async def on_startup():
    await get_db_pool() # Ініціалізувати пул при старті
    asyncio.create_task(fetch_and_post_news_task())
    asyncio.create_task(delete_expired_news_task())
    asyncio.create_task(send_daily_digest())
    asyncio.create_task(generate_ai_news_task())
    # Встановлення вебхука
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        await bot.set_webhook(f"{webhook_url}/webhook")
        logger.info(f"Webhook встановлено на {webhook_url}/webhook")
    else:
        logger.warning("WEBHOOK_URL не встановлено. Бот працюватиме в режимі long polling (якщо запущено окремо).")

@app.on_event("shutdown")
async def on_shutdown():
    if db_pool:
        await db_pool.close()
        logger.info("Database connection pool closed.")
    await bot.session.close()
    logger.info("Bot session closed.")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root_webhook():
    return HTMLResponse("<h1>Bot is running!</h1>")

# --- API для адмін-панелі ---

@app.get("/api/admin/news", response_model=List[News])
async def get_admin_news_api(api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM news ORDER BY published_at DESC;")
            news_data = await cur.fetchall()
            return [News(**data) for data in news_data]

@app.get("/api/admin/news/{news_id}", response_model=News)
async def get_admin_news_by_id_api(news_id: int, api_key: str = Depends(get_api_key)):
    news_item = await get_news_by_id(news_id)
    if not news_item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="News not found.")
    return news_item

@app.post("/api/admin/news", response_model=News, status_code=status.HTTP_201_CREATED)
async def create_admin_news_api(news: News, api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            params = [news.source_id, news.title, news.content, news.source_url,
                      news.image_url, news.published_at, news.ai_summary,
                      json.dumps(news.ai_classified_topics) if news.ai_classified_topics else None,
                      news.moderation_status, news.expires_at, news.is_published_to_channel]
            await cur.execute(
                """
                INSERT INTO news (source_id, title, content, source_url, image_url,
                                  published_at, ai_summary, ai_classified_topics,
                                  moderation_status, expires_at, is_published_to_channel)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *;
                """,
                tuple(params)
            )
            new_news = await cur.fetchone()
            return News(**new_news)

@app.put("/api/admin/news/{news_id}", response_model=News)
async def update_admin_news_api(news_id: int, news: News, api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # Перевіряємо, чи новина існує
            existing_news = await get_news_by_id(news_id)
            if not existing_news:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="News not found.")

            params = [news.source_id, news.title, news.content, news.source_url,
                      news.image_url, news.published_at, news.ai_summary,
                      json.dumps(news.ai_classified_topics) if news.ai_classified_topics else None,
                      news.moderation_status, news.expires_at, news.is_published_to_channel, news_id]
            await cur.execute(
                """
                UPDATE news SET source_id = %s, title = %s, content = %s, source_url = %s,
                image_url = %s, published_at = %s, ai_summary = %s,
                ai_classified_topics = %s, moderation_status = %s, expires_at = %s,
                is_published_to_channel = %s WHERE id = %s RETURNING *;
                """,
                tuple(params)
            )
            updated_rec = await cur.fetchone()
            if not updated_rec:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="News not found.")
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

# Реєстрація роутера в диспетчері
dp.include_router(router)

# Запуск бота в режимі long polling, якщо WEBHOOK_URL не встановлено
async def main():
    if not os.getenv("WEBHOOK_URL"):
        logger.info("WEBHOOK_URL не встановлено. Запускаю бота в режимі long polling.")
        await get_db_pool() # Ініціалізувати пул для long polling
        asyncio.create_task(fetch_and_post_news_task())
        asyncio.create_task(delete_expired_news_task())
        asyncio.create_task(send_daily_digest())
        asyncio.create_task(generate_ai_news_task())
        await dp.start_polling(bot)
    else:
        logger.info("WEBHOOK_URL встановлено. Бот працюватиме через вебхук.")
        # Для вебхука FastAPI запускається окремо

if __name__ == "__main__":
    from pydantic import BaseModel, Field
    # Цей блок потрібен для локального запуску FastAPI та бота
    # Якщо ви використовуєте Render, він запускається через uvicorn
    # uvicorn bot:app --host 0.0.0.0 --port $PORT
    # Якщо ви хочете запустити long polling локально, закоментуйте uvicorn і розкоментуйте asyncio.run(main())
    
    # Для FastAPI
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

    # Для Long Polling (якщо WEBHOOK_URL не встановлено)
    # asyncio.run(main())
