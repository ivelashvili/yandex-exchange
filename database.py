"""
Модуль для работы с базой данных
Поддерживает PostgreSQL (через asyncpg) и SQLite (через aiosqlite)
"""
import json
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import asyncio

# Импорты для PostgreSQL
try:
    import asyncpg
    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False

# Импорты для SQLite
try:
    import aiosqlite
    HAS_AIOSQLITE = True
except ImportError:
    HAS_AIOSQLITE = False

# Импорты для синхронного SQLite (fallback)
import sqlite3

from db_config import (
    is_postgresql, is_sqlite, get_postgresql_dsn, get_sqlite_path,
    DB_POOL_MIN_SIZE, DB_POOL_MAX_SIZE, DATABASE_URL
)
from logging_config import database_logger

# Глобальный пул соединений для PostgreSQL
_pg_pool: Optional[asyncpg.Pool] = None

# ========== ИНИЦИАЛИЗАЦИЯ ==========

async def init_pool():
    """Инициализировать пул соединений для PostgreSQL"""
    global _pg_pool
    if is_postgresql() and HAS_ASYNCPG:
        if _pg_pool is None:
            try:
                dsn = get_postgresql_dsn()
                _pg_pool = await asyncpg.create_pool(
                    dsn,
                    min_size=DB_POOL_MIN_SIZE,
                    max_size=DB_POOL_MAX_SIZE,
                    command_timeout=60
                )
                database_logger.info(f"Пул соединений PostgreSQL инициализирован (min={DB_POOL_MIN_SIZE}, max={DB_POOL_MAX_SIZE})")
            except Exception as e:
                database_logger.error(f"Ошибка инициализации пула PostgreSQL: {str(e)}", exc_info=True)
                raise
    return _pg_pool

async def close_pool():
    """Закрыть пул соединений"""
    global _pg_pool
    if _pg_pool:
        try:
            await _pg_pool.close()
            database_logger.info("Пул соединений PostgreSQL закрыт")
        except Exception as e:
            database_logger.error(f"Ошибка при закрытии пула PostgreSQL: {str(e)}", exc_info=True)
        finally:
            _pg_pool = None

def get_pool() -> Optional[asyncpg.Pool]:
    """Получить пул соединений (для синхронного доступа через asyncio.run)"""
    return _pg_pool

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

def _convert_row_to_dict(row) -> Dict:
    """Преобразовать строку БД в словарь"""
    if isinstance(row, dict):
        return row
    elif hasattr(row, '_mapping'):  # asyncpg Record
        return dict(row)
    elif hasattr(row, 'keys'):  # sqlite3.Row
        return {key: row[key] for key in row.keys()}
    else:
        return dict(row)

# ========== МИГРАЦИИ: переход между раундами ==========

async def _pg_ensure_round_transition_columns(conn) -> None:
    await conn.execute(
        "ALTER TABLE games ADD COLUMN IF NOT EXISTS round_trading_locked BOOLEAN NOT NULL DEFAULT FALSE"
    )
    await conn.execute(
        "ALTER TABLE players ADD COLUMN IF NOT EXISTS ready_next_round BOOLEAN NOT NULL DEFAULT FALSE"
    )
    await conn.execute(
        "ALTER TABLE players ADD COLUMN IF NOT EXISTS ready_next_round_for INTEGER"
    )


async def _sqlite_ensure_round_transition_columns(conn) -> None:
    for table, col, col_type in (
        ("games", "round_trading_locked", "INTEGER NOT NULL DEFAULT 0"),
        ("players", "ready_next_round", "INTEGER NOT NULL DEFAULT 0"),
        ("players", "ready_next_round_for", "INTEGER"),
    ):
        cursor = await conn.execute(f"PRAGMA table_info({table})")
        existing = [row[1] for row in await cursor.fetchall()]
        if col not in existing:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


async def _pg_ensure_round_settings_text_columns(conn) -> None:
    await conn.execute(
        "ALTER TABLE round_settings ADD COLUMN IF NOT EXISTS resource_texts JSONB"
    )
    await conn.execute(
        "ALTER TABLE round_settings ADD COLUMN IF NOT EXISTS building_texts JSONB"
    )


async def _sqlite_ensure_round_settings_text_columns(conn) -> None:
    cursor = await conn.execute("PRAGMA table_info(round_settings)")
    existing = [row[1] for row in await cursor.fetchall()]
    for col in ("resource_texts", "building_texts"):
        if col not in existing:
            await conn.execute(f"ALTER TABLE round_settings ADD COLUMN {col} TEXT")


async def ensure_round_settings_text_columns() -> None:
    """Миграция: комментарии к коэффициентам в round_settings."""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await _pg_ensure_round_settings_text_columns(conn)
    elif is_sqlite():
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await _sqlite_ensure_round_settings_text_columns(conn)
            await conn.commit()


async def set_game_round_trading_locked(game_id: int, locked: bool) -> None:
    flag = bool(locked)
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE games SET round_trading_locked = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2",
                flag,
                game_id,
            )
    else:
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "UPDATE games SET round_trading_locked = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (1 if flag else 0, game_id),
            )
            await conn.commit()


async def set_player_ready_next_round(
    player_id: str, ready: bool, for_round: Optional[int] = None
) -> None:
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE players SET ready_next_round = $1, ready_next_round_for = $2
                WHERE id = $3
                """,
                bool(ready),
                for_round,
                player_id,
            )
    else:
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                """
                UPDATE players SET ready_next_round = ?, ready_next_round_for = ?
                WHERE id = ?
                """,
                (1 if ready else 0, for_round, player_id),
            )
            await conn.commit()


async def clear_all_players_ready_next_round(game_id: int) -> None:
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE players SET ready_next_round = FALSE, ready_next_round_for = NULL
                WHERE game_id = $1
                """,
                game_id,
            )
    else:
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                """
                UPDATE players SET ready_next_round = 0, ready_next_round_for = NULL
                WHERE game_id = ?
                """,
                (game_id,),
            )
            await conn.commit()


# ========== POSTGRESQL ФУНКЦИИ ==========

async def _pg_init_database():
    """Инициализация PostgreSQL - создание всех таблиц"""
    pool = await init_pool()
    if not pool:
        return
    
    async with pool.acquire() as conn:
        # Таблица игр
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id SERIAL PRIMARY KEY,
                num_players INTEGER NOT NULL,
                current_round INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                game_code VARCHAR(6) UNIQUE,
                company_name VARCHAR(255),
                description TEXT,
                config_data JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Таблица игроков
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id TEXT PRIMARY KEY,
                game_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                character_name TEXT,
                character_image TEXT,
                money REAL NOT NULL DEFAULT 2500,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
            )
        """)
        
        # Таблица ресурсов игроков
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS player_resources (
                player_id TEXT NOT NULL,
                resource_name TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (player_id, resource_name),
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
            )
        """)
        
        # Таблица объектов игроков
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS buildings (
                id TEXT PRIMARY KEY,
                player_id TEXT NOT NULL,
                game_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                started_round INTEGER NOT NULL,
                completed_round INTEGER NOT NULL,
                status TEXT NOT NULL,
                sale_round INTEGER,
                sale_price REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
            )
        """)
        
        # Таблица цен на ресурсы (история)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS resource_prices (
                id SERIAL PRIMARY KEY,
                game_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                resource_name TEXT NOT NULL,
                price REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                UNIQUE(game_id, round_number, resource_name)
            )
        """)
        
        # Таблица текущих цен
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS current_prices (
                game_id INTEGER NOT NULL,
                resource_name TEXT NOT NULL,
                price REAL NOT NULL,
                PRIMARY KEY (game_id, resource_name),
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
            )
        """)
        
        # Таблица событий раундов
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS round_events (
                id SERIAL PRIMARY KEY,
                game_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                event_text TEXT,
                positive_event TEXT,
                negative_event TEXT,
                positive2_event TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                UNIQUE(game_id, round_number)
            )
        """)
        # Миграция: добавить колонки, если таблица уже существовала без них
        for col in ("positive_event", "negative_event", "positive2_event"):
            await conn.execute(f"ALTER TABLE round_events ADD COLUMN IF NOT EXISTS {col} TEXT")
        await conn.execute("ALTER TABLE round_events ADD COLUMN IF NOT EXISTS custom_event_text TEXT")
        await conn.execute("ALTER TABLE round_events ADD COLUMN IF NOT EXISTS custom_event_image_url TEXT")
        await conn.execute("ALTER TABLE round_events ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        
        # Таблица действий игроков
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS player_actions (
                id SERIAL PRIMARY KEY,
                game_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                resource_name TEXT NOT NULL,
                amount INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
            )
        """)
        
        # Таблица версии игры
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS game_version (
                game_id INTEGER PRIMARY KEY,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE
            )
        """)
        
        # Таблица снимков состояния игры
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS game_snapshots (
                id SERIAL PRIMARY KEY,
                game_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                snapshot_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                UNIQUE(game_id, round_number)
            )
        """)
        
        # Индексы
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_players_game_id ON players(game_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_buildings_player_id ON buildings(player_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_buildings_game_id ON buildings(game_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_resource_prices_game_round ON resource_prices(game_id, round_number)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_player_actions_game_round ON player_actions(game_id, round_number)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_game_snapshots_game_round ON game_snapshots(game_id, round_number)")
        
        
        # Таблица контента для раундов
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS round_content (
                id SERIAL PRIMARY KEY,
                game_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                content_url TEXT,
                content_type TEXT DEFAULT 'video',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                UNIQUE(game_id, round_number)
            )
        """)
        
        # Таблица настроек раундов (коэффициенты)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS round_settings (
                id SERIAL PRIMARY KEY,
                game_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                resource_modifiers JSONB,
                building_modifiers JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                UNIQUE(game_id, round_number)
            )
        """)
        
        # Таблица персонажей игры (настраиваются в админке, показываются в мини-аппе)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS game_characters (
                id SERIAL PRIMARY KEY,
                game_id INTEGER NOT NULL,
                character_name VARCHAR(100) NOT NULL,
                character_image TEXT,
                character_description TEXT,
                is_available BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                UNIQUE(game_id, character_name)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_game_characters_game_id ON game_characters(game_id)")

        # Дополнительные индексы для новых таблиц
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_games_game_code ON games(game_code)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_round_content_game_round ON round_content(game_id, round_number)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_round_settings_game_round ON round_settings(game_id, round_number)")

        # События ВЕБ (проектор): текст, картинки, подсветка ресурсов/объектов
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS round_web_events (
                id SERIAL PRIMARY KEY,
                game_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                event_text TEXT,
                image_url TEXT,
                bg_image_url TEXT,
                highlight_resources JSONB DEFAULT '[]',
                highlight_buildings JSONB DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                UNIQUE(game_id, round_number)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_round_web_events_game_round ON round_web_events(game_id, round_number)"
        )

        await _pg_ensure_round_transition_columns(conn)

# ========== SQLITE ФУНКЦИИ (ASYNC) ==========

async def _sqlite_init_database():
    """Инициализация SQLite - создание всех таблиц"""
    if not is_sqlite():
        return
    
    db_path = get_sqlite_path()
    async with aiosqlite.connect(db_path) as conn:
        # Таблица игр
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                num_players INTEGER NOT NULL,
                current_round INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                game_code VARCHAR(6) UNIQUE,
                company_name VARCHAR(255),
                description TEXT,
                config_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Таблица игроков
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id TEXT PRIMARY KEY,
                game_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                character_name TEXT,
                character_image TEXT,
                money REAL NOT NULL DEFAULT 2500,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id)
            )
        """)
        
        # Таблица ресурсов игроков
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS player_resources (
                player_id TEXT NOT NULL,
                resource_name TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (player_id, resource_name),
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE
            )
        """)
        
        # Таблица объектов игроков
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS buildings (
                id TEXT PRIMARY KEY,
                player_id TEXT NOT NULL,
                game_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                started_round INTEGER NOT NULL,
                completed_round INTEGER NOT NULL,
                status TEXT NOT NULL,
                sale_round INTEGER,
                sale_price REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (player_id) REFERENCES players(id) ON DELETE CASCADE,
                FOREIGN KEY (game_id) REFERENCES games(id)
            )
        """)
        
        # Таблица цен на ресурсы (история)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS resource_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                resource_name TEXT NOT NULL,
                price REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id),
                UNIQUE(game_id, round_number, resource_name)
            )
        """)
        
        # Таблица текущих цен
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS current_prices (
                game_id INTEGER NOT NULL,
                resource_name TEXT NOT NULL,
                price REAL NOT NULL,
                PRIMARY KEY (game_id, resource_name),
                FOREIGN KEY (game_id) REFERENCES games(id)
            )
        """)
        
        # Таблица событий раундов
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS round_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                event_text TEXT,
                positive_event TEXT,
                negative_event TEXT,
                positive2_event TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id),
                UNIQUE(game_id, round_number)
            )
        """)
        # Миграция для существующей таблицы: добавить колонки, если их нет (SQLite не поддерживает ADD COLUMN IF NOT EXISTS)
        cursor = await conn.execute("PRAGMA table_info(round_events)")
        existing_cols = [row[1] for row in await cursor.fetchall()]
        for col in ("positive_event", "negative_event", "positive2_event"):
            if col not in existing_cols:
                await conn.execute(f"ALTER TABLE round_events ADD COLUMN {col} TEXT")
        for col in ("custom_event_text", "custom_event_image_url", "updated_at"):
            if col not in existing_cols:
                await conn.execute(f"ALTER TABLE round_events ADD COLUMN {col} TEXT")
        
        # Таблица действий игроков
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS player_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                resource_name TEXT NOT NULL,
                amount INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id),
                FOREIGN KEY (player_id) REFERENCES players(id)
            )
        """)
        
        # Таблица версии игры
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS game_version (
                game_id INTEGER PRIMARY KEY,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id)
            )
        """)
        
        # Таблица снимков состояния игры
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS game_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                snapshot_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id),
                UNIQUE(game_id, round_number)
            )
        """)
        
        
        
        # Индексы
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_players_game_id ON players(game_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_buildings_player_id ON buildings(player_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_buildings_game_id ON buildings(game_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_resource_prices_game_round ON resource_prices(game_id, round_number)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_player_actions_game_round ON player_actions(game_id, round_number)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_game_snapshots_game_round ON game_snapshots(game_id, round_number)")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_games_game_code ON games(game_code)")
        
        # Таблица контента для раундов
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS round_content (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                content_url TEXT,
                content_type TEXT DEFAULT 'video',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                UNIQUE(game_id, round_number)
            )
        """)
        
        # Таблица настроек раундов (коэффициенты)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS round_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                resource_modifiers TEXT,
                building_modifiers TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                UNIQUE(game_id, round_number)
            )
        """)
        
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_round_content_game_round ON round_content(game_id, round_number)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_round_settings_game_round ON round_settings(game_id, round_number)")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS round_web_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                event_text TEXT,
                image_url TEXT,
                bg_image_url TEXT,
                highlight_resources TEXT DEFAULT '[]',
                highlight_buildings TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                UNIQUE(game_id, round_number)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_round_web_events_game_round ON round_web_events(game_id, round_number)"
        )

        # Таблица персонажей игры (настраиваются в админке, показываются в мини-аппе)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS game_characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL,
                character_name VARCHAR(100) NOT NULL,
                character_image TEXT,
                character_description TEXT,
                is_available BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (game_id) REFERENCES games(id) ON DELETE CASCADE,
                UNIQUE(game_id, character_name)
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_game_characters_game_id ON game_characters(game_id)")

        await _sqlite_ensure_round_transition_columns(conn)

        await conn.commit()

# ========== ОБЩИЕ ФУНКЦИИ ==========

async def init_database():
    """Инициализация базы данных - создание всех таблиц"""
    if is_postgresql():
        await _pg_init_database()
    elif is_sqlite():
        await _sqlite_init_database()
    else:
        raise ValueError(f"Unsupported database type: {DATABASE_URL}")
    await ensure_round_settings_text_columns()

async def create_game(num_players: int, company_name: Optional[str] = None) -> int:
    """Создать новую игру в БД (с автоматической генерацией кода)"""
    # Используем новую функцию create_game_with_code для обратной совместимости
    return await create_game_with_code(num_players, company_name=company_name)

async def get_active_game_id() -> Optional[int]:
    """Получить ID активной игры"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            game_id = await conn.fetchval("""
                SELECT id FROM games 
                WHERE status = 'active' 
                ORDER BY id DESC LIMIT 1
            """)
            return game_id
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
                SELECT id FROM games 
                WHERE status = 'active' 
                ORDER BY id DESC LIMIT 1
            """)
            row = await cursor.fetchone()
        return row['id'] if row else None

async def save_game_state(game_id: int, current_round: int):
    """Сохранить состояние игры"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    UPDATE games 
                    SET current_round = $1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = $2
                """, current_round, game_id)
                
                await conn.execute("""
                    UPDATE game_version 
                    SET version = version + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE game_id = $1
                """, game_id)
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("""
            UPDATE games 
            SET current_round = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (current_round, game_id))
        
            await conn.execute("""
            UPDATE game_version 
            SET version = version + 1, updated_at = CURRENT_TIMESTAMP
            WHERE game_id = ?
        """, (game_id,))
            await conn.commit()

async def save_player(player_id: str, game_id: int, name: str, 
                     character_name: Optional[str] = None, 
                     character_image: Optional[str] = None, 
                     money: float = 2500):
    """Сохранить/обновить игрока"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO players 
                (id, game_id, name, character_name, character_image, money)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (id) DO UPDATE SET
                    game_id = EXCLUDED.game_id,
                    name = EXCLUDED.name,
                    character_name = EXCLUDED.character_name,
                    character_image = EXCLUDED.character_image,
                    money = EXCLUDED.money
            """, player_id, game_id, name, character_name, character_image, money)
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("""
            INSERT OR REPLACE INTO players 
            (id, game_id, name, character_name, character_image, money)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (player_id, game_id, name, character_name, character_image, money))
            await conn.commit()

async def save_player_resources(player_id: str, resources: Dict[str, int]):
    """Сохранить ресурсы игрока"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    DELETE FROM player_resources WHERE player_id = $1
                """, player_id)
                
                for resource_name, amount in resources.items():
                    if amount > 0:
                        await conn.execute("""
                            INSERT INTO player_resources (player_id, resource_name, amount)
                            VALUES ($1, $2, $3)
                        """, player_id, resource_name, amount)
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("""
                DELETE FROM player_resources WHERE player_id = ?
            """, (player_id,))
            
            for resource_name, amount in resources.items():
                if amount > 0:
                    await conn.execute("""
                    INSERT INTO player_resources (player_id, resource_name, amount)
                    VALUES (?, ?, ?)
                """, (player_id, resource_name, amount))
            await conn.commit()

async def save_building(building_id: str, player_id: str, game_id: int, name: str, 
                  started_round: int, completed_round: int, status: str,
                  sale_round: Optional[int] = None, sale_price: Optional[float] = None):
    """Сохранить/обновить объект"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO buildings 
                (id, player_id, game_id, name, started_round, completed_round, status, sale_round, sale_price)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (id) DO UPDATE SET
                    player_id = EXCLUDED.player_id,
                    game_id = EXCLUDED.game_id,
                    name = EXCLUDED.name,
                    started_round = EXCLUDED.started_round,
                    completed_round = EXCLUDED.completed_round,
                    status = EXCLUDED.status,
                    sale_round = EXCLUDED.sale_round,
                    sale_price = EXCLUDED.sale_price
            """, building_id, player_id, game_id, name, started_round, completed_round, 
                status, sale_round, sale_price)
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("""
            INSERT OR REPLACE INTO buildings 
            (id, player_id, game_id, name, started_round, completed_round, status, sale_round, sale_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (building_id, player_id, game_id, name, started_round, completed_round, 
                  status, sale_round, sale_price))
            await conn.commit()

async def delete_building(building_id: str):
    """Удалить объект"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM buildings WHERE id = $1", building_id)
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("DELETE FROM buildings WHERE id = ?", (building_id,))
            await conn.commit()

async def save_current_prices(game_id: int, prices: Dict[str, float]):
    """Сохранить текущие цены"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    DELETE FROM current_prices WHERE game_id = $1
                """, game_id)
                
                for resource_name, price in prices.items():
                    await conn.execute("""
                        INSERT INTO current_prices (game_id, resource_name, price)
                        VALUES ($1, $2, $3)
                    """, game_id, resource_name, price)
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("""
                DELETE FROM current_prices WHERE game_id = ?
            """, (game_id,))
            
            for resource_name, price in prices.items():
                await conn.execute("""
                INSERT INTO current_prices (game_id, resource_name, price)
                VALUES (?, ?, ?)
            """, (game_id, resource_name, price))
            await conn.commit()

async def save_round_prices(game_id: int, round_number: int, prices: Dict[str, float]):
    """Сохранить цены для конкретного раунда (история)"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            for resource_name, price in prices.items():
                await conn.execute("""
                    INSERT INTO resource_prices 
                    (game_id, round_number, resource_name, price)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (game_id, round_number, resource_name) 
                    DO UPDATE SET price = EXCLUDED.price
                """, game_id, round_number, resource_name, price)
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            for resource_name, price in prices.items():
                await conn.execute("""
                INSERT OR REPLACE INTO resource_prices 
                (game_id, round_number, resource_name, price)
                VALUES (?, ?, ?, ?)
            """, (game_id, round_number, resource_name, price))
            await conn.commit()

async def save_round_events(game_id: int, round_number: int, event_text: Optional[str] = None):
    """Сохранить события раунда"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO round_events 
                (game_id, round_number, event_text)
                VALUES ($1, $2, $3)
                ON CONFLICT (game_id, round_number) 
                DO UPDATE SET
                    event_text = EXCLUDED.event_text
            """, game_id, round_number, event_text)
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("""
            INSERT OR REPLACE INTO round_events 
            (game_id, round_number, event_text)
            VALUES (?, ?, ?)
        """, (game_id, round_number, event_text))
            await conn.commit()

async def save_player_action(game_id: int, round_number: int, player_id: str, 
                       action_type: str, resource_name: str, amount: int):
    """Сохранить действие игрока"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO player_actions 
                (game_id, round_number, player_id, action_type, resource_name, amount)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, game_id, round_number, player_id, action_type, resource_name, amount)
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("""
            INSERT INTO player_actions 
            (game_id, round_number, player_id, action_type, resource_name, amount)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (game_id, round_number, player_id, action_type, resource_name, amount))
            await conn.commit()

async def clear_round_actions(game_id: int, round_number: int):
    """Очистить действия для раунда"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                DELETE FROM player_actions 
                WHERE game_id = $1 AND round_number = $2
            """, game_id, round_number)
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("""
            DELETE FROM player_actions 
            WHERE game_id = ? AND round_number = ?
        """, (game_id, round_number))
            await conn.commit()


async def clear_rounds_after(game_id: int, target_round: int):
    """Удалить из БД все данные по раундам с номером больше target_round.
    Используется при откате игры к снимку (например к раунду 0)."""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM resource_prices WHERE game_id = $1 AND round_number > $2",
                game_id, target_round
            )
            await conn.execute(
                "DELETE FROM round_events WHERE game_id = $1 AND round_number > $2",
                game_id, target_round
            )
            await conn.execute(
                "DELETE FROM player_actions WHERE game_id = $1 AND round_number > $2",
                game_id, target_round
            )
            await conn.execute(
                "DELETE FROM game_snapshots WHERE game_id = $1 AND round_number > $2",
                game_id, target_round
            )
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                "DELETE FROM resource_prices WHERE game_id = ? AND round_number > ?",
                (game_id, target_round)
            )
            await conn.execute(
                "DELETE FROM round_events WHERE game_id = ? AND round_number > ?",
                (game_id, target_round)
            )
            await conn.execute(
                "DELETE FROM player_actions WHERE game_id = ? AND round_number > ?",
                (game_id, target_round)
            )
            await conn.execute(
                "DELETE FROM game_snapshots WHERE game_id = ? AND round_number > ?",
                (game_id, target_round)
            )
            await conn.commit()


# ========== МЕТОДЫ ЗАГРУЗКИ ==========

async def load_game(game_id: int) -> Optional[Dict]:
    """Загрузить информацию об игре"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM games WHERE id = $1", game_id)
            if row:
                return _convert_row_to_dict(row)
            return None
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM games WHERE id = ?", (game_id,))
            row = await cursor.fetchone()
            if row:
                return _convert_row_to_dict(row)
            return None

async def load_all_players(game_id: int) -> List[Dict]:
    """Загрузить всех игроков игры"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM players WHERE game_id = $1", game_id)
            return [_convert_row_to_dict(row) for row in rows]
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM players WHERE game_id = ?", (game_id,))
            rows = await cursor.fetchall()
            return [_convert_row_to_dict(row) for row in rows]

async def load_player_resources(player_id: str) -> Dict[str, int]:
    """Загрузить ресурсы игрока"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT resource_name, amount 
                FROM player_resources 
                WHERE player_id = $1
            """, player_id)
            return {row['resource_name']: row['amount'] for row in rows}
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
                SELECT resource_name, amount 
                FROM player_resources 
                WHERE player_id = ?
            """, (player_id,))
            rows = await cursor.fetchall()
            return {row['resource_name']: row['amount'] for row in rows}

async def load_player_buildings(player_id: str, game_id: int) -> List[Dict]:
    """Загрузить объекты игрока для конкретной игры"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM buildings 
                WHERE player_id = $1 AND game_id = $2
            """, player_id, game_id)
            return [_convert_row_to_dict(row) for row in rows]
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
                SELECT * FROM buildings 
                WHERE player_id = ? AND game_id = ?
            """, (player_id, game_id))
            rows = await cursor.fetchall()
            return [_convert_row_to_dict(row) for row in rows]

async def load_current_prices(game_id: int) -> Dict[str, float]:
    """Загрузить текущие цены"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT resource_name, price 
                FROM current_prices 
                WHERE game_id = $1
            """, game_id)
            return {row['resource_name']: row['price'] for row in rows}
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
                SELECT resource_name, price 
                FROM current_prices 
                WHERE game_id = ?
            """, (game_id,))
            rows = await cursor.fetchall()
            return {row['resource_name']: row['price'] for row in rows}

async def load_round_prices(game_id: int, round_number: int) -> Optional[Dict[str, float]]:
    """Загрузить цены для конкретного раунда"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT resource_name, price 
                FROM resource_prices 
                WHERE game_id = $1 AND round_number = $2
            """, game_id, round_number)
            if rows:
                return {row['resource_name']: row['price'] for row in rows}
            return None
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
            SELECT resource_name, price 
            FROM resource_prices 
            WHERE game_id = ? AND round_number = ?
        """, (game_id, round_number))
            rows = await cursor.fetchall()
            if rows:
                return {row['resource_name']: row['price'] for row in rows}
            return None

async def load_round_events(game_id: int, round_number: int) -> Optional[str]:
    """Загрузить события раунда"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            event_text = await conn.fetchval("""
                SELECT event_text 
                FROM round_events 
                WHERE game_id = $1 AND round_number = $2
            """, game_id, round_number)
            return event_text
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
            SELECT event_text 
            FROM round_events 
            WHERE game_id = ? AND round_number = ?
        """, (game_id, round_number))
            row = await cursor.fetchone()
            if row:
                return row['event_text']
            return None

async def load_round_actions(game_id: int, round_number: int) -> Tuple[Dict[str, int], Dict[str, int]]:
    """Загрузить действия игроков для расчета спроса/предложения"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT action_type, resource_name, COUNT(DISTINCT player_id) as player_count
                FROM player_actions
                WHERE game_id = $1 AND round_number = $2
                GROUP BY action_type, resource_name
            """, game_id, round_number)
            
            players_bought = {}
            players_sold = {}
            
            for row in rows:
                resource = row['resource_name']
                count = row['player_count']
                if row['action_type'] == 'buy':
                    players_bought[resource] = count
                elif row['action_type'] == 'sell':
                    players_sold[resource] = count
            
            return players_bought, players_sold
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
                SELECT action_type, resource_name, COUNT(DISTINCT player_id) as player_count
                FROM player_actions
                WHERE game_id = ? AND round_number = ?
                GROUP BY action_type, resource_name
            """, (game_id, round_number))
            
            players_bought = {}
            players_sold = {}
            
            rows = await cursor.fetchall()
            for row in rows:
                resource = row['resource_name']
                count = row['player_count']
                if row['action_type'] == 'buy':
                    players_bought[resource] = count
                elif row['action_type'] == 'sell':
                    players_sold[resource] = count
            
            return players_bought, players_sold

async def save_game_snapshot(game_id: int, round_number: int, snapshot_data: Dict):
    """Сохранить снимок состояния игры"""
    snapshot_json = json.dumps(snapshot_data, ensure_ascii=False)
    
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO game_snapshots 
                (game_id, round_number, snapshot_data)
                VALUES ($1, $2, $3)
                ON CONFLICT (game_id, round_number) 
                DO UPDATE SET snapshot_data = EXCLUDED.snapshot_data
            """, game_id, round_number, snapshot_json)
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("""
            INSERT OR REPLACE INTO game_snapshots 
            (game_id, round_number, snapshot_data)
            VALUES (?, ?, ?)
            """, (game_id, round_number, snapshot_json))
            await conn.commit()

async def load_game_snapshot(game_id: int, round_number: int) -> Optional[Dict]:
    """Загрузить снимок состояния игры"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT snapshot_data 
                FROM game_snapshots 
                WHERE game_id = $1 AND round_number = $2
            """, game_id, round_number)
            if row:
                return json.loads(row['snapshot_data'])
            return None
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
            SELECT snapshot_data 
            FROM game_snapshots 
            WHERE game_id = ? AND round_number = ?
        """, (game_id, round_number))
            row = await cursor.fetchone()
            if row:
                return json.loads(row['snapshot_data'])
            return None

async def get_available_snapshots(game_id: int) -> List[int]:
    """Получить список доступных раундов со снимками"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT round_number 
                FROM game_snapshots 
                WHERE game_id = $1 
                ORDER BY round_number DESC
            """, game_id)
            return [row['round_number'] for row in rows]
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
            SELECT round_number 
            FROM game_snapshots 
            WHERE game_id = ? 
            ORDER BY round_number DESC
        """, (game_id,))
            rows = await cursor.fetchall()
            return [row['round_number'] for row in rows]

async def get_game_version(game_id: int) -> int:
    """Получить версию игры (для оптимистичных блокировок)"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            version = await conn.fetchval("""
                SELECT version FROM game_version WHERE game_id = $1
            """, game_id)
            return version if version else 1
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
                SELECT version FROM game_version WHERE game_id = ?
            """, (game_id,))
            row = await cursor.fetchone()
            return row['version'] if row else 1

# ========== ФУНКЦИИ ДЛЯ АДМИН-ПАНЕЛИ ==========

async def save_round_content(game_id: int, round_number: int, content_url: str, content_type: str = 'video'):
    """Сохранить контент для раунда"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO round_content (game_id, round_number, content_url, content_type, updated_at)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (game_id, round_number)
                DO UPDATE SET content_url = $3, content_type = $4, updated_at = NOW()
            """, game_id, round_number, content_url, content_type)
    else:
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("""
                INSERT OR REPLACE INTO round_content (game_id, round_number, content_url, content_type, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (game_id, round_number, content_url, content_type))
            await conn.commit()

async def get_round_content(game_id: int, round_number: Optional[int] = None) -> List[Dict]:
    """Получить контент для раунда(ов)"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            if round_number is not None:
                rows = await conn.fetch("""
                    SELECT round_number, content_url, content_type
                    FROM round_content
                    WHERE game_id = $1 AND round_number = $2
                """, game_id, round_number)
            else:
                rows = await conn.fetch("""
                    SELECT round_number, content_url, content_type
                    FROM round_content
                    WHERE game_id = $1
                    ORDER BY round_number
                """, game_id)
            return [dict(row) for row in rows]
    else:
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            if round_number is not None:
                cursor = await conn.execute("""
                    SELECT round_number, content_url, content_type
                    FROM round_content
                    WHERE game_id = ? AND round_number = ?
                """, (game_id, round_number))
            else:
                cursor = await conn.execute("""
                    SELECT round_number, content_url, content_type
                    FROM round_content
                    WHERE game_id = ?
                    ORDER BY round_number
                """, (game_id,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def save_round_event_admin(
    game_id: int,
    round_number: int,
    event_text: Optional[str] = None,
    image_url: Optional[str] = None,
):
    """Текст и картинка из админки (колонки custom_*), не трогает игровой event_text.
    Оба поля пустые — обнуляет только custom_* у существующей строки."""
    t = (event_text or "").strip()
    u = (image_url or "").strip()
    text_val = t if t else None
    url_val = u if u else None

    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            if text_val is None and url_val is None:
                await conn.execute(
                    """
                    UPDATE round_events
                    SET custom_event_text = NULL, custom_event_image_url = NULL, updated_at = NOW()
                    WHERE game_id = $1 AND round_number = $2
                    """,
                    game_id,
                    round_number,
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO round_events (game_id, round_number, custom_event_text, custom_event_image_url, updated_at)
                    VALUES ($1, $2, $3, $4, NOW())
                    ON CONFLICT (game_id, round_number)
                    DO UPDATE SET
                        custom_event_text = EXCLUDED.custom_event_text,
                        custom_event_image_url = EXCLUDED.custom_event_image_url,
                        updated_at = NOW()
                    """,
                    game_id,
                    round_number,
                    text_val,
                    url_val,
                )
    else:
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            if text_val is None and url_val is None:
                await conn.execute(
                    """
                    UPDATE round_events
                    SET custom_event_text = NULL, custom_event_image_url = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE game_id = ? AND round_number = ?
                    """,
                    (game_id, round_number),
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO round_events (game_id, round_number, custom_event_text, custom_event_image_url, updated_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(game_id, round_number) DO UPDATE SET
                        custom_event_text = excluded.custom_event_text,
                        custom_event_image_url = excluded.custom_event_image_url,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (game_id, round_number, text_val, url_val),
                )
            await conn.commit()

async def list_round_events_admin(game_id: int, round_number: Optional[int] = None) -> List[Dict]:
    """События из админки: custom_event_text / custom_event_image_url как event_text / image_url."""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            if round_number is not None:
                rows = await conn.fetch(
                    """
                    SELECT round_number, custom_event_text, custom_event_image_url
                    FROM round_events
                    WHERE game_id = $1 AND round_number = $2
                    ORDER BY round_number
                    """,
                    game_id,
                    round_number,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT round_number, custom_event_text, custom_event_image_url
                    FROM round_events
                    WHERE game_id = $1
                    ORDER BY round_number
                    """,
                    game_id,
                )
            return [
                {
                    "round_number": row["round_number"],
                    "event_text": row["custom_event_text"] or "",
                    "image_url": row["custom_event_image_url"] or "",
                }
                for row in rows
            ]
    else:
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            if round_number is not None:
                cursor = await conn.execute(
                    """
                    SELECT round_number, custom_event_text, custom_event_image_url
                    FROM round_events
                    WHERE game_id = ? AND round_number = ?
                    ORDER BY round_number
                    """,
                    (game_id, round_number),
                )
            else:
                cursor = await conn.execute(
                    """
                    SELECT round_number, custom_event_text, custom_event_image_url
                    FROM round_events
                    WHERE game_id = ?
                    ORDER BY round_number
                    """,
                    (game_id,),
                )
            rows = await cursor.fetchall()
            return [
                {
                    "round_number": row["round_number"],
                    "event_text": row["custom_event_text"] or "",
                    "image_url": row["custom_event_image_url"] or "",
                }
                for row in rows
            ]

def _parse_json_list(value) -> List:
    import json
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


async def save_round_web_event(
    game_id: int,
    round_number: int,
    event_text: Optional[str] = None,
    image_url: Optional[str] = None,
    bg_image_url: Optional[str] = None,
    highlight_resources: Optional[List[str]] = None,
    highlight_buildings: Optional[List[str]] = None,
):
    """Сохранить событие ВЕБ для раунда (проектор)."""
    import json

    t = (event_text or "").strip()
    img = (image_url or "").strip()
    bg = (bg_image_url or "").strip()
    resources = highlight_resources if highlight_resources is not None else []
    buildings = highlight_buildings if highlight_buildings is not None else []
    resources_json = json.dumps(resources, ensure_ascii=False)
    buildings_json = json.dumps(buildings, ensure_ascii=False)

    text_val = t if t else None
    img_val = img if img else None
    bg_val = bg if bg else None

    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO round_web_events (
                    game_id, round_number, event_text, image_url, bg_image_url,
                    highlight_resources, highlight_buildings, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, NOW())
                ON CONFLICT (game_id, round_number)
                DO UPDATE SET
                    event_text = EXCLUDED.event_text,
                    image_url = EXCLUDED.image_url,
                    bg_image_url = EXCLUDED.bg_image_url,
                    highlight_resources = EXCLUDED.highlight_resources,
                    highlight_buildings = EXCLUDED.highlight_buildings,
                    updated_at = NOW()
                """,
                game_id,
                round_number,
                text_val,
                img_val,
                bg_val,
                resources_json,
                buildings_json,
            )
    else:
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute(
                """
                INSERT INTO round_web_events (
                    game_id, round_number, event_text, image_url, bg_image_url,
                    highlight_resources, highlight_buildings, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(game_id, round_number) DO UPDATE SET
                    event_text = excluded.event_text,
                    image_url = excluded.image_url,
                    bg_image_url = excluded.bg_image_url,
                    highlight_resources = excluded.highlight_resources,
                    highlight_buildings = excluded.highlight_buildings,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    game_id,
                    round_number,
                    text_val,
                    img_val,
                    bg_val,
                    resources_json,
                    buildings_json,
                ),
            )
            await conn.commit()


async def list_round_web_events_admin(
    game_id: int, round_number: Optional[int] = None
) -> List[Dict]:
    """Список событий ВЕБ для админки."""
    import json

    def row_to_dict(row) -> Dict:
        return {
            "round_number": row["round_number"],
            "event_text": row["event_text"] or "",
            "image_url": row["image_url"] or "",
            "bg_image_url": row["bg_image_url"] or "",
            "highlight_resources": _parse_json_list(row["highlight_resources"]),
            "highlight_buildings": _parse_json_list(row["highlight_buildings"]),
        }

    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            if round_number is not None:
                rows = await conn.fetch(
                    """
                    SELECT round_number, event_text, image_url, bg_image_url,
                           highlight_resources, highlight_buildings
                    FROM round_web_events
                    WHERE game_id = $1 AND round_number = $2
                    ORDER BY round_number
                    """,
                    game_id,
                    round_number,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT round_number, event_text, image_url, bg_image_url,
                           highlight_resources, highlight_buildings
                    FROM round_web_events
                    WHERE game_id = $1
                    ORDER BY round_number
                    """,
                    game_id,
                )
            return [row_to_dict(row) for row in rows]
    else:
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            if round_number is not None:
                cursor = await conn.execute(
                    """
                    SELECT round_number, event_text, image_url, bg_image_url,
                           highlight_resources, highlight_buildings
                    FROM round_web_events
                    WHERE game_id = ? AND round_number = ?
                    ORDER BY round_number
                    """,
                    (game_id, round_number),
                )
            else:
                cursor = await conn.execute(
                    """
                    SELECT round_number, event_text, image_url, bg_image_url,
                           highlight_resources, highlight_buildings
                    FROM round_web_events
                    WHERE game_id = ?
                    ORDER BY round_number
                    """,
                    (game_id,),
                )
            rows = await cursor.fetchall()
            return [row_to_dict(row) for row in rows]


async def get_round_web_event(game_id: int, round_number: int) -> Optional[Dict]:
    """Одно событие ВЕБ для раунда."""
    rows = await list_round_web_events_admin(game_id, round_number)
    return rows[0] if rows else None


async def save_round_settings(
    game_id: int,
    round_number: int,
    resource_modifiers: Dict,
    building_modifiers: Dict,
    resource_texts: Optional[Dict] = None,
    building_texts: Optional[Dict] = None,
):
    """Сохранить настройки раунда (коэффициенты и комментарии)"""
    import json
    resource_texts = resource_texts if resource_texts is not None else {}
    building_texts = building_texts if building_texts is not None else {}
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO round_settings (
                    game_id, round_number, resource_modifiers, building_modifiers,
                    resource_texts, building_texts, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (game_id, round_number)
                DO UPDATE SET
                    resource_modifiers = EXCLUDED.resource_modifiers,
                    building_modifiers = EXCLUDED.building_modifiers,
                    resource_texts = EXCLUDED.resource_texts,
                    building_texts = EXCLUDED.building_texts,
                    updated_at = NOW()
            """, game_id, round_number,
                json.dumps(resource_modifiers), json.dumps(building_modifiers),
                json.dumps(resource_texts), json.dumps(building_texts))
    else:
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("""
                INSERT OR REPLACE INTO round_settings (
                    game_id, round_number, resource_modifiers, building_modifiers,
                    resource_texts, building_texts, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                game_id, round_number,
                json.dumps(resource_modifiers), json.dumps(building_modifiers),
                json.dumps(resource_texts), json.dumps(building_texts),
            ))
            await conn.commit()

async def get_round_settings(game_id: int, round_number: Optional[int] = None) -> List[Dict]:
    """Получить настройки раунда(ов)"""
    import json
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            if round_number:
                rows = await conn.fetch("""
                    SELECT round_number, resource_modifiers, building_modifiers,
                           resource_texts, building_texts
                    FROM round_settings
                    WHERE game_id = $1 AND round_number = $2
                """, game_id, round_number)
            else:
                rows = await conn.fetch("""
                    SELECT round_number, resource_modifiers, building_modifiers,
                           resource_texts, building_texts
                    FROM round_settings
                    WHERE game_id = $1
                    ORDER BY round_number
                """, game_id)
            result = []
            for row in rows:
                result.append({
                    "round_number": row["round_number"],
                    "resource_modifiers": json.loads(row["resource_modifiers"]) if row["resource_modifiers"] else {},
                    "building_modifiers": json.loads(row["building_modifiers"]) if row["building_modifiers"] else {},
                    "resource_texts": json.loads(row["resource_texts"]) if row["resource_texts"] else {},
                    "building_texts": json.loads(row["building_texts"]) if row["building_texts"] else {},
                })
            return result
    else:
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            if round_number:
                cursor = await conn.execute("""
                    SELECT round_number, resource_modifiers, building_modifiers,
                           resource_texts, building_texts
                    FROM round_settings
                    WHERE game_id = ? AND round_number = ?
                """, (game_id, round_number))
            else:
                cursor = await conn.execute("""
                    SELECT round_number, resource_modifiers, building_modifiers,
                           resource_texts, building_texts
                    FROM round_settings
                    WHERE game_id = ?
                    ORDER BY round_number
                """, (game_id,))
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                result.append({
                    "round_number": row["round_number"],
                    "resource_modifiers": json.loads(row["resource_modifiers"]) if row["resource_modifiers"] else {},
                    "building_modifiers": json.loads(row["building_modifiers"]) if row["building_modifiers"] else {},
                    "resource_texts": json.loads(row["resource_texts"]) if row["resource_texts"] else {},
                    "building_texts": json.loads(row["building_texts"]) if row["building_texts"] else {},
                })
            return result


async def get_game_characters(game_id: int) -> List[Dict]:
    """Получить список персонажей игры (для мини-аппа и админки)"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT character_name, character_image, character_description
                FROM game_characters
                WHERE game_id = $1 AND (is_available IS NULL OR is_available = TRUE)
                ORDER BY character_name
            """, game_id)
            return [
                {
                    "name": row["character_name"],
                    "image": row["character_image"] or "",
                    "description": row["character_description"]
                }
                for row in rows
            ]
    else:
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
                SELECT character_name, character_image, character_description
                FROM game_characters
                WHERE game_id = ? AND (is_available IS NULL OR is_available = 1)
                ORDER BY character_name
            """, (game_id,))
            rows = await cursor.fetchall()
            return [
                {
                    "name": row["character_name"],
                    "image": row["character_image"] or "",
                    "description": row["character_description"]
                }
                for row in rows
            ]


async def save_game_character(
    game_id: int,
    character_name: str,
    character_image: str,
    character_description: Optional[str] = None
):
    """Добавить или обновить персонажа игры"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO game_characters (game_id, character_name, character_image, character_description)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (game_id, character_name)
                DO UPDATE SET character_image = $3, character_description = $4
            """, game_id, character_name, character_image, character_description or None)
    else:
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("""
                INSERT INTO game_characters (game_id, character_name, character_image, character_description)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (game_id, character_name)
                DO UPDATE SET character_image = excluded.character_image, character_description = excluded.character_description
            """, (game_id, character_name, character_image, character_description or None))
            await conn.commit()


async def delete_game_character(game_id: int, character_name: str) -> bool:
    """Удалить персонажа из игры. Возвращает True если запись была удалена."""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("""
                DELETE FROM game_characters
                WHERE game_id = $1 AND character_name = $2
            """, game_id, character_name)
            return result.split()[-1] != "0"
    else:
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute("""
                DELETE FROM game_characters
                WHERE game_id = ? AND character_name = ?
            """, (game_id, character_name))
            await conn.commit()
            return cursor.rowcount > 0


# ========== СИНХРОННЫЕ ОБЕРТКИ ДЛЯ ОБРАТНОЙ СОВМЕСТИМОСТИ ==========
# Эти функции используются в game_engine.py и других синхронных частях кода
# Они создают event loop если его нет, или используют существующий

def _run_async(coro):
    """Запустить async функцию синхронно"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Если loop уже запущен, создаем новый в отдельном потоке
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, coro)
                return future.result()
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        # Нет event loop, создаем новый
        return asyncio.run(coro)

# Синхронные обертки для всех async функций
def create_game_sync(num_players: int) -> int:
    """Синхронная обертка для create_game"""
    return _run_async(create_game(num_players))

def get_active_game_id_sync() -> Optional[int]:
    """Синхронная обертка для get_active_game_id"""
    return _run_async(get_active_game_id())

def save_game_state_sync(game_id: int, current_round: int):
    """Синхронная обертка для save_game_state"""
    return _run_async(save_game_state(game_id, current_round))

def save_player_sync(player_id: str, game_id: int, name: str, 
                    character_name: Optional[str] = None, 
                    character_image: Optional[str] = None, 
                    money: float = 2500):
    """Синхронная обертка для save_player"""
    return _run_async(save_player(player_id, game_id, name, character_name, character_image, money))

def save_player_resources_sync(player_id: str, resources: Dict[str, int]):
    """Синхронная обертка для save_player_resources"""
    return _run_async(save_player_resources(player_id, resources))

def save_building_sync(building_id: str, player_id: str, game_id: int, name: str, 
                       started_round: int, completed_round: int, status: str,
                       sale_round: Optional[int] = None, sale_price: Optional[float] = None):
    """Синхронная обертка для save_building"""
    return _run_async(save_building(building_id, player_id, game_id, name, 
                                   started_round, completed_round, status, sale_round, sale_price))

def delete_building_sync(building_id: str):
    """Синхронная обертка для delete_building"""
    return _run_async(delete_building(building_id))

def save_current_prices_sync(game_id: int, prices: Dict[str, float]):
    """Синхронная обертка для save_current_prices"""
    return _run_async(save_current_prices(game_id, prices))

def save_round_prices_sync(game_id: int, round_number: int, prices: Dict[str, float]):
    """Синхронная обертка для save_round_prices"""
    return _run_async(save_round_prices(game_id, round_number, prices))

def save_round_events_sync(game_id: int, round_number: int, events: Dict):
    """Синхронная обертка для save_round_events"""
    return _run_async(save_round_events(game_id, round_number, events))

def save_player_action_sync(game_id: int, round_number: int, player_id: str, 
                           action_type: str, resource_name: str, amount: int):
    """Синхронная обертка для save_player_action"""
    return _run_async(save_player_action(game_id, round_number, player_id, action_type, resource_name, amount))

def clear_round_actions_sync(game_id: int, round_number: int):
    """Синхронная обертка для clear_round_actions"""
    return _run_async(clear_round_actions(game_id, round_number))

def load_game_sync(game_id: int) -> Optional[Dict]:
    """Синхронная обертка для load_game"""
    return _run_async(load_game(game_id))

def load_all_players_sync(game_id: int) -> List[Dict]:
    """Синхронная обертка для load_all_players"""
    return _run_async(load_all_players(game_id))

def load_player_resources_sync(player_id: str) -> Dict[str, int]:
    """Синхронная обертка для load_player_resources"""
    return _run_async(load_player_resources(player_id))

def load_player_buildings_sync(player_id: str, game_id: int) -> List[Dict]:
    """Синхронная обертка для load_player_buildings"""
    return _run_async(load_player_buildings(player_id, game_id))

def load_current_prices_sync(game_id: int) -> Dict[str, float]:
    """Синхронная обертка для load_current_prices"""
    return _run_async(load_current_prices(game_id))

def load_round_prices_sync(game_id: int, round_number: int) -> Optional[Dict[str, float]]:
    """Синхронная обертка для load_round_prices"""
    return _run_async(load_round_prices(game_id, round_number))

def load_round_events_sync(game_id: int, round_number: int) -> Optional[Dict]:
    """Синхронная обертка для load_round_events"""
    return _run_async(load_round_events(game_id, round_number))

def load_round_actions_sync(game_id: int, round_number: int) -> Tuple[Dict[str, int], Dict[str, int]]:
    """Синхронная обертка для load_round_actions"""
    return _run_async(load_round_actions(game_id, round_number))

def save_game_snapshot_sync(game_id: int, round_number: int, snapshot_data: Dict):
    """Синхронная обертка для save_game_snapshot"""
    return _run_async(save_game_snapshot(game_id, round_number, snapshot_data))

def load_game_snapshot_sync(game_id: int, round_number: int) -> Optional[Dict]:
    """Синхронная обертка для load_game_snapshot"""
    return _run_async(load_game_snapshot(game_id, round_number))

def get_available_snapshots_sync(game_id: int) -> List[int]:
    """Синхронная обертка для get_available_snapshots"""
    return _run_async(get_available_snapshots(game_id))

def get_game_version_sync(game_id: int) -> int:
    """Синхронная обертка для get_game_version"""
    return _run_async(get_game_version(game_id))

def init_database_sync():
    """Синхронная обертка для init_database"""
    return _run_async(init_database())

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С КОДАМИ ИГР ==========

async def generate_game_code() -> str:
    """Генерирует уникальный 6-значный код игры"""
    import random
    max_attempts = 100
    
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            for _ in range(max_attempts):
                code = str(random.randint(100000, 999999))
                exists = await conn.fetchval("""
                    SELECT COUNT(*) FROM games WHERE game_code = $1
                """, code)
                if exists == 0:
                    return code
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            for _ in range(max_attempts):
                code = str(random.randint(100000, 999999))
                cursor = await conn.execute(
                    "SELECT COUNT(*) FROM games WHERE game_code = ?", (code,)
                )
                exists = (await cursor.fetchone())[0]
                if exists == 0:
                    return code
    
    raise Exception("Не удалось сгенерировать уникальный код игры после 100 попыток")

async def game_code_exists(game_code: str) -> bool:
    """Проверяет, существует ли игра с таким кодом"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval("""
                SELECT COUNT(*) FROM games WHERE game_code = $1
            """, game_code)
            return count > 0
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM games WHERE game_code = ?", (game_code,)
            )
            count = (await cursor.fetchone())[0]
            return count > 0

async def get_game_by_code(game_code: str) -> Optional[Dict]:
    """Получить игру по коду"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT id, num_players, current_round, status, game_code, 
                       company_name, description, config_data, created_at, updated_at,
                       round_trading_locked
                FROM games WHERE game_code = $1
            """, game_code)
            if row:
                return dict(row)
            return None
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
                SELECT id, num_players, current_round, status, game_code,
                       company_name, description, config_data, created_at, updated_at,
                       round_trading_locked
                FROM games WHERE game_code = ?
            """, (game_code,))
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None

async def get_all_games() -> List[Dict]:
    """Получить список всех игр"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, num_players, current_round, status, game_code,
                       company_name, description, created_at, updated_at
                FROM games
                ORDER BY created_at DESC
            """)
            return [dict(row) for row in rows]
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("""
                SELECT id, num_players, current_round, status, game_code,
                       company_name, description, created_at, updated_at
                FROM games
                ORDER BY created_at DESC
            """)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

async def create_game_with_code(num_players: int, company_name: Optional[str] = None, 
                                description: Optional[str] = None, 
                                config_data: Optional[Dict] = None) -> tuple:
    """Создать новую игру с автоматической генерацией кода"""
    game_code = await generate_game_code()
    
    if is_postgresql():
        import json
        pool = await init_pool()
        async with pool.acquire() as conn:
            game_id = await conn.fetchval("""
                INSERT INTO games (num_players, current_round, status, game_code, 
                                 company_name, description, config_data)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
            """, num_players, 1, 'active', game_code, company_name, description,
                json.dumps(config_data) if config_data else None)
            
            await conn.execute("""
                INSERT INTO game_version (game_id, version)
                VALUES ($1, 1)
            """, game_id)
            
            return (game_id, game_code)
    else:  # SQLite
        import json
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            cursor = await conn.execute("""
                INSERT INTO games (num_players, current_round, status, game_code,
                                 company_name, description, config_data)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (num_players, 1, 'active', game_code, company_name, description,
                  json.dumps(config_data) if config_data else None))
            game_id = cursor.lastrowid
            
            await conn.execute("""
                INSERT INTO game_version (game_id, version)
                VALUES (?, 1)
            """, (game_id,))
            await conn.commit()
            return (game_id, game_code)

async def update_game_status(game_id: int, status: str):
    """Обновить статус игры"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE games SET status = $1, updated_at = CURRENT_TIMESTAMP
                WHERE id = $2
            """, status, game_id)
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("""
                UPDATE games SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (status, game_id))
            await conn.commit()

async def delete_game(game_id: int):
    """Удалить игру (каскадное удаление через FOREIGN KEY)"""
    if is_postgresql():
        pool = await init_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM games WHERE id = $1", game_id)
    else:  # SQLite
        db_path = get_sqlite_path()
        async with aiosqlite.connect(db_path) as conn:
            await conn.execute("DELETE FROM games WHERE id = ?", (game_id,))
            await conn.commit()



# Синхронные обертки для новых функций
def generate_game_code_sync() -> str:
    """Синхронная обертка для generate_game_code"""
    return _run_async(generate_game_code())

def game_code_exists_sync(game_code: str) -> bool:
    """Синхронная обертка для game_code_exists"""
    return _run_async(game_code_exists(game_code))

def get_game_by_code_sync(game_code: str) -> Optional[Dict]:
    """Синхронная обертка для get_game_by_code"""
    return _run_async(get_game_by_code(game_code))

def get_all_games_sync() -> List[Dict]:
    """Синхронная обертка для get_all_games"""
    return _run_async(get_all_games())

def create_game_with_code_sync(num_players: int, company_name: Optional[str] = None,
                               description: Optional[str] = None,
                               config_data: Optional[Dict] = None) -> int:
    """Синхронная обертка для create_game_with_code"""
    return _run_async(create_game_with_code(num_players, company_name, description, config_data))

def update_game_status_sync(game_id: int, status: str):
    """Синхронная обертка для update_game_status"""
    return _run_async(update_game_status(game_id, status))

def delete_game_sync(game_id: int):
    """Синхронная обертка для delete_game"""
    return _run_async(delete_game(game_id))


async def save_game_config(game_id: int, config_data: Dict):
    """Сохранить конфигурацию игры (ресурсы и объекты)"""
    import json
    try:
        if is_postgresql():
            pool = await init_pool()
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE games 
                    SET config_data = $1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = $2
                """, json.dumps(config_data), game_id)
        else:  # SQLite
            db_path = get_sqlite_path()
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute("""
                    UPDATE games 
                    SET config_data = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (json.dumps(config_data), game_id))
                await conn.commit()
    except Exception as e:
        raise Exception(f"Ошибка сохранения конфигурации игры: {e}")

async def get_game_config(game_id: int) -> Optional[Dict]:
    """Получить конфигурацию игры (ресурсы и объекты)"""
    import json
    try:
        if is_postgresql():
            pool = await init_pool()
            async with pool.acquire() as conn:
                config_json = await conn.fetchval("""
                    SELECT config_data FROM games WHERE id = $1
                """, game_id)
                # NULL — нет строки/колонки; пустой JSON {} — валидный dict (не считать falsy как «нет конфига»)
                if config_json is None:
                    return None
                if isinstance(config_json, str):
                    return json.loads(config_json) if config_json else {}
                if isinstance(config_json, dict):
                    return config_json
                return config_json
        else:  # SQLite
            db_path = get_sqlite_path()
            async with aiosqlite.connect(db_path) as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute("""
                    SELECT config_data FROM games WHERE id = ?
                """, (game_id,)) as cursor:
                    row = await cursor.fetchone()
                    if not row or row['config_data'] is None:
                        return None
                    config_json = row['config_data']
                    if isinstance(config_json, str):
                        return json.loads(config_json) if config_json else {}
                    if isinstance(config_json, dict):
                        return config_json
                    return config_json
    except Exception as e:
        raise Exception(f"Ошибка загрузки конфигурации игры: {e}")


async def get_game_story_id(game_id: int) -> Optional[str]:
    """ID активного сюжета из config_data игры."""
    config = await get_game_config(game_id)
    if not config:
        return None
    story_id = config.get("story_id")
    if isinstance(story_id, str) and story_id.strip():
        return story_id.strip()
    return None


async def set_game_story_id(game_id: int, story_id: Optional[str]) -> None:
    """Сохранить или очистить ID сюжета в config_data игры."""
    config = await get_game_config(game_id) or {}
    if story_id and str(story_id).strip():
        config["story_id"] = str(story_id).strip()
    else:
        config.pop("story_id", None)
    await save_game_config(game_id, config)


async def apply_story_to_game(game_id: int, story: Dict) -> Dict[str, int]:
    """Применить сюжет: настройки раундов, события mini app и события ВЕБ."""
    from game_config import RESOURCE_PRICES, BUILDING_COSTS

    resources = list(RESOURCE_PRICES.keys())
    buildings = list(BUILDING_COSTS.keys())
    story_rounds = story.get("rounds") or {}

    existing_events = {
        row["round_number"]: row for row in await list_round_events_admin(game_id)
    }
    existing_web = {
        row["round_number"]: row for row in await list_round_web_events_admin(game_id)
    }

    rounds_saved = 0
    events_saved = 0
    web_saved = 0

    for round_num in range(1, 11):
        rd = story_rounds.get(str(round_num)) or story_rounds.get(round_num)
        if not rd:
            continue

        resource_modifiers = {}
        resource_texts = {}
        building_modifiers = {}
        building_texts = {}
        for resource in resources:
            item = (rd.get("resources") or {}).get(resource) or {}
            resource_modifiers[resource] = float(item.get("coef", 1.0) or 1.0)
            resource_texts[resource] = str(item.get("comment") or "")
        for building in buildings:
            item = (rd.get("buildings") or {}).get(building) or {}
            building_modifiers[building] = float(item.get("coef", 1.0) or 1.0)
            building_texts[building] = str(item.get("comment") or "")

        await save_round_settings(
            game_id,
            round_num,
            resource_modifiers,
            building_modifiers,
            resource_texts=resource_texts,
            building_texts=building_texts,
        )
        rounds_saved += 1

        label = str(rd.get("event_label") or "").strip()
        if label:
            ev = existing_events.get(round_num, {})
            await save_round_event_admin(
                game_id,
                round_num,
                label,
                ev.get("image_url") or "",
            )
            events_saved += 1

            web = existing_web.get(round_num, {})
            await save_round_web_event(
                game_id,
                round_num,
                label,
                web.get("image_url") or "",
                web.get("bg_image_url") or "",
                list(rd.get("highlight_resources") or []),
                list(rd.get("highlight_buildings") or []),
            )
            web_saved += 1

    return {
        "rounds_saved": rounds_saved,
        "events_saved": events_saved,
        "web_saved": web_saved,
    }


# ПРИМЕЧАНИЕ: Асинхронные функции остаются асинхронными
# Синхронные обертки доступны с суффиксом _sync для обратной совместимости
# Код, использующий await, должен использовать асинхронные версии напрямую
# Код, требующий синхронных вызовов, должен использовать функции с суффиксом _sync