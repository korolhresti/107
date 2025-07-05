import asyncio
import logging
import logging.handlers
from datetime import datetime, timedelta, timezone
import json
import os
from typing import List, Optional, Dict, Any, Union
import random
import io
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

AI_REQUEST_TIMESTAMPS: Dict[int, float] = {}
AI_REQUEST_LIMIT_SECONDS = 5

app = FastAPI(title="Telegram AI News Bot API", version="1.0.0")
app.mount("/static", StaticFiles(directory="."), name="static")

api_key_header = APIKeyHeader(name="X-API-Key")

async def get_api_key(api_key: str = Depends(api_key_header)):
    if not ADMIN_API_KEY: raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="ADMIN_API_KEY not configured.")
    if api_key is None or api_key != ADMIN_API_KEY: raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key.")
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
            async with db_pool.connection() as conn: await conn.execute("SELECT 1")
            logger.info("DB pool initialized.")
        except Exception as e:
            logger.error(f"DB pool error: {e}")
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
    email: Optional[str] = None
    view_mode: Optional[str] = 'detailed'

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
        'help_text': ("<b>Доступні команди:</b>\n"
                      "/start - Почати роботу\n"
                      "/menu - Головне меню\n"
                      "/cancel - Скасувати дію\n"
                      "/my_news - Мої новини\n"
                      "/add_source - Додати джерело\n"
                      "/ask_expert - Запитати експерта\n"
                      "/price_analysis - Аналіз ціни\n"
                      "/invite - Запросити друзів\n\n"
                      "<b>AI-функції:</b> Доступні під новиною.\n"
                      "<b>AI-медіа:</b> /ai_media_menu\n"
                      "<b>Аналітика:</b> /analytics_menu"),
        'action_cancelled': "Дію скасовано. Оберіть наступну дію:",
        'add_source_prompt': "Надішліть URL джерела:",
        'invalid_url': "Будь ласка, введіть дійсний URL.",
        'select_source_type': "Оберіть тип джерела:",
        'source_url_not_found': "URL джерела не знайдено.",
        'source_added_success': "Джерело '{source_url}' успішно додано!",
        'add_source_error': "Помилка при додаванні джерела.",
        'no_new_news': "Немає нових новин.",
        'news_not_found': "Новина не знайдена.",
        'no_more_news': "Більше новин немає.",
        'first_news': "Це перша новина.",
        'error_start_menu': "Сталася помилка. Почніть з /menu.",
        'ai_functions_prompt': "Оберіть AI-функцію:",
        'ai_function_premium_only': "Ця функція лише для преміум.",
        'news_title_label': "Заголовок:",
        'news_content_label': "Зміст:",
        'published_at_label': "Опубліковано:",
        'news_progress': "Новина {current_index} з {total_news}",
        'read_source_btn': "🔗 Читати джерело",
        'ai_functions_btn': "🧠 AI-функції",
        'prev_btn': "⬅️ Попередня",
        'next_btn': "➡️ Далі",
        'main_menu_btn': "⬅️ До головного меню",
        'generating_ai_summary': "Генерую AI-резюме...",
        'ai_summary_label': "AI-резюме:",
        'select_translate_language': "Оберіть мову для перекладу:",
        'translating_news': "Перекладаю новину на {language_name}...",
        'translation_label': "Переклад на {language_name}:",
        'generating_audio': "Генерую аудіо...",
        'audio_news_caption': "🔊 Новина: {title}",
        'audio_error': "Помилка генерації аудіо.",
        'ask_news_ai_prompt': "Надішліть ваше запитання щодо новини:",
        'processing_question': "Обробляю ваше запитання...",
        'ai_response_label': "Відповідь AI:",
        'ai_news_not_found': "Новина для запитання не знайдена.",
        'ask_free_ai_prompt': "Надішліть ваше запитання до AI:",
        'extracting_entities': "Витягую ключові сутності...",
        'entities_label': "Ключові сутності:",
        'explain_term_prompt': "Надішліть термін, який потрібно пояснити:",
        'explaining_term': "Пояснюю термін...",
        'term_explanation_label': "Пояснення терміну '{term}':",
        'classifying_topics': "Класифікую за темами...",
        'topics_label': "Теми:",
        'checking_facts': "Перевіряю факти...",
        'fact_check_label': "Перевірка фактів:",
        'analyzing_sentiment': "Аналізую настрій...",
        'sentiment_label': "Аналіз настрою:",
        'detecting_bias': "Виявляю упередженість...",
        'bias_label': "Виявлення упередженості:",
        'generating_audience_summary': "Генерую резюме для аудиторії...",
        'audience_summary_label': "Резюме для аудиторії:",
        'searching_historical_analogues': "Шукаю історичні аналоги...",
        'historical_analogues_label': "Історичні аналоги:",
        'analyzing_impact': "Аналізую вплив...",
        'impact_label': "Аналіз впливу:",
        'performing_monetary_analysis': "Виконую грошовий аналіз...",
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
        'report_sent_success': "Дякуємо! Ваш репорт надіслано.",
        'report_action_done': "Дякуємо за ваш внесок! Оберіть наступну дію:",
        'user_not_identified': "Не вдалося ідентифікувати користувача.",
        'no_admin_access': "У вас немає доступу до цієї команди.",
        'loading_moderation_news': "Завантажую новини на модерацію...",
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
        'your_invite_code': "Ваш інвайт-код: <code>{invite_code}</code>\nПоділіться: {invite_link}",
        'invite_error': "Не вдалося згенерувати інвайт-код.",
        'daily_digest_header': "📰 Ваш щоденний AI-дайджест новин:",
        'daily_digest_entry': "<b>{idx}. {title}</b>\n{summary}\n🔗 <a href='{source_url}'>Читати повністю</a>\n\n",
        'no_news_for_digest': "Немає нових новин для дайджесту.",
        'ai_rate_limit_exceeded': "Забагато запитів до AI. Зачекайте {seconds_left} секунд.",
        'what_new_digest_header': "👋 Привіт! Ви пропустили {count} новин. Ось дайджест:",
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
        'news_channel_link_error': "Невірне посилання на канал.",
        'news_channel_link_warning': "Невірний формат посилання на канал.",
        'news_published_success': "Новина '{title}' опублікована в каналі {identifier}.",
        'news_publish_error': "Помилка публікації новини '{title}': {error}",
        'source_parsing_warning': "Не вдалося спарсити контент з джерела: {name} ({url}).",
        'source_parsing_error': "Критична помилка парсингу джерела {name} ({url}): {error}",
        'no_active_sources': "Немає активних джерел для парсингу.",
        'news_already_exists': "Новина з URL {url} вже існує.",
        'news_added_success': "Новина '{title}' успішно додана до БД.",
        'news_not_added': "Новина з джерела {name} не була додана до БД.",
        'source_last_parsed_updated': "Оновлено last_parsed для джерела {name}.",
        'deleted_expired_news': "Видалено {count} прострочених новин.",
        'no_expired_news': "Немає прострочених новин.",
        'daily_digest_no_users': "Немає користувачів для дайджесту.",
        'daily_digest_no_news': "Для користувача {user_id} немає нових новин для дайджесту.",
        'daily_digest_sent_success': "Дайджест надіслано користувачу {user_id}.",
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
        'ai_smart_summary_generating': "Генерую {summary_type} резюме...",
        'ai_smart_summary_label': "<b>AI-резюме ({summary_type}):</b>\n{summary}",
        'ask_expert_prompt': "Оберіть експерта:",
        'expert_portnikov_btn': "Віталій Портников (Журналіст)",
        'expert_libsits_btn': "Ігор Лібсіц (Економіст)",
        'ask_expert_question_prompt': "Надішліть ваше запитання до {expert_name}:",
        'expert_response_label': "Відповідь {expert_name}:",
        'price_analysis_prompt': "Надішліть опис товару та, якщо є, посилання на зображення:",
        'price_analysis_generating': "Виконую аналіз ціни...",
        'price_analysis_result': "<b>Аналіз ціни:</b>\n{result}",
        'ai_media_menu_prompt': "AI-медіа функції:",
        'generate_ai_news_btn': "📝 AI-новина на основі трендів",
        'youtube_to_news_btn': "▶️ YouTube → Новина",
        'generate_short_video_post_btn': "🎬 Генерувати відео-пост",
        'create_filtered_channel_btn': "➕ Створити мій канал",
        'create_ai_media_btn': "🤖 Створити AI-медіа",
        'ai_news_generating': "Генерую AI-новину...",
        'ai_news_generated_success': "AI-новина '{title}' згенерована та додана.",
        'youtube_url_prompt': "Надішліть посилання на YouTube-відео:",
        'youtube_processing': "Обробляю YouTube-відео...",
        'youtube_summary_label': "<b>YouTube Новина:</b>\n{summary}",
        'short_video_generating': "Генерую короткий відео-пост...",
        'short_video_generated': "Короткий відео-пост згенеровано (імітація).",
        'filtered_channel_prompt': "Надішліть назву для вашого каналу та ключові теми (через кому):",
        'filtered_channel_creating': "Створюю канал '{channel_name}' з фільтрацією за темами: {topics}...",
        'filtered_channel_created': "Канал '{channel_name}' успішно 'створено'!",
        'ai_media_creating': "Створюю ваше AI-медіа...",
        'ai_media_created': "Ваше AI-медіа '{media_name}' успішно 'створено'!",
        'analytics_menu_prompt': "Аналітика та дослідження:",
        'infographics_btn': "📈 Інфографіка",
        'trust_index_btn': "⚖️ Індекс довіри до джерел",
        'long_term_connections_btn': "🔗 Довгострокові зв'язки",
        'ai_prediction_btn': "🔮 AI-прогноз",
        'infographics_generating': "Генерую інфографіку...",
        'infographics_result': "<b>Інфографіка:</b>\n{result}",
        'trust_index_calculating': "Розраховую індекс довіри...",
        'trust_index_result': "<b>Індекс довіри:</b>\n{result}",
        'long_term_connections_generating': "Шукаю довгострокові зв'язки...",
        'long_term_connections_result': "<b>Довгострокові зв'язки:</b>\n{result}",
        'ai_prediction_generating': "Генерую AI-прогноз...",
        'ai_prediction_result': "<b>AI-прогноз:</b>\n{result}",
        'onboarding_step_1': "Крок 1: Додайте джерело, натиснувши '➕ Додати джерело'.",
        'onboarding_step_2': "Крок 2: Перегляньте новини, натиснувши '📰 Мої новини'.",
        'onboarding_step_3': "Крок 3: Натисніть '🧠 AI-функції' під новиною, щоб спробувати AI.",
    },
    'en': {
        'welcome': "Hello, {first_name}! I'm your AI News Bot. Choose an action:",
        'main_menu_prompt': "Choose an action:",
        'help_text': ("<b>Available Commands:</b>\n"
                      "/start - Start the bot\n"
                      "/menu - Main menu\n"
                      "/cancel - Cancel action\n"
                      "/my_news - My News\n"
                      "/add_source - Add Source\n"
                      "/ask_expert - Ask an Expert\n"
                      "/price_analysis - Price Analysis\n"
                      "/invite - Invite Friends\n\n"
                      "<b>AI Functions:</b> Available below news.\n"
                      "<b>AI Media:</b> /ai_media_menu\n"
                      "<b>Analytics:</b> /analytics_menu"),
        'action_cancelled': "Action cancelled. Choose your next action:",
        'add_source_prompt': "Send source URL:",
        'invalid_url': "Please enter a valid URL.",
        'select_source_type': "Select source type:",
        'source_url_not_found': "Source URL not found.",
        'source_added_success': "Source '{source_url}' successfully added!",
        'add_source_error': "Error adding source.",
        'no_new_news': "No new news.",
        'news_not_found': "News not found.",
        'no_more_news': "No more news available.",
        'first_news': "This is the first news item.",
        'error_start_menu': "An error occurred. Please start with /menu.",
        'ai_functions_prompt': "Select an AI function:",
        'ai_function_premium_only': "This function is for premium users only.",
        'news_title_label': "Title:",
        'news_content_label': "Content:",
        'published_at_label': "Published:",
        'news_progress': "News {current_index} of {total_news}",
        'read_source_btn': "🔗 Read Source",
        'ai_functions_btn': "🧠 AI Functions",
        'prev_btn': "⬅️ Previous",
        'next_btn': "➡️ Next",
        'main_menu_btn': "⬅️ Back to Main Menu",
        'generating_ai_summary': "Generating AI summary...",
        'ai_summary_label': "AI Summary:",
        'select_translate_language': "Select language for translation:",
        'translating_news': "Translating news to {language_name}...",
        'translation_label': "Translation to {language_name}:",
        'generating_audio': "Generating audio...",
        'audio_news_caption': "🔊 News: {title}",
        'audio_error': "Error generating audio.",
        'ask_news_ai_prompt': "Send your question about the news:",
        'processing_question': "Processing your question...",
        'ai_response_label': "AI Response:",
        'ai_news_not_found': "News for the question not found.",
        'ask_free_ai_prompt': "Send your question to AI:",
        'extracting_entities': "Extracting key entities...",
        'entities_label': "Key Entities:",
        'explain_term_prompt': "Send the term to explain:",
        'explaining_term': "Explaining term...",
        'term_explanation_label': "Explanation of term '{term}':",
        'classifying_topics': "Classifying by topics...",
        'topics_label': "Topics:",
        'checking_facts': "Checking facts...",
        'fact_check_label': "Fact Check:",
        'analyzing_sentiment': "Analyzing sentiment...",
        'sentiment_label': "Sentiment Analysis:",
        'detecting_bias': "Detecting bias...",
        'bias_label': "Bias Detection:",
        'generating_audience_summary': "Generating audience summary...",
        'audience_summary_label': "Audience Summary:",
        'searching_historical_analogues': "Searching historical analogues...",
        'historical_analogues_label': "Historical Analogues:",
        'analyzing_impact': "Analyzing impact...",
        'impact_label': "Impact Analysis:",
        'performing_monetary_analysis': "Performing monetary analysis...",
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
        'report_sent_success': "Thank you! Your report has been sent.",
        'report_action_done': "Thank you for your contribution! Choose your next action:",
        'user_not_identified': "Could not identify user.",
        'no_admin_access': "You do not have access to this command.",
        'loading_moderation_news': "Loading news for moderation...",
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
        'first_moderation_news': "This is the first news for moderation.",
        'source_stats_label': "📊 Source Statistics (top-10 by publications):",
        'source_stats_entry': "{idx}. {source_name}: {count} publications",
        'no_source_stats': "No source statistics available.",
        'your_invite_code': "Your invite code: <code>{invite_code}</code>\nShare: {invite_link}",
        'invite_error': "Failed to generate invite code.",
        'daily_digest_header': "📰 Your daily AI News Digest:",
        'daily_digest_entry': "<b>{idx}. {title}</b>\n{summary}\n🔗 <a href='{source_url}'>Read full article</a>\n\n",
        'no_news_for_digest': "No new news for digest for you.",
        'ai_rate_limit_exceeded': "Too many AI requests. Please wait {seconds_left} seconds.",
        'what_new_digest_header': "👋 Hello! You missed {count} news items. Here's a digest:",
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
        'news_channel_link_error': "Invalid channel link.",
        'news_channel_link_warning': "Invalid channel link format.",
        'news_published_success': "News '{title}' published to channel {identifier}.",
        'news_publish_error': "Error publishing news '{title}': {error}",
        'source_parsing_warning': "Failed to parse content from source: {name} ({url}).",
        'source_parsing_error': "Critical error parsing source {name} ({url}): {error}",
        'no_active_sources': "No active sources for parsing.",
        'news_already_exists': "News with URL {url} already exists.",
        'news_added_success': "News '{title}' successfully added to DB.",
        'news_not_added': "News from source {name} was not added to DB.",
        'source_last_parsed_updated': "Updated last_parsed for source {name}.",
        'deleted_expired_news': "Deleted {count} expired news items.",
        'no_expired_news': "No expired news.",
        'daily_digest_no_users': "No users for digest.",
        'daily_digest_no_news': "No new news for digest for user {user_id}.",
        'daily_digest_sent_success': "Digest successfully sent to user {user_id}.",
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
        'ai_smart_summary_generating': "Generating {summary_type} summary...",
        'ai_smart_summary_label': "<b>AI Summary ({summary_type}):</b>\n{summary}",
        'ask_expert_prompt': "Select an expert:",
        'expert_portnikov_btn': "Vitaliy Portnikov (Journalist)",
        'expert_libsits_btn': "Igor Libsits (Economist)",
        'ask_expert_question_prompt': "Send your question to {expert_name}:",
        'expert_response_label': "Response from {expert_name}:",
        'price_analysis_prompt': "Send product description and, if available, image link:",
        'price_analysis_generating': "Performing price analysis...",
        'price_analysis_result': "<b>Price Analysis:</b>\n{result}",
        'ai_media_menu_prompt': "AI Media Functions:",
        'generate_ai_news_btn': "📝 AI News based on trends",
        'youtube_to_news_btn': "▶️ YouTube → News",
        'generate_short_video_post_btn': "🎬 Generate Video Post",
        'create_filtered_channel_btn': "➕ Create My Channel",
        'create_ai_media_btn': "🤖 Create AI Media",
        'ai_news_generating': "Generating AI news...",
        'ai_news_generated_success': "AI news '{title}' generated and added.",
        'youtube_url_prompt': "Send YouTube video link:",
        'youtube_processing': "Processing YouTube video...",
        'youtube_summary_label': "<b>YouTube News:</b>\n{summary}",
        'short_video_generating': "Generating short video post...",
        'short_video_generated': "Short video post generated (mock).",
        'filtered_channel_prompt': "Send channel name and keywords (comma-separated):",
        'filtered_channel_creating': "Creating channel '{channel_name}' with topics: {topics}...",
        'filtered_channel_created': "Channel '{channel_name}' successfully 'created'!",
        'ai_media_creating': "Creating your AI media...",
        'ai_media_created': "Your AI media '{media_name}' successfully 'created'!",
        'analytics_menu_prompt': "Analytics & Research:",
        'infographics_btn': "📈 Infographics",
        'trust_index_btn': "⚖️ Source Trust Index",
        'long_term_connections_btn': "🔗 Long-Term Connections",
        'ai_prediction_btn': "🔮 AI Prediction",
        'infographics_generating': "Generating infographics...",
        'infographics_result': "<b>Infographics:</b>\n{result}",
        'trust_index_calculating': "Calculating trust index...",
        'trust_index_result': "<b>Trust Index:</b>\n{result}",
        'long_term_connections_generating': "Searching for long-term connections...",
        'long_term_connections_result': "<b>Long-Term Connections:</b>\n{result}",
        'ai_prediction_generating': "Generating AI prediction...",
        'ai_prediction_result': "<b>AI Prediction:</b>\n{result}",
        'onboarding_step_1': "Step 1: Add a source by clicking '➕ Add Source'.",
        'onboarding_step_2': "Step 2: View news by clicking '📰 My News'.",
        'onboarding_step_3': "Step 3: Click '🧠 AI Functions' below the news to try AI.",
    }
}

def get_message(user_lang: str, key: str, **kwargs) -> str:
    return MESSAGES.get(user_lang, MESSAGES['uk']).get(key, f"MISSING_TRANSLATION_{key}").format(**kwargs)

# DB Functions (simplified for brevity, assume they work as before)
async def create_or_update_user(user_data: types.User):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            telegram_id = user_data.id
            username = user_data.username
            first_name = user_data.first_name
            last_name = user_data.last_name
            await cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
            user_record = await cur.fetchone()
            if user_record:
                await cur.execute(
                    """UPDATE users SET username = %s, first_name = %s, last_name = %s, last_active = CURRENT_TIMESTAMP WHERE telegram_id = %s RETURNING *;""",
                    (username, first_name, last_name, telegram_id)
                )
            else:
                await cur.execute(
                    """INSERT INTO users (telegram_id, username, first_name, last_name, created_at, last_active) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) RETURNING *;""",
                    (telegram_id, username, first_name, last_name)
                )
            return User(**await cur.fetchone())

async def get_user_by_telegram_id(telegram_id: int) -> Optional[User]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
            user_record = await cur.fetchone()
            return User(**user_record) if user_record else None

async def add_news_to_db(news_data: Dict[str, Any]) -> Optional[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id FROM sources WHERE source_url = %s", (str(news_data['source_url']),))
            source_record = await cur.fetchone()
            source_id = None
            if source_record:
                source_id = source_record['id']
            else:
                user_id_for_source = news_data.get('user_id_for_source')
                parsed_url = HttpUrl(news_data['source_url'])
                source_name = parsed_url.host if parsed_url.host else 'Unknown Source'
                await cur.execute(
                    """INSERT INTO sources (user_id, source_name, source_url, source_type, added_at) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) ON CONFLICT (source_url) DO UPDATE SET source_name = EXCLUDED.source_name, source_type = EXCLUDED.source_type, status = 'active', last_parsed = NULL RETURNING id;""",
                    (user_id_for_source, source_name, str(news_data['source_url']), news_data.get('source_type', 'web'))
                )
                source_id = (await cur.fetchone())['id']
            await cur.execute("SELECT id FROM news WHERE source_url = %s", (str(news_data['source_url']),))
            if await cur.fetchone():
                return None
            moderation_status = 'approved' if news_data.get('user_id_for_source') is None else 'pending'
            await cur.execute(
                """INSERT INTO news (source_id, title, content, source_url, image_url, published_at, ai_summary, ai_classified_topics, moderation_status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *;""",
                (source_id, news_data['title'], news_data['content'], str(news_data['source_url']), str(news_data['image_url']) if news_data.get('image_url') else None, news_data['published_at'], news_data.get('ai_summary'), json.dumps(news_data.get('ai_classified_topics')) if news_data.get('ai_classified_topics') else None, moderation_status)
            )
            return News(**await cur.fetchone())

async def get_news_for_user(user_id: int, limit: int = 10, offset: int = 0) -> List[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            query = """SELECT * FROM news WHERE id NOT IN (SELECT news_id FROM user_news_views WHERE user_id = %s) AND moderation_status = 'approved' AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP) ORDER BY published_at DESC LIMIT %s OFFSET %s;"""
            await cur.execute(query, (user_id, limit, offset))
            return [News(**record) for record in await cur.fetchall()]

async def get_news_to_publish(limit: int = 1) -> List[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            # Select news that are approved, not expired, and not yet marked as published
            # For simplicity, we'll use a new status 'published' or a separate table/flag
            # For now, let's assume we pick the oldest approved news that hasn't been published to the channel yet.
            # This requires a new column in `news` table, e.g., `is_published_to_channel BOOLEAN DEFAULT FALSE`
            # For this example, let's just pick the oldest approved news.
            await cur.execute("""SELECT * FROM news WHERE moderation_status = 'approved' AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP) ORDER BY published_at ASC LIMIT %s;""", (limit,))
            return [News(**record) for record in await cur.fetchall()]

async def mark_news_as_published_to_channel(news_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # This would update a flag like `is_published_to_channel`
            # For now, we'll just log it.
            logger.info(f"News {news_id} marked as published to channel (mock).")

async def count_unseen_news(user_id: int) -> int:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("""SELECT COUNT(*) FROM news WHERE id NOT IN (SELECT news_id FROM user_news_views WHERE user_id = %s) AND moderation_status = 'approved' AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP);""", (user_id,))
            return (await cur.fetchone())['count']

async def mark_news_as_viewed(user_id: int, news_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""INSERT INTO user_news_views (user_id, news_id, viewed_at) VALUES (%s, %s, CURRENT_TIMESTAMP) ON CONFLICT (user_id, news_id) DO NOTHING;""", (user_id, news_id))
            await conn.commit()

async def get_news_by_id(news_id: int) -> Optional[News]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM news WHERE id = %s", (news_id,))
            news_record = await cur.fetchone()
            return News(**news_record) if news_record else None

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
    waiting_for_translate_language = State()
    waiting_for_free_question = State()
    waiting_for_summary_type = State()
    waiting_for_youtube_url = State()
    waiting_for_expert_question = State()
    expert_type = State()
    waiting_for_price_analysis_input = State()
    waiting_for_filtered_channel_details = State()
    waiting_for_ai_media_name = State()

class ModerationStates(StatesGroup):
    browsing_pending_news = State()

# Inline Keyboards (simplified for brevity)
def get_main_menu_keyboard(user_lang: str):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📰 Мої новини", callback_data="my_news"), InlineKeyboardButton(text="➕ Додати джерело", callback_data="add_source"))
    builder.row(InlineKeyboardButton(text="🧠 AI-функції (вільні)", callback_data="ask_free_ai"), InlineKeyboardButton(text="❓ Запитати експерта", callback_data="ask_expert"))
    builder.row(InlineKeyboardButton(text="💰 Аналіз ціни", callback_data="price_analysis"), InlineKeyboardButton(text="🤖 AI-медіа", callback_data="ai_media_menu"))
    builder.row(InlineKeyboardButton(text="📈 Аналітика", callback_data="analytics_menu"), InlineKeyboardButton(text="✉️ Запросити друзів", callback_data="invite_friends"))
    return builder.as_markup()

def get_ai_news_functions_keyboard(news_id: int, user_lang: str, page: int = 0):
    builder = InlineKeyboardBuilder()
    if page == 0:
        builder.row(InlineKeyboardButton(text="📝 AI-резюме", callback_data=f"ai_summary_select_type_{news_id}"), InlineKeyboardButton(text="🌐 Перекласти", callback_data=f"translate_select_lang_{news_id}"))
        builder.row(InlineKeyboardButton(text="❓ Запитати AI", callback_data=f"ask_news_ai_{news_id}"), InlineKeyboardButton(text="🔊 Прослухати новину", callback_data=f"listen_news_{news_id}"))
        builder.row(InlineKeyboardButton(text="➡️ Далі", callback_data=f"ai_functions_page_1_{news_id}"))
    elif page == 1:
        builder.row(InlineKeyboardButton(text="🧑‍🤝‍🧑 Ключові сутності", callback_data=f"extract_entities_{news_id}"), InlineKeyboardButton(text="❓ Пояснити термін", callback_data=f"explain_term_{news_id}"))
        builder.row(InlineKeyboardButton(text="🏷️ Класифікувати за темами", callback_data=f"classify_topics_{news_id}"), InlineKeyboardButton(text="✅ Перевірити факт", callback_data=f"fact_check_news_{news_id}"))
        builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ai_functions_page_0_{news_id}"))
    builder.row(InlineKeyboardButton(text="❤️ В обране", callback_data=f"bookmark_news_add_{news_id}"), InlineKeyboardButton(text="🚩 Повідомити про фейк", callback_data=f"report_fake_news_{news_id}"))
    builder.row(InlineKeyboardButton(text="⬅️ До головного меню", callback_data="main_menu"))
    return builder.as_markup()

def get_translate_language_keyboard(news_id: int, user_lang: str):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🇬🇧 Англійська", callback_data=f"translate_to_en_{news_id}"), InlineKeyboardButton(text="🇺🇦 Українська", callback_data=f"translate_to_uk_{news_id}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад до AI функцій", callback_data=f"ai_news_functions_menu_{news_id}"))
    return builder.as_markup()

def get_source_type_keyboard(user_lang: str):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Веб-сайт", callback_data="source_type_web"), InlineKeyboardButton(text="RSS", callback_data="source_type_rss"))
    builder.row(InlineKeyboardButton(text="Telegram", callback_data="source_type_telegram"), InlineKeyboardButton(text="Соц. мережі", callback_data="source_type_social_media"))
    builder.row(InlineKeyboardButton(text="Скасувати", callback_data="cancel_action"))
    return builder.as_markup()

def get_smart_summary_type_keyboard(news_id: int, user_lang: str):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="1 речення", callback_data=f"smart_summary_1_sentence_{news_id}"))
    builder.row(InlineKeyboardButton(text="3 ключових факти", callback_data=f"smart_summary_3_facts_{news_id}"))
    builder.row(InlineKeyboardButton(text="Глибокий огляд", callback_data=f"smart_summary_deep_dive_{news_id}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад до AI функцій", callback_data=f"ai_news_functions_menu_{news_id}"))
    return builder.as_markup()

def get_expert_selection_keyboard(user_lang: str):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=get_message(user_lang, 'expert_portnikov_btn'), callback_data="ask_expert_portnikov"))
    builder.row(InlineKeyboardButton(text=get_message(user_lang, 'expert_libsits_btn'), callback_data="ask_expert_libsits"))
    builder.row(InlineKeyboardButton(text="⬅️ До головного меню", callback_data="main_menu"))
    return builder.as_markup()

def get_ai_media_menu_keyboard(user_lang: str):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=get_message(user_lang, 'generate_ai_news_btn'), callback_data="generate_ai_news"))
    builder.row(InlineKeyboardButton(text=get_message(user_lang, 'youtube_to_news_btn'), callback_data="youtube_to_news"))
    builder.row(InlineKeyboardButton(text=get_message(user_lang, 'generate_short_video_post_btn'), callback_data="generate_short_video_post"))
    builder.row(InlineKeyboardButton(text=get_message(user_lang, 'create_filtered_channel_btn'), callback_data="create_filtered_channel"))
    builder.row(InlineKeyboardButton(text=get_message(user_lang, 'create_ai_media_btn'), callback_data="create_ai_media"))
    builder.row(InlineKeyboardButton(text="⬅️ До головного меню", callback_data="main_menu"))
    return builder.as_markup()

def get_analytics_menu_keyboard(user_lang: str):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=get_message(user_lang, 'infographics_btn'), callback_data="infographics"))
    builder.row(InlineKeyboardButton(text=get_message(user_lang, 'trust_index_btn'), callback_data="trust_index"))
    builder.row(InlineKeyboardButton(text=get_message(user_lang, 'long_term_connections_btn'), callback_data="long_term_connections"))
    builder.row(InlineKeyboardButton(text=get_message(user_lang, 'ai_prediction_btn'), callback_data="ai_prediction"))
    builder.row(InlineKeyboardButton(text="⬅️ До головного меню", callback_data="main_menu"))
    return builder.as_markup()

# Handlers (simplified for brevity, focusing on new/modified logic)
@router.message(CommandStart())
async def command_start_handler(message: Message, state: FSMContext):
    await state.clear()
    user = await create_or_update_user(message.from_user)
    user_lang = user.language if user else 'uk'
    if message.text and len(message.text.split()) > 1:
        invite_code = message.text.split()[1]
        await handle_invite_code(user.id, invite_code)
    
    if user and (datetime.now(timezone.utc) - user.created_at).total_seconds() < 60:
        onboarding_messages = [
            get_message(user_lang, 'welcome', first_name=message.from_user.first_name),
            get_message(user_lang, 'onboarding_step_1'),
            get_message(user_lang, 'onboarding_step_2'),
            get_message(user_lang, 'onboarding_step_3')
        ]
        for msg_text in onboarding_messages:
            await message.answer(msg_text)
    else:
        if user:
            last_active_dt = user.last_active.replace(tzinfo=timezone.utc) if user.last_active else datetime.now(timezone.utc)
            time_since_last_active = datetime.now(timezone.utc) - last_active_dt
            if time_since_last_active > timedelta(days=2):
                unseen_count = await count_unseen_news(user.id)
                if unseen_count > 0:
                    await message.answer(get_message(user_lang, 'what_new_digest_header', count=unseen_count))
                    news_for_digest = await get_news_for_user(user.id, limit=3)
                    digest_text = ""
                    for i, news_item in enumerate(news_for_digest):
                        summary = await call_gemini_api(f"Зроби коротке резюме новини українською мовою: {news_item.content}")
                        digest_text += get_message(user_lang, 'daily_digest_entry', idx=i+1, title=news_item.title, summary=summary, source_url=news_item.source_url)
                        await mark_news_as_viewed(user.id, news_item.id)
                    if digest_text:
                        await message.answer(digest_text + get_message(user_lang, 'what_new_digest_footer'), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    await message.answer(get_message(user_lang, 'welcome', first_name=message.from_user.first_name), reply_markup=get_main_menu_keyboard(user_lang))

@router.message(Command("menu"))
async def command_menu_handler(message: Message, state: FSMContext):
    await state.clear()
    user = await create_or_update_user(message.from_user)
    user_lang = user.language if user else 'uk'
    await message.answer(get_message(user_lang, 'main_menu_prompt'), reply_markup=get_main_menu_keyboard(user_lang))

@router.message(Command("cancel"))
async def command_cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'
    await message.answer(get_message(user_lang, 'action_cancelled'), reply_markup=get_main_menu_keyboard(user_lang))

@router.callback_query(F.data == "main_menu")
async def callback_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'main_menu_prompt'), reply_markup=get_main_menu_keyboard(user_lang))
    await callback.answer()

@router.callback_query(F.data == "add_source")
async def callback_add_source(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'add_source_prompt'), reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel_action")).as_markup())
    await state.set_state(AddSourceStates.waiting_for_url)
    await callback.answer()

@router.message(AddSourceStates.waiting_for_url)
async def process_source_url(message: Message, state: FSMContext):
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
    user_data = await state.get_data()
    source_url = user_data.get("source_url")
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    if not source_url:
        await callback.message.edit_text(get_message(user_lang, 'source_url_not_found'), reply_markup=get_main_menu_keyboard(user_lang))
        await state.clear()
        return
    try:
        pool = await get_db_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                parsed_url = HttpUrl(source_url)
                source_name = parsed_url.host if parsed_url.host else 'Невідоме джерело'
                await cur.execute(
                    """INSERT INTO sources (user_id, source_name, source_url, source_type, added_at) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) ON CONFLICT (source_url) DO UPDATE SET source_name = EXCLUDED.source_name, source_type = EXCLUDED.source_type, status = 'active', last_parsed = NULL RETURNING id;""",
                    (user.id, source_name, source_url, source_type)
                )
                await conn.commit()
        await callback.message.edit_text(get_message(user_lang, 'source_added_success', source_url=source_url, source_type=source_type), reply_markup=get_main_menu_keyboard(user_lang))
    except Exception as e:
        logger.error(f"Error adding source '{source_url}': {e}")
        await callback.message.edit_text(get_message(user_lang, 'add_source_error'), reply_markup=get_main_menu_keyboard(user_lang))
    await state.clear()
    await callback.answer()

@router.callback_query(F.data == "my_news")
async def handle_my_news_command(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    news_items = await get_news_for_user(user.id, limit=1)
    if not news_items:
        await callback.message.edit_text(get_message(user_lang, 'no_new_news'), reply_markup=get_main_menu_keyboard(user_lang))
        await callback.answer()
        return
    all_news_ids = [n.id for n in await get_news_for_user(user.id, limit=100, offset=0)]
    current_state_data = await state.get_data()
    last_message_id = current_state_data.get('last_message_id')
    if last_message_id:
        try: await bot.delete_message(chat_id=callback.message.chat.id, message_id=last_message_id)
        except Exception as e: logger.warning(f"Failed to delete previous message {last_message_id}: {e}")
    await state.update_data(current_news_index=0, news_ids=all_news_ids)
    await state.set_state(NewsBrowse.Browse_news)
    await send_news_to_user(callback.message.chat.id, news_items[0].id, 0, len(all_news_ids), state)
    await callback.answer()

@router.callback_query(NewsBrowse.Browse_news, F.data == "next_news")
async def handle_next_news_command(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    user_data = await state.get_data()
    news_ids = user_data.get("news_ids", [])
    current_index = user_data.get("current_news_index", 0)
    last_message_id = user_data.get('last_message_id')
    if not news_ids or current_index + 1 >= len(news_ids):
        await callback.message.edit_text(get_message(user_lang, 'no_more_news'), reply_markup=get_main_menu_keyboard(user_lang))
        await callback.answer()
        return
    next_index = current_index + 1
    await state.update_data(current_news_index=next_index)
    if last_message_id:
        try: await bot.delete_message(chat_id=callback.message.chat.id, message_id=last_message_id)
        except Exception as e: logger.warning(f"Failed to delete previous message {last_message_id}: {e}")
    await send_news_to_user(callback.message.chat.id, news_ids[next_index], next_index, len(news_ids), state)
    await callback.answer()

@router.callback_query(NewsBrowse.Browse_news, F.data == "prev_news")
async def handle_prev_news_command(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    user_data = await state.get_data()
    news_ids = user_data.get("news_ids", [])
    current_index = user_data.get("current_news_index", 0)
    last_message_id = user_data.get('last_message_id')
    if not news_ids or current_index <= 0:
        await callback.message.edit_text(get_message(user_lang, 'first_news'), reply_markup=get_main_menu_keyboard(user_lang))
        await callback.answer()
        return
    prev_index = current_index - 1
    await state.update_data(current_news_index=prev_index)
    if last_message_id:
        try: await bot.delete_message(chat_id=callback.message.chat.id, message_id=last_message_id)
        except Exception as e: logger.warning(f"Failed to delete previous message {last_message_id}: {e}")
    await send_news_to_user(callback.message.chat.id, news_ids[prev_index], prev_index, len(news_ids), state)
    await callback.answer()

async def send_news_to_user(chat_id: int, news_id: int, current_index: int, total_news: int, state: FSMContext):
    news_item = await get_news_by_id(news_id)
    user = await get_user_by_telegram_id(chat_id)
    user_lang = user.language if user else 'uk'
    if not news_item:
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
    keyboard_builder.row(InlineKeyboardButton(text=get_message(user_lang, 'ai_functions_btn'), callback_data=f"ai_news_functions_menu_{news_item.id}"))
    nav_buttons = []
    if current_index > 0: nav_buttons.append(InlineKeyboardButton(text=get_message(user_lang, 'prev_btn'), callback_data="prev_news"))
    if current_index < total_news - 1: nav_buttons.append(InlineKeyboardButton(text=get_message(user_lang, 'next_btn'), callback_data="next_news"))
    if nav_buttons: keyboard_builder.row(*nav_buttons)
    keyboard_builder.row(InlineKeyboardButton(text=get_message(user_lang, 'main_menu_btn'), callback_data="main_menu"))
    msg = None
    if news_item.image_url:
        try: msg = await bot.send_photo(chat_id=chat_id, photo=str(news_item.image_url), caption=text, reply_markup=keyboard_builder.as_markup(), parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"Failed to send photo for news {news_id}: {e}. Sending without photo.")
            placeholder_image_url = "https://placehold.co/600x400/CCCCCC/000000?text=No+Image"
            msg = await bot.send_photo(chat_id=chat_id, photo=placeholder_image_url, caption=text + f"\n[Image]({news_item.image_url})", reply_markup=keyboard_builder.as_markup(), parse_mode=ParseMode.HTML, disable_web_page_preview=False)
    else:
        placeholder_image_url = "https://placehold.co/600x400/CCCCCC/000000?text=No+Image"
        msg = await bot.send_photo(chat_id=chat_id, photo=placeholder_image_url, caption=text, reply_markup=keyboard_builder.as_markup(), parse_mode=ParseMode.HTML)
    if msg: await state.update_data(last_message_id=msg.message_id)
    if user: await mark_news_as_viewed(user.id, news_item.id)

# AI Functions
async def call_gemini_api(prompt: str, user_telegram_id: Optional[int] = None, chat_history: Optional[List[Dict]] = None, image_data: Optional[str] = None) -> Optional[str]:
    if not GEMINI_API_KEY: return "AI is not available."
    if user_telegram_id:
        user = await get_user_by_telegram_id(user_telegram_id)
        if not user or not user.is_premium:
            current_time = time.time()
            last_request_time = AI_REQUEST_TIMESTAMPS.get(user_telegram_id, 0)
            if current_time - last_request_time < AI_REQUEST_LIMIT_SECONDS:
                seconds_left = int(AI_REQUEST_LIMIT_SECONDS - (current_time - last_request_time))
                return get_message(user.language if user else 'uk', 'ai_rate_limit_exceeded', seconds_left=seconds_left)
            AI_REQUEST_TIMESTAMPS[user_telegram_id] = current_time

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    contents = []
    if chat_history:
        for entry in chat_history: contents.append({"role": entry["role"], "parts": [{"text": entry["text"]}]})
    
    parts = [{"text": prompt}]
    if image_data:
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": image_data}}) # Assuming JPEG for simplicity
    contents.append({"role": "user", "parts": parts})

    payload = {"contents": contents, "generationConfig": {"temperature": 0.7, "topK": 40, "topP": 0.95, "maxOutputTokens": 1000}}
    try:
        async with ClientSession() as session:
            async with session.post(url, headers=headers, json=payload) as response:
                if response.status == 429: return "Too many AI requests. Please try again later."
                response.raise_for_status()
                data = await response.json()
                if data and data.get("candidates"): return data["candidates"][0]["content"]["parts"][0]["text"]
                return "Failed to get AI response."
    except Exception as e:
        logger.error(f"Error calling Gemini API: {e}", exc_info=True)
        return "An error occurred with AI."

async def check_premium_access(user_telegram_id: int) -> bool:
    user = await get_user_by_telegram_id(user_telegram_id)
    return user and user.is_premium

@router.callback_query(F.data.startswith("ai_summary_select_type_"))
async def handle_ai_summary_select_type(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[4])
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await state.update_data(news_id_for_summary=news_id)
    await callback.message.edit_text(get_message(user_lang, 'ai_smart_summary_prompt'), reply_markup=get_smart_summary_type_keyboard(news_id, user_lang))
    await state.set_state(AIAssistant.waiting_for_summary_type)
    await callback.answer()

@router.callback_query(AIAssistant.waiting_for_summary_type, F.data.startswith("smart_summary_"))
async def handle_smart_summary(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split('_')
    summary_type_key = parts[2]
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
    await callback.message.edit_text(get_message(user_lang, 'ai_smart_summary_generating', summary_type=summary_type_text))
    summary = await call_gemini_api(prompt, user_telegram_id=callback.from_user.id)
    await callback.message.edit_text(get_message(user_lang, 'ai_smart_summary_label', summary_type=summary_type_text, summary=summary), reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await state.clear()
    await callback.answer()

@router.callback_query(F.data.startswith("translate_select_lang_"))
async def handle_translate_select_language(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[3])
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await state.update_data(news_id_for_translate=news_id)
    await callback.message.edit_text(get_message(user_lang, 'select_translate_language'), reply_markup=get_translate_language_keyboard(news_id, user_lang))
    await state.set_state(AIAssistant.waiting_for_translate_language)
    await callback.answer()

@router.callback_query(AIAssistant.waiting_for_translate_language, F.data.startswith("translate_to_"))
async def handle_translate_to_language(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split('_')
    lang_code = parts[2]
    news_id = int(parts[3])
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    language_names = {"en": "English", "pl": "Polish", "de": "German", "es": "Spanish", "fr": "French", "uk": "Ukrainian"}
    language_name = language_names.get(lang_code, "selected language")
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
        await bot.send_voice(chat_id=callback.message.chat.id, voice=BufferedInputFile(audio_buffer.getvalue(), filename=f"news_{news_id}.mp3"), caption=get_message(user_lang, 'audio_news_caption', title=news_item.title), reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
        await callback.message.delete()
    except Exception as e:
        logger.error(f"Error generating or sending audio for news {news_id}: {e}")
        await callback.message.edit_text(get_message(user_lang, 'audio_error'), reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await callback.answer()

@router.callback_query(F.data.startswith("ask_news_ai_"))
async def handle_ask_news_ai(callback: CallbackQuery, state: FSMContext):
    news_id = int(callback.data.split('_')[3])
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await state.update_data(waiting_for_news_id_for_ai=news_id)
    await callback.message.edit_text(get_message(user_lang, 'ask_news_ai_prompt'), reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel_action")).as_markup())
    await state.set_state(AIAssistant.waiting_for_question)
    await callback.answer()

@router.message(AIAssistant.waiting_for_question)
async def process_ai_question(message: Message, state: FSMContext):
    user_question = message.text
    user_data = await state.get_data()
    news_id = user_data.get("waiting_for_news_id_for_ai")
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'
    if not news_id:
        await message.answer(get_message(user_lang, 'ai_news_not_found'), reply_markup=get_main_menu_keyboard(user_lang))
        await state.clear()
        return
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await message.answer(get_message(user_lang, 'news_not_found'), reply_markup=get_main_menu_keyboard(user_lang))
        await state.clear()
        return
    await message.answer(get_message(user_lang, 'processing_question'))
    chat_history = user_data.get('ai_chat_history', [])
    chat_history.append({"role": "user", "text": user_question})
    prompt = f"Новина: {news_item.content}\n\nЗапитання: {user_question}\n\nВідповідь:"
    ai_response = await call_gemini_api(prompt, user_telegram_id=message.from_user.id, chat_history=chat_history)
    chat_history.append({"role": "model", "text": ai_response})
    await state.update_data(ai_chat_history=chat_history)
    await message.answer(get_message(user_lang, 'ai_response_label') + f"\n{ai_response}", reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await state.clear()

@router.callback_query(F.data == "ask_free_ai")
async def handle_ask_free_ai(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'ask_free_ai_prompt'), reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel_action")).as_markup())
    await state.set_state(AIAssistant.waiting_for_free_question)
    await state.update_data(ai_chat_history=[])
    await callback.answer()

@router.message(AIAssistant.waiting_for_free_question)
async def process_free_ai_question(message: Message, state: FSMContext):
    user_question = message.text
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'
    await message.answer(get_message(user_lang, 'processing_question'))
    user_data = await state.get_data()
    chat_history = user_data.get('ai_chat_history', [])
    chat_history.append({"role": "user", "text": user_question})
    ai_response = await call_gemini_api(f"Відповідай як новинний аналітик на запитання: {user_question}", user_telegram_id=message.from_user.id, chat_history=chat_history)
    chat_history.append({"role": "model", "text": ai_response})
    await state.update_data(ai_chat_history=chat_history)
    await message.answer(get_message(user_lang, 'ai_response_label') + f"\n{ai_response}", reply_markup=get_main_menu_keyboard(user_lang))

@router.callback_query(F.data.startswith("extract_entities_"))
async def handle_extract_entities(callback: CallbackQuery):
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
    term = message.text
    user_data = await state.get_data()
    news_id = user_data.get("waiting_for_news_id_for_ai")
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'
    if not news_id:
        await message.answer(get_message(user_lang, 'ai_news_not_found'), reply_markup=get_main_menu_keyboard(user_lang))
        await state.clear()
        return
    news_item = await get_news_by_id(news_id)
    if not news_item:
        await message.answer(get_message(user_lang, 'news_not_found'), reply_markup=get_main_menu_keyboard(user_lang))
        await state.clear()
        return
    await message.answer(get_message(user_lang, 'explaining_term'))
    explanation = await call_gemini_api(f"Поясни термін '{term}' у контексті наступної новини українською мовою: {news_item.content}", user_telegram_id=message.from_user.id)
    await message.answer(get_message(user_lang, 'term_explanation_label', term=term) + f"\n{explanation}", reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    await state.clear()

@router.callback_query(F.data.startswith("classify_topics_"))
async def handle_classify_topics(callback: CallbackQuery):
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

@router.callback_query(F.data.startswith("bookmark_news_"))
async def handle_bookmark_news(callback: CallbackQuery, state: FSMContext):
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
                    await cur.execute("""INSERT INTO bookmarks (user_id, news_id, bookmarked_at) VALUES (%s, %s, CURRENT_TIMESTAMP) ON CONFLICT (user_id, news_id) DO NOTHING;""", (user.id, news_id))
                    if cur.rowcount > 0: await callback.answer(get_message(user_lang, 'bookmark_added'), show_alert=True)
                    else: await callback.answer(get_message(user_lang, 'bookmark_already_exists'), show_alert=True)
                except Exception as e: await callback.answer(get_message(user_lang, 'bookmark_add_error'), show_alert=True)
            elif action == 'remove':
                try:
                    await cur.execute("DELETE FROM bookmarks WHERE user_id = %s AND news_id = %s;", (user.id, news_id))
                    if cur.rowcount > 0: await callback.answer(get_message(user_lang, 'bookmark_removed'), show_alert=True)
                    else: await callback.answer(get_message(user_lang, 'bookmark_not_found'), show_alert=True)
                except Exception as e: await callback.answer(get_message(user_lang, 'bookmark_remove_error'), show_alert=True)
            await conn.commit()
    current_state_data = await state.get_data()
    last_message_id = current_state_data.get('last_message_id')
    if last_message_id: await callback.message.edit_text(get_message(user_lang, 'ai_functions_prompt'), reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))
    else: await callback.message.edit_text(get_message(user_lang, 'action_done'), reply_markup=get_main_menu_keyboard(user_lang))
    await callback.answer()

@router.callback_query(F.data.startswith("report_fake_news_"))
async def handle_report_fake_news(callback: CallbackQuery):
    news_id = int(callback.data.split('_')[3])
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    if not user:
        await callback.answer(get_message(user_lang, 'user_not_identified'), show_alert=True)
        return
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM reports WHERE user_id = %s AND target_type = 'news' AND target_id = %s;", (user.id, news_id))
            if await cur.fetchone():
                await callback.answer(get_message(user_lang, 'report_already_sent'), show_alert=True)
                return
            await cur.execute("""INSERT INTO reports (user_id, target_type, target_id, reason, created_at, status) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, 'pending');""", (user.id, 'news', news_id, 'Fake news report'))
            await conn.commit()
    await callback.answer(get_message(user_lang, 'report_sent_success'), show_alert=True)
    await callback.message.edit_text(get_message(user_lang, 'report_action_done'), reply_markup=get_ai_news_functions_keyboard(news_id, user_lang))

async def send_news_to_channel(news_item: News):
    if not NEWS_CHANNEL_LINK: return
    channel_identifier = NEWS_CHANNEL_LINK
    if 't.me/' in NEWS_CHANNEL_LINK: channel_identifier = NEWS_CHANNEL_LINK.split('/')[-1]
    if channel_identifier.startswith('-100') and channel_identifier[1:].replace('-', '').isdigit(): pass
    elif channel_identifier.startswith('+'): return logger.error(get_message('uk', 'news_channel_link_error', link=NEWS_CHANNEL_LINK))
    elif not channel_identifier.startswith('@'): channel_identifier = '@' + channel_identifier
    
    display_content = news_item.content
    if len(display_content) > 250:
        # Use AI to summarize if too long
        display_content = await call_gemini_api(f"Скороти цей текст до 250 символів, зберігаючи суть: {display_content}")
        if len(display_content) > 247: # Ensure it's still short enough after AI processing
            display_content = display_content[:247] + "..."

    text = (
        f"<b>Нова новина:</b> {news_item.title}\n\n"
        f"{display_content}\n\n"
        f"🔗 <a href='{news_item.source_url}'>Читати повністю</a>\n"
        f"Опубліковано: {news_item.published_at.strftime('%d.%m.%Y %H:%M')}"
    )
    try:
        if news_item.image_url:
            await bot.send_photo(chat_id=channel_identifier, photo=str(news_item.image_url), caption=text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        else:
            await bot.send_message(chat_id=channel_identifier, text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
        logger.info(get_message('uk', 'news_published_success', title=news_item.title, identifier=channel_identifier))
        await mark_news_as_published_to_channel(news_item.id) # Mark as published
    except Exception as e:
        logger.error(get_message('uk', 'news_publish_error', title=news_item.title, identifier=channel_identifier, error=e), exc_info=True)

async def fetch_and_post_news_task():
    logger.info("Running fetch_and_post_news_task.")
    pool = await get_db_pool()
    
    # 1. Parse from active sources
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM sources WHERE status = 'active'")
            sources = await cur.fetchall()
    for source in sources:
        if not all([source.get('source_type'), source.get('source_url'), source.get('source_name')]): continue
        news_data = None
        try:
            if source['source_type'] == 'rss': news_data = await rss_parser.parse_rss_feed(source['source_url'])
            elif source['source_type'] == 'web': news_data = await web_parser.parse_website(source['source_url'])
            elif source['source_type'] == 'telegram': news_data = await telegram_parser.get_telegram_channel_posts(source['source_url'])
            elif source['source_type'] == 'social_media': news_data = await social_media_parser.get_social_media_posts(source['source_url'], "general")
            if news_data:
                news_data.update({'source_id': source['id'], 'source_name': source['source_name'], 'source_type': source['source_type'], 'user_id_for_source': None})
                added_news_item = await add_news_to_db(news_data)
                if added_news_item: await update_source_stats_publication_count(source['id'])
                else: logger.info(get_message('uk', 'news_not_added', name=source['source_name']))
                async with pool.connection() as conn_update:
                    async with conn_update.cursor() as cur_update:
                        await cur_update.execute("UPDATE sources SET last_parsed = CURRENT_TIMESTAMP WHERE id = %s", (source['id'],))
                        await conn_update.commit()
            else: logger.warning(get_message('uk', 'source_parsing_warning', name=source['source_name'], url=source['source_url']))
        except Exception as e: logger.error(get_message('uk', 'source_parsing_error', name=source.get('source_name', 'N/A'), url=source.get('source_url', 'N/A'), error=e), exc_info=True)
    
    # 2. Post approved news from DB to channel (one by one, oldest first)
    news_to_post = await get_news_to_publish(limit=1) # Fetch one news at a time
    if news_to_post:
        news_item = news_to_post[0]
        await send_news_to_channel(news_item)
    else:
        logger.info("No approved news to post to channel.")

async def delete_expired_news_task():
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM news WHERE expires_at IS NOT NULL AND expires_at < CURRENT_TIMESTAMP;")
            deleted_count = cur.rowcount
            await conn.commit()
            if deleted_count > 0: logger.info(get_message('uk', 'deleted_expired_news', count=deleted_count))
            else: logger.info(get_message('uk', 'no_expired_news'))

async def send_daily_digest():
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id, telegram_id, language FROM users WHERE auto_notifications = TRUE AND digest_frequency = 'daily';")
            users_for_digest = await cur.fetchall()
    if not users_for_digest: return
    for user_data in users_for_digest:
        user_db_id = user_data['id']
        user_telegram_id = user_data['telegram_id']
        user_lang = user_data['language']
        news_items = await get_news_for_user(user_db_id, limit=5)
        if not news_items: continue
        digest_text = get_message(user_lang, 'daily_digest_header') + "\n\n"
        for i, news_item in enumerate(news_items):
            summary = await call_gemini_api(f"Зроби коротке резюме новини українською мовою: {news_item.content}", user_telegram_id=user_telegram_id)
            digest_text += get_message(user_lang, 'daily_digest_entry', idx=i+1, title=news_item.title, summary=summary, source_url=news_item.source_url)
            await mark_news_as_viewed(user_db_id, news_item.id)
        try:
            await bot.send_message(chat_id=user_telegram_id, text=digest_text, reply_markup=get_main_menu_keyboard(user_lang), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e: logger.error(get_message('uk', 'daily_digest_send_error', user_id=user_telegram_id, error=e), exc_info=True)

async def generate_invite_code() -> str: return ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=8))

async def create_invite(inviter_user_db_id: int) -> Optional[str]:
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            invite_code = await generate_invite_code()
            try:
                await cur.execute("""INSERT INTO invitations (inviter_user_id, invite_code, created_at, status) VALUES (%s, %s, CURRENT_TIMESTAMP, 'pending') RETURNING invite_code;""", (inviter_user_db_id, invite_code))
                await conn.commit()
                return invite_code
            except Exception as e:
                logger.error(f"Error creating invite for user {inviter_user_db_id}: {e}")
                return None

async def handle_invite_code(new_user_db_id: int, invite_code: str):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""SELECT id, inviter_user_id FROM invitations WHERE invite_code = %s AND status = 'pending' AND used_at IS NULL;""", (invite_code,))
            invite_record = await cur.fetchone()
            if invite_record:
                invite_id, inviter_user_db_id = invite_record
                await cur.execute("""UPDATE invitations SET used_at = CURRENT_TIMESTAMP, status = 'accepted', invitee_telegram_id = %s WHERE id = %s;""", (new_user_db_id, invite_id))
                await cur.execute("UPDATE users SET inviter_id = %s WHERE id = %s;", (inviter_user_db_id, new_user_db_id))
                await conn.commit()

@router.callback_query(F.data == "invite_friends")
async def command_invite_handler(callback: CallbackQuery):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    if not user: user = await create_or_update_user(callback.from_user)
    invite_code = await create_invite(user.id)
    if invite_code:
        invite_link = f"https://t.me/{bot.me.username}?start={invite_code}"
        await callback.message.edit_text(get_message(user_lang, 'your_invite_code', invite_code=invite_code, invite_link=hlink(get_message(user_lang, 'invite_link_label'), invite_link)), parse_mode=ParseMode.HTML, disable_web_page_preview=False, reply_markup=get_main_menu_keyboard(user_lang))
    else: await callback.message.edit_text(get_message(user_lang, 'invite_error'), reply_markup=get_main_menu_keyboard(user_lang))
    await callback.answer()

async def update_source_stats_publication_count(source_id: int):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""INSERT INTO source_stats (source_id, publication_count, last_updated) VALUES (%s, 1, CURRENT_TIMESTAMP) ON CONFLICT (source_id) DO UPDATE SET publication_count = source_stats.publication_count + 1, last_updated = CURRENT_TIMESTAMP;""", (source_id,))
            await conn.commit()

@router.callback_query(F.data == "ask_expert")
async def handle_ask_expert(callback: CallbackQuery):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'ask_expert_prompt'), reply_markup=get_expert_selection_keyboard(user_lang))
    await callback.answer()

@router.callback_query(F.data.startswith("ask_expert_"))
async def handle_expert_selection(callback: CallbackQuery, state: FSMContext):
    expert_type = callback.data.replace("ask_expert_", "")
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    expert_name = "Віталій Портников" if expert_type == "portnikov" else "Ігор Лібсіц"
    await state.update_data(expert_type=expert_type)
    await callback.message.edit_text(get_message(user_lang, 'ask_expert_question_prompt', expert_name=expert_name), reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel_action")).as_markup())
    await state.set_state(AIAssistant.waiting_for_expert_question)
    await callback.answer()

@router.message(AIAssistant.waiting_for_expert_question)
async def process_expert_question(message: Message, state: FSMContext):
    user_question = message.text
    user_data = await state.get_data()
    expert_type = user_data.get("expert_type")
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'
    expert_name = "Віталій Портников" if expert_type == "portnikov" else "Ігор Лібсіц"
    
    prompt = ""
    if expert_type == "portnikov":
        prompt = f"Відповідай на запитання як відомий український журналіст Віталій Портников, аналізуючи політичні та соціальні події: {user_question}"
    elif expert_type == "libsits":
        prompt = f"Відповідай на запитання як відомий український економіст Ігор Лібсіц, аналізуючи економічні тенденції та їх наслідки: {user_question}"
    
    await message.answer(get_message(user_lang, 'processing_question'))
    ai_response = await call_gemini_api(prompt, user_telegram_id=message.from_user.id)
    await message.answer(get_message(user_lang, 'expert_response_label', expert_name=expert_name) + f"\n{ai_response}", reply_markup=get_main_menu_keyboard(user_lang))
    await state.clear()

@router.callback_query(F.data == "price_analysis")
async def handle_price_analysis(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'price_analysis_prompt'), reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel_action")).as_markup())
    await state.set_state(AIAssistant.waiting_for_price_analysis_input)
    await callback.answer()

@router.message(AIAssistant.waiting_for_price_analysis_input)
async def process_price_analysis_input(message: Message, state: FSMContext):
    user_input = message.text
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'
    
    image_url = None
    if message.photo:
        # Get the largest photo
        file_id = message.photo[-1].file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path
        image_bytes = await bot.download_file(file_path)
        image_data_base64 = base64.b64encode(image_bytes.read()).decode('utf-8')
        # Here we would use image_data_base64 in call_gemini_api
        # For this example, we'll just acknowledge its presence
        image_url = "Image provided." # Placeholder

    await message.answer(get_message(user_lang, 'price_analysis_generating'))
    
    prompt = f"Проаналізуй опис товару '{user_input}' та, якщо є, зображення ({image_url if image_url else 'відсутнє'}) і розрахуй приблизну ціну в UAH. Вкажи фактори, що впливають на ціну."
    price_analysis_result = await call_gemini_api(prompt, user_telegram_id=message.from_user.id, image_data=image_data_base64 if image_url else None) # Pass image data
    await message.answer(get_message(user_lang, 'price_analysis_result', result=price_analysis_result), reply_markup=get_main_menu_keyboard(user_lang))
    await state.clear()

@router.callback_query(F.data == "ai_media_menu")
async def handle_ai_media_menu(callback: CallbackQuery):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'ai_media_menu_prompt'), reply_markup=get_ai_media_menu_keyboard(user_lang))
    await callback.answer()

@router.callback_query(F.data == "generate_ai_news")
async def handle_generate_ai_news(callback: CallbackQuery):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'ai_news_generating'))
    
    # Simulate trend analysis and news generation
    prompt = "Згенеруй коротку, цікаву новину українською мовою на основі поточних глобальних трендів (наприклад, AI, клімат, економіка)."
    ai_generated_content = await call_gemini_api(prompt, user_telegram_id=callback.from_user.id)
    
    ai_news_data = {
        "title": ai_generated_content.split('.')[0] if ai_generated_content else "AI-генерована новина",
        "content": ai_generated_content,
        "source_url": "https://ai.news.bot/generated/" + str(int(time.time())),
        "image_url": "https://placehold.co/600x400/87CEEB/FFFFFF?text=AI+News",
        "published_at": datetime.now(timezone.utc),
        "source_type": "ai_generated",
        "user_id_for_source": None # System generated
    }
    added_news = await add_news_to_db(ai_news_data)
    if added_news:
        await callback.message.edit_text(get_message(user_lang, 'ai_news_generated_success', title=added_news.title), reply_markup=get_ai_media_menu_keyboard(user_lang))
    else:
        await callback.message.edit_text(get_message(user_lang, 'add_source_error'), reply_markup=get_ai_media_menu_keyboard(user_lang))
    await callback.answer()

@router.callback_query(F.data == "youtube_to_news")
async def handle_youtube_to_news(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'youtube_url_prompt'), reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel_action")).as_markup())
    await state.set_state(AIAssistant.waiting_for_youtube_url)
    await callback.answer()

@router.message(AIAssistant.waiting_for_youtube_url)
async def process_youtube_url(message: Message, state: FSMContext):
    youtube_url = message.text
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'
    if not (youtube_url.startswith("http://") or youtube_url.startswith("https://")) or "youtube.com" not in youtube_url:
        await message.answer(get_message(user_lang, 'invalid_url'))
        return
    
    await message.answer(get_message(user_lang, 'youtube_processing'))
    
    # Simulate YouTube transcript API call
    mock_transcript = "Це імітований транскрипт YouTube відео. Відео розповідає про останні новини в галузі штучного інтелекту та їх вплив на суспільство. Обговорюються етичні аспекти та майбутні перспективи розвитку AI."
    
    prompt = f"На основі цього транскрипту YouTube відео, згенеруй новину українською мовою, включаючи заголовок, короткий зміст, переклад (якщо потрібно, але зараз українською) та аналітику:\n\n{mock_transcript}"
    ai_news_content = await call_gemini_api(prompt, user_telegram_id=message.from_user.id)
    
    # Extract title from AI response or create a default one
    title = ai_news_content.split('\n')[0] if ai_news_content and '\n' in ai_news_content else "YouTube Відео Новина"
    
    youtube_news_data = {
        "title": title,
        "content": ai_news_content,
        "source_url": youtube_url,
        "image_url": f"https://img.youtube.com/vi/{youtube_url.split('v=')[-1].split('&')[0]}/0.jpg", # Basic YouTube thumbnail
        "published_at": datetime.now(timezone.utc),
        "source_type": "youtube",
        "user_id_for_source": None
    }
    added_news = await add_news_to_db(youtube_news_data)
    if added_news:
        await message.answer(get_message(user_lang, 'youtube_summary_label', summary=ai_news_content), reply_markup=get_ai_media_menu_keyboard(user_lang))
    else:
        await message.answer(get_message(user_lang, 'add_source_error'), reply_markup=get_ai_media_menu_keyboard(user_lang))
    await state.clear()

@router.callback_query(F.data == "generate_short_video_post")
async def handle_generate_short_video_post(callback: CallbackQuery):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'short_video_generating'))
    
    # Simulate generation of video post (AI voice, slides, DALL-E image)
    # This is a mock, as actual video generation is complex.
    mock_video_description = "Створено короткий відеоролик з AI-голосом та слайдами про останні технологічні новини. Включає AI-генероване зображення."
    
    await callback.message.edit_text(get_message(user_lang, 'short_video_generated') + f"\n\n{mock_video_description}", reply_markup=get_ai_media_menu_keyboard(user_lang))
    await callback.answer()

@router.callback_query(F.data == "create_filtered_channel")
async def handle_create_filtered_channel(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'filtered_channel_prompt'), reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel_action")).as_markup())
    await state.set_state(AIAssistant.waiting_for_filtered_channel_details)
    await callback.answer()

@router.message(AIAssistant.waiting_for_filtered_channel_details)
async def process_filtered_channel_details(message: Message, state: FSMContext):
    details = message.text.split(',')
    if len(details) < 2:
        await message.answer(get_message('uk', 'filtered_channel_prompt'))
        return
    channel_name = details[0].strip()
    topics = [t.strip() for t in details[1:]]
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'
    
    await message.answer(get_message(user_lang, 'filtered_channel_creating', channel_name=channel_name, topics=', '.join(topics)))
    
    # Simulate channel creation and news filtering setup
    mock_channel_link = f"https://t.me/{channel_name.replace(' ', '_').lower()}_ai_news"
    
    await message.answer(get_message(user_lang, 'filtered_channel_created', channel_name=channel_name) + f"\nПосилання на ваш канал: {mock_channel_link}", reply_markup=get_ai_media_menu_keyboard(user_lang))
    await state.clear()

@router.callback_query(F.data == "create_ai_media")
async def handle_create_ai_media(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'ai_media_creating'), reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text=get_message(user_lang, 'cancel_btn'), callback_data="cancel_action")).as_markup())
    await state.set_state(AIAssistant.waiting_for_ai_media_name)
    await callback.answer()

@router.message(AIAssistant.waiting_for_ai_media_name)
async def process_ai_media_name(message: Message, state: FSMContext):
    media_name = message.text.strip()
    user = await get_user_by_telegram_id(message.from_user.id)
    user_lang = user.language if user else 'uk'
    
    await message.answer(get_message(user_lang, 'ai_media_creating'))
    
    # Simulate creation of AI media (AI texts, publication channels, analytics, digests)
    mock_media_description = f"Ваше AI-медіа '{media_name}' тепер генерує AI-тексти, публікує їх у власному каналі, надає аналітику та щоденні дайджести."
    
    await message.answer(get_message(user_lang, 'ai_media_created', media_name=media_name) + f"\n\n{mock_media_description}", reply_markup=get_ai_media_menu_keyboard(user_lang))
    await state.clear()


@router.callback_query(F.data == "analytics_menu")
async def handle_analytics_menu(callback: CallbackQuery):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'analytics_menu_prompt'), reply_markup=get_analytics_menu_keyboard(user_lang))
    await callback.answer()

@router.callback_query(F.data == "infographics")
async def handle_infographics(callback: CallbackQuery):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'infographics_generating'))
    
    # Simulate data fetching and AI generation of infographics description
    mock_data_summary = "За останній місяць було 150 новин про економіку, 80 про війну, 30 про AI. Економічні новини зростають на 10%, військові падають на 5%."
    prompt = f"На основі цих даних, опиши інфографіку про новини за темами та джерелами: {mock_data_summary}"
    infographics_description = await call_gemini_api(prompt, user_telegram_id=callback.from_user.id)
    
    await callback.message.edit_text(get_message(user_lang, 'infographics_result', result=infographics_description), reply_markup=get_analytics_menu_keyboard(user_lang))
    await callback.answer()

@router.callback_query(F.data == "trust_index")
async def handle_trust_index(callback: CallbackQuery):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'trust_index_calculating'))
    
    # Simulate trust index calculation
    mock_trust_data = "Джерело 'BBC News' має 95% довіри (0 репортів, високе схвалення). Джерело 'FakeNewsDaily' має 20% довіри (багато репортів, перетин з фейками)."
    prompt = f"На основі цих даних, опиши 'Індекс довіри до джерел': {mock_trust_data}"
    trust_index_result = await call_gemini_api(prompt, user_telegram_id=callback.from_user.id)
    
    await callback.message.edit_text(get_message(user_lang, 'trust_index_result', result=trust_index_result), reply_markup=get_analytics_menu_keyboard(user_lang))
    await callback.answer()

@router.callback_query(F.data == "long_term_connections")
async def handle_long_term_connections(callback: CallbackQuery):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'long_term_connections_generating'))
    
    # Simulate long-term connection analysis
    mock_news_context = "Поточна новина про підвищення цін на пальне. Минулого місяця була новина про скорочення видобутку нафти."
    prompt = f"Знайди довгострокові зв'язки між подіями. Наприклад, 'В цій новині згадується рішення, яке продовжує історію з минулого місяця'. Поточна новина: {mock_news_context}"
    connections_result = await call_gemini_api(prompt, user_telegram_id=callback.from_user.id)
    
    await callback.message.edit_text(get_message(user_lang, 'long_term_connections_result', result=connections_result), reply_markup=get_analytics_menu_keyboard(user_lang))
    await callback.answer()

@router.callback_query(F.data == "ai_prediction")
async def handle_ai_prediction(callback: CallbackQuery):
    user = await get_user_by_telegram_id(callback.from_user.id)
    user_lang = user.language if user else 'uk'
    await callback.message.edit_text(get_message(user_lang, 'ai_prediction_generating'))
    
    # Simulate AI prediction
    mock_news_for_prediction = "Новина: Уряд розглядає новий податок на імпорт. Прогноз: Можливе підвищення цін на імпортні товари на 5-10% протягом наступних 3 місяців."
    prompt = f"Зроби прогноз: 'Що буде далі' на основі цієї новини: {mock_news_for_prediction}"
    prediction_result = await call_gemini_api(prompt, user_telegram_id=callback.from_user.id)
    
    await callback.message.edit_text(get_message(user_lang, 'ai_prediction_result', result=prediction_result), reply_markup=get_analytics_menu_keyboard(user_lang))
    await callback.answer()

# Scheduler
async def scheduler():
    fetch_schedule_expression = '*/5 * * * *'
    delete_schedule_expression = '0 */5 * * *' 
    daily_digest_schedule_expression = '0 9 * * *'
    ai_news_generation_schedule = '0 */6 * * *' # Generate AI news every 6 hours
    
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

        ai_news_itr = croniter(ai_news_generation_schedule, now)
        next_ai_news_run = ai_news_itr.get_next(datetime)
        ai_news_delay_seconds = (next_ai_news_run - now).total_seconds()

        min_delay = min(fetch_delay_seconds, delete_delay_seconds, daily_digest_delay_seconds, ai_news_delay_seconds)
        
        logger.info(f"Next fetch_and_post_news_task in {int(fetch_delay_seconds)}s.")
        logger.info(f"Next delete_expired_news_task in {int(delete_delay_seconds)}s.")
        logger.info(f"Next send_daily_digest in {int(daily_digest_delay_seconds)}s.")
        logger.info(f"Next generate_ai_news_task in {int(ai_news_delay_seconds)}s.")
        logger.info(f"Bot sleeping for {int(min_delay)}s.")

        await asyncio.sleep(min_delay)

        current_utc_time = datetime.now(timezone.utc)
        if (current_utc_time - next_fetch_run).total_seconds() >= -1: await fetch_and_post_news_task()
        if (current_utc_time - next_delete_run).total_seconds() >= -1: await delete_expired_news_task()
        if (current_utc_time - next_daily_digest_run).total_seconds() >= -1: await send_daily_digest()
        if (current_utc_time - next_ai_news_run).total_seconds() >= -1: await generate_ai_news_task()

async def generate_ai_news_task():
    logger.info("Running generate_ai_news_task.")
    # Simulate trend analysis and news generation
    prompt = "Згенеруй коротку, цікаву новину українською мовою на основі поточних глобальних трендів (наприклад, AI, клімат, економіка). Це має бути готова новина для публікації."
    ai_generated_content = await call_gemini_api(prompt) # No user_telegram_id for system task

    if ai_generated_content:
        # Attempt to extract a title from the first sentence
        title = ai_generated_content.split('.')[0] if ai_generated_content and '.' in ai_generated_content else "AI-generated News"
        if len(title) > 255: # Ensure title fits in DB
            title = title[:252] + "..."

        ai_news_data = {
            "title": title,
            "content": ai_generated_content,
            "source_url": "https://ai.news.bot/generated/" + str(int(time.time())),
            "image_url": "https://placehold.co/600x400/87CEEB/FFFFFF?text=AI+News",
            "published_at": datetime.now(timezone.utc),
            "source_type": "ai_generated",
            "user_id_for_source": None # System generated
        }
        added_news = await add_news_to_db(ai_news_data)
        if added_news:
            logger.info(f"AI-generated news '{added_news.title}' added to DB.")
        else:
            logger.warning("AI-generated news was not added to DB (possibly already exists).")
    else:
        logger.warning("Failed to generate AI news content.")

# FastAPI endpoints
@app.on_event("startup")
async def on_startup():
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        render_external_url = os.getenv("RENDER_EXTERNAL_HOSTNAME")
        if render_external_url: webhook_url = f"https://{render_external_url}/telegram_webhook"
        else: logger.error("WEBHOOK_URL not defined. Webhook will not be set.")
    try:
        logger.info(f"Attempting to set webhook to: {webhook_url}")
        await bot.set_webhook(url=webhook_url)
        logger.info(f"Webhook successfully set to {webhook_url}")
    except Exception as e: logger.error(f"Error setting webhook: {e}")
    asyncio.create_task(scheduler())
    logger.info("FastAPI app started.")

@app.on_event("shutdown")
async def on_shutdown():
    if db_pool: await db_pool.close()
    await bot.session.close()
    logger.info("FastAPI app shut down.")

@app.post("/telegram_webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
        aiogram_update = types.Update.model_validate(update, context={"bot": bot})
        await dp.feed_update(bot, aiogram_update)
    except Exception as e: logger.error(f"Error processing Telegram webhook: {e}", exc_info=True)
    return {"ok": True}

@app.post("/")
async def root_webhook(request: Request):
    try:
        update = await request.json()
        aiogram_update = types.Update.model_validate(update, context={"bot": bot})
        await dp.feed_update(bot, aiogram_update)
    except Exception as e: logger.error(f"Error processing Telegram webhook at root path: {e}", exc_info=True)
    return {"ok": True}

@app.get("/healthz", response_class=PlainTextResponse)
async def healthz(): return "OK"

@app.get("/ping", response_class=PlainTextResponse)
async def ping(): return "pong"

# Admin endpoints (simplified, assuming they work as before)
@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open("index.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/dashboard", response_class=HTMLResponse)
async def read_dashboard(api_key: str = Depends(get_api_key)):
    with open("dashboard.html", "r", encoding="utf-8") as f: return f.read()

@app.get("/users", response_class=HTMLResponse)
async def read_users(api_key: str = Depends(get_api_key), limit: int = 10, offset: int = 0):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id, telegram_id, username, first_name, last_name, created_at, is_admin, last_active, language, auto_notifications, digest_frequency, safe_mode, current_feed_id, is_premium, premium_expires_at, level, badges, inviter_id, email, view_mode FROM users ORDER BY created_at DESC LIMIT %s OFFSET %s;", (limit, offset))
            return {"users": await cur.fetchall()}

@app.get("/reports", response_class=HTMLResponse)
async def read_reports(api_key: str = Depends(get_api_key), limit: int = 10, offset: int = 0):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            query = "SELECT r.*, u.username, u.first_name, n.title as news_title, n.source_url as news_source_url FROM reports r LEFT JOIN users u ON r.user_id = u.id LEFT JOIN news n ON r.target_id = n.id WHERE r.target_type = 'news' ORDER BY r.created_at DESC LIMIT %s OFFSET %s;"
            await cur.execute(query, (limit, offset))
            return await cur.fetchall()

@app.get("/api/admin/sources")
async def get_admin_sources(api_key: str = Depends(get_api_key), limit: int = 100, offset: int = 0):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT id, user_id, source_name, source_url, source_type, status, added_at, last_parsed, parse_frequency FROM sources ORDER BY added_at DESC LIMIT %s OFFSET %s;", (limit, offset))
            return [Source(**s) for s in await cur.fetchall()]

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
            return {"total_users": total_users, "total_news": total_news, "active_users_count": active_users_count}

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
            return await cur.fetchall()

@app.get("/api/admin/news/counts_by_status")
async def get_news_counts_by_status(api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT moderation_status, COUNT(*) FROM news GROUP BY moderation_status;")
            return {row['moderation_status']: row['count'] for row in await cur.fetchall()}

@app.put("/api/admin/news/{news_id}")
async def update_admin_news(news_id: int, news: News, api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            params = [news.source_id, news.title, news.content, str(news.source_url), str(news.image_url) if news.image_url else None, news.published_at, news.ai_summary, json.dumps(news.ai_classified_topics) if news.ai_classified_topics else None, news.moderation_status, news.expires_at, news_id]
            await cur.execute("""UPDATE news SET source_id = %s, title = %s, content = %s, source_url = %s, image_url = %s, published_at = %s, ai_summary = %s, ai_classified_topics = %s, moderation_status = %s, expires_at = %s WHERE id = %s RETURNING *;""", tuple(params))
            updated_rec = await cur.fetchone()
            if not updated_rec: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="News not found.")
            return News(**updated_rec).__dict__

@app.delete("/api/admin/news/{news_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_admin_news_api(news_id: int, api_key: str = Depends(get_api_key)):
    pool = await get_db_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("DELETE FROM news WHERE id = %s", (news_id,))
            if cur.rowcount == 0: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="News not found.")
            return

if __name__ == "__main__":
    import uvicorn
    if not os.getenv("WEBHOOK_URL"):
        logger.info("WEBHOOK_URL not set. Running bot in polling mode.")
        async def start_polling(): await dp.start_polling(bot)
        asyncio.run(start_polling())
    uvicorn.run(app, host="0.0.0.0", port=8000)

