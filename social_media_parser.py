import logging
from typing import Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)

async def get_social_media_posts(link: str, platform_type: str, limit: int = 1) -> Optional[Dict[str, Any]]:
    """
    Імітує отримання постів із соціальних мереж (Twitter/Instagram).
    У реальній системі це вимагатиме використання офіційних API платформ
    та обробки їхніх обмежень.
    Для демонстрації повертає мок-дані.
    """
    logger.info(f"SIMULATED PARSING: Отримання постів з {platform_type}: {link}")
    
    # Мок-дані
    mock_title = f"Оновлення з {platform_type} {link.split('/')[-1]} на {datetime.now().strftime('%H:%M')}"
    mock_content = (
        f"Останній пост у {platform_type} за посиланням {link} висвітлює важливу тему. "
        "У ньому обговорюються нові виклики та можливості у сучасному цифровому світі. "
        "Автор ділиться своїми думками щодо майбутнього технологій та їхнього впливу на суспільство. "
        "Допис викликав жваву дискусію серед підписників."
    )
    mock_image_url = None
    if platform_type == 'instagram':
        mock_image_url = "https://placehold.co/600x400/FFD700/000000?text=Instagram+Post"
    elif platform_type == 'twitter':
        mock_image_url = "https://placehold.co/600x400/1DA1F2/FFFFFF?text=Twitter+Tweet"

    return {
        "title": mock_title,
        "content": mock_content,
        "image_url": mock_image_url,
        "source_url": link,
        "lang": "uk"
    }

