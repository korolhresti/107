import asyncio
import logging
import logging.handlers # Додано для логування у файл
from datetime import datetime, timedelta, timezone
import json
import os
from typing import List, Optional, Dict, Any, Union
import random
import io # Для роботи з аудіо в пам'яті
import time # Для rate-limiting

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

# --- Налаштування логування в окремі файли ---
# Основний логер
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Форматувальник
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Хендлер для bot.log (INFO та вище)
file_handler = logging.handlers.RotatingFileHandler('bot.log', maxBytes=10*1024*1024, backupCount=5)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Хендлер для errors.log (ERROR та вище)
error_file_handler = logging.handlers.RotatingFileHandler('errors.log', maxBytes=10*1024*1024, backupCount=5)
error_file_handler.setLevel(logging.ERROR)
error_file_handler.setFormatter(formatter)
logger.addHandler(error_file_handler)

# Консольний хендлер
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.INFO)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)
# --- Кінець налаштування логування ---


# --- Глобальні змінні для Rate Limiting AI запитів ---
# Словник для зберігання часу останнього запиту до AI для кожного користувача
# key: user_id (telegram_id), value: timestamp останнього запиту
AI_REQUEST_TIMESTAMPS: Dict[int, float] = {}
AI_REQUEST_LIMIT_SECONDS = 5 # Затримка між AI запитами для непреміум користувачів
# --- Кінець Rate Limiting ---


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

# --- Локалізація (i18n) ---
MESSAGES = {
    'uk': {
        'welcome': "Привіт, {first_name}! Я ваш AI News Bot. Оберіть дію:",
        'main_menu_prompt': "Оберіть дію:",
        'help_text': (
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
        ),
        'action_cancelled': "Дію скасовано. Оберіть наступну дію:",
        'add_source_prompt': "Будь ласка, надішліть URL джерела (наприклад, веб-сайт, RSS-стрічка, посилання на Telegram-канал або профіль соцмережі):",
        'invalid_url': "Будь ласка, введіть дійсний URL, що починається з http:// або https://.",
        'select_source_type': "Будь ласка, оберіть тип джерела:",
        'source_url_not_found': "Вибачте, URL джерела не знайдено. Будь ласка, спробуйте знову.",
        'source_added_success': "Джерело '{source_url}' типу '{source_type}' успішно додано!",
        'add_source_error': "Виникла помилка при додаванні джерела. Будь ласка, спробуйте пізніше.",
        'no_new_news': "Наразі немає нових новин для вас. Спробуйте додати більше джерел або завітайте пізніше!",
        'news_not_found': "На жаль, ця новина не знайдена.",
        'no_more_news': "Більше новин немає. Спробуйте пізніше або змініть фільтри.",
        'first_news': "Це перша новина. Більше новин немає.",
        'error_start_menu': "Сталася помилка. Будь ласка, почніть з /menu.",
        'ai_functions_prompt': "Оберіть AI-функцію:",
        'ai_function_premium_only': "Ця функція доступна лише для преміум-користувачів.",
        'news_title_label': "Заголовок:",
        'news_content_label': "Зміст:",
        'published_at_label': "Опубліковано:",
        'news_progress': "Новина {current_index} з {total_news}",
        'read_source_btn': "🔗 Читати джерело",
        'ai_functions_btn': "🧠 AI-функції",
        'prev_btn': "⬅️ Попередня",
        'next_btn': "➡️ Далі",
        'main_menu_btn': "⬅️ До головного меню",
        'generating_ai_summary': "Генерую AI-резюме, зачекайте...",
        'ai_summary_label': "AI-резюме:",
        'select_translate_language': "Оберіть мову для перекладу:",
        'translating_news': "Перекладаю новину на {language_name} мову, зачекайте...",
        'translation_label': "Переклад на {language_name} мову:",
        'generating_audio': "Генерую аудіо, зачекайте...",
        'audio_news_caption': "🔊 Новина: {title}",
        'audio_error': "Виникла помилка при генерації аудіо. Будь ласка, спробуйте пізніше.",
        'ask_news_ai_prompt': "Надішліть ваше запитання щодо новини:",
        'processing_question': "Обробляю ваше запитання, зачекайте...",
        'ai_response_label': "Відповідь AI:",
        'ai_news_not_found': "Вибачте, новина для запитання не знайдена. Будь ласка, спробуйте знову.",
        'ask_free_ai_prompt': "Надішліть ваше запитання до AI (наприклад, 'Що сталося з гривнею?', 'Як це вплине на ціни?'):",
        'extracting_entities': "Витягую ключові сутності, зачекайте...",
        'entities_label': "Ключові сутності:",
        'explain_term_prompt': "Надішліть термін, який потрібно пояснити:",
        'explaining_term': "Пояснюю термін, зачекайте...",
        'term_explanation_label': "Пояснення терміну '{term}':",
        'classifying_topics': "Класифікую за темами, зачекайте...",
        'topics_label': "Теми:",
        'checking_facts': "Перевіряю факти, зачекайте...",
        'fact_check_label': "Перевірка фактів:",
        'analyzing_sentiment': "Аналізую настрій, зачекайте...",
        'sentiment_label': "Аналіз настрою:",
        'detecting_bias': "Виявляю упередженість, зачекайте...",
        'bias_label': "Виявлення упередженості:",
        'generating_audience_summary': "Генерую резюме для аудиторії, зачекайте...",
        'audience_summary_label': "Резюме для аудиторії:",
        'searching_historical_analogues': "Шукаю історичні аналоги, зачекайте...",
        'historical_analogues_label': "Історичні аналоги:",
        'analyzing_impact': "Аналізую вплив, зачекайте...",
        'impact_label': "Аналіз впливу:",
        'performing_monetary_analysis': "Виконую грошовий аналіз, зачекайте...",
        'monetary_analysis_label': "Грошовий аналіз:",
        'bookmark_added': "Новину додано до закладок!",
        'bookmark_already_exists': "Ця новина вже у ваших закладках.",
        'bookmark_add_error': "Помилка при додаванні до закладок.",
        'bookmark_removed': "Новину видалено із закладок!",
        'bookmark_not_found': "Цієї новини немає у ваших закладках.",
        'bookmark_remove_error': "Помилка при видаленні із закладок.",
        'no_bookmarks': "У вас поки немає закладок.",
        'your_bookmarks_label': "Ваші закладки:",
        'report_fake_news_btn': "🚩 Повідомити про фейк",
        'report_already_sent': "Ви вже надсилали репорт на цю новину.",
        'report_sent_success': "Дякуємо! Ваше повідомлення про фейкову новину було надіслано на модерацію.",
        'report_action_done': "Дякуємо за ваш внесок! Оберіть наступну дію:",
        'user_not_identified': "Не вдалося ідентифікувати вашого користувача в системі.",
        'no_admin_access': "У вас немає доступу до цієї команди.",
        'loading_moderation_news': "Завантажую новини на модерацію, зачекайте...",
        'no_pending_news': "Немає новин на модерації.",
        'moderation_news_label': "Новина на модерації ({current_index} з {total_news}):",
        'source_label': "Джерело:",
        'status_label': "Статус:",
        'approve_btn': "✅ Схвалити",
        'reject_btn': "❌ Відхилити",
        'news_approved': "Новину {news_id} схвалено!",
        'news_rejected': "Новину {news_id} відхилено!",
        'all_moderation_done': "Усі новини на модерації оброблено.",
        'no_more_moderation_news': "Більше новин на модерації немає.",
        'first_moderation_news': "Це перша новина на модерації.",
        'source_stats_label': "📊 Статистика джерел (топ-10 за публікаціями):",
        'source_stats_entry': "{idx}. {source_name}: {count} публікацій",
        'no_source_stats': "Статистика джерел відсутня.",
        'your_invite_code': "Ваш персональний інвайт-код: <code>{invite_code}</code>\nПоділіться цим посиланням, щоб запросити друзів: {invite_link}",
        'invite_error': "Не вдалося згенерувати інвайт-код. Спробуйте пізніше.",
        'daily_digest_header': "📰 Ваш щоденний AI-дайджест новин:",
        'daily_digest_entry': "<b>{idx}. {title}</b>\n{summary}\n🔗 <a href='{source_url}'>Читати повністю</a>\n\n",
        'no_news_for_digest': "Для вас немає нових новин для дайджесту.",
        'ai_rate_limit_exceeded': "Вибачте, ви надсилаєте запити до AI занадто швидко. Будь ласка, зачекайте {seconds_left} секунд перед наступним запитом.",
        'what_new_digest_header': "👋 Привіт! Ви пропустили {count} новин за час вашої відсутності. Ось короткий дайджест:",
        'what_new_digest_footer': "\n\nБажаєте переглянути всі новини? Натисніть '📰 Мої новини'.",
        'web_site_btn': "Веб-сайт",
        'rss_btn': "RSS",
        'telegram_btn': "Telegram",
        'social_media_btn': "Соц. мережі",
        'cancel_btn': "Скасувати",
        'toggle_notifications_btn': "🔔 Сповіщення",
        'set_digest_frequency_btn': "🔄 Частота дайджесту",
        'toggle_safe_mode_btn': "🔒 Безпечний режим",
        'set_view_mode_btn': "👁️ Режим перегляду",
        'ai_summary_btn': "📝 AI-резюме",
        'translate_btn': "🌐 Перекласти",
        'ask_ai_btn': "❓ Запитати AI",
        'extract_entities_btn': "🧑‍🤝‍🧑 Ключові сутності",
        'explain_term_btn': "❓ Пояснити термін",
        'classify_topics_btn': "🏷️ Класифікувати за темами",
        'listen_news_btn': "🔊 Прослухати новину",
        'next_ai_page_btn': "➡️ Далі (AI функції)",
        'fact_check_btn': "✅ Перевірити факт (Преміум)",
        'sentiment_trend_analysis_btn': "📊 Аналіз тренду настроїв (Преміум)",
        'bias_detection_btn': "🔍 Виявлення упередженості (Преміум)",
        'audience_summary_btn': "📝 Резюме для аудиторії (Преміум)",
        'historical_analogues_btn': "📜 Історичні аналоги (Преміум)",
        'impact_analysis_btn': "💥 Аналіз впливу (Преміум)",
        'monetary_impact_btn': "💰 Грошовий аналіз (Преміум)",
        'prev_ai_page_btn': "⬅️ Назад (AI функції)",
        'bookmark_add_btn': "❤️ В обране",
        'comments_btn': "💬 Коментарі",
        'english_lang': "🇬🇧 Англійська",
        'polish_lang': "🇵🇱 Польська",
        'german_lang': "🇩🇪 Німецька",
        'spanish_lang': "🇪🇸 Іспанська",
        'french_lang': "🇫🇷 Французька",
        'ukrainian_lang': "🇺🇦 Українська",
        'back_to_ai_btn': "⬅️ Назад до AI функцій",
        'ask_free_ai_btn': "💬 Запитай AI",
        'news_channel_link_error': "NEWS_CHANNEL_LINK '{link}' виглядає як посилання-запрошення. Для публікації потрібен @username каналу або його числовий ID (наприклад, -1001234567890).",
        'news_channel_link_warning': "NEWS_CHANNEL_LINK '{link}' не містить '@' або '-' на початку. Спробую використати '{identifier}'.",
        'news_published_success': "Новина '{title}' успішно опублікована в каналі {identifier}.",
        'news_publish_error': "Помилка публікації новини '{title}' в каналі {identifier}: {error}",
        'source_parsing_warning': "Не вдалося спарсити контент з джерела: {name} ({url}). Пропускаю.",
        'source_parsing_error': "Критична помилка парсингу джерела {name} ({url}): {error}",
        'no_active_sources': "Немає активних джерел для парсингу. Пропускаю fetch_and_post_news_task.",
        'news_already_exists': "Новина з URL {url} вже існує. Пропускаю.",
        'news_added_success': "Новина '{title}' успішно додана до БД.",
        'news_not_added': "Новина з джерела {name} не була додана до БД (можливо, вже існує).",
        'source_last_parsed_updated': "Оновлено last_parsed для джерела {name}.",
        'deleted_expired_news': "Видалено {count} прострочених новин.",
        'no_expired_news': "Немає прострочених новин для видалення.",
        'daily_digest_no_users': "Немає користувачів для щоденного дайджесту.",
        'daily_digest_no_news': "Для користувача {user_id} немає нових новин для дайджесту.",
        'daily_digest_sent_success': "Щоденний дайджест успішно надіслано користувачу {user_id}.",
        'daily_digest_send_error': "Помилка надсилання дайджесту користувачу {user_id}: {error}",
        'invite_link_label': "Посилання для запрошення",
        'source_stats_top_10': "📊 Статистика джерел (топ-10 за публікаціями):",
        'source_stats_item': "{idx}. {source_name}: {publication_count} публікацій",
        'no_source_stats_available': "Статистика джерел відсутня.",
        'moderation_news_header': "Новина на модерації ({current_index} з {total_news}):",
        'moderation_news_approved': "Новину {news_id} схвалено!",
        'moderation_news_rejected': "Новину {news_id} відхилено!",
        'moderation_all_done': "Усі новини на модерації оброблено.",
        'moderation_no_more_news': "Більше новин на модерації немає.",
        'moderation_first_news': "Це перша новина на модерації.",
        'ai_smart_summary_prompt': "Оберіть тип резюме:",
        'ai_smart_summary_1_sentence': "1 речення",
        'ai_smart_summary_3_facts': "3 ключових факти",
        'ai_smart_summary_deep_dive': "Глибокий огляд",
        'ai_smart_summary_generating': "Генерую {summary_type} резюме, зачекайте...",
        'ai_smart_summary_label': "<b>AI-резюме ({summary_type}):</b>\n{summary}",
    },
    'en': {
        'welcome': "Hello, {first_name}! I'm your AI News Bot. Choose an action:",
        'main_menu_prompt': "Choose an action:",
        'help_text': (
            "<b>Available Commands:</b>\n"
            "/start - Start the bot and go to the main menu\n"
            "/menu - Main menu\n"
            "/cancel - Cancel current action\n"
            "/myprofile - View your profile\n"
            "/my_news - View news selection\n"
            "/add_source - Add a new news source\n"
            "/setfiltersources - Configure news sources\n"
            "/resetfilters - Reset all news filters\n\n"
            "<b>AI Functions:</b>\n"
            "Available after selecting a news item (buttons below the news).\n\n"
            "<b>Marketplace Integration:</b>\n"
            "Buttons 'Help Sell' and 'Help Buy' redirect to channels:\n"
            "Sell: https://t.me/BigmoneycreateBot\n"
            "Buy: https://t.me/+eZEMW4FMEWQxMjYy"
        ),
        'action_cancelled': "Action cancelled. Choose your next action:",
        'add_source_prompt': "Please send the source URL (e.g., website, RSS feed, Telegram channel link, or social media profile):",
        'invalid_url': "Please enter a valid URL starting with http:// or https://.",
        'select_source_type': "Please select the source type:",
        'source_url_not_found': "Sorry, source URL not found. Please try again.",
        'source_added_success': "Source '{source_url}' of type '{source_type}' successfully added!",
        'add_source_error': "An error occurred while adding the source. Please try again later.",
        'no_new_news': "Currently, there are no new news for you. Try adding more sources or check back later!",
        'news_not_found': "Sorry, this news item was not found.",
        'no_more_news': "No more news available. Try later or change filters.",
        'first_news': "This is the first news item. No more news.",
        'error_start_menu': "An error occurred. Please start with /menu.",
        'ai_functions_prompt': "Select an AI function:",
        'ai_function_premium_only': "This function is available only for premium users.",
        'news_title_label': "Title:",
        'news_content_label': "Content:",
        'published_at_label': "Published:",
        'news_progress': "News {current_index} of {total_news}",
        'read_source_btn': "🔗 Read Source",
        'ai_functions_btn': "🧠 AI Functions",
        'prev_btn': "⬅️ Previous",
        'next_btn': "➡️ Next",
        'main_menu_btn': "⬅️ Back to Main Menu",
        'generating_ai_summary': "Generating AI summary, please wait...",
        'ai_summary_label': "AI Summary:",
        'select_translate_language': "Select language for translation:",
        'translating_news': "Translating news to {language_name}, please wait...",
        'translation_label': "Translation to {language_name}:",
        'generating_audio': "Generating audio, please wait...",
        'audio_news_caption': "🔊 News: {title}",
        'audio_error': "An error occurred while generating audio. Please try again later.",
        'ask_news_ai_prompt': "Send your question about the news:",
        'processing_question': "Processing your question, please wait...",
        'ai_response_label': "AI Response:",
        'ai_news_not_found': "Sorry, news for the question not found. Please try again.",
        'ask_free_ai_prompt': "Send your question to AI (e.g., 'What happened to the hryvnia?', 'How will this affect prices?'):",
        'extracting_entities': "Extracting key entities, please wait...",
        'entities_label': "Key Entities:",
        'explain_term_prompt': "Send the term to explain:",
        'explaining_term': "Explaining term, please wait...",
        'term_explanation_label': "Explanation of term '{term}':",
        'classifying_topics': "Classifying by topics, please wait...",
        'topics_label': "Topics:",
        'checking_facts': "Checking facts, please wait...",
        'fact_check_label': "Fact Check:",
        'analyzing_sentiment': "Analyzing sentiment, please wait...",
        'sentiment_label': "Sentiment Analysis:",
        'detecting_bias': "Detecting bias, please wait...",
        'bias_label': "Bias Detection:",
        'generating_audience_summary': "Generating audience summary, please wait...",
        'audience_summary_label': "Audience Summary:",
        'searching_historical_analogues': "Searching historical analogues, please wait...",
        'historical_analogues_label': "Historical Analogues:",
        'analyzing_impact': "Analyzing impact, please wait...",
        'impact_label': "Impact Analysis:",
        'performing_monetary_analysis': "Performing monetary analysis, please wait...",
        'monetary_analysis_label': "Monetary Analysis:",
        'bookmark_added': "News added to bookmarks!",
        'bookmark_already_exists': "This news is already in your bookmarks.",
        'bookmark_add_error': "Error adding to bookmarks.",
        'bookmark_removed': "News removed from bookmarks!",
        'bookmark_not_found': "This news is not in your bookmarks.",
        'bookmark_remove_error': "Error removing from bookmarks.",
        'no_bookmarks': "You have no bookmarks yet.",
        'your_bookmarks_label': "Your Bookmarks:",
        'report_fake_news_btn': "🚩 Report Fake News",
        'report_already_sent': "You have already sent a report for this news.",
        'report_sent_success': "Thank you! Your fake news report has been sent for moderation.",
        'report_action_done': "Thank you for your contribution! Choose your next action:",
        'user_not_identified': "Could not identify your user in the system.",
        'no_admin_access': "You do not have access to this command.",
        'loading_moderation_news': "Loading news for moderation, please wait...",
        'no_pending_news': "No news pending moderation.",
        'moderation_news_label': "News for moderation ({current_index} of {total_news}):",
        'source_label': "Source:",
        'status_label': "Status:",
        'approve_btn': "✅ Approve",
        'reject_btn': "❌ Reject",
        'news_approved': "News {news_id} approved!",
        'news_rejected': "News {news_id} rejected!",
        'all_moderation_done': "All news pending moderation processed.",
        'no_more_moderation_news': "No more news for moderation.",
        'moderation_first_news': "This is the first news for moderation.",
        'source_stats_label': "📊 Source Statistics (top-10 by publications):",
        'source_stats_entry': "{idx}. {source_name}: {publication_count} publications",
        'no_source_stats_available': "No source statistics available.",
        'your_invite_code': "Your personal invite code: <code>{invite_code}</code>\nShare this link to invite friends: {invite_link}",
        'invite_error': "Failed to generate invite code. Please try again later.",
        'daily_digest_header': "📰 Your daily AI News Digest:",
        'daily_digest_entry': "<b>{idx}. {title}</b>\n{summary}\n🔗 <a href='{source_url}'>Read full article</a>\n\n",
        'no_news_for_digest': "No new news for digest for you.",
        'daily_digest_sent_success': "Daily digest successfully sent to user {user_id}.",
        'daily_digest_send_error': "Error sending digest to user {user_id}: {error}",
        'ai_rate_limit_exceeded': "Sorry, you are sending AI requests too fast. Please wait {seconds_left} seconds before your next request.",
        'what_new_digest_header': "👋 Hello! You missed {count} news items during your absence. Here's a short digest:",
        'what_new_digest_footer': "\n\nWant to see all news? Click '📰 My News'.",
        'web_site_btn': "Website",
        'rss_btn': "RSS",
        'telegram_btn': "Telegram",
        'social_media_btn': "Social Media",
        'cancel_btn': "Cancel",
        'toggle_notifications_btn': "🔔 Notifications",
        'set_digest_frequency_btn': "🔄 Digest Frequency",
        'toggle_safe_mode_btn': "🔒 Safe Mode",
        'set_view_mode_btn': "👁️ View Mode",
        'ai_summary_btn': "📝 AI Summary",
        'translate_btn': "🌐 Translate",
        'ask_ai_btn': "❓ Ask AI",
        'extract_entities_btn': "🧑‍🤝‍🧑 Key Entities",
        'explain_term_btn': "❓ Explain Term",
        'classify_topics_btn': "🏷️ Classify by Topics",
        'listen_news_btn': "🔊 Listen to News",
        'next_ai_page_btn': "➡️ Next (AI Functions)",
        'fact_check_btn': "✅ Fact Check (Premium)",
        'sentiment_trend_analysis_btn': "📊 Sentiment Trend Analysis (Premium)",
        'bias_detection_btn': "🔍 Bias Detection (Premium)",
        'audience_summary_btn': "📝 Audience Summary (Premium)",
        'historical_analogues_btn': "📜 Historical Analogues (Premium)",
        'impact_analysis_btn': "💥 Impact Analysis (Premium)",
        'monetary_impact_btn': "💰 Monetary Analysis (Premium)",
        'prev_ai_page_btn': "⬅️ Back (AI Functions)",
        'bookmark_add_btn': "❤️ Bookmark",
        'comments_btn': "💬 Comments",
        'english_lang': "🇬🇧 English",
        'polish_lang': "🇵🇱 Polish",
        'german_lang': "🇩🇪 German",
        'spanish_lang': "🇪🇸 Spanish",
        'french_lang': "🇫🇷 French",
        'ukrainian_lang': "🇺🇦 Ukrainian",
        'back_to_ai_btn': "⬅️ Back to AI Functions",
        'ask_free_ai_btn': "💬 Ask AI",
        'news_channel_link_error': "NEWS_CHANNEL_LINK '{link}' looks like an invite link. For publishing, a channel @username or its numeric ID (e.g., -1001234567890) is required.",
        'news_channel_link_warning': "NEWS_CHANNEL_LINK '{link}' does not start with '@' or '-'. Attempting to use '{identifier}'.",
        'news_published_success': "News '{title}' successfully published to channel {identifier}.",
        'news_publish_error': "Error publishing news '{title}' to channel {identifier}: {error}",
        'source_parsing_warning': "Failed to parse content from source: {name} ({url}). Skipping.",
        'source_parsing_error': "Critical error parsing source {name} ({url}): {error}",
        'no_active_sources': "No active sources for parsing. Skipping fetch_and_post_news_task.",
        'news_already_exists': "News with URL {url} already exists. Skipping.",
        'news_added_success': "News '{title}' successfully added to DB.",
        'news_not_added': "News from source {name} was not added to DB (possibly already exists).",
        'source_last_parsed_updated': "Updated last_parsed for source {name}.",
        'deleted_expired_news': "Deleted {count} expired news items.",
        'no_expired_news': "No expired news to delete.",
        'daily_digest_no_users': "No users for daily digest.",
        'daily_digest_no_news': "No new news for digest for user {user_id}.",
        'daily_digest_sent_success': "Daily digest successfully sent to user {user_id}.",
        'daily_digest_send_error': "Error sending digest to user {user_id}: {error}",
        'invite_link_label': "Invite Link",
        'source_stats_top_10': "📊 Source Statistics (top-10 by publications):",
        'source_stats_item': "{idx}. {source_name}: {publication_count} publications",
        'no_source_stats_available': "No source statistics available.",
        'moderation_news_header': "News for moderation ({current_index} of {total_news}):",
        'moderation_news_approved': "News {news_id} approved!",
        'moderation_news_rejected': "News {news_id} rejected!",
        'moderation_all_done': "All news pending moderation processed.",
        'moderation_no_more_news': "No more news for moderation.",
        'moderation_first_news': "This is the first news for moderation.",
        'ai_smart_summary_prompt': "Select summary type:",
        'ai_smart_summary_1_sentence': "1 sentence",
        'ai_smart_summary_3_facts': "3 key facts",
        'ai_smart_summary_deep_dive': "Deep dive",
        'ai_smart_summary_generating': "Generating {summary_type} summary, please wait...",
        'ai_smart_summary_label': "<b>AI Summary ({summary_type}):</b>\n{summary}",
    }
}

def get_message(user_lang: str, key: str, **kwargs) -> str:
    """Отримує локалізоване повідомлення."""
    return MESSAGES.get(user_lang, MESSAGES['uk']).get(key, f"MISSING_TRANSLATION_{key}").format(**kwargs)

# --- Кінець локалізації ---

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
            await cur.execute("SELECT id FROM sources WHERE source_url = %s", (str(news_data['source_url']),))
            source_record = await cur.fetchone()
            source_id = None
            if source_record:
                source_id = source_record['id']
            else:
                user_id_for_source = news_data.get('user_id_for_source')
                parsed_url = HttpUrl(news_data['source_url'])
                source_name = parsed_url.host if parsed_url.host else 'Невідоме джерело'

                await cur.execute(
                    """
                    INSERT INTO sources (user_id, source_name, source_url, source_type, added_at)
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (source_url) DO UPDATE SET
                        source_name = EXCLUDED.source_name,
                        source_type = EXCLUDED.source_type,
                        status = 'active',
                        last_parsed = NULL
                    RETURNING id;
                    """,
                    (user_id_for_source, source_name, str(news_data['source_url']), news_data.get('source_type', 'web'))
                )
                source_id = await cur.fetchone()['id']
                logger.info(get_message('uk', 'news_added_success', title=news_data['source_url']))

            # Перевірка, чи новина вже існує
            await cur.execute("SELECT id FROM news WHERE source_url = %s", (str(news_data['source_url']),))
            news_exists = await cur.fetchone()
            if news_exists:
                logger.info(get_message('uk', 'news_already_exists', url=news_data['source_url']))
                return None

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
            logger.info(get_message('uk', 'news_added_success', title=new_news['title']))
            return News(**new_news)

async def get_news_for_user(user_id: int, limit: int = 10, offset: int = 0) -> List[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT news_id FROM user_news_views WHERE user_id = %s", (user_id,))
            viewed_news_ids = [row['news_id'] for row in await cur.fetchall()]

            query = """
            SELECT * FROM news
            WHERE id NOT IN (SELECT news_id FROM user_news_views WHERE user_id = %s)
            AND moderation_status = 'approved'
            AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            ORDER BY published_at DESC
            LIMIT %s OFFSET %s;
            """
            await cur.execute(query, (user_id, limit, offset))
            news_records = await cur.fetchall()
            return [News(**record) for record in news_records]

async def count_unseen_news(user_id: int) -> int:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""
                SELECT COUNT(*) FROM news
                WHERE id NOT IN (SELECT news_id FROM user_news_views WHERE user_id = %s)
                AND moderation_status = 'approved'
                AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP);
            """, (user_id,))
            return (await cur.fetchone())['count']

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
    last_message_id = State()

class AIAssistant(StatesGroup):
    waiting_for_question = State()
    waiting_for_news_id_for_ai = State()
    waiting_for_term_to_explain = State()
    waiting_for_fact_to_check = State()
    fact_check_news_id = State()
    waiting_for_audience_summary_type = State()
    audience_summary_news_id = State()
    waiting_for_what_if_query = State()
    what_if_news_id = State()
    waiting_for_youtube_interview_url = State()
    waiting_for_translate_language = State()
    waiting_for_free_question = State()
    waiting_for_summary_type = State() # NEW: State for Smart Summary

class MonetaryImpactAnalysis(StatesGroup):
    waiting_for_monetary_impact_news_id = State()

class ReportFakeNews(StatesGroup):
    waiting_for_report_details = State()
    news_id_to_report = State()

class FilterSetup(StatesGroup):
    waiting_for_source_selection = State()

class LanguageSelection(StatesGroup):
    waiting_for_language = State()

class ModerationStates(StatesGroup):
    browsing_pending_news = State()


# Inline Keyboards
def get_main_menu_keyboard(user_lang: str):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'my_news'), callback_data="my_news"),
        InlineKeyboardButton(text=get_message(user_lang, 'add_source'), callback_data="add_source")
    )
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'settings_menu'), callback_data="settings_menu"),
        InlineKeyboardButton(text=get_message(user_lang, 'ask_free_ai_btn'), callback_data="ask_free_ai")
    )
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'help_menu'), callback_data="help_menu"),
        InlineKeyboardButton(text=get_message(user_lang, 'language_selection_menu'), callback_data="language_selection_menu")
    )
    builder.row(
        InlineKeyboardButton(text="🛍️ Допоможи купити", url="https://t.me/+eZEMW4FMEWQxMjYy"),
        InlineKeyboardButton(text="🤝 Допоможи продати", url="https://t.me/BigmoneycreateBot")
    )
    return builder.as_markup()

def get_ai_news_functions_keyboard(news_id: int, user_lang: str, page: int = 0):
    builder = InlineKeyboardBuilder()
    
    if page == 0:
        builder.row(
            InlineKeyboardButton(text=get_message(user_lang, 'ai_summary_btn'), callback_data=f"ai_summary_select_type_{news_id}"), # Змінено callback
            InlineKeyboardButton(text=get_message(user_lang, 'translate_btn'), callback_data=f"translate_select_lang_{news_id}")
        )
        builder.row(
            InlineKeyboardButton(text=get_message(user_lang, 'ask_ai_btn'), callback_data=f"ask_news_ai_{news_id}"),
            InlineKeyboardButton(text=get_message(user_lang, 'extract_entities_btn'), callback_data=f"extract_entities_{news_id}")
        )
        builder.row(
            InlineKeyboardButton(text=get_message(user_lang, 'explain_term_btn'), callback_data=f"explain_term_{news_id}"),
            InlineKeyboardButton(text=get_message(user_lang, 'classify_topics_btn'), callback_data=f"classify_topics_{news_id}")
        )
        builder.row(
            InlineKeyboardButton(text=get_message(user_lang, 'listen_news_btn'), callback_data=f"listen_news_{news_id}")
        )
        builder.row(
            InlineKeyboardButton(text=get_message(user_lang, 'next_ai_page_btn'), callback_data=f"ai_functions_page_1_{news_id}")
        )
    elif page == 1:
        builder.row(
            InlineKeyboardButton(text=get_message(user_lang, 'fact_check_btn'), callback_data=f"fact_check_news_{news_id}"),
            InlineKeyboardButton(text=get_message(user_lang, 'sentiment_trend_analysis_btn'), callback_data=f"sentiment_trend_analysis_{news_id}")
        )
        builder.row(
            InlineKeyboardButton(text=get_message(user_lang, 'bias_detection_btn'), callback_data=f"bias_detection_{news_id}"),
            InlineKeyboardButton(text=get_message(user_lang, 'audience_summary_btn'), callback_data=f"audience_summary_{news_id}")
        )
        builder.row(
            InlineKeyboardButton(text=get_message(user_lang, 'historical_analogues_btn'), callback_data=f"historical_analogues_{news_id}"),
            InlineKeyboardButton(text=get_message(user_lang, 'impact_analysis_btn'), callback_data=f"impact_analysis_{news_id}")
        )
        builder.row(
            InlineKeyboardButton(text=get_message(user_lang, 'monetary_impact_btn'), callback_data=f"monetary_impact_{news_id}")
        )
        builder.row(
            InlineKeyboardButton(text=get_message(user_lang, 'prev_ai_page_btn'), callback_data=f"ai_functions_page_0_{news_id}")
        )
    
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'report_fake_news_btn'), callback_data=f"report_fake_news_{news_id}")
    )
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'bookmark_add_btn'), callback_data=f"bookmark_news_add_{news_id}"),
        InlineKeyboardButton(text=get_message(user_lang, 'comments_btn'), callback_data=f"view_comments_{news_id}")
    )
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'main_menu_btn'), callback_data="main_menu")
    )
    return builder.as_markup()

def get_translate_language_keyboard(news_id: int, user_lang: str):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'english_lang'), callback_data=f"translate_to_en_{news_id}"),
        InlineKeyboardButton(text=get_message(user_lang, 'polish_lang'), callback_data=f"translate_to_pl_{news_id}")
    )
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'german_lang'), callback_data=f"translate_to_de_{news_id}"),
        InlineKeyboardButton(text=get_message(user_lang, 'spanish_lang'), callback_data=f"translate_to_es_{news_id}")
    )
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'french_lang'), callback_data=f"translate_to_fr_{news_id}"),
        InlineKeyboardButton(text=get_message(user_lang, 'ukrainian_lang'), callback_data=f"translate_to_uk_{news_id}")
    )
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'back_to_ai_btn'), callback_data=f"ai_news_functions_menu_{news_id}")
    )
    return builder.as_markup()

def get_settings_menu_keyboard(user_lang: str):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'toggle_notifications_btn'), callback_data="toggle_notifications"),
        InlineKeyboardButton(text=get_message(user_lang, 'set_digest_frequency_btn'), callback_data="set_digest_frequency")
    )
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'toggle_safe_mode_btn'), callback_data="toggle_safe_mode"),
        InlineKeyboardButton(text=get_message(user_lang, 'set_view_mode_btn'), callback_data="set_view_mode")
    )
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'main_menu_btn'), callback_data="main_menu")
    )
    return builder.as_markup()

def get_source_type_keyboard(user_lang: str):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'web_site_btn'), callback_data="source_type_web"),
        InlineKeyboardButton(text=get_message(user_lang, 'rss_btn'), callback_data="source_type_rss")
    )
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'telegram_btn'), callback_data="source_type_telegram"),
        InlineKeyboardButton(text=get_message(user_lang, 'social_media_btn'), callback_data="source_type_social_media")
    )
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel_action")
    )
    return builder.as_markup()

def get_smart_summary_type_keyboard(news_id: int, user_lang: str):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'ai_smart_summary_1_sentence'), callback_data=f"smart_summary_1_sentence_{news_id}")
    )
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'ai_smart_summary_3_facts'), callback_data=f"smart_summary_3_facts_{news_id}")
    )
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'ai_smart_summary_deep_dive'), callback_data=f"smart_summary_deep_dive_{news_id}")
    )
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'back_to_ai_btn'), callback_data=f"ai_news_functions_menu_{news_id}")
    )
    return builder.as_markup()


# Handlers
@router.message(CommandStart()) # Обробник для команди /start
async def command_start_handler(message: Message, state: FSMContext):
    logger.info(f"Отримано команду /start від користувача {message.from_user.id}")
    await state.clear()
    user = await create_or_update_user(message.from_user) # Переконаємося, що користувач існує
    user_lang = user.language if user else 'uk' # Використовуємо мову користувача

    # Обробка інвайт-коду, якщо він є
    if message.text and len(message.text.split()) > 1:
        invite_code = message.text.split()[1]
        await handle_invite_code(user.id, invite_code) # Передаємо user.id (з БД)
    
    # --- Онбординг для нових користувачів ---
    if user and (datetime.now(timezone.utc) - user.created_at).total_seconds() < 60: # Якщо користувач новий (менше хвилини)
        onboarding_messages = [
            get_message(user_lang, 'welcome', first_name=message.from_user.first_name),
            get_message(user_lang, 'onboarding_step_1'), # "Крок 1: Додайте джерело"
            get_message(user_lang, 'onboarding_step_2'), # "Крок 2: Перегляньте новини"
            get_message(user_lang, 'onboarding_step_3')  # "Крок 3: Натисніть 🧠 для AI"
        ]
        for msg_text in onboarding_messages:
            await message.answer(msg_text)
        await message.answer(get_message(user_lang, 'main_menu_prompt'), reply_markup=get_main_menu_keyboard(user_lang))
    else:
        # --- Меню "Що нового?" для існуючих користувачів ---
        if user:
            last_active_dt = user.last_active.replace(tzinfo=timezone.utc) if user.last_active else datetime.now(timezone.utc)
            time_since_last_active = datetime.now(timezone.utc) - last_active_dt
            
            if time_since_last_active > timedelta(days=2):
                unseen_count = await count_unseen_news(user.id)
                if unseen_count > 0:
                    await message.answer(get_message(user_lang, 'what_new_digest_header', count=unseen_count))
                    # Відправляємо міні-дайджест з 3 новин
                    news_for_digest = await get_news_for_user(user.id, limit=3)
                    digest_text = ""
                    for i, news_item in enumerate(news_for_digest):
                        summary = await call_gemini_api(f"Зроби коротке резюме новини українською мовою: {news_item.content}")
                        digest_text += get_message(user_lang, 'daily_digest_entry', idx=i+1, title=news_item.title, summary=summary, source_url=news_item.source_url)
                        await mark_news_as_viewed(user.id, news_item.id) # Позначаємо новини як переглянуті
                    
                    if digest_text:
                        await message.answer(digest_text + get_message(user_lang, 'what_new_digest_footer'), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

        await message.answer(get_message(user_lang, 'welcome', first_name=message.from_user.first_name), reply_markup=get_main_menu_keyboard(user_lang))

@router.message(Command("menu"))
async def command_menu_handler(message: Message, state: FSMContext):
    logger.info(f"Отримано команду /menu від користувача {message.from_user.id}")
    await state.clear()
    user = await create_or_update_user(message.from_user)
    user_lang = user.language if user else 'uk'
    await message.answer(get_message(user_lang, 'main_menu_prompt'), reply_markup=get_main_menu_keyboard(user_lang))

@router.message(Command("cancel"))
async def command_cancel_handler(message: Message, state: FSMContext):
    logger.info(f"Отримано команду /cancel від користувача {message.from_user.id}")
    await state.clear()
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'
    await message.answer(get_message(user_lang, 'action_cancelled'), reply_markup=get_main_menu_keyboard(user_lang))

@router.message(Command("test"))
async def command_test_handler(message: Message):
    logger.info(f"Отримано команду /test від користувача {message.from_user.id}")
    await message.answer("Я працюю! ✅")

@router.callback_query(F.data == "main_menu")
async def callback_main_menu(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'main_menu' від користувача {callback.from_user.id}")
    await state.clear()
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'main_menu_prompt'), reply_markup=get_main_menu_keyboard(user_lang))
    await callback.answer()

@router.callback_query(F.data == "help_menu")
async def callback_help_menu(callback: CallbackQuery):
    logger.info(f"Отримано callback 'help_menu' від користувача {callback.from_user.id}")
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'help_text'), reply_markup=get_main_menu_keyboard(user_lang), disable_web_page_preview=True)
    await callback.answer()

@router.callback_query(F.data == "cancel_action")
async def callback_cancel_action(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'cancel_action' від користувача {callback.from_user.id}")
    await state.clear()
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'action_cancelled'), reply_markup=get_main_menu_keyboard(user_lang))
    await callback.answer()

@router.callback_query(F.data == "add_source")
async def callback_add_source(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'add_source' від користувача {callback.from_user.id}")
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'add_source_prompt'), reply_markup=get_main_menu_keyboard(user_lang))
    await state.set_state(AddSourceStates.waiting_for_url)
    await callback.answer()

@router.message(AddSourceStates.waiting_for_url)
async def process_source_url(message: Message, state: FSMContext):
    logger.info(f"Отримано URL джерела від користувача {message.from_user.id}: {message.text}")
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'
    source_url = message.text
    if not (source_url.startswith("http://") or source_url.startswith("https://")):
        await message.answer(get_message(user_lang, 'invalid_url'))
        return
    
    await state.update_data(source_url=source_url)
    await message.answer(get_message(user_lang, 'select_source_type'), reply_markup=get_source_type_keyboard(user_lang))
    await state.set_state(AddSourceStates.waiting_for_type)

@router.callback_query(AddSourceStates.waiting_for_type, F.data.startswith("source_type_"))
async def process_source_type(callback: CallbackQuery, state: FSMContext):
    source_type = callback.data.replace("source_type_", "")
    logger.info(f"Отримано тип джерела '{source_type}' від користувача {callback.from_user.id}")
    user_data = await state.get_data()
    source_url = user_data.get("source_url")
    user_telegram_id = callback.from_user.id
    user = await get_user_by_telegram_id(user_telegram_id)
    user_lang = user.language if user else 'uk'

    if not source_url:
        logger.error(f"URL джерела не знайдено в FSM для користувача {user_telegram_id}")
        await callback.message.edit_text(get_message(user_lang, 'source_url_not_found'), reply_markup=get_main_menu_keyboard(user_lang))
        await state.clear()
        return

    user_db_id = user.id

    try:
        pool = await get_db_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                parsed_url = HttpUrl(source_url)
                source_name = parsed_url.host if parsed_url.host else 'Невідоме джерело'

                await cur.execute(
                    """
                    INSERT INTO sources (user_id, source_name, source_url, source_type, added_at)
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (source_url) DO UPDATE SET
                        source_name = EXCLUDED.source_name,
                        source_type = EXCLUDED.source_type,
                        status = 'active',
                        last_parsed = NULL
                    RETURNING id;
                    """,
                    (user_db_id, source_name, source_url, source_type)
                )
                source_id = await cur.fetchone()[0]
                await conn.commit()
        logger.info(f"Джерело '{source_url}' типу '{source_type}' успішно додано до БД (ID: {source_id})")
        await callback.message.edit_text(get_message(user_lang, 'source_added_success', source_url=source_url, source_type=source_type), reply_markup=get_main_menu_keyboard(user_lang))
    except Exception as e:
        logger.error(f"Помилка при додаванні джерела '{source_url}': {e}", exc_info=True)
        await callback.message.edit_text(get_message(user_lang, 'add_source_error'), reply_markup=get_main_menu_keyboard(user_lang))
    
    await state.clear()
    await callback.answer()

@router.callback_query(F.data == "my_news")
async def handle_my_news_command(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'my_news' від користувача {callback.from_user.id}")
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    if not user:
        user = await create_or_update_user(callback.from_user)

    news_items = await get_news_for_user(user.id, limit=1)
    
    if not news_items:
        await callback.message.edit_text(get_message(user_lang, 'no_new_news'), reply_markup=get_main_menu_keyboard(user_lang))
        await callback.answer()
        return

    all_news_ids = [n.id for n in await get_news_for_user(user.id, limit=100, offset=0)]
    
    current_state_data = await state.get_data()
    last_message_id = current_state_data.get('last_message_id')
    if last_message_id:
        try:
            await bot.delete_message(chat_id=callback.message.chat.id, message_id=last_message_id)
            logger.info(f"Видалено попереднє повідомлення {last_message_id} для користувача {callback.from_user.id}")
        except Exception as e:
            logger.warning(f"Не вдалося видалити попереднє повідомлення {last_message_id} для користувача {callback.from_user.id}: {e}")

    await state.update_data(current_news_index=0, news_ids=all_news_ids)
    await state.set_state(NewsBrowse.Browse_news)

    current_news_id = news_items[0].id
    await send_news_to_user(callback.message.chat.id, current_news_id, 0, len(all_news_ids), state)
    await callback.answer()

@router.callback_query(NewsBrowse.Browse_news, F.data == "next_news")
async def handle_next_news_command(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'next_news' від користувача {callback.from_user.id}")
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    if not user:
        await callback.message.edit_text(get_message(user_lang, 'error_start_menu'), reply_markup=get_main_menu_keyboard(user_lang))
        await callback.answer()
        return

    user_data = await state.get_data()
    news_ids = user_data.get("news_ids", [])
    current_index = user_data.get("current_news_index", 0)
    last_message_id = user_data.get('last_message_id')

    if not news_ids or current_index + 1 >= len(news_ids):
        await callback.message.edit_text(get_message(user_lang, 'no_more_news'), reply_markup=get_main_menu_keyboard(user_lang))
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

    await send_news_to_user(callback.message.chat.id, next_news_id, next_index, len(news_ids), state)
    await callback.answer()

@router.callback_query(NewsBrowse.Browse_news, F.data == "prev_news")
async def handle_prev_news_command(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'prev_news' від користувача {callback.from_user.id}")
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    if not user:
        await callback.message.edit_text(get_message(user_lang, 'error_start_menu'), reply_markup=get_main_menu_keyboard(user_lang))
        await callback.answer()
        return

    user_data = await state.get_data()
    news_ids = user_data.get("news_ids", [])
    current_index = user_data.get("current_news_index", 0)
    last_message_id = user_data.get('last_message_id')

    if not news_ids or current_index <= 0:
        await callback.message.edit_text(get_message(user_lang, 'first_news'), reply_markup=get_main_menu_keyboard(user_lang))
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

    await send_news_to_user(callback.message.chat.id, prev_news_id, prev_index, len(news_ids), state)
    await callback.answer()

@router.callback_query(F.data.startswith("ai_news_functions_menu"))
async def handle_ai_news_functions_menu(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'ai_news_functions_menu' від користувача {callback.from_user.id}")
    logger.debug(f"Callback data for AI functions menu: {callback.data}")
    
    parts = callback.data.split('_')
    news_id = None
    if len(parts) > 3 and parts[3].isdigit():
        news_id = int(parts[3])
    
    if not news_id:
        logger.warning(f"Не знайдено news_id для AI функцій для користувача {callback.from_user.id}. Callback data: {callback.data}")
        await callback.answer(get_message('uk', 'news_not_found'), show_alert=True)
        return

    await state.update_data(current_news_id_for_ai_menu=news_id)
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    
    page = 0 

    await callback.message.edit_text(get_message(user_lang, 'ai_functions_prompt'), reply_markup=get_ai_news_functions_keyboard(news_id, user_lang, page))
    await callback.answer()

@router.callback_query(F.data.startswith("ai_functions_page_"))
async def handle_ai_functions_pagination(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'ai_functions_page_' від користувача {callback.from_user.id}")
    logger.debug(f"Callback data for AI functions pagination: {callback.data}")
    
    parts = callback.data.split('_')
    page = int(parts[3])
    news_id = int(parts[4])

    user_data = await state.get_data()
    user_telegram_id = callback.from_user.id
    user_in_db = await get_user_by_telegram_id(user_telegram_id)
    user_lang = user_in_db.language if user_in_db else 'uk'

    if page == 1 and (not user_in_db or not user_in_db.is_premium):
        await callback.answer(get_message(user_lang, 'ai_function_premium_only'), show_alert=True)
        return

    await callback.message.edit_text(get_message(user_lang, 'ai_functions_prompt'), reply_markup=get_ai_news_functions_keyboard(news_id, user_lang, page))
    await callback.answer()


async def send_news_to_user(chat_id: int, news_id: int, current_index: int, total_news: int, state: FSMContext):
    logger.info(f"Надсилання новини {news_id} користувачу {chat_id}")
    news_item = await get_news_by_id(news_id)
    user = await get_user_by_telegram_id(chat_id)
    user_lang = user.language if user else 'uk'

    if not news_item:
        logger.warning(f"Новина {news_id} не знайдена для надсилання користувачу {chat_id}")
        await bot.send_message(chat_id, get_message(user_lang, 'news_not_found'))
        return

    source_info = await get_source_by_id(news_item.source_id)
    source_name = source_info['source_name'] if source_info else get_message(user_lang, 'unknown_source')

    text = (
        f"<b>{get_message(user_lang, 'news_title_label')}</b> {news_item.title}\n\n"
        f"<b>{get_message(user_lang, 'news_content_label')}</b>\n{news_item.content}\n\n"
        f"{get_message(user_lang, 'published_at_label')} {news_item.published_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"{get_message(user_lang, 'news_progress', current_index=current_index + 1, total_news=total_news)}\n\n"
    )

    keyboard_builder = InlineKeyboardBuilder()
    keyboard_builder.row(InlineKeyboardButton(text=get_message(user_lang, 'read_source_btn'), url=str(news_item.source_url)))
    
    keyboard_builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'ai_functions_btn'), callback_data=f"ai_news_functions_menu_{news_item.id}")
    )

    nav_buttons = []
    if current_index > 0:
        nav_buttons.append(InlineKeyboardButton(text=get_message(user_lang, 'prev_btn'), callback_data="prev_news"))
    if current_index < total_news - 1:
        nav_buttons.append(InlineKeyboardButton(text=get_message(user_lang, 'next_btn'), callback_data="next_news"))
    
    if nav_buttons:
        keyboard_builder.row(*nav_buttons)

    keyboard_builder.row(InlineKeyboardButton(text=get_message(user_lang, 'main_menu_btn'), callback_data="main_menu"))

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
            # Fallback to default image URL or a placeholder
            placeholder_image_url = "https://placehold.co/600x400/CCCCCC/000000?text=No+Image" # Default placeholder
            msg = await bot.send_photo( # Changed to send_photo with placeholder
                chat_id=chat_id,
                photo=placeholder_image_url,
                caption=text + f"\n[Зображення новини]({news_item.image_url})", # Still provide original URL
                reply_markup=keyboard_builder.as_markup(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False
            )
            logger.info(f"Новина {news_id} надіслана з placeholder фото користувачу {chat_id}.")
    else:
        # Fallback for news without any image_url
        placeholder_image_url = "https://placehold.co/600x400/CCCCCC/000000?text=No+Image"
        msg = await bot.send_photo( # Send a placeholder image if no image_url
            chat_id=chat_id,
            photo=placeholder_image_url,
            caption=text,
            reply_markup=keyboard_builder.as_markup(),
            parse_mode=ParseMode.HTML
        )
        logger.info(f"Новина {news_id} надіслана без фото (з placeholder) користувачу {chat_id}.")
    
    if msg:
        await state.update_data(last_message_id=msg.message_id)
        logger.info(f"Збережено message_id {msg.message_id} для новини {news_id} користувача {chat_id}.")

    if user:
        await mark_news_as_viewed(user.id, news_item.id)
        logger.info(f"Новина {news_id} позначена як переглянута для користувача {user.id}.")


# AI-функції
async def call_gemini_api(prompt: str, user_telegram_id: Optional[int] = None, chat_history: Optional[List[Dict]] = None) -> Optional[str]:
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY не встановлено.")
        return "Вибачте, функціонал AI наразі недоступний через відсутність API ключа."

    # --- Rate Limiting для AI запитів ---
    if user_telegram_id:
        user = await get_user_by_telegram_id(user_telegram_id)
        if not user or not user.is_premium: # Застосовуємо rate-limit тільки для непреміум користувачів
            current_time = time.time()
            last_request_time = AI_REQUEST_TIMESTAMPS.get(user_telegram_id, 0)
            if current_time - last_request_time < AI_REQUEST_LIMIT_SECONDS:
                seconds_left = int(AI_REQUEST_LIMIT_SECONDS - (current_time - last_request_time))
                logger.warning(f"Rate limit exceeded for user {user_telegram_id}. {seconds_left} seconds left.")
                return get_message(user.language if user else 'uk', 'ai_rate_limit_exceeded', seconds_left=seconds_left)
            AI_REQUEST_TIMESTAMPS[user_telegram_id] = current_time
    # --- Кінець Rate Limiting ---

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    # Формуємо вміст запиту з історією чату, якщо вона є
    contents = []
    if chat_history:
        for entry in chat_history:
            contents.append({"role": entry["role"], "parts": [{"text": entry["text"]}]})
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    payload = {
        "contents": contents,
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
                if response.status == 429:
                    logger.warning("Отримано 429 Too Many Requests від Gemini API. Спробую пізніше.")
                    return "Вибачте, зараз забагато запитів до AI. Будь ласка, спробуйте через хвилину."
                response.raise_for_status()
                data = await response.json()
                if data and data.get("candidates"):
                    return data["candidates"][0]["content"]["parts"][0]["text"]
                return "Не вдалося отримати відповідь від AI."
    except Exception as e:
        logger.error(f"Помилка виклику Gemini API: {e}", exc_info=True)
        return "Виникла помилка при зверненні до AI."

async def check_premium_access(user_telegram_id: int) -> bool:
    user = await get_user_by_telegram_id(user_telegram_id)
    return user and user.is_premium

@router.callback_query(F.data.startswith("ai_summary_select_type_"))
async def handle_ai_summary_select_type(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'ai_summary_select_type_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[4])
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await state.update_data(news_id_for_summary=news_id)
    await callback.message.edit_text(get_message(user_lang, 'ai_smart_summary_prompt'), reply_markup=get_smart_summary_type_keyboard(news_id, user_lang))
    await state.set_state(AIAssistant.waiting_for_summary_type)
    await callback.answer()

@router.callback_query(AIAssistant.waiting_for_summary_type, F.data.startswith("smart_summary_"))
async def handle_smart_summary(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'smart_summary_' від користувача {callback.from_user.id}")
    parts = callback.data.split('_')
    summary_type_key = parts[2] # e.g., '1', '3', 'deep'
    news_id = int(parts[3])

    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'

    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer(get_message(user_lang, 'news_not_found'), show_alert=True)
        await state.clear()
        return

    summary_type_text = ""
    prompt = ""
    if summary_type_key == '1_sentence':
        summary_type_text = get_message(user_lang, 'ai_smart_summary_1_sentence')
        prompt = f"Зроби резюме наступної новини в одне речення українською мовою: {news_item.content}"
    elif summary_type_key == '3_facts':
        summary_type_text = get_message(user_lang, 'ai_smart_summary_3_facts')
        prompt = f"Виділи 3 ключових факти з наступної новини українською мовою: {news_item.content}"
    elif summary_type_key == 'deep_dive':
        summary_type_text = get_message(user_lang, 'ai_smart_summary_deep_dive')
        prompt = f"Зроби глибокий огляд наступної новини українською мовою, включаючи контекст, можливі наслідки та аналіз: {news_item.content}"
    else:
        await callback.answer("Невідомий тип резюме.", show_alert=True)
        await state.clear()
        return

    await callback.message.edit_text(get_message(user_lang, 'ai_smart_summary_generating', summary_type=summary_type_text))
    summary = await call_gemini_api(prompt, user_telegram_id=callback.from_user.id)
    await callback.message.edit_text(get_message(user_lang, 'ai_smart_summary_label', summary_type=summary_type_text, summary=summary), reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await state.clear()
    await callback.answer()


@router.callback_query(F.data.startswith("translate_select_lang_"))
async def handle_translate_select_language(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'translate_select_lang_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[3])
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await state.update_data(news_id_for_translate=news_id)
    await callback.message.edit_text(get_message(user_lang, 'select_translate_language'), reply_markup=get_translate_language_keyboard(news_id, user_lang))
    await state.set_state(AIAssistant.waiting_for_translate_language)
    await callback.answer()

@router.callback_query(AIAssistant.waiting_for_translate_language, F.data.startswith("translate_to_"))
async def handle_translate_to_language(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'translate_to_' від користувача {callback.from_user.id}")
    parts = callback.data.split('_')
    lang_code = parts[2]
    news_id = int(parts[3])

    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'

    language_names = {
        "en": get_message(user_lang, 'english_lang').replace('🇬🇧 ', ''),
        "pl": get_message(user_lang, 'polish_lang').replace('🇵🇱 ', ''),
        "de": get_message(user_lang, 'german_lang').replace('🇩🇪 ', ''),
        "es": get_message(user_lang, 'spanish_lang').replace('🇪🇸 ', ''),
        "fr": get_message(user_lang, 'french_lang').replace('🇫🇷 ', ''),
        "uk": get_message(user_lang, 'ukrainian_lang').replace('🇺🇦 ', '')
    }
    language_name = language_names.get(lang_code, get_message(user_lang, 'selected_language'))

    news_item = await get_news_by_id(news_id)
    if not news_item:
        await callback.answer(get_message(user_lang, 'news_not_found'), show_alert=True)
        await state.clear()
        return

    await callback.message.edit_text(get_message(user_lang, 'translating_news', language_name=language_name))
    translation = await call_gemini_api(f"Переклади цю новину на {language_name} мовою:\n\n{news_item.content}", user_telegram_id=callback.from_user.id)
    await callback.message.edit_text(get_message(user_lang, 'translation_label', language_name=language_name, translation=translation), reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await state.clear()
    await callback.answer()

@router.callback_query(F.data.startswith("listen_news_"))
async def handle_listen_news(callback: CallbackQuery):
    logger.info(f"Отримано callback 'listen_news_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'

    if not news_item:
        await callback.answer(get_message(user_lang, 'news_not_found'), show_alert=True)
        return

    await callback.message.edit_text(get_message(user_lang, 'generating_audio'))
    
    try:
        text_to_speak = f"{news_item.title}. {news_item.content}"
        
        tts = gTTS(text=text_to_speak, lang='uk')
        
        audio_buffer = io.BytesIO()
        await asyncio.to_thread(tts.write_to_fp, audio_buffer)
        audio_buffer.seek(0)

        await bot.send_voice(
            chat_id=callback.message.chat.id,
            voice=BufferedInputFile(audio_buffer.getvalue(), filename=f"news_{news_id}.mp3"),
            caption=get_message(user_lang, 'audio_news_caption', title=news_item.title),
            reply_markup=get_ai_news_functions_keyboard(news_id, user_lang)
        )
        await callback.message.delete()
        logger.info(f"Аудіо для новини {news_id} успішно надіслано користувачу {callback.from_user.id}.")
    except Exception as e:
        logger.error(f"Помилка генерації або надсилання аудіо для новини {news_id}: {e}", exc_info=True)
        await callback.message.edit_text(get_message(user_lang, 'audio_error'),
                                         reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await callback.answer()


@router.callback_query(F.data.startswith("ask_news_ai_"))
async def handle_ask_news_ai(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'ask_news_ai_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[3])
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await state.update_data(waiting_for_news_id_for_ai=news_id)
    await callback.message.edit_text(get_message(user_lang, 'ask_news_ai_prompt'), reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel_action")).as_markup())
    await state.set_state(AIAssistant.waiting_for_question)
    await callback.answer()

@router.message(AIAssistant.waiting_for_question)
async def process_ai_question(message: Message, state: FSMContext):
    logger.info(f"Отримано запитання AI від користувача {message.from_user.id}: {message.text}")
    user_question = message.text
    user_data = await state.get_data()
    news_id = user_data.get("waiting_for_news_id_for_ai")
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'

    if not news_id:
        logger.error(f"Новина для запитання AI не знайдена в FSM для користувача {message.from_user.id}")
        await message.answer(get_message(user_lang, 'ai_news_not_found'), reply_markup=get_main_menu_keyboard(user_lang))
        await state.clear()
        return

    news_item = await get_news_by_id(news_id)
    if not news_item:
        logger.error(f"Новина {news_id} не знайдена в БД для запитання AI від користувача {message.from_user.id}")
        await message.answer(get_message(user_lang, 'news_not_found'), reply_markup=get_main_menu_keyboard(user_lang))
        await state.clear()
        return

    await message.answer(get_message(user_lang, 'processing_question'))
    # --- Зберігання контексту для AI-чату ---
    chat_history = user_data.get('ai_chat_history', [])
    chat_history.append({"role": "user", "text": user_question})

    prompt = f"Новина: {news_item.content}\n\nЗапитання: {user_question}\n\nВідповідь:"
    ai_response = await call_gemini_api(prompt, user_telegram_id=message.from_user.id, chat_history=chat_history)
    
    chat_history.append({"role": "model", "text": ai_response})
    await state.update_data(ai_chat_history=chat_history) # Оновлюємо історію
    # --- Кінець зберігання контексту ---

    await message.answer(get_message(user_lang, 'ai_response_label') + f"\n{ai_response}", reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await state.clear()
    logger.info(f"Відповідь AI надіслана користувачу {message.from_user.id} для новини {news_id}.")

@router.callback_query(F.data == "ask_free_ai")
async def handle_ask_free_ai(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'ask_free_ai' від користувача {callback.from_user.id}")
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'ask_free_ai_prompt'),
                                     reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel_action")).as_markup())
    await state.set_state(AIAssistant.waiting_for_free_question)
    # Ініціалізуємо порожню історію для нового вільного чату
    await state.update_data(ai_chat_history=[])
    await callback.answer()

@router.message(AIAssistant.waiting_for_free_question)
async def process_free_ai_question(message: Message, state: FSMContext):
    logger.info(f"Отримано вільне запитання AI від користувача {message.from_user.id}: {message.text}")
    user_question = message.text
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'

    await message.answer(get_message(user_lang, 'processing_question'))
    
    # --- Зберігання контексту для AI-чату ---
    user_data = await state.get_data()
    chat_history = user_data.get('ai_chat_history', [])
    chat_history.append({"role": "user", "text": user_question})

    ai_response = await call_gemini_api(f"Відповідай як новинний аналітик на запитання: {user_question}", user_telegram_id=message.from_user.id, chat_history=chat_history)
    
    chat_history.append({"role": "model", "text": ai_response})
    await state.update_data(ai_chat_history=chat_history) # Оновлюємо історію
    # --- Кінець зберігання контексту ---

    await message.answer(get_message(user_lang, 'ai_response_label') + f"\n{ai_response}", reply_markup=get_main_menu_keyboard(user_lang))
    logger.info(f"Вільна відповідь AI надіслана користувачу {message.from_user.id}.")


@router.callback_query(F.data.startswith("extract_entities_"))
async def handle_extract_entities(callback: CallbackQuery):
    logger.info(f"Отримано callback 'extract_entities_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'

    if not news_item:
        await callback.answer(get_message(user_lang, 'news_not_found'), show_alert=True)
        return
    
    if not await check_premium_access(callback.from_user.id):
        await callback.answer(get_message(user_lang, 'ai_function_premium_only'), show_alert=True)
        return

    await callback.message.edit_text(get_message(user_lang, 'extracting_entities'))
    entities = await call_gemini_api(f"Витягни ключові сутності (імена людей, прізвища, прізвиська, організації, місця, дати) з наступної новини українською мовою. Перелічи їх через кому, зберігаючи оригінальний вигляд: {news_item.content}", user_telegram_id=callback.from_user.id)
    await callback.message.edit_text(get_message(user_lang, 'entities_label') + f"\n{entities}", reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await callback.answer()

@router.callback_query(F.data.startswith("explain_term_"))
async def handle_explain_term(callback: CallbackQuery, state: FSMContext):
    logger.info(f"Отримано callback 'explain_term_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'

    if not news_item:
        await callback.answer(get_message(user_lang, 'news_not_found'), show_alert=True)
        return

    if not await check_premium_access(callback.from_user.id):
        await callback.answer(get_message(user_lang, 'ai_function_premium_only'), show_alert=True)
        return

    await state.update_data(waiting_for_news_id_for_ai=news_id)
    await callback.message.edit_text(get_message(user_lang, 'explain_term_prompt'), reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel_action")).as_markup())
    await state.set_state(AIAssistant.waiting_for_term_to_explain)
    await callback.answer()

@router.message(AIAssistant.waiting_for_term_to_explain)
async def process_term_explanation(message: Message, state: FSMContext):
    logger.info(f"Отримано термін для пояснення від користувача {message.from_user.id}: {message.text}")
    term = message.text
    user_data = await state.get_data()
    news_id = user_data.get("waiting_for_news_id_for_ai")
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'

    if not news_id:
        logger.error(f"Новина для пояснення терміну не знайдена в FSM для користувача {message.from_user.id}")
        await message.answer(get_message(user_lang, 'ai_news_not_found'), reply_markup=get_main_menu_keyboard(user_lang))
        await state.clear()
        return

    news_item = await get_news_by_id(news_id)
    if not news_item:
        logger.error(f"Новина {news_id} не знайдена в БД для пояснення терміну від користувача {message.from_user.id}")
        await message.answer(get_message(user_lang, 'news_not_found'), reply_markup=get_main_menu_keyboard(user_lang))
        await state.clear()
        return

    await message.answer(get_message(user_lang, 'explaining_term'))
    explanation = await call_gemini_api(f"Поясни термін '{term}' у контексті наступної новини українською мовою: {news_item.content}", user_telegram_id=message.from_user.id)
    await message.answer(get_message(user_lang, 'term_explanation_label', term=term) + f"\n{explanation}", reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await state.clear()
    logger.info(f"Пояснення терміну надіслано користувачу {message.from_user.id} для новини {news_id}.")

@router.callback_query(F.data.startswith("classify_topics_"))
async def handle_classify_topics(callback: CallbackQuery):
    logger.info(f"Отримано callback 'classify_topics_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'

    if not news_item:
        await callback.answer(get_message(user_lang, 'news_not_found'), show_alert=True)
        return
    
    if not await check_premium_access(callback.from_user.id):
        await callback.answer(get_message(user_lang, 'ai_function_premium_only'), show_alert=True)
        return

    await callback.message.edit_text(get_message(user_lang, 'classifying_topics'))
    topics = await call_gemini_api(f"Класифікуй наступну новину за 3-5 основними темами/категоріями українською мовою, перелічи їх через кому: {news_item.content}", user_telegram_id=callback.from_user.id)
    await callback.message.edit_text(get_message(user_lang, 'topics_label') + f"\n{topics}", reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await callback.answer()

@router.callback_query(F.data.startswith("fact_check_news_"))
async def handle_fact_check_news(callback: CallbackQuery):
    logger.info(f"Отримано callback 'fact_check_news_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[3])
    news_item = await get_news_by_id(news_id)
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'

    if not news_item:
        await callback.answer(get_message(user_lang, 'news_not_found'), show_alert=True)
        return
    
    if not await check_premium_access(callback.from_user.id):
        await callback.answer(get_message(user_lang, 'ai_function_premium_only'), show_alert=True)
        return

    await callback.message.edit_text(get_message(user_lang, 'checking_facts'))
    fact_check = await call_gemini_api(f"Виконай швидку перевірку фактів для наступної новини українською мовою. Вкажи, чи є в ній очевидні неточності або маніпуляції. Якщо є, наведи джерела: {news_item.content}", user_telegram_id=callback.from_user.id)
    await callback.message.edit_text(get_message(user_lang, 'fact_check_label') + f"\n{fact_check}", reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await callback.answer()

@router.callback_query(F.data.startswith("sentiment_trend_analysis_"))
async def handle_sentiment_trend_analysis(callback: CallbackQuery):
    logger.info(f"Отримано callback 'sentiment_trend_analysis_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[3])
    news_item = await get_news_by_id(news_id)
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'

    if not news_item:
        await callback.answer(get_message(user_lang, 'news_not_found'), show_alert=True)
        return
    
    if not await check_premium_access(callback.from_user.id):
        await callback.answer(get_message(user_lang, 'ai_function_premium_only'), show_alert=True)
        return

    await callback.message.edit_text(get_message(user_lang, 'analyzing_sentiment'))
    sentiment = await call_gemini_api(f"Виконай аналіз настрою (позитивний, негативний, нейтральний) для наступної новини українською мовою та поясни чому: {news_item.content}", user_telegram_id=callback.from_user.id)
    await callback.message.edit_text(get_message(user_lang, 'sentiment_label') + f"\n{sentiment}", reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await callback.answer()

@router.callback_query(F.data.startswith("bias_detection_"))
async def handle_bias_detection(callback: CallbackQuery):
    logger.info(f"Отримано callback 'bias_detection_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'

    if not news_item:
        await callback.answer(get_message(user_lang, 'news_not_found'), show_alert=True)
        return
    
    if not await check_premium_access(callback.from_user.id):
        await callback.answer(get_message(user_lang, 'ai_function_premium_only'), show_alert=True)
        return

    await callback.message.edit_text(get_message(user_lang, 'detecting_bias'))
    bias = await call_gemini_api(f"Вияви потенційну упередженість або маніпуляції в наступній новині українською мовою: {news_item.content}", user_telegram_id=callback.from_user.id)
    await callback.message.edit_text(get_message(user_lang, 'bias_label') + f"\n{bias}", reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await callback.answer()

@router.callback_query(F.data.startswith("audience_summary_"))
async def handle_audience_summary(callback: CallbackQuery):
    logger.info(f"Отримано callback 'audience_summary_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'

    if not news_item:
        await callback.answer(get_message(user_lang, 'news_not_found'), show_alert=True)
        return
    
    if not await check_premium_access(callback.from_user.id):
        await callback.answer(get_message(user_lang, 'ai_function_premium_only'), show_alert=True)
        return

    await callback.message.edit_text(get_message(user_lang, 'generating_audience_summary'))
    summary = await call_gemini_api(f"Зроби резюме наступної новини українською мовою, адаптований для широкої аудиторії (простою мовою, без складних термінів): {news_item.content}", user_telegram_id=callback.from_user.id)
    await callback.message.edit_text(get_message(user_lang, 'audience_summary_label') + f"\n{summary}", reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await callback.answer()

@router.callback_query(F.data.startswith("historical_analogues_"))
async def handle_historical_analogues(callback: CallbackQuery):
    logger.info(f"Отримано callback 'historical_analogues_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'

    if not news_item:
        await callback.answer(get_message(user_lang, 'news_not_found'), show_alert=True)
        return
    
    if not await check_premium_access(callback.from_user.id):
        await callback.answer(get_message(user_lang, 'ai_function_premium_only'), show_alert=True)
        return

    await callback.message.edit_text(get_message(user_lang, 'searching_historical_analogues'))
    analogues = await call_gemini_api(f"Знайди історичні аналоги або схожі події для ситуації, описаної в наступній новині українською мовою. Опиши їх наслідки та вплив, включаючи умовні підрахунки (наприклад, 'це призвело до втрат у розмірі X мільйонів доларів' або 'змінило економічний ландер на Y%'): {news_item.content}", user_telegram_id=callback.from_user.id)
    await callback.message.edit_text(get_message(user_lang, 'historical_analogues_label') + f"\n{analogues}", reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await callback.answer()

@router.callback_query(F.data.startswith("impact_analysis_"))
async def handle_impact_analysis(callback: CallbackQuery):
    logger.info(f"Отримано callback 'impact_analysis_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'

    if not news_item:
        await callback.answer(get_message(user_lang, 'news_not_found'), show_alert=True)
        return
    
    if not await check_premium_access(callback.from_user.id):
        await callback.answer(get_message(user_lang, 'ai_function_premium_only'), show_alert=True)
        return

    await callback.message.edit_text(get_message(user_lang, 'analyzing_impact'))
    impact = await call_gemini_api(f"Виконай аналіз потенційного впливу наступної новини на різні аспекти (економіка, суспільство, політика) українською мовою, включаючи умовні підрахунки (наприклад, 'це може призвести до зростання цін на X%'): {news_item.content}", user_telegram_id=callback.from_user.id)
    await callback.message.edit_text(get_message(user_lang, 'impact_label') + f"\n{impact}", reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await callback.answer()

@router.callback_query(F.data.startswith("monetary_impact_"))
async def handle_monetary_impact(callback: CallbackQuery):
    logger.info(f"Отримано callback 'monetary_impact_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[2])
    news_item = await get_news_by_id(news_id)
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'

    if not news_item:
        await callback.answer(get_message(user_lang, 'news_not_found'), show_alert=True)
        return

    if not await check_premium_access(callback.from_user.id):
        await callback.answer(get_message(user_lang, 'ai_function_premium_only'), show_alert=True)
        return

    await callback.message.edit_text(get_message(user_lang, 'performing_monetary_analysis'))
    monetary_analysis = await call_gemini_api(f"Проаналізуй потенційний грошовий вплив подій, описаних у новині, на поточний момент та на протязі року. Вкажи умовні підрахунки та можливі фінансові наслідки українською мовою: {news_item.content}", user_telegram_id=callback.from_user.id)
    await callback.message.edit_text(get_message(user_lang, 'monetary_analysis_label') + f"\n{monetary_analysis}", reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await callback.answer()

# Нові обробники для закладок (bookmarks)
@router.callback_query(F.data.startswith("bookmark_news_"))
async def handle_bookmark_news(callback: CallbackQuery):
    logger.info(f"Отримано callback 'bookmark_news_' від користувача {callback.from_user.id}")
    parts = callback.data.split('_')
    action = parts[2]
    news_id = int(parts[3])

    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    if not user:
        await callback.answer(get_message(user_lang, 'user_not_identified'), show_alert=True)
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
                        await callback.answer(get_message(user_lang, 'bookmark_added'), show_alert=True)
                        logger.info(f"Новину {news_id} додано до закладок користувачем {user.id}")
                    else:
                        await callback.answer(get_message(user_lang, 'bookmark_already_exists'), show_alert=True)
                except Exception as e:
                    logger.error(f"Помилка додавання закладки: {e}", exc_info=True)
                    await callback.answer(get_message(user_lang, 'bookmark_add_error'), show_alert=True)
            elif action == 'remove':
                try:
                    await cur.execute(
                        "DELETE FROM bookmarks WHERE user_id = %s AND news_id = %s;",
                        (user.id, news_id)
                    )
                    if cur.rowcount > 0:
                        await callback.answer(get_message(user_lang, 'bookmark_removed'), show_alert=True)
                        logger.info(f"Новину {news_id} видалено із закладок користувачем {user.id}")
                    else:
                        await callback.answer(get_message(user_lang, 'bookmark_not_found'), show_alert=True)
                except Exception as e:
                    logger.error(f"Помилка видалення закладки: {e}", exc_info=True)
                    await callback.answer(get_message(user_lang, 'bookmark_remove_error'), show_alert=True)
            await conn.commit()
    
    current_state_data = await state.get_data()
    last_message_id = current_state_data.get('last_message_id')
    if last_message_id:
        await callback.message.edit_text(get_message(user_lang, 'ai_functions_prompt'), reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    else:
        await callback.message.edit_text(get_message(user_lang, 'action_done'), reply_markup=get_main_menu_keyboard(user_lang))
    await callback.answer()

@router.message(Command("bookmarks"))
async def command_bookmarks_handler(message: Message, state: FSMContext):
    logger.info(f"Отримано команду /bookmarks від користувача {message.from_user.id}")
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'
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
        await message.answer(get_message(user_lang, 'no_bookmarks'), reply_markup=get_main_menu_keyboard(user_lang))
        return

    response_text = get_message(user_lang, 'your_bookmarks_label') + "\n\n"
    for idx, news_item_data in enumerate(bookmarked_news):
        news_item = News(**news_item_data)
        response_text += f"{idx+1}. <a href='{news_item.source_url}'>{news_item.title}</a>\n"
        response_text += f"   <i>{news_item.published_at.strftime('%d.%m.%Y %H:%M')}</i>\n\n"
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=get_message(user_lang, 'main_menu_btn'), callback_data="main_menu"))
    
    await message.answer(response_text, reply_markup=builder.as_markup(), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    logger.info(f"Надіслано закладки користувачу {user.id}")


@router.callback_query(F.data.startswith("report_fake_news_"))
async def handle_report_fake_news(callback: CallbackQuery):
    logger.info(f"Отримано callback 'report_fake_news_' від користувача {callback.from_user.id}")
    news_id = int(callback.data.split('_')[3])
    user_telegram_id = callback.from_user.id
    user_in_db = await get_user_by_telegram_id(user_telegram_id)
    user_db_id = user_in_db.id if user_in_db else None
    user_lang = user_in_db.language if user_in_db else 'uk'

    if not user_db_id:
        logger.error(f"Не вдалося ідентифікувати користувача {user_telegram_id} для повідомлення про фейк.")
        await callback.answer(get_message(user_lang, 'user_not_identified'), show_alert=True)
        return

    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM reports WHERE user_id = %s AND target_type = 'news' AND target_id = %s;",
                (user_db_id, news_id)
            )
            if await cur.fetchone():
                await callback.answer(get_message(user_lang, 'report_already_sent'), show_alert=True)
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
    await callback.answer(get_message(user_lang, 'report_sent_success'), show_alert=True)
    await callback.message.edit_text(get_message(user_lang, 'report_action_done'), reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))

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
    
    if channel_identifier.startswith('-100') and channel_identifier[1:].replace('-', '').isdigit(): # Ensure it's a valid numeric ID
        pass
    elif channel_identifier.startswith('+'):
        logger.error(get_message('uk', 'news_channel_link_error', link=NEWS_CHANNEL_LINK))
        return
    elif not channel_identifier.startswith('@'):
        channel_identifier = '@' + channel_identifier
        logger.warning(get_message('uk', 'news_channel_link_warning', link=NEWS_CHANNEL_LINK, identifier=channel_identifier))


    display_content = news_item.content
    if len(display_content) > 250:
        display_content = display_content[:247] + "..."

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
        logger.info(get_message('uk', 'news_published_success', title=news_item.title, identifier=channel_identifier))
    except Exception as e:
        logger.error(get_message('uk', 'news_publish_error', title=news_item.title, identifier=channel_identifier, error=e), exc_info=True)


async def fetch_and_post_news_task():
    logger.info("Запущено фонове завдання: fetch_and_post_news_task")
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM sources WHERE status = 'active'")
            sources = await cur.fetchall()
    
    if not sources:
        logger.info(get_message('uk', 'no_active_sources'))
        return

    for source in sources:
        logger.info(f"Обробка джерела: {source.get('source_name', 'N/A')} ({source.get('source_url', 'N/A')})")
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
                    logger.warning(get_message('uk', 'source_parsing_warning', name=source['source_name'], url=source['source_url']))
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
                news_data['user_id_for_source'] = None
                
                added_news_item = await add_news_to_db(news_data)
                if added_news_item:
                    logger.info(get_message('uk', 'news_added_success', title=added_news_item.title))
                    await send_news_to_channel(added_news_item)
                    await update_source_stats_publication_count(source['id'])
                else:
                    logger.info(get_message('uk', 'news_not_added', name=source['source_name']))
                
                async with pool.connection() as conn_update:
                    async with conn_update.cursor() as cur_update:
                        await cur_update.execute("UPDATE sources SET last_parsed = CURRENT_TIMESTAMP WHERE id = %s", (source['id'],))
                        await conn_update.commit()
                        logger.info(get_message('uk', 'source_last_parsed_updated', name=source['source_name']))
            else:
                logger.warning(get_message('uk', 'source_parsing_warning', name=source['source_name'], url=source['source_url']))
        except Exception as e:
            source_name_log = source.get('source_name', 'N/A')
            source_url_log = source.get('source_url', 'N/A')
            logger.error(get_message('uk', 'source_parsing_error', name=source_name_log, url=source_url_log, error=e), exc_info=True)

async def delete_expired_news_task():
    logger.info("Запущено фонове завдання: delete_expired_news_task")
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM news WHERE expires_at IS NOT NULL AND expires_at < CURRENT_TIMESTAMP;")
            deleted_count = cur.rowcount
            await conn.commit()
            if deleted_count > 0:
                logger.info(get_message('uk', 'deleted_expired_news', count=deleted_count))
            else:
                logger.info(get_message('uk', 'no_expired_news'))

async def send_daily_digest():
    logger.info("Запущено фонове завдання: send_daily_digest")
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id, telegram_id, language FROM users WHERE auto_notifications = TRUE AND digest_frequency = 'daily';")
            users_for_digest = await cur.fetchall()

    if not users_for_digest:
        logger.info(get_message('uk', 'daily_digest_no_users'))
        return

    for user_data in users_for_digest:
        user_db_id = user_data['id']
        user_telegram_id = user_data['telegram_id']
        user_lang = user_data['language']

        logger.info(f"Надсилаю щоденний дайджест користувачу {user_telegram_id}")

        news_items = await get_news_for_user(user_db_id, limit=5)

        if not news_items:
            logger.info(get_message('uk', 'daily_digest_no_news', user_id=user_telegram_id))
            continue

        digest_text = get_message(user_lang, 'daily_digest_header') + "\n\n"
        for i, news_item in enumerate(news_items):
            summary = await call_gemini_api(f"Зроби коротке резюме новини українською мовою: {news_item.content}", user_telegram_id=user_telegram_id)
            digest_text += get_message(user_lang, 'daily_digest_entry', idx=i+1, title=news_item.title, summary=summary, source_url=news_item.source_url)
            await mark_news_as_viewed(user_db_id, news_item.id)

        try:
            await bot.send_message(
                chat_id=user_telegram_id,
                text=digest_text,
                reply_markup=get_main_menu_keyboard(user_lang),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
            logger.info(get_message('uk', 'daily_digest_sent_success', user_id=user_telegram_id))
        except Exception as e:
            logger.error(get_message('uk', 'daily_digest_send_error', user_id=user_telegram_id, error=e), exc_info=True)


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
                        invitee_telegram_id = %s
                    WHERE id = %s;
                    """,
                    (new_user_db_id, invite_id)
                )
                await cur.execute(
                    "UPDATE users SET inviter_id = %s WHERE id = %s;",
                    (inviter_user_db_id, new_user_db_id)
                )
                logger.info(f"Запрошення {invite_code} використано користувачем {new_user_db_id}. Запрошувач: {inviter_user_db_id}")
                await conn.commit()
            else:
                logger.warning(f"Недійсний або використаний інвайт-код: {invite_code} для користувача {new_user_db_id}")

@router.message(Command("invite"))
async def command_invite_handler(message: Message):
    logger.info(f"Отримано команду /invite від користувача {message.from_user.id}")
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'
    if not user:
        user = await create_or_update_user(message.from_user)
    
    invite_code = await create_invite(user.id)
    if invite_code:
        invite_link = f"https://t.me/{bot.me.username}?start={invite_code}"
        await message.answer(get_message(user_lang, 'your_invite_code', invite_code=invite_code, invite_link=hlink(get_message(user_lang, 'invite_link_label'), invite_link)),
                             parse_mode=ParseMode.HTML, disable_web_page_preview=False)
    else:
        await message.answer(get_message(user_lang, 'invite_error'))


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

@router.callback_query(F.data == "source_stats_menu")
async def handle_source_stats_menu(callback: CallbackQuery):
    logger.info(f"Отримано callback 'source_stats_menu' від користувача {callback.from_user.id}")
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
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
        await callback.message.edit_text(get_message(user_lang, 'no_source_stats_available'), reply_markup=get_main_menu_keyboard(user_lang))
        await callback.answer()
        return

    response_text = get_message(user_lang, 'source_stats_top_10') + "\n\n"
    for idx, stat in enumerate(stats):
        response_text += get_message(user_lang, 'source_stats_item', idx=idx+1, source_name=stat['source_name'], publication_count=stat['publication_count']) + "\n"
    
    await callback.message.edit_text(response_text, reply_markup=get_main_menu_keyboard(user_lang), parse_mode=ParseMode.HTML)
    await callback.answer()

@router.message(Command("moderation"))
async def command_moderation_handler(message: Message, state: FSMContext):
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'
    if not user or not user.is_admin:
        await message.answer(get_message(user_lang, 'no_admin_access'), reply_markup=get_main_menu_keyboard(user_lang))
        return

    logger.info(f"Адміністратор {message.from_user.id} викликав команду /moderation.")
    await message.answer(get_message(user_lang, 'loading_moderation_news'))

    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id FROM news WHERE moderation_status = 'pending' ORDER BY published_at ASC;")
            pending_news_ids = [row['id'] for row in await cur.fetchall()]

    if not pending_news_ids:
        await message.answer(get_message(user_lang, 'no_pending_news'), reply_markup=get_main_menu_keyboard(user_lang))
        await state.clear()
        return

    await state.update_data(moderation_news_ids=pending_news_ids, current_moderation_index=0)
    await state.set_state(ModerationStates.browsing_pending_news)

    await send_moderation_news_to_admin(message.chat.id, pending_news_ids[0], 0, len(pending_news_ids), user_lang)


async def send_moderation_news_to_admin(chat_id: int, news_id: int, current_index: int, total_news: int, user_lang: str):
    news_item = await get_news_by_id(news_id)
    if not news_item:
        logger.warning(f"Новина {news_id} не знайдена для модерації.")
        await bot.send_message(chat_id, get_message(user_lang, 'news_not_found_for_moderation'))
        return

    source_info = await get_source_by_id(news_item.source_id)
    source_name = source_info['source_name'] if source_info else get_message(user_lang, 'unknown_source')

    text = (
        get_message(user_lang, 'moderation_news_header', current_index=current_index + 1, total_news=total_news) + "\n\n"
        f"<b>{get_message(user_lang, 'news_title_label')}</b> {news_item.title}\n\n"
        f"<b>{get_message(user_lang, 'news_content_label')}</b>\n{news_item.content}\n\n"
        f"{get_message(user_lang, 'source_label')} {source_name} ({news_item.source_url})\n"
        f"{get_message(user_lang, 'published_at_label')} {news_item.published_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"{get_message(user_lang, 'status_label')} {news_item.moderation_status}"
    )

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text=get_message(user_lang, 'approve_btn'), callback_data=f"moderate_news_approve_{news_item.id}"),
        InlineKeyboardButton(text=get_message(user_lang, 'reject_btn'), callback_data=f"moderate_news_reject_{news_item.id}")
    )
    
    nav_buttons = []
    if current_index > 0:
        nav_buttons.append(InlineKeyboardButton(text=get_message(user_lang, 'prev_btn'), callback_data="moderation_prev_news"))
    if current_index < total_news - 1:
        nav_buttons.append(InlineKeyboardButton(text=get_message(user_lang, 'next_btn'), callback_data="moderation_next_news"))
    
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.row(InlineKeyboardButton(text=get_message(user_lang, 'main_menu_btn'), callback_data="main_menu"))

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
            placeholder_image_url = "https://placehold.co/600x400/CCCCCC/000000?text=No+Image"
            await bot.send_photo(
                chat_id=chat_id,
                photo=placeholder_image_url,
                caption=text + f"\n[Зображення новини]({news_item.image_url})",
                reply_markup=builder.as_markup(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False
            )
    else:
        placeholder_image_url = "https://placehold.co/600x400/CCCCCC/000000?text=No+Image"
        await bot.send_photo(
            chat_id=chat_id,
            photo=placeholder_image_url,
            caption=text,
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML
        )

@router.callback_query(ModerationStates.browsing_pending_news, F.data.startswith("moderate_news_"))
async def handle_moderation_action(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    if not user or not user.is_admin:
        await callback.answer(get_message(user_lang, 'no_admin_access'), show_alert=True)
        return

    parts = callback.data.split('_')
    action = parts[2]
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
                await callback.answer(get_message(user_lang, f'moderation_news_{action}', news_id=news_id), show_alert=True)
                logger.info(f"Новину {news_id} {new_status} адміністратором {user.telegram_id}.")
                
                if new_status == 'approved':
                    await send_news_to_channel(News(**updated_news))

                user_data = await state.get_data()
                pending_news_ids = user_data.get("moderation_news_ids", [])
                current_index = user_data.get("current_moderation_index", 0)

                if news_id in pending_news_ids:
                    pending_news_ids.remove(news_id)
                    await state.update_data(moderation_news_ids=pending_news_ids)

                if pending_news_ids:
                    if current_index >= len(pending_news_ids):
                        current_index = len(pending_news_ids) - 1
                    
                    await state.update_data(current_moderation_index=current_index)
                    await callback.message.delete()
                    await send_moderation_news_to_admin(callback.message.chat.id, pending_news_ids[current_index], current_index, len(pending_news_ids), user_lang)
                else:
                    await callback.message.edit_text(get_message(user_lang, 'moderation_all_done'), reply_markup=get_main_menu_keyboard(user_lang))
                    await state.clear()
            else:
                await callback.answer(get_message(user_lang, 'news_not_found'), show_alert=True)
    await callback.answer()

@router.callback_query(ModerationStates.browsing_pending_news, F.data == "moderation_next_news")
async def handle_moderation_next_news(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    if not user or not user.is_admin:
        await callback.answer(get_message(user_lang, 'no_admin_access'), show_alert=True)
        return

    user_data = await state.get_data()
    pending_news_ids = user_data.get("moderation_news_ids", [])
    current_index = user_data.get("current_moderation_index", 0)

    if not pending_news_ids or current_index + 1 >= len(pending_news_ids):
        await callback.answer(get_message(user_lang, 'no_more_moderation_news'), show_alert=True)
        return

    next_index = current_index + 1
    await state.update_data(current_moderation_index=next_index)
    await callback.message.delete()
    await send_moderation_news_to_admin(callback.message.chat.id, pending_news_ids[next_index], next_index, len(pending_news_ids), user_lang)
    await callback.answer()

@router.callback_query(ModerationStates.browsing_pending_news, F.data == "moderation_prev_news")
async def handle_moderation_prev_news(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    if not user or not user.is_admin:
        await callback.answer(get_message(user_lang, 'no_admin_access'), show_alert=True)
        return

    user_data = await state.get_data()
    pending_news_ids = user_data.get("moderation_news_ids", [])
    current_index = user_data.get("current_moderation_index", 0)

    if not pending_news_ids or current_index <= 0:
        await callback.answer(get_message(user_lang, 'moderation_first_news'), show_alert=True)
        return

    prev_index = current_index - 1
    await state.update_data(current_moderation_index=prev_index)
    await callback.message.delete()
    await send_moderation_news_to_admin(callback.message.chat.id, pending_news_ids[prev_index], prev_index, len(pending_news_ids), user_lang)
    await callback.answer()

# Планувальник завдань
async def scheduler():
    fetch_schedule_expression = '*/5 * * * *'
    delete_schedule_expression = '0 */5 * * *' 
    daily_digest_schedule_expression = '0 9 * * *' # Щоденний дайджест о 9:00 UTC
    
    while True:
        now = datetime.now(timezone.utc)
        
        fetch_itr = croniter(fetch_schedule_expression, now)
        next_fetch_run = fetch_itr.get_next(datetime)
        fetch_delay_seconds = (next_fetch_run - now).total_seconds()

        delete_itr = croniter(delete_schedule_expression, now)
        next_delete_run = delete_itr.get_next(datetime)
        delete_delay_seconds = (next_delete_run - now).total_seconds()

        daily_digest_itr = croniter(daily_digest_schedule_expression, now)
        next_daily_digest_run = daily_digest_itr.get_next(datetime)
        daily_digest_delay_seconds = (next_daily_digest_run - now).total_seconds()

        min_delay = min(fetch_delay_seconds, delete_delay_seconds, daily_digest_delay_seconds)
        
        logger.info(f"Наступний запуск fetch_and_post_news_task через {int(fetch_delay_seconds)} секунд.")
        logger.info(f"Наступний запуск delete_expired_news_task через {int(delete_delay_seconds)} секунд.")
        logger.info(f"Наступний запуск send_daily_digest через {int(daily_digest_delay_seconds)} секунд.")
        logger.info(f"Бот засинає на {int(min_delay)} секунд.")

        await asyncio.sleep(min_delay)

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
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
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
        logger.debug(f"Отримано оновлення Telegram (повний об'єкт): {update}")
        aiogram_update = types.Update.model_validate(update, context={"bot": bot})
        await dp.feed_update(bot, aiogram_update)
        logger.info("Успішно оброблено оновлення Telegram.")
    except Exception as e:
        logger.error(f"Помилка обробки вебхука Telegram: {e}", exc_info=True)
    return {"ok": True}

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

@app.get("/healthz", response_class=PlainTextResponse)
async def healthz():
    """Health check endpoint for Kubernetes/Load Balancers."""
    return "OK"

@app.get("/ping", response_class=PlainTextResponse)
async def ping():
    """Simple ping endpoint to check service responsiveness."""
    return "pong"

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
            return {"users": users}

@app.get("/reports", response_class=HTMLResponse)
async def read_reports(api_key: str = Depends(get_api_key), limit: int = 10, offset: int = 0):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            query = "SELECT r.*, u.username, u.first_name, n.title as news_title, n.source_url as news_source_url FROM reports r LEFT JOIN users u ON r.user_id = u.id LEFT JOIN news n ON r.target_id = n.id WHERE r.target_type = 'news'"
            params = []
            query += " ORDER BY r.created_at DESC LIMIT %s OFFSET %s;"
            params.extend([limit, offset])
            await cur.execute(query, tuple(params))
            reports = await cur.fetchall()
            return reports

@app.get("/api/admin/sources")
async def get_admin_sources(api_key: str = Depends(get_api_key), limit: int = 100, offset: int = 0):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
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

if __name__ == "__main__":
    import uvicorn
    if not os.getenv("WEBHOOK_URL"):
        logger.info("WEBHOOK_URL не встановлено. Запускаю бота в режимі polling.")
        async def start_polling():
            await dp.start_polling(bot)
        asyncio.run(start_polling())
    
    uvicorn.run(app, host="0.0.0.0", port=8000)