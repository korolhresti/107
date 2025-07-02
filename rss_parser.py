import requests
from bs4 import BeautifulSoup
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import asyncio

async def parse_rss_feed(url: str) -> Optional[Dict[str, Any]]:
    """
    Парсить RSS-стрічку за допомогою requests та BeautifulSoup.
    Повертає дані останньої новини.
    """
    print(f"Парсинг RSS-стрічки: {url}")
    try:
        # requests є синхронною бібліотекою, тому запускаємо її в окремому потоці
        # для уникнення блокування циклу подій asyncio.
        response = await asyncio.to_thread(requests.get, url, timeout=10)
        response.raise_for_status() # Виклик винятку для поганих відповідей (4xx або 5xx)

        soup = BeautifulSoup(response.content, 'xml') # Парсимо як XML

        latest_entry = soup.find('item') # Для RSS 2.0
        if not latest_entry:
            latest_entry = soup.find('entry') # Для Atom feeds

        if latest_entry:
            title_tag = latest_entry.find('title')
            title = title_tag.get_text() if title_tag else 'Без заголовка'

            content_tag = latest_entry.find('description') or latest_entry.find('summary') or latest_entry.find('content')
            content = content_tag.get_text() if content_tag else 'Без змісту'

            link_tag = latest_entry.find('link')
            link = link_tag.get_text() if link_tag else url
            if link_tag and link_tag.has_attr('href'): # Для Atom feeds
                link = link_tag['href']

            # Спроба отримати дату публікації
            published_at = datetime.now(timezone.utc) # За замовчуванням
            pub_date_tag = latest_entry.find('pubDate') or latest_entry.find('updated')
            if pub_date_tag:
                try:
                    # BeautifulSoup повертає рядок, який потрібно розпарсити
                    # Це може бути складно, тому краще покладатися на загальний формат ISO
                    # або використовувати більш надійні бібліотеки для парсингу дат.
                    # Для простоти, спробуємо стандартний парсинг.
                    published_at = datetime.fromisoformat(pub_date_tag.get_text().replace('Z', '+00:00'))
                except ValueError:
                    pass # Використовуємо datetime.now(), якщо парсинг дати невдалий

            image_url = None
            # Спроба знайти зображення в тегах media:thumbnail або enclosure
            media_thumbnail = latest_entry.find('media:thumbnail')
            if media_thumbnail and media_thumbnail.has_attr('url'):
                image_url = media_thumbnail['url']
            else:
                enclosure = latest_entry.find('enclosure', type=lambda x: x and x.startswith('image/'))
                if enclosure and enclosure.has_attr('url'):
                    image_url = enclosure['url']
            
            # Якщо image_url все ще немає, спробуємо знайти в content за допомогою BeautifulSoup
            if not image_url and content:
                content_soup = BeautifulSoup(content, 'html.parser')
                img_tag = content_soup.find('img')
                if img_tag and img_tag.get('src'):
                    image_url = img_tag['src']

            return {
                "title": title,
                "content": content,
                "source_url": link,
                "image_url": image_url,
                "published_at": published_at,
                "lang": "uk" # Можна спробувати визначити мову за допомогою бібліотеки
            }
        else:
            print(f"RSS-стрічка {url} не містить записів або має невідомий формат.")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Помилка HTTP запиту до RSS-стрічки {url}: {e}")
        return None
    except Exception as e:
        print(f"Помилка при парсингу RSS-стрічки {url}: {e}")
        return None

# Для тестування (якщо потрібно запускати окремо)
if __name__ == "__main__":
    async def test_parser():
        print("Тестування rss_parser (без feedparser)...")
        # Приклад використання: замініть на реальне посилання на RSS-стрічку
        test_rss_url = "http://rss.cnn.com/rss/cnn_topstories.rss" # Приклад RSS 2.0
        # test_rss_url = "https://www.theverge.com/rss/index.xml" # Приклад Atom feed
        data = await parse_rss_feed(test_rss_url)
        if data:
            print(f"Заголовок: {data.get('title')}")
            print(f"Зміст (фрагмент): {data.get('content')[:200]}...")
            print(f"Зображення: {data.get('image_url')}")
            print(f"Опубліковано: {data.get('published_at')}")
        else:
            print(f"Не вдалося спарсити {test_rss_url}")

    asyncio.run(test_parser())
