import feedparser
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import asyncio

async def parse_rss_feed(url: str) -> Optional[Dict[str, Any]]:
    """
    Парсить RSS-стрічку за допомогою feedparser.
    Повертає дані останньої новини.
    """
    print(f"Парсинг RSS-стрічки: {url}")
    try:
        # feedparser не є асинхронним, тому запускаємо його в окремому потоці
        # для уникнення блокування циклу подій.
        feed = await asyncio.to_thread(feedparser.parse, url)

        if feed.entries:
            latest_entry = feed.entries[0]

            title = latest_entry.get('title', 'Без заголовка')
            content = latest_entry.get('summary', latest_entry.get('description', 'Без змісту'))

            # Спроба отримати дату публікації
            published_at = datetime.now(timezone.utc) # За замовчуванням
            if hasattr(latest_entry, 'published_parsed'):
                try:
                    published_at = datetime(*latest_entry.published_parsed[:6], tzinfo=timezone.utc)
                except Exception:
                    pass # Використовуємо datetime.now(), якщо парсинг дати невдалий

            image_url = None
            if hasattr(latest_entry, 'media_thumbnail') and latest_entry.media_thumbnail:
                image_url = latest_entry.media_thumbnail[0]['url']
            elif hasattr(latest_entry, 'enclosures') and latest_entry.enclosures:
                for enc in latest_entry.enclosures:
                    if enc.get('type', '').startswith('image/'):
                        image_url = enc['href']
                        break
            elif hasattr(latest_entry, 'links'):
                for link in latest_entry.links:
                    if link.get('rel') == 'enclosure' and link.get('type', '').startswith('image/'):
                        image_url = link['href']
                        break
            
            # Якщо image_url все ще немає, спробуємо знайти в content за допомогою BeautifulSoup
            if not image_url and content:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(content, 'html.parser')
                img_tag = soup.find('img')
                if img_tag and img_tag.get('src'):
                    image_url = img_tag['src']

            return {
                "title": title,
                "content": content,
                "source_url": latest_entry.get('link', url),
                "image_url": image_url,
                "published_at": published_at,
                "lang": latest_entry.get('language', 'uk') # Можна спробувати визначити мову
            }
        else:
            print(f"RSS-стрічка {url} не містить записів.")
            return None
    except Exception as e:
        print(f"Помилка при парсингу RSS-стрічки {url}: {e}")
        return None

# Для тестування (якщо потрібно запускати окремо)
if __name__ == "__main__":
    async def test_parser():
        print("Тестування rss_parser...")
        # Приклад використання: замініть на реальне посилання на RSS-стрічку
        test_rss_url = "http://rss.cnn.com/rss/cnn_topstories.rss" # Приклад
        data = await parse_rss_feed(test_rss_url)
        if data:
            print(f"Заголовок: {data.get('title')}")
            print(f"Зміст (фрагмент): {data.get('content')[:200]}...")
            print(f"Зображення: {data.get('image_url')}")
            print(f"Опубліковано: {data.get('published_at')}")
        else:
            print(f"Не вдалося спарсити {test_rss_url}")

    asyncio.run(test_parser())
