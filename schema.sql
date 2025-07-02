-- patch_schema.sql - Скрипт для оновлення існуючої схеми бази даних, якщо schema.sql не був виконаний повністю.

-- Додавання/оновлення таблиці custom_feeds, якщо її немає або потрібно оновити
CREATE TABLE IF NOT EXISTS custom_feeds (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    feed_name TEXT NOT NULL,
    filters JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, feed_name)
);

-- Додавання відсутніх стовпців до таблиці users
-- (Якщо ці стовпці вже існують, ALTER TABLE ADD COLUMN IF NOT EXISTS просто пропустить їх)
ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS language VARCHAR(10) DEFAULT 'uk';
ALTER TABLE users ADD COLUMN IF NOT EXISTS auto_notifications BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS digest_frequency VARCHAR(50) DEFAULT 'daily';
ALTER TABLE users ADD COLUMN IF NOT EXISTS safe_mode BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS current_feed_id INT REFERENCES custom_feeds(id);
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_premium BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_expires_at TIMESTAMP;
ALTER TABLE users ADD COLUMN IF NOT EXISTS level INT DEFAULT 1;
ALTER TABLE users ADD COLUMN IF NOT EXISTS badges TEXT[] DEFAULT ARRAY[]::TEXT[];
ALTER TABLE users ADD COLUMN IF NOT EXISTS inviter_id INT REFERENCES users(id);
ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT UNIQUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS view_mode TEXT DEFAULT 'manual';
ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_id BIGINT; -- Додаємо telegram_id для users.html

-- Додавання/оновлення таблиці news, якщо її немає
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
    moderation_status VARCHAR(50) DEFAULT 'approved',
    expires_at TIMESTAMP WITH TIME ZONE DEFAULT (CURRENT_TIMESTAMP + INTERVAL '5 days')
);
-- Додавання відсутніх стовпців до news
ALTER TABLE news ADD COLUMN IF NOT EXISTS source_url TEXT;
ALTER TABLE news ADD COLUMN IF NOT EXISTS image_url TEXT;
ALTER TABLE news ADD COLUMN IF NOT EXISTS ai_summary TEXT;
ALTER TABLE news ADD COLUMN IF NOT EXISTS ai_classified_topics JSONB;
ALTER TABLE news ADD COLUMN IF NOT EXISTS moderation_status VARCHAR(50) DEFAULT 'approved';
ALTER TABLE news ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP WITH TIME ZONE DEFAULT (CURRENT_TIMESTAMP + INTERVAL '5 days');


-- Додавання/оновлення таблиці sources, якщо її немає
CREATE TABLE IF NOT EXISTS sources (
    id SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    link TEXT UNIQUE NOT NULL,
    type TEXT DEFAULT 'web',
    status TEXT DEFAULT 'active'
);

-- Додавання/оновлення таблиці user_news_views
CREATE TABLE IF NOT EXISTS user_news_views (
    user_id BIGINT NOT NULL REFERENCES users(id),
    news_id INTEGER NOT NULL REFERENCES news(id),
    viewed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, news_id)
);

-- Додавання/оновлення таблиці user_stats
CREATE TABLE IF NOT EXISTS user_stats (
    user_id BIGINT PRIMARY KEY REFERENCES users(id),
    viewed_news_count INT DEFAULT 0,
    last_active TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    viewed_topics JSONB DEFAULT '[]'::jsonb
);
ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS viewed_topics JSONB DEFAULT '[]'::jsonb;

-- Додавання/оновлення таблиці comments
CREATE TABLE IF NOT EXISTS comments (
    id SERIAL PRIMARY KEY,
    news_id INT REFERENCES news(id),
    user_id INT REFERENCES users(id),
    comment_text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Додавання/оновлення таблиці reports
CREATE TABLE IF NOT EXISTS reports (
    id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(id),
    report_type TEXT NOT NULL,
    target_id INT,
    details JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE reports ADD COLUMN IF NOT EXISTS target_id INT;

-- Додавання/оновлення таблиці feedback
CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(id),
    feedback_text TEXT NOT NULL,
    rating INT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Додавання/оновлення таблиці summaries
CREATE TABLE IF NOT EXISTS summaries (
    id SERIAL PRIMARY KEY,
    news_id INT REFERENCES news(id),
    summary_text TEXT NOT NULL,
    summary_type TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Додавання/оновлення таблиці blocks
CREATE TABLE IF NOT EXISTS blocks (
    id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(id),
    block_type TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, block_type, value)
);

-- Додавання/оновлення таблиці bookmarks
CREATE TABLE IF NOT EXISTS bookmarks (
    id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(id),
    news_id INT REFERENCES news(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, news_id)
);

-- Додавання/оновлення таблиці invites
CREATE TABLE IF NOT EXISTS invites (
    id SERIAL PRIMARY KEY,
    inviter_id INT REFERENCES users(id),
    invite_code TEXT UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    accepted_at TIMESTAMP
);
ALTER TABLE invites ADD COLUMN IF NOT EXISTS inviter_id INT REFERENCES users(id);

-- Додавання/оновлення таблиці admin_actions
CREATE TABLE IF NOT EXISTS admin_actions (
    id SERIAL PRIMARY KEY,
    admin_user_id INT,
    action_type TEXT NOT NULL,
    target_id INT,
    details JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Додавання/оновлення таблиці source_stats
CREATE TABLE IF NOT EXISTS source_stats (
    id SERIAL PRIMARY KEY,
    source_id INT REFERENCES sources(id) UNIQUE,
    publication_count INT DEFAULT 0,
    avg_rating REAL DEFAULT 0.0,
    report_count INT DEFAULT 0,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Створення або перестворення індексів. IF NOT EXISTS тут особливо корисний.
CREATE INDEX IF NOT EXISTS idx_news_published_expires_moderation ON news (published_at DESC, expires_at, moderation_status);
-- CREATE INDEX IF NOT EXISTS idx_filters_user_id ON filters (user_id); -- filters table not defined
CREATE INDEX IF NOT EXISTS idx_blocks_user_type_value ON blocks (user_id, block_type, value);
CREATE INDEX IF NOT EXISTS idx_bookmarks_user_id ON bookmarks (user_id);
CREATE INDEX IF NOT EXISTS idx_user_stats_user_id ON user_stats (user_id);
CREATE INDEX IF NOT EXISTS idx_comments_news_id ON comments (news_id);
CREATE INDEX IF NOT EXISTS idx_user_news_views_user_news_id ON user_news_views (user_id, news_id);
CREATE INDEX IF NOT EXISTS idx_reports_user_id_target_id ON reports (user_id, target_id);
CREATE INDEX IF NOT EXISTS idx_feedback_user_id ON feedback (user_id);
CREATE INDEX IF NOT EXISTS idx_invites_inviter_id ON invites (inviter_id);
CREATE INDEX IF NOT EXISTS idx_admin_actions_admin_user_id ON admin_actions (admin_user_id);
CREATE INDEX IF NOT EXISTS idx_source_stats_source_id ON source_stats (source_id);
