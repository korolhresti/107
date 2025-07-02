-- patch_schema.sql - Скрипт для оновлення існуючої схеми бази даних, якщо schema.sql не був виконаний повністю.

-- Додавання/оновлення таблиці users
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY, -- Змінено з SERIAL на BIGSERIAL для підтримки великих ID Telegram
    telegram_id BIGINT UNIQUE, -- Залишається BIGINT
    username VARCHAR(255),
    first_name VARCHAR(255),
    last_name VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    is_admin BOOLEAN DEFAULT FALSE,
    last_active TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    language VARCHAR(10) DEFAULT 'uk',
    auto_notifications BOOLEAN DEFAULT FALSE,
    digest_frequency VARCHAR(50) DEFAULT 'daily',
    safe_mode BOOLEAN DEFAULT FALSE,
    current_feed_id INT,
    is_premium BOOLEAN DEFAULT FALSE,
    premium_expires_at TIMESTAMP WITH TIME ZONE,
    level INT DEFAULT 1,
    badges JSONB DEFAULT '[]'::JSONB,
    inviter_id BIGINT,
    email VARCHAR(255) UNIQUE,
    view_mode VARCHAR(50) DEFAULT 'detailed'
);

-- Додавання відсутніх стовпців до таблиці users
-- (Якщо ці стовпці вже існують, ALTER TABLE ADD COLUMN IF NOT EXISTS просто пропустить їх)
-- Ці ALTER TABLE команди були перенесені сюди з попередньої версії schema.sql
-- і тепер вони будуть виконуватися після CREATE TABLE IF NOT EXISTS users
-- Це забезпечить, що стовпці будуть додані, якщо таблиця існувала, але не мала цих стовпців.
-- Якщо таблиця створюється вперше з BIGSERIAL, ці ALTER TABLE будуть пропущені.
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
ALTER TABLE users ADD COLUMN IF NOT EXISTS current_feed_id INT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_premium BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_expires_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS level INT DEFAULT 1;
ALTER TABLE users ADD COLUMN IF NOT EXISTS badges JSONB DEFAULT '[]'::JSONB;
ALTER TABLE users ADD COLUMN IF NOT EXISTS inviter_id BIGINT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255) UNIQUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS view_mode VARCHAR(50) DEFAULT 'detailed';
ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_id BIGINT UNIQUE;


-- Додавання/оновлення таблиці custom_feeds, якщо її немає або потрібно оновити
CREATE TABLE IF NOT EXISTS custom_feeds (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id), -- Змінено на BIGINT
    feed_name TEXT NOT NULL,
    filters JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, feed_name)
);

-- Додавання/оновлення таблиці news
CREATE TABLE IF NOT EXISTS news (
    id SERIAL PRIMARY KEY,
    source_id INT REFERENCES sources(id),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source_url TEXT UNIQUE NOT NULL,
    image_url TEXT,
    ai_summary TEXT,
    ai_classified_topics JSONB,
    published_at TIMESTAMP WITH TIME ZONE NOT NULL,
    moderation_status VARCHAR(50) DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
    expires_at TIMESTAMP WITH TIME ZONE
);

-- Додавання/оновлення таблиці sources
CREATE TABLE IF NOT EXISTS sources (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id), -- Змінено на BIGINT
    source_name VARCHAR(255) NOT NULL,
    source_url TEXT UNIQUE NOT NULL,
    source_type VARCHAR(50) NOT NULL, -- 'rss', 'web', 'telegram', 'social_media'
    status VARCHAR(50) DEFAULT 'active', -- 'active', 'inactive', 'blocked'
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_parsed TIMESTAMP,
    parse_frequency INTERVAL DEFAULT '1 hour'
);

-- Додавання/оновлення таблиці user_news_views
CREATE TABLE IF NOT EXISTS user_news_views (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id), -- Змінено на BIGINT
    news_id INT REFERENCES news(id),
    viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, news_id)
);

-- Додавання/оновлення таблиці user_stats
CREATE TABLE IF NOT EXISTS user_stats (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id) UNIQUE, -- Змінено на BIGINT
    news_read_count INT DEFAULT 0,
    comments_count INT DEFAULT 0,
    reports_count INT DEFAULT 0,
    last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    viewed_topics JSONB DEFAULT '[]'::JSONB,
    favorite_sources JSONB DEFAULT '[]'::JSONB
);

-- Додавання/оновлення таблиці comments
CREATE TABLE IF NOT EXISTS comments (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id), -- Змінено на BIGINT
    news_id INT REFERENCES news(id),
    parent_comment_id INT REFERENCES comments(id),
    comment_text TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    moderation_status VARCHAR(50) DEFAULT 'pending'
);

-- Додавання/оновлення таблиці reports
CREATE TABLE IF NOT EXISTS reports (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id), -- Змінено на BIGINT
    target_type VARCHAR(50) NOT NULL, -- 'news', 'comment', 'user', 'source'
    target_id BIGINT NOT NULL, -- Змінено на BIGINT, оскільки може бути ID користувача або новини
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(50) DEFAULT 'pending' -- 'pending', 'resolved', 'rejected'
);

-- Додавання/оновлення таблиці feedback
CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id), -- Змінено на BIGINT
    feedback_text TEXT NOT NULL,
    rating INT, -- 1-5
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(50) DEFAULT 'new' -- 'new', 'reviewed', 'resolved'
);

-- Додавання/оновлення таблиці summaries
CREATE TABLE IF NOT EXISTS summaries (
    id SERIAL PRIMARY KEY,
    news_id INT REFERENCES news(id) UNIQUE,
    summary_text TEXT NOT NULL,
    generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    model_used VARCHAR(100)
);

-- Додавання/оновлення таблиці blocks
CREATE TABLE IF NOT EXISTS blocks (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id), -- Змінено на BIGINT
    block_type VARCHAR(50) NOT NULL, -- 'source', 'topic', 'keyword', 'user'
    value TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, block_type, value)
);

-- Додавання/оновлення таблиці bookmarks
CREATE TABLE IF NOT EXISTS bookmarks (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id), -- Змінено на BIGINT
    news_id INT REFERENCES news(id),
    bookmarked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, news_id)
);

-- Додавання/оновлення таблиці invites
CREATE TABLE IF NOT EXISTS invites (
    id SERIAL PRIMARY KEY,
    inviter_id BIGINT REFERENCES users(id), -- Змінено на BIGINT
    invite_code VARCHAR(50) UNIQUE NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP WITH TIME ZONE,
    used_by_user_id BIGINT REFERENCES users(id), -- Змінено на BIGINT
    used_at TIMESTAMP WITH TIME ZONE
);

-- Додавання/оновлення таблиці admin_actions
CREATE TABLE IF NOT EXISTS admin_actions (
    id SERIAL PRIMARY KEY,
    admin_user_id BIGINT REFERENCES users(id), -- Змінено на BIGINT
    action_type VARCHAR(100) NOT NULL, -- e.g., 'moderate_news', 'block_user', 'change_user_role'
    target_id BIGINT, -- Змінено на BIGINT, може бути ID новини, користувача, тощо
    details JSONB,
    action_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
