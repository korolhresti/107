import feedparser
import logging
from typing import Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)

async def parse_rss_feed(feed_url: str) -> Optional[Dict[str, Any]]:
    """
    Парсить RSS-стрічку та повертає дані останньої новини.
    """
    logger.info(f"Парсинг RSS-стрічки: {feed_url}")
    try:
        feed = feedparser.parse(feed_url)
        if not feed.entries:
            logger.warning(f"RSS-стрічка {feed_url} не містить записів.")
            return None
        
        latest_entry = feed.entries[0] # Беремо останній запис

        title = latest_entry.get('title', 'Без заголовка')
        # Видаляємо HTML-теги зі змісту, якщо вони є
        content = BeautifulSoup(latest_entry.get('summary', latest_entry.get('description', 'Без змісту')), 'lxml').get_text()
        link = latest_entry.get('link', feed_url)
        
        image_url = None
        if 'media_content' in latest_entry and latest_entry.media_content:
            for media in latest_entry.media_content:
                if media.get('type', '').startswith('image/'):
                    image_url = media.get('url')
                    break
        elif 'image' in latest_entry and 'href' in latest_entry.image:
            image_url = latest_entry.image.href
        elif 'links' in latest_entry:
            for l in latest_entry.links:
                if l.get('rel') == 'enclosure' and l.get('type', '').startswith('image/'):
                    image_url = l.get('href')
                    break

        published_at = datetime.now()
        if hasattr(latest_entry, 'published_parsed'):
            published_at = datetime(*latest_entry.published_parsed[:6])

        return {
            "title": title,
            "content": content,
            "image_url": image_url,
            "source_url": link,
            "published_at": published_at,
            "lang": "uk" # Можна спробувати визначити мову
        }
    except Exception as e:
        logger.error(f"Помилка парсингу RSS-стрічки {feed_url}: {e}")
        return None

