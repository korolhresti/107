import requests
from bs4 import BeautifulSoup
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

async def fetch_html(url: str) -> Optional[str]:
    """
    Асинхронно завантажує HTML-вміст за заданим URL.
    """
    try:
        # Використовуємо requests, оскільки aiohttp вже є в bot.py, але requests простіший для одноразових запитів
        # та демонстрації без зміни асинхронного клієнта.
        response = requests.get(url, timeout=10)
        response.raise_for_status()  # Викликає HTTPError для поганих відповідей (4xx або 5xx)
        return response.text
    except requests.exceptions.RequestException as e:
        logger.error(f"Помилка завантаження HTML з {url}: {e}")
        return None

async def parse_website(url: str) -> Optional[Dict[str, Any]]:
    """
    Імітує парсинг веб-сайту. У реальній системі тут була б складна логіка
    з селекторами для кожного сайту або універсальний парсер.
    Для демонстрації повертає мок-дані.
    """
    logger.info(f"SIMULATED PARSING: Парсинг веб-сайту: {url}")
    html_content = await fetch_html(url)
    
    if not html_content:
        return None

    # Проста імітація вилучення заголовка та частини тексту
    soup = BeautifulSoup(html_content, 'lxml') # Використовуємо lxml для швидшого парсингу
    
    title_tag = soup.find('title')
    title = title_tag.get_text() if title_tag else "Заголовок не знайдено"

    # Спроба знайти основний контент. Це дуже спрощено.
    # У реальному парсері потрібні були б конкретні селектори для кожного сайту.
    main_content_div = soup.find('div', class_='article-body') or \
                       soup.find('main') or \
                       soup.find('article')
    
    content = "Зміст не знайдено."
    if main_content_div:
        paragraphs = main_content_div.find_all('p')
        content = "\n".join([p.get_text() for p in paragraphs[:5]]) # Беремо перші 5 абзаців
        if not content: # Якщо абзаців немає, спробувати весь текст
            content = main_content_div.get_text()[:1000] + "..." if main_content_div.get_text() else "Зміст не знайдено."
    
    image_tag = soup.find('img', class_='article-image') or \
                soup.find('meta', property='og:image')
    image_url = image_tag['content'] if image_tag and 'content' in image_tag.attrs else \
                (image_tag['src'] if image_tag and 'src' in image_tag.attrs else None)

    return {
        "title": title,
        "content": content,
        "image_url": image_url,
        "source_url": url,
        "lang": "uk" # Можна спробувати визначити мову за допомогою бібліотек, але для прикладу 'uk'
    }

