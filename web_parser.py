import asyncio
from typing import Optional, Dict, Any
from bs4 import BeautifulSoup
import httpx # Використовуємо httpx для асинхронних запитів
from datetime import datetime # <--- ДОДАНО: Імпорт datetime

async def parse_website(url: str) -> Optional[Dict[str, Any]]:
    """
    Імітує парсинг веб-сайту.
    У реальному застосуванні тут буде логіка для отримання та аналізу HTML.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, follow_redirects=True, timeout=10)
            response.raise_for_status() # Виклик винятку для поганих відповідей (4xx або 5xx)

        soup = BeautifulSoup(response.text, 'html.parser')

        # Приклад отримання заголовка та першого параграфа
        title = soup.find('title').get_text() if soup.find('title') else "Заголовок не знайдено"
        
        # Спробуємо знайти основний контент, об'єднуючи параграфи
        content_elements = soup.find_all('p')
        content = "\n".join([p.get_text() for p in content_elements[:5]]) # Беремо перші 5 параграфів

        # Спроба знайти зображення
        image_tag = soup.find('img')
        image_url = image_tag['src'] if image_tag and image_tag.get('src') else None
        if image_url and not image_url.startswith(('http://', 'https://')):
            # Перетворюємо відносні URL на абсолютні
            from urllib.parse import urljoin
            image_url = urljoin(url, image_url)

        return {
            "title": title,
            "content": content if content else "Зміст не знайдено",
            "source_url": url,
            "image_url": image_url,
            "published_at": datetime.now(), # Або спробуйте витягнути з мета-тегів
            "lang": "uk" # Можна спробувати визначити мову за допомогою бібліотеки
        }
    except httpx.RequestError as e:
        # Спеціальна обробка для 403 Forbidden
        if "403 Forbidden" in str(e):
            print(f"Помилка HTTP запиту: {e}. Доступ до сайту {url} заборонено. Пропускаю.")
            return None
        print(f"Помилка HTTP запиту: {e}")
        return None
    except Exception as e:
        print(f"Помилка парсингу веб-сайту {url}: {e}")
        return None

# Для тестування (якщо потрібно запускати окремо)
if __name__ == "__main__":
    # from datetime import datetime # Цей імпорт тепер на початку файлу
    async def test_parser():
        print("Тестування web_parser...")
        # Приклад використання: замініть на реальне посилання
        test_url = "https://www.bbc.com/ukrainian"
        data = await parse_website(test_url)
        if data:
            print(f"Заголовок: {data.get('title')}")
            print(f"Зміст (фрагмент): {data.get('content')[:200]}...")
            print(f"Зображення: {data.get('image_url')}")
        else:
            print(f"Не вдалося спарсити {test_url}")

    asyncio.run(test_parser())
