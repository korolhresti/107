import asyncio
from typing import Optional, Dict, Any
from bs4 import BeautifulSoup
import httpx
from datetime import datetime, timezone
from urllib.parse import urljoin

async def parse_website(url: str) -> Optional[Dict[str, Any]]:
    """
    Парсить веб-сайт, намагаючись витягти заголовок, основний контент,
    URL зображення та дату публікації.
    """
    print(f"Парсинг веб-сайту: {url}")
    try:
        async with httpx.AsyncClient(timeout=15) as client: # Збільшено таймаут
            # Додаємо User-Agent, щоб імітувати браузер і уникнути 403 Forbidden
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.4896.127 Safari/537.36'
            }
            response = await client.get(url, follow_redirects=True, headers=headers)
            response.raise_for_status() # Виклик винятку для поганих відповідей (4xx або 5xx)

        soup = BeautifulSoup(response.text, 'html.parser')

        # 1. Вилучення заголовка
        title = None
        # Пошук в <title>
        title_tag = soup.find('title')
        if title_tag:
            title = title_tag.get_text().strip()
        
        # Пошук в Open Graph / Twitter Card мета-тегах
        if not title:
            og_title = soup.find('meta', property='og:title')
            if og_title and og_title.get('content'):
                title = og_title['content'].strip()
        if not title:
            twitter_title = soup.find('meta', property='twitter:title')
            if twitter_title and twitter_title.get('content'):
                title = twitter_title['content'].strip()
        
        if not title:
            title = "Заголовок не знайдено"

        # 2. Вилучення основного контенту
        content = ""
        # Спробуємо знайти елементи, які зазвичай містять основний контент статті
        article_body = soup.find('article') or soup.find('main') or soup.find(class_='entry-content') or soup.find(class_='post-content')

        if article_body:
            # Збираємо текст з параграфів всередині основного елемента
            paragraphs = article_body.find_all('p')
            content = "\n".join([p.get_text().strip() for p in paragraphs if p.get_text().strip()])
        
        # Якщо контент все ще порожній, спробуємо знайти загальні параграфи
        if not content:
            content_elements = soup.find_all('p')
            content = "\n".join([p.get_text().strip() for p in content_elements[:10] if p.get_text().strip()]) # Беремо перші 10 параграфів

        if not content:
            content = "Зміст не знайдено"

        # 3. Вилучення URL зображення
        image_url = None
        # Пошук в Open Graph / Twitter Card мета-тегах (основне зображення)
        og_image = soup.find('meta', property='og:image')
        if og_image and og_image.get('content'):
            image_url = og_image['content']
        if not image_url:
            twitter_image = soup.find('meta', property='twitter:image')
            if twitter_image and twitter_image.get('content'):
                image_url = twitter_image['content']

        # Якщо мета-теги не дали результату, шукаємо перше значне зображення
        if not image_url:
            img_tag = soup.find('img', src=True) # Шукаємо будь-який img з атрибутом src
            if img_tag and img_tag.get('src'):
                image_url = img_tag['src']
        
        # Перетворюємо відносні URL на абсолютні
        if image_url and not image_url.startswith(('http://', 'https://')):
            image_url = urljoin(url, image_url)
        
        # 4. Вилучення дати публікації
        published_at = datetime.now(timezone.utc) # За замовчуванням
        # Спробуємо знайти дату в мета-тегах (наприклад, opengraph, schema.org)
        pub_date_meta = soup.find('meta', property='article:published_time') or \
                        soup.find('meta', property='og:updated_time') or \
                        soup.find('meta', property='last-modified') or \
                        soup.find('meta', {'name': 'date'}) or \
                        soup.find('time', datetime=True)

        if pub_date_meta:
            date_str = pub_date_meta.get('content') or pub_date_meta.get('datetime')
            if date_str:
                try:
                    published_at = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                    # Перетворюємо на UTC, якщо не вказано
                    if published_at.tzinfo is None:
                        published_at = published_at.replace(tzinfo=timezone.utc)
                except ValueError:
                    pass # Якщо парсинг дати невдалий, залишаємо datetime.now()

        return {
            "title": title,
            "content": content,
            "source_url": url,
            "image_url": image_url,
            "published_at": published_at,
            "lang": "uk" # Можна спробувати визначити мову за допомогою бібліотеки, але за замовчуванням 'uk'
        }
    except httpx.RequestError as e:
        print(f"Помилка HTTP запиту до веб-сайту {url}: {e}")
        # Спеціальна обробка для 403 Forbidden
        if "403 Forbidden" in str(e) or "401 Unauthorized" in str(e):
            print(f"Доступ до сайту {url} заборонено або вимагає авторизації. Пропускаю.")
        return None
    except Exception as e:
        print(f"Помилка при парсингу веб-сайту {url}: {e}")
        return None

# Для тестування (якщо потрібно запускати окремо)
if __name__ == "__main__":
    async def test_parser():
        print("Тестування web_parser...")
        test_urls = [
            "https://www.bbc.com/ukrainian/articles/c4nn97520e7o", # Приклад статті
            "https://tsn.ua/ukrayina/novini-ukrayini-ta-svitu-sogodni-ostanni-novini-ukrayini-ta-svitu-onlayn-2591605.html", # Ще один приклад
            "https://example.com" # Загальний сайт
        ]
        for url in test_urls:
            print(f"\n--- Парсинг: {url} ---")
            data = await parse_website(url)
            if data:
                print(f"Заголовок: {data.get('title')}")
                print(f"Зміст (фрагмент): {data.get('content')[:500]}...")
                print(f"Зображення: {data.get('image_url')}")
                print(f"Опубліковано: {data.get('published_at')}")
            else:
                print(f"Не вдалося спарсити {url}")

    asyncio.run(test_parser())
