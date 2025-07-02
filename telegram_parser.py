import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

async def get_telegram_channel_posts(channel_link: str, limit: int = 1) -> Optional[Dict[str, Any]]:
    """
    Імітує отримання останніх постів з Telegram-каналу.
    У реальній системі це вимагатиме використання Telegram API (наприклад, Telethon userbot)
    або спеціальних дозволів для бота.
    Для демонстрації повертає мок-дані.
    """
    logger.info(f"SIMULATED PARSING: Отримання постів з Telegram-каналу: {channel_link}")
    
    # Мок-дані, які імітують останній пост
    mock_title = f"Оновлення з Telegram-каналу {channel_link.split('/')[-1]} на {datetime.now().strftime('%H:%M')}"
    mock_content = (
        f"Сьогодні канал {channel_link.split('/')[-1]} опублікував важливу новину про останні події. "
        "У дописі йдеться про нові тенденції у сфері штучного інтелекту та їхній вплив на повсякденне життя. "
        "Експерти зазначають, що швидкість впровадження AI-технологій значно зростає, "
        "і це відкриває нові можливості для розвитку різних галузей. "
        "Детальніше читайте у повному дописі."
    )
    mock_image_url = "https://placehold.co/600x400/80B3FF/FFFFFF?text=Telegram+News"

    return {
        "title": mock_title,
        "content": mock_content,
        "image_url": mock_image_url,
        "source_url": channel_link,
        "lang": "uk"
    }

