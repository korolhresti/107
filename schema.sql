-- Схема бази даних для Telegram AI News Bot

-- Таблиця користувачів
CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY,
    username VARCHAR(255),
    first_name VARCHAR(255),
    last_name VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    is_admin BOOLEAN DEFAULT FALSE,
    last_active TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    language VARCHAR(10) DEFAULT 'uk',
    auto_notifications BOOLEAN DEFAULT FALSE,
    digest_frequency VARCHAR(50) DEFAULT 'daily'
);

-- Таблиця новин
CREATE TABLE IF NOT EXISTS news (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source_url TEXT,
    image_url TEXT,
    published_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    lang VARCHAR(10) NOT NULL DEFAULT 'uk',
    ai_summary TEXT,
    ai_classified_topics JSONB,
    moderation_status VARCHAR(50) DEFAULT 'approved', -- 'pending_review', 'approved', 'declined'
    expires_at TIMESTAMP WITH TIME ZONE DEFAULT (CURRENT_TIMESTAMP + INTERVAL '5 days')
);

-- Таблиця для користувацьких фільтрів (наприклад, джерела новин)
CREATE TABLE IF NOT EXISTS custom_feeds (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    feed_name TEXT NOT NULL,
    filters JSONB, -- Зберігатиме JSON об'єкт з фільтрами (наприклад, {"source_ids": [1, 2, 3]})
    UNIQUE (user_id, feed_name)
);

-- Таблиця джерел новин
CREATE TABLE IF NOT EXISTS sources (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    link TEXT UNIQUE NOT NULL,
    type TEXT DEFAULT 'web', -- 'web', 'telegram', 'rss', 'twitter'
    status TEXT DEFAULT 'active' -- 'active', 'inactive', 'blocked'
);

-- Таблиця для відстеження переглянутих новин користувачами
CREATE TABLE IF NOT EXISTS user_news_views (
    user_id BIGINT NOT NULL REFERENCES users(id),
    news_id INTEGER NOT NULL REFERENCES news(id),
    viewed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, news_id)
);

-- Таблиця для статистики користувачів
CREATE TABLE IF NOT EXISTS user_stats (
    user_id BIGINT PRIMARY KEY REFERENCES users(id),
    viewed_news_count INT DEFAULT 0,
    last_active TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    viewed_topics JSONB DEFAULT '[]'::jsonb -- Зберігатиме список тем, які користувач переглядав
);

-- Додавання початкових даних до таблиці sources, якщо вона порожня
INSERT INTO sources (name, link, type, status)
SELECT 'BBC News Україна', 'https://www.bbc.com/ukrainian', 'web', 'active'
WHERE NOT EXISTS (SELECT 1 FROM sources WHERE name = 'BBC News Україна');

INSERT INTO sources (name, link, type, status)
SELECT 'Укрінформ', 'https://www.ukrinform.ua/', 'web', 'active'
WHERE NOT EXISTS (SELECT 1 FROM sources WHERE name = 'Укрінформ');

INSERT INTO sources (name, link, type, status)
SELECT 'Ліга.net', 'https://www.liga.net/', 'web', 'active'
WHERE NOT EXISTS (SELECT 1 FROM sources WHERE name = 'Ліга.net');

INSERT INTO sources (name, link, type, status)
SELECT 'Цензор.НЕТ', 'https://censor.net/', 'web', 'active'
WHERE NOT EXISTS (SELECT 1 FROM sources WHERE name = 'Цензор.НЕТ');

INSERT INTO sources (name, link, type, status)
SELECT 'Економічна правда', 'https://www.epravda.com.ua/', 'web', 'active'
WHERE NOT EXISTS (SELECT 1 FROM sources WHERE name = 'Економічна правда');

INSERT INTO sources (name, link, type, status)
SELECT 'РБК-Україна', 'https://www.rbc.ua/', 'web', 'active'
WHERE NOT EXISTS (SELECT 1 FROM sources WHERE name = 'РБК-Україна');

INSERT INTO sources (name, link, type, status)
SELECT 'Obozrevatel', 'https://www.obozrevatel.com/', 'web', 'active'
WHERE NOT EXISTS (SELECT 1 FROM sources WHERE name = 'Obozrevatel');

INSERT INTO sources (name, link, type, status)
SELECT 'NV', 'https://nv.ua/', 'web', 'active'
WHERE NOT EXISTS (SELECT 1 FROM sources WHERE name = 'NV');

INSERT INTO sources (name, link, type, status)
SELECT 'ZN.UA', 'https://zn.ua/', 'web', 'active'
WHERE NOT EXISTS (SELECT 1 FROM sources WHERE name = 'ZN.UA');

INSERT INTO sources (name, link, type, status)
SELECT 'Українська правда', 'https://www.pravda.com.ua/', 'web', 'active'
WHERE NOT EXISTS (SELECT 1 FROM sources WHERE name = 'Українська правда');
