import requests
from bs4 import BeautifulSoup
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import asyncio
import re

async def parse_rss_feed(url: str) -> Optional[Dict[str, Any]]:
    """
    Парсить RSS-стрічку за допомогою requests та BeautifulSoup.
    Повертає дані останньої новини.
    """
    print(f"Парсинг RSS-стрічки: {url}")
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36'
        }
        response = await asyncio.to_thread(requests.get, url, timeout=10, headers=headers)
        response.raise_for_status() # Виклик винятку для поганих відповідей (4xx або 5xx)

        soup = BeautifulSoup(response.content, 'xml') # Парсимо як XML

        latest_entry = soup.find('item') # Для RSS 2.0
        if not latest_entry:
            latest_entry = soup.find('entry') # Для Atom feeds

        if latest_entry:
            title_tag = latest_entry.find('title')
            title = title_tag.get_text().strip() if title_tag else 'Без заголовка'

            content_tag = latest_entry.find('description') or latest_entry.find('summary') or latest_entry.find('content')
            content = content_tag.get_text().strip() if content_tag else 'Без змісту'

            link_tag = latest_entry.find('link')
            link = link_tag.get_text().strip() if link_tag else url
            if link_tag and link_tag.has_attr('href'): # Для Atom feeds
                link = link_tag['href'].strip()

            # Спроба отримати дату публікації
            published_at = datetime.now(timezone.utc) # За замовчуванням
            pub_date_tag = latest_entry.find('pubDate') or latest_entry.find('updated')
            if pub_date_tag:
                date_str = pub_date_tag.get_text().strip()
                try:
                    # Спробуємо різні формати дати, які часто зустрічаються в RSS/Atom
                    # RFC 822 (наприклад, "Tue, 01 Jan 2024 12:00:00 GMT")
                    published_at = datetime.strptime(date_str, '%a, %d %b %Y %H:%M:%S %Z').replace(tzinfo=timezone.utc)
                except ValueError:
                    try:
                        # ISO 8601 (наприклад, "2024-01-01T12:00:00Z")
                        published_at = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                        if published_at.tzinfo is None:
                            published_at = published_at.replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass # Якщо парсинг дати невдалий, залишаємо datetime.now()

            image_url = None
            # Спроба знайти зображення в тегах media:thumbnail або enclosure
            media_thumbnail = latest_entry.find('media:thumbnail')
            if media_thumbnail and media_thumbnail.has_attr('url'):
                image_url = media_thumbnail['url'].strip()
            else:
                enclosure = latest_entry.find('enclosure', type=lambda x: x and x.startswith('image/'))
                if enclosure and enclosure.has_attr('url'):
                    image_url = enclosure['url'].strip()
            
            # Якщо image_url все ще немає, спробуємо знайти в content за допомогою BeautifulSoup
            if not image_url and content:
                content_soup = BeautifulSoup(content, 'html.parser')
                img_tag = content_soup.find('img')
                if img_tag and img_tag.get('src'):
                    image_url = img_tag['src'].strip()

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
        print("Тестування rss_parser...")
        test_rss_urls = [
            "http://rss.cnn.com/rss/cnn_topstories.rss", # Приклад RSS 2.0
            "https://www.theverge.com/rss/index.xml", # Приклад Atom feed
            "https://ukranews.com/rss/ukr/news" # Український приклад
        ]
        for url in test_rss_urls:
            print(f"\n--- Парсинг: {url} ---")
            data = await parse_rss_feed(url)
            if data:
                print(f"Заголовок: {data.get('title')}")
                print(f"Зміст (фрагмент): {data.get('content')[:500]}...")
                print(f"Зображення: {data.get('image_url')}")
                print(f"Опубліковано: {data.get('published_at')}")
            else:
                print(f"Не вдалося спарсити {url}")

    asyncio.run(test_parser())
