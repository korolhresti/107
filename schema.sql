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
    view_mode VARCHAR(50) DEFAULT 'detailed',
    onboarding_completed BOOLEAN DEFAULT FALSE, -- NEW: Для відстеження онбордингу
    ai_usage_count INT DEFAULT 0, -- NEW: Кількість використань AI функцій
    last_ai_usage TIMESTAMP WITH TIME ZONE, -- NEW: Час останнього використання AI
    total_sources_added INT DEFAULT 0, -- NEW: Кількість доданих джерел
    total_reports_sent INT DEFAULT 0, -- NEW: Кількість надісланих репортів
    total_ai_feedback_given INT DEFAULT 0, -- NEW: Кількість наданих AI фідбеків
    ai_feedback_positive_count INT DEFAULT 0, -- NEW: Кількість позитивних AI фідбеків
    ai_feedback_negative_count INT DEFAULT 0, -- NEW: Кількість негативних AI фідбеків
    news_reactions JSONB DEFAULT '{}'::JSONB -- NEW: Зберігання реакцій користувача на новини (JSONB для гнучкості)
);

-- Додавання відсутніх стовпців до таблиці users
-- (Якщо ці стовпці вже існують, ALTER TABLE ADD COLUMN IF NOT EXISTS просто пропустить їх)
DO $$ BEGIN
    ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarding_completed BOOLEAN DEFAULT FALSE;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_usage_count INT DEFAULT 0;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS last_ai_usage TIMESTAMP WITH TIME ZONE;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS total_sources_added INT DEFAULT 0;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS total_reports_sent INT DEFAULT 0;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS total_ai_feedback_given INT DEFAULT 0;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_feedback_positive_count INT DEFAULT 0;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS ai_feedback_negative_count INT DEFAULT 0;
    ALTER TABLE users ADD COLUMN IF NOT EXISTS news_reactions JSONB DEFAULT '{}'::JSONB;
END $$;


-- Додавання/оновлення таблиці sources
CREATE TABLE IF NOT EXISTS sources (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id), -- Хто додав джерело (може бути NULL для системних)
    source_name VARCHAR(255) NOT NULL,
    source_url VARCHAR(2048) UNIQUE NOT NULL,
    source_type VARCHAR(50) NOT NULL, -- 'web', 'rss', 'telegram', 'social_media'
    status VARCHAR(50) DEFAULT 'active', -- 'active', 'inactive', 'paused'
    added_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_parsed TIMESTAMP WITH TIME ZONE,
    parse_frequency VARCHAR(50) DEFAULT 'hourly' -- 'hourly', 'daily', 'weekly'
);

-- Додавання/оновлення таблиці news
CREATE TABLE IF NOT EXISTS news (
    id SERIAL PRIMARY KEY,
    source_id INT REFERENCES sources(id),
    title TEXT NOT NULL,
    content TEXT,
    source_url VARCHAR(2048) UNIQUE NOT NULL,
    image_url VARCHAR(2048),
    published_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ai_summary TEXT,
    ai_classified_topics JSONB,
    moderation_status VARCHAR(50) DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
    expires_at TIMESTAMP WITH TIME ZONE -- Дата, після якої новина вважається неактуальною
);

-- Додавання/оновлення таблиці user_news_views
CREATE TABLE IF NOT EXISTS user_news_views (
    user_id BIGINT REFERENCES users(id),
    news_id INT REFERENCES news(id),
    viewed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, news_id)
);

-- Додавання/оновлення таблиці blocks
CREATE TABLE IF NOT EXISTS blocks (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    block_type VARCHAR(50) NOT NULL, -- 'keyword', 'source', 'topic'
    value VARCHAR(255) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, block_type, value)
);

-- Додавання/оновлення таблиці bookmarks
CREATE TABLE IF NOT EXISTS bookmarks (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    news_id INT REFERENCES news(id),
    bookmarked_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, news_id)
);

-- Додавання/оновлення таблиці user_stats
CREATE TABLE IF NOT EXISTS user_stats (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id) UNIQUE,
    news_read_count INT DEFAULT 0,
    ai_requests_count INT DEFAULT 0,
    sources_added_count INT DEFAULT 0,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Додавання/оновлення таблиці comments
CREATE TABLE IF NOT EXISTS comments (
    id SERIAL PRIMARY KEY,
    news_id INT REFERENCES news(id),
    user_id BIGINT REFERENCES users(id),
    parent_comment_id INT REFERENCES comments(id), -- Для гілок коментарів
    comment_text TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(50) DEFAULT 'active' -- 'active', 'moderated', 'deleted'
);

-- Додавання/оновлення таблиці reports
CREATE TABLE IF NOT EXISTS reports (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    target_type VARCHAR(50) NOT NULL, -- 'news', 'comment', 'user'
    target_id INT, -- ID новини, коментаря або користувача
    reason TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(50) DEFAULT 'pending' -- 'pending', 'resolved', 'rejected'
);

-- Додавання/оновлення таблиці feedback
CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    feedback_text TEXT NOT NULL,
    rating INT, -- Наприклад, від 1 до 5
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    status VARCHAR(50) DEFAULT 'new' -- 'new', 'reviewed', 'archived'
);

-- Додавання/оновлення таблиці invitations
CREATE TABLE IF NOT EXISTS invitations (
    id SERIAL PRIMARY KEY,
    inviter_user_id BIGINT REFERENCES users(id),
    invite_code VARCHAR(255) UNIQUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    used_at TIMESTAMP WITH TIME ZONE,
    status VARCHAR(50) DEFAULT 'pending', -- 'pending', 'accepted', 'rejected'
    invitee_telegram_id BIGINT -- Telegram ID користувача, який використав запрошення
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

-- NEW: Таблиця для зберігання фідбеку по AI відповідям
CREATE TABLE IF NOT EXISTS ai_feedback (
    id SERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    news_id INT REFERENCES news(id), -- Може бути NULL для вільних AI запитів
    ai_function VARCHAR(255) NOT NULL, -- Назва функції (наприклад, 'summary', 'translate', 'ask_ai')
    rating BOOLEAN NOT NULL, -- TRUE для 👍, FALSE для 👎
    feedback_text TEXT, -- Додатковий коментар
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
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
CREATE INDEX IF NOT EXISTS idx_invitations_inviter_user_id ON invitations (inviter_user_id);
CREATE INDEX IF NOT EXISTS idx_ai_feedback_user_id ON ai_feedback (user_id);
CREATE INDEX IF NOT EXISTS idx_ai_feedback_news_id ON ai_feedback (news_id);
