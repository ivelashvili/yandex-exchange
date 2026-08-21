"""
Веб-сервер для отображения игры на проекторе
FastAPI + WebSocket для обновлений в реальном времени
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from typing import Dict, List, Optional
from datetime import datetime, timedelta
import json
import asyncio
import hmac
import hashlib
import base64
import math
from urllib.parse import unquote, parse_qs
from game_engine import (
    Game,
    BuildingStatus,
    parse_building_status,
    Player,
    PROJECTOR_PCT_BASELINES_CONFIG_KEY,
)
from game_config import RESOURCE_PRICES, BUILDING_COSTS, BUILDING_INCOME, STARTING_MONEY
from game_events import EventSystem
import database
from fastapi.responses import JSONResponse
from security.validators import (
    BuyResourceRequest, SellResourceRequest, BuildBuildingRequest, SellBuildingRequest,
    PlayerAuthRequest, CreateGameRequest, SetRoundRequest, RollbackRequest
)
from security.telegram_auth import (
    verify_telegram_init_data, get_player_id_from_init_data, 
    get_user_id_from_init_data, is_valid_telegram_user
)
from logging_config import api_logger, security_logger
import time
import traceback
import os
import re
from monitoring import (
    record_request, record_error, get_metrics, get_health_status,
    update_websocket_connections
)

app = FastAPI(title="Королевская биржа - Веб-интерфейс")

# Глобальный обработчик всех исключений для предотвращения 500 ошибок
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Глобальный обработчик всех исключений"""
    error_type = type(exc).__name__
    error_message = str(exc)
    
    # Логируем ошибку
    api_logger.error(
        f"Необработанное исключение: {error_type}: {error_message}",
        extra={
            "endpoint": request.url.path,
            "method": request.method,
            "error_type": error_type,
            "error_message": error_message,
            "exception": traceback.format_exc()
        },
        exc_info=True
    )
    
    # Записываем метрику ошибки
    record_error(error_type, error_message)
    
    # Возвращаем JSON ответ с ошибкой
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"Внутренняя ошибка сервера: {error_type}",
            "error_type": error_type,
            "message": error_message if os.getenv("DEBUG", "false").lower() == "true" else "Произошла внутренняя ошибка"
        }
    )

# Обработчик ошибок валидации (422)
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Обработчик ошибок валидации Pydantic"""
    import json
    
    # Используем стандартный формат FastAPI, но конвертируем несериализуемые объекты
    errors_list = []
    for error in exc.errors():
        error_dict = {}
        for key, value in error.items():
            if key == 'loc':
                error_dict[key] = list(value) if isinstance(value, (tuple, list)) else [value]
            elif key == 'ctx' and isinstance(value, dict):
                error_dict[key] = {k: str(v) if isinstance(v, (ValueError, Exception)) else v for k, v in value.items()}
            elif isinstance(value, (str, int, float, bool, type(None), list, dict)):
                error_dict[key] = value
            else:
                error_dict[key] = str(value)
        errors_list.append(error_dict)
    
    # Проверяем сериализуемость перед возвратом
    try:
        json.dumps(errors_list)
    except TypeError:
        # Если не удалось сериализовать, используем упрощенный формат
        errors_list = [{"type": str(err.get("type", "unknown")), "loc": str(err.get("loc", [])), "msg": str(err.get("msg", ""))} for err in exc.errors()]
    
    return JSONResponse(
        status_code=422,
        content={"detail": errors_list}
    )

# Глобальные переменные для кэширования (не связаны с конкретной игрой)
previous_leaderboard: List[Dict] = []
previous_leaderboard_ranks: Dict[str, int] = {}  # {player_id: rank} для отслеживания позиций
initial_prices: Dict[str, float] = RESOURCE_PRICES.copy()
player_capitalization_history: Dict[str, Dict] = {}  # {player_id: {"previous": float, "initial": float}}

# WebSocket подключения: {game_code: [WebSocket, ...]}
# Используем None для соединений без game_code (legacy поддержка)
active_connections: Dict[Optional[str], List[WebSocket]] = {}

# ========== HELPER ФУНКЦИИ ДЛЯ РАБОТЫ С ИГРАМИ ==========

async def get_game_by_code(game_code: str, allow_archived: bool = False) -> Game:
    """Загрузить игру по коду из БД. allow_archived=True разрешает загрузку архивированных игр (для read-only, например round-info)."""
    try:
        if not game_code or len(game_code) != 6 or not game_code.isdigit():
            raise HTTPException(status_code=400, detail="Неверный формат кода игры. Код должен состоять из 6 цифр")
        
        try:
            game_data = await database.get_game_by_code(game_code)
        except Exception as e:
            api_logger.error(f"Ошибка БД при получении игры {game_code}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Ошибка базы данных: {str(e)}")
        
        if not game_data:
            raise HTTPException(status_code=404, detail=f"Игра с кодом {game_code} не найдена")
        
        # Проверяем статус игры - архивированные игры недоступны (кроме read-only запросов)
        game_status = game_data.get('status', 'active')
        if game_status == 'archived' and not allow_archived:
            raise HTTPException(
                status_code=403, 
                detail="Игра завершена и перемещена в архив. Новая игра недоступна."
            )
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f"Неожиданная ошибка в get_game_by_code для {game_code}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка: {str(e)}")
    
    # Используем прямые SQL запросы, избегая рекурсии в синхронных обертках
    from db_config import is_postgresql, get_sqlite_path
    import aiosqlite
    from game_engine import (
        Game as GameClass,
        Player,
        Building,
        BuildingStatus,
        parse_building_status,
        PLAYER_CAPITALIZATION_BY_ROUND_ENTRY_KEY,
    )
    from market_dynamics import MarketDynamics
    from game_events import EventSystem
    from game_config import RESOURCE_PRICES
    
    # Создаем игру без load_from_db, чтобы избежать рекурсии
    game = GameClass.__new__(GameClass)  # Создаем объект без вызова __init__
    game.game_id = game_data['id']
    game.num_players = game_data['num_players']
    game.players = []
    game.current_prices = {}
    game.current_round = 1
    game.market = MarketDynamics(game_data['num_players'])
    game.event_system = EventSystem()
    game.previous_round_players_bought = {}
    game.previous_round_players_sold = {}
    game.current_round_players_bought = {}
    game.current_round_players_sold = {}
    game.round_history = []
    game.player_capitalization_by_round_entry = {}
    game.round_trading_locked = bool(game_data.get("round_trading_locked", False))
    game._initialized = True  # Помечаем как инициализированную, т.к. загружаем напрямую
    game._load_from_db = False
    
    # Конфигурация игры (нужна для buy_resource/sell_resource и др.)
    try:
        config = await database.get_game_config(game.game_id)
        if config:
            game.enabled_resources = config.get("enabled_resources", list(RESOURCE_PRICES.keys()))
            game.enabled_buildings = config.get("enabled_buildings", list(BUILDING_COSTS.keys()))
            game.game_resource_prices = config.get("resource_prices", RESOURCE_PRICES.copy())
            game.game_building_costs = config.get("building_costs", BUILDING_COSTS.copy())
            game.game_building_income = config.get("building_income", BUILDING_INCOME.copy())
        else:
            game.enabled_resources = list(RESOURCE_PRICES.keys())
            game.enabled_buildings = list(BUILDING_COSTS.keys())
            game.game_resource_prices = RESOURCE_PRICES.copy()
            game.game_building_costs = BUILDING_COSTS.copy()
            game.game_building_income = BUILDING_INCOME.copy()
    except Exception as e:
        api_logger.warning(f"Не удалось загрузить конфиг игры {game.game_id}, используем значения по умолчанию: {e}")
        game.enabled_resources = list(RESOURCE_PRICES.keys())
        game.enabled_buildings = list(BUILDING_COSTS.keys())
        game.game_resource_prices = RESOURCE_PRICES.copy()
        game.game_building_costs = BUILDING_COSTS.copy()
        game.game_building_income = BUILDING_INCOME.copy()
    
    # Загружаем данные игры напрямую через SQL
    try:
        if is_postgresql():
            try:
                pool = await database.init_pool()
            except Exception as e:
                api_logger.error(f"Ошибка инициализации пула БД для игры {game_code}: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=f"Ошибка подключения к базе данных: {str(e)}")
            
            async with pool.acquire() as conn:
                # Загружаем данные игры
                row = await conn.fetchrow("SELECT * FROM games WHERE id = $1", game_data['id'])
                if row:
                    game.current_round = row.get('current_round', 1)
                    game.round_trading_locked = bool(row.get('round_trading_locked', False))
                
                # Загружаем текущие цены
                price_rows = await conn.fetch("SELECT resource_name, price FROM current_prices WHERE game_id = $1", game_data['id'])
                if price_rows:
                    game.current_prices = {row['resource_name']: row['price'] for row in price_rows}
                else:
                    game.current_prices = RESOURCE_PRICES.copy()
                
                # Загружаем игроков
                players_rows = await conn.fetch("SELECT * FROM players WHERE game_id = $1", game_data['id'])
                for p_row in players_rows:
                    player = Player(
                        id=p_row['id'],  # В PostgreSQL колонка называется 'id', а не 'player_id'
                        name=p_row['name'],
                        money=p_row.get('money', 1000)
                    )
                    if p_row.get('character_name'):
                        player.character_name = p_row['character_name']
                    if p_row.get('character_image'):
                        player.character_image = p_row['character_image']
                    if p_row.get('nickname'):
                        player.nickname = p_row['nickname']
                    if p_row.get('photo_url'):
                        player.photo_url = p_row['photo_url']
                    player.ready_next_round = bool(p_row.get('ready_next_round', False))
                    player.ready_next_round_for = p_row.get('ready_next_round_for')
                    
                    # Загружаем ресурсы
                    resource_rows = await conn.fetch(
                        "SELECT resource_name, amount FROM player_resources WHERE player_id = $1",
                        p_row['id']  # В PostgreSQL колонка называется 'id', а не 'player_id'
                    )
                    for r_row in resource_rows:
                        player.add_resource(r_row['resource_name'], r_row['amount'])
                    
                    # Загружаем здания
                    building_rows = await conn.fetch(
                        "SELECT * FROM buildings WHERE player_id = $1 AND game_id = $2",
                        p_row['id'], game_data['id']  # В PostgreSQL колонка называется 'id', а не 'player_id'
                    )
                    for b_row in building_rows:
                        status_val = b_row['status']
                        b_status = parse_building_status(str(status_val))
                        building = Building(
                            id=b_row['id'],
                            name=b_row['name'],
                            started_round=b_row['started_round'],
                            completed_round=b_row['completed_round'],
                            status=b_status,
                            sale_round=b_row.get('sale_round'),
                            sale_price=b_row.get('sale_price')
                        )
                        player.buildings.append(building)
                    
                    game.players.append(player)
                
                # Загружаем историю раундов (round_history) для PostgreSQL
                import json
                # Загружаем цены по раундам из resource_prices
                round_prices_rows = await conn.fetch("""
                SELECT round_number, resource_name, price
                FROM resource_prices
                WHERE game_id = $1
                ORDER BY round_number, resource_name
                """, game_data['id'])
                
                # Группируем цены по раундам
                prices_by_round = {}
                for rp_row in round_prices_rows:
                    round_num = rp_row['round_number']
                    if round_num not in prices_by_round:
                        prices_by_round[round_num] = {}
                    prices_by_round[round_num][rp_row['resource_name']] = rp_row['price']
                
                # Загружаем события по раундам
                round_events_rows = await conn.fetch("""
                SELECT round_number, positive_event, negative_event, positive2_event
                FROM round_events
                WHERE game_id = $1
                ORDER BY round_number
                """, game_data['id'])
                
                events_by_round = {}
                for re_row in round_events_rows:
                    round_num = re_row['round_number']
                    events_by_round[round_num] = {
                        "positive": re_row.get('positive_event'),
                        "negative": re_row.get('negative_event'),
                        "positive2": re_row.get('positive2_event')
                    }
                
                # Формируем round_history
                for round_num in sorted(prices_by_round.keys()):
                    game.round_history.append({
                        "round_number": round_num,
                        "prices": prices_by_round[round_num],
                        "events": events_by_round.get(round_num, {})
                    })
        else:  # SQLite
            db_path = get_sqlite_path()
            async with aiosqlite.connect(db_path) as conn:
                conn.row_factory = aiosqlite.Row
                # Загружаем данные игры
                cursor = await conn.execute("SELECT * FROM games WHERE id = ?", (game_data['id'],))
                row = await cursor.fetchone()
                if row:
                    game.current_round = row['current_round'] if row['current_round'] else 1
                    if 'round_trading_locked' in row.keys():
                        game.round_trading_locked = bool(row['round_trading_locked'])
                
                # Загружаем текущие цены
                cursor = await conn.execute("SELECT resource_name, price FROM current_prices WHERE game_id = ?", (game_data['id'],))
                price_rows = await cursor.fetchall()
                if price_rows:
                    game.current_prices = {row['resource_name']: row['price'] for row in price_rows}
                else:
                    game.current_prices = RESOURCE_PRICES.copy()
                
                # Загружаем игроков
                cursor = await conn.execute("SELECT * FROM players WHERE game_id = ?", (game_data['id'],))
                players_rows = await cursor.fetchall()
                for p_row in players_rows:
                    # В SQLite колонка называется 'id'
                    player_id = p_row['id']
                    player = Player(
                        id=player_id,
                        name=p_row['name'],
                        money=p_row.get('money', 1000) if 'money' in p_row.keys() else 1000
                    )
                    if p_row.get('character_name'):
                        player.character_name = p_row['character_name']
                    if p_row.get('character_image'):
                        player.character_image = p_row['character_image']
                    if 'ready_next_round' in p_row.keys():
                        player.ready_next_round = bool(p_row['ready_next_round'])
                    if 'ready_next_round_for' in p_row.keys():
                        player.ready_next_round_for = p_row['ready_next_round_for']
                    
                    # Загружаем ресурсы
                    player_id_for_resources = p_row['id'] if 'id' in p_row.keys() else p_row['player_id']
                    cursor = await conn.execute(
                        "SELECT resource_name, amount FROM player_resources WHERE player_id = ?",
                        (player_id_for_resources,)
                    )
                    resource_rows = await cursor.fetchall()
                    for r_row in resource_rows:
                        player.add_resource(r_row['resource_name'], r_row['amount'])
                    
                    # Загружаем здания
                    player_id_for_buildings = p_row['id'] if 'id' in p_row.keys() else p_row['player_id']
                    cursor = await conn.execute(
                        "SELECT * FROM buildings WHERE player_id = ? AND game_id = ?",
                        (player_id_for_buildings, game_data['id'])
                    )
                    building_rows = await cursor.fetchall()
                    for b_row in building_rows:
                        status_val = b_row['status']
                        b_status = parse_building_status(str(status_val))
                        building = Building(
                            id=b_row['id'],
                            name=b_row['name'],
                            started_round=b_row['started_round'],
                            completed_round=b_row['completed_round'],
                            status=b_status,
                            sale_round=b_row.get('sale_round'),
                            sale_price=b_row.get('sale_price')
                        )
                        player.buildings.append(building)
                    
                    game.players.append(player)
                
                # Загружаем историю раундов (round_history) для SQLite
                import json
                # Загружаем цены по раундам из resource_prices
                cursor = await conn.execute("""
                    SELECT round_number, resource_name, price
                    FROM resource_prices
                    WHERE game_id = ?
                    ORDER BY round_number, resource_name
                """, (game_data['id'],))
                round_prices_rows = await cursor.fetchall()
                
                # Группируем цены по раундам
                prices_by_round = {}
                for rp_row in round_prices_rows:
                    round_num = rp_row['round_number']
                    if round_num not in prices_by_round:
                        prices_by_round[round_num] = {}
                    prices_by_round[round_num][rp_row['resource_name']] = rp_row['price']
                
                # Загружаем события по раундам
                cursor = await conn.execute("""
                    SELECT round_number, positive_event, negative_event, positive2_event
                    FROM round_events
                    WHERE game_id = ?
                    ORDER BY round_number
                """, (game_data['id'],))
                round_events_rows = await cursor.fetchall()
                
                events_by_round = {}
                for re_row in round_events_rows:
                    round_num = re_row['round_number']
                    events_by_round[round_num] = {
                        "positive": re_row.get('positive_event'),
                        "negative": re_row.get('negative_event'),
                        "positive2": re_row.get('positive2_event')
                    }
                
                # Формируем round_history
                for round_num in sorted(prices_by_round.keys()):
                    game.round_history.append({
                        "round_number": round_num,
                        "prices": prices_by_round[round_num],
                        "events": events_by_round.get(round_num, {})
                    })
    
        # Если данных нет, используем значения по умолчанию
        if not game.current_prices:
            game.current_prices = RESOURCE_PRICES.copy()
        
        # Снимок 0: создаём при отсутствии; не сохраняем пустой players. Перезаписываем битый (players:[]), если в игре уже есть игроки
        try:
            existing_snapshot_0 = await database.load_game_snapshot(game.game_id, 0)
            snap_p = (existing_snapshot_0 or {}).get("players") or []
            if not existing_snapshot_0 and game.players:
                await database.save_game_snapshot(game.game_id, 0, game.create_snapshot())
            elif (not snap_p) and game.players and existing_snapshot_0:
                await database.save_game_snapshot(game.game_id, 0, game.create_snapshot())
        except Exception as e:
            api_logger.warning(f"Не удалось создать снимок 0 при загрузке игры {game_code}: {e}")
        
        # get_game_by_code обходит Game.__init__/load_from_database — подтягиваем опоры тренда проектора из config_data
        game.building_players_pct_prev_round_start = {}
        game.building_players_pct_current_round_start = {}
        hydrated_pct = await game.hydrate_building_pct_baselines_from_config()
        if not hydrated_pct:
            game.rebuild_building_pct_round_starts_from_current_players(clear_prev=True)
        if await database.get_game_config(game.game_id) is None:
            try:
                await game._persist_building_pct_baselines_to_config()
            except Exception as _e:
                api_logger.debug("seed_projector_baseline: %s", _e)
        
        try:
            _cfg_cap = await database.get_game_config(game.game_id)
        except Exception:
            _cfg_cap = None
        game.player_capitalization_by_round_entry = GameClass.parse_round_entry_snapshots_from_config(
            (_cfg_cap or {}).get(PLAYER_CAPITALIZATION_BY_ROUND_ENTRY_KEY)
            if isinstance(_cfg_cap, dict)
            else {}
        )
        _cr_snap = int(getattr(game, "current_round", 1) or 1)
        game.player_capitalization_by_round_entry = {
            rr: vv
            for rr, vv in game.player_capitalization_by_round_entry.items()
            if rr <= _cr_snap
        }
        if game.players and _cr_snap not in game.player_capitalization_by_round_entry:
            game.capture_player_capitalization_at_round_entry(_cr_snap)
            await game.persist_player_round_entry_capitalization_snapshots_async()
        
        return game
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f"Ошибка загрузки игры {game_code} из БД: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка загрузки игры: {str(e)}")

def validate_game_code(game_code: str) -> bool:
    """Валидация формата кода (6 цифр)"""
    return game_code and len(game_code) == 6 and game_code.isdigit()


def _history_prices_effective_round(round_data: dict) -> int:
    """
    Раунд, с которого действуют цены из снимка history (начало раунда для игрока).
    В БД: round_number = next_round из process_round (после раунда 1 это 2).
    В памяти: в round_result поле round = обработанный раунд, цены — на начало следующего.
    """
    if round_data.get("round_number") is not None:
        return int(round_data["round_number"])
    r = round_data.get("round")
    if r is not None:
        return int(r) + 1
    return 1


async def build_resource_price_history(
    game: Game,
    resource_name: str,
    fallback: float,
) -> List[Dict[str, int]]:
    """
    История для графика: по оси X — номер раунда, по Y — цена в начале этого раунда
    (раунд 1 — снимок 0 / конфиг, далее — цены из history с правильной привязкой к раунду;
    для текущего раунда — current_prices, без дублирования одной и той же пары).
    """
    current_price = float(game.current_prices.get(resource_name) or 0)
    current_round = int(getattr(game, "current_round", 1) or 1)
    by_round: Dict[int, float] = {}

    try:
        snap0 = await database.load_game_snapshot(game.game_id, 0)
    except Exception as e:
        api_logger.debug("Снимок 0 для графика цен недоступен game_id=%s: %s", game.game_id, e)
        snap0 = None

    gcfg = getattr(game, "game_resource_prices", None) or {}
    snap_cr: Optional[int] = None
    if snap0 is not None:
        try:
            snap_cr = int(snap0.get("current_round") or 1)
        except (TypeError, ValueError):
            snap_cr = None
    # Снимок 0 при отсутствии в БД создаётся при загрузке из текущего состояния — тогда
    # current_round > 1 и цены не «старт раунда 1», а текущие (плоский график).
    use_snap_for_r1 = bool(
        snap0
        and snap0.get("current_prices")
        and resource_name in snap0["current_prices"]
        and snap_cr is not None
        and snap_cr <= 1
    )
    if use_snap_for_r1:
        by_round[1] = float(snap0["current_prices"][resource_name])
    else:
        base = float(gcfg.get(resource_name, fallback))
        r1_price = base
        try:
            rs_list = await database.get_round_settings(game.game_id, 1)
            if rs_list:
                mods = (rs_list[0].get("resource_modifiers") or {})
                coef = float(mods.get(resource_name, 1.0))
                r1_price = round(base * coef, 2)
        except Exception as e:
            api_logger.debug(
                "recompute r1 price from settings game_id=%s: %s", game.game_id, e
            )
        by_round[1] = r1_price

    for round_data in getattr(game, "round_history", None) or []:
        r_eff = _history_prices_effective_round(round_data)
        if r_eff <= 1:
            continue
        rp = round_data.get("prices") or {}
        if resource_name not in rp:
            continue
        by_round[r_eff] = float(rp[resource_name])

    if current_round >= 1 and resource_name in (game.current_prices or {}):
        by_round[current_round] = current_price

    if not by_round:
        return [{"round": current_round, "price": int(round(current_price))}]

    return [
        {"round": r, "price": int(round(by_round[r]))} for r in sorted(by_round.keys())
    ]


@app.on_event("startup")
async def startup():
    """Инициализация при старте"""
    api_logger.info("Запуск сервера...")
    # Инициализируем пул соединений для PostgreSQL (если используется)
    await database.init_pool()
    
    # Инициализируем БД (создание таблиц)
    await database.init_database()
    
    # Обновляем метрики активных игр
    try:
        from monitoring import update_active_games
        all_games = await database.get_all_games()
        active_count = len([g for g in all_games if g.get('status') == 'active'])
        update_active_games(active_count)
    except Exception as e:
        api_logger.warning(f"Не удалось обновить метрики активных игр: {e}")
    
    api_logger.info("✅ База данных инициализирована. Игры загружаются по запросу через game_code")

@app.on_event("shutdown")
async def shutdown():
    """Закрытие при остановке"""
    api_logger.info("Остановка сервера...")
    # Закрываем пул соединений PostgreSQL
    await database.close_pool()
    api_logger.info("✅ Сервер остановлен")

# Middleware для логирования запросов
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Логирование всех HTTP запросов"""
    start_time = time.time()
    
    # Получаем информацию о запросе
    game_code = request.query_params.get("game_code", "N/A")
    player_id = None
    ip_address = request.client.host if request.client else "unknown"
    
    # Пытаемся получить player_id из заголовков
    try:
        x_telegram_init_data = request.headers.get("X-Telegram-Init-Data")
        if x_telegram_init_data:
            player_id = get_player_id_from_init_data(x_telegram_init_data)
    except:
        pass
    
    # Выполняем запрос
    try:
        response = await call_next(request)
        duration_ms = (time.time() - start_time) * 1000
        
        # Логируем запрос
        api_logger.info(
            f"{request.method} {request.url.path}",
            extra={
                "endpoint": request.url.path,
                "method": request.method,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
                "game_code": game_code if game_code != "N/A" else None,
                "player_id": player_id,
                "ip_address": ip_address
            }
        )
        
        # Записываем метрику
        record_request(request.url.path, request.method, response.status_code, duration_ms)
        
        return response
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        error_type = type(e).__name__
        
        api_logger.error(
            f"Ошибка при обработке запроса: {str(e)}",
            extra={
                "endpoint": request.url.path,
                "method": request.method,
                "duration_ms": round(duration_ms, 2),
                "game_code": game_code if game_code != "N/A" else None,
                "player_id": player_id,
                "ip_address": ip_address,
                "error_type": error_type,
                "exception": traceback.format_exc()
            },
            exc_info=True
        )
        
        # Записываем метрику ошибки
        record_error(error_type, str(e))
        record_request(request.url.path, request.method, 500, duration_ms)
        
        raise

@app.get("/", response_class=HTMLResponse)
async def get_main_page():
    """Главная страница"""
    with open("templates/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/miniapp", response_class=HTMLResponse)
async def get_miniapp_page():
    """Страница Telegram Mini App"""
    with open("templates/miniapp.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/miniapp_test", response_class=HTMLResponse)
async def get_miniapp_test_page():
    """Тестовая точка входа Mini App: тот же шаблон, что и /miniapp (UI всегда синхронизирован)."""
    with open("templates/miniapp.html", "r", encoding="utf-8") as f:
        return f.read()

# Endpoint для сводки по раунду - регистрируем рано, чтобы избежать конфликтов
@app.get("/api/round/{round_number}/content")
async def get_round_content_for_player(
    round_number: int,
    game_code: str = Query(..., description="Код игры (6 цифр)")
):
    """Получить контент (видео) для раунда (для игроков)"""
    try:
        
        try:
            game = await get_game_by_code(game_code)
        except HTTPException as he:
            return JSONResponse(status_code=he.status_code, content={"error": he.detail})
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        # Загружаем контент для конкретного раунда
        content_list = await database.get_round_content(game.game_id, round_number)
        
        if content_list and len(content_list) > 0:
            content = content_list[0]  # Берем первый (должен быть один для раунда)
            return {
                "success": True,
                "round_number": round_number,
                "content_url": content.get("content_url"),
                "content_type": content.get("content_type", "video")
            }
        else:
            return {
                "success": False,
                "round_number": round_number,
                "content_url": None,
                "message": "Контент для этого раунда не настроен"
            }
    except Exception as e:
        api_logger.error(f"Ошибка получения контента раунда {round_number}: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/intro/content")
async def get_intro_content(game_code: str = Query(..., description="Код игры (6 цифр)")):
    """Получить контент интро для игры (для игроков на начальном экране)"""
    try:
        try:
            game = await get_game_by_code(game_code)
        except HTTPException as he:
            return JSONResponse(status_code=he.status_code, content={"error": he.detail})
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        content_list = await database.get_round_content(game.game_id, 0)
        if content_list and len(content_list) > 0:
            content = content_list[0]
            return {
                "success": True,
                "content_url": content.get("content_url"),
                "content_type": content.get("content_type", "video")
            }
        return {"success": False, "content_url": None, "message": "Интро не настроено"}
    except Exception as e:
        api_logger.error(f"Ошибка получения интро: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


def _coef_change_percent(coef: float) -> tuple:
    """Процент изменения и направление из коэффициента раунда (1.0 = без изменений)."""
    try:
        c = float(coef)
    except (TypeError, ValueError):
        c = 1.0
    if c < 1.0:
        return round((1.0 - c) * 100, 0), "down"
    if c > 1.0:
        return round((c - 1.0) * 100, 0), "up"
    return 0, "neutral"


async def build_round_summary_payload(game_id: int, round_number: int) -> dict:
    """
    Сводка для проектора: текст/картинки из «События ВЕБ», изменения — из настроек раунда в админке.
    """
    web_event = await database.get_round_web_event(game_id, round_number) or {}
    event_text = (web_event.get("event_text") or "").strip()
    image_url = (web_event.get("image_url") or "").strip()
    bg_image_url = (web_event.get("bg_image_url") or "").strip()
    highlight_resources = web_event.get("highlight_resources") or []
    highlight_buildings = web_event.get("highlight_buildings") or []

    resource_modifiers = {}
    building_modifiers = {}
    settings_rows = await database.get_round_settings(game_id, round_number)
    if settings_rows:
        resource_modifiers = settings_rows[0].get("resource_modifiers") or {}
        building_modifiers = settings_rows[0].get("building_modifiers") or {}

    key_resources = []
    for resource in highlight_resources:
        coef = resource_modifiers.get(resource, 1.0)
        change_percent, direction = _coef_change_percent(coef)
        if direction == "neutral":
            continue
        key_resources.append({
            "name": resource,
            "change_percent": change_percent,
            "direction": direction,
            "reason": "Коэффициент раунда (настройки админки)",
        })
    key_resources.sort(key=lambda x: abs(x["change_percent"]), reverse=True)

    key_buildings = []
    for building_name in highlight_buildings:
        coef = building_modifiers.get(building_name, 1.0)
        change_percent, direction = _coef_change_percent(coef)
        if direction == "neutral":
            continue
        key_buildings.append({
            "name": building_name,
            "income_change_percent": change_percent,
            "direction": direction,
            "reason": "Коэффициент раунда (настройки админки)",
        })
    key_buildings.sort(key=lambda x: abs(x["income_change_percent"]), reverse=True)

    title = f"Раунд {round_number}"
    if round_number == 1 and not event_text:
        title = "Раунд 1: Начало игры"

    return {
        "title": title,
        "events": {
            "event_text": event_text or None,
            "image_url": image_url or None,
            "bg_image_url": bg_image_url or None,
        },
        "key_resources": key_resources,
        "key_buildings": key_buildings,
    }


@app.get("/api/round/{round_number}/summary")
async def get_round_summary(
    request: Request,
    round_number: int,
    game_code: str = Query(..., description="Код игры (6 цифр)")
):
    """Сводка раунда для проектора: «События ВЕБ» + коэффициенты из настроек раунда."""
    from fastapi.responses import JSONResponse

    try:
        game = await get_game_by_code(game_code)

        if round_number < 1 or round_number > 10:
            raise HTTPException(status_code=400, detail="Некорректный номер раунда (1-10)")

        payload = await build_round_summary_payload(game.game_id, round_number)
        return JSONResponse(content=payload)
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f"Ошибка get_round_summary раунд {round_number}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ошибка получения сводки: {str(e)}")

def _player_buildings_portfolio(player_obj) -> List[Dict]:
    """По одному элементу на каждый объект (портфель / модалка): id, имя, статус. Проданные не показываем."""
    if not player_obj:
        return []
    rows: List[Dict] = []
    for b in getattr(player_obj, "buildings", []) or []:
        name = getattr(b, "name", None)
        st = getattr(b, "status", None)
        if not name or not st:
            continue
        status_val = st if isinstance(st, str) else getattr(st, "value", None)
        if not status_val or status_val == "sold":
            continue
        bid = getattr(b, "id", None)
        if bid is None:
            continue
        rows.append({"id": bid, "name": name, "status": status_val})
    rows.sort(key=lambda x: (x["name"], x["status"], str(x["id"])))
    return rows


def _resource_bag_label_norm(label) -> str:
    if label is None:
        return ""
    return str(label).strip().lower()


def _bag_amount_matching_label(bag: Dict, resource_label: str) -> float:
    """Сумма по ключам мешка, совпадающим с именем ресурса (регистр и крайние пробелы игнорируются)."""
    if not bag or not resource_label:
        return 0.0
    target = _resource_bag_label_norm(resource_label)
    if not target:
        return 0.0
    total = 0.0
    for key, raw in bag.items():
        if _resource_bag_label_norm(key) != target:
            continue
        try:
            total += float(raw or 0)
        except (TypeError, ValueError):
            continue
    return total


def _canonical_resource_ui_name(resource_label: str) -> str:
    """Отображаемое имя как в RESOURCE_PRICES при совпадении без учёта регистра."""
    if not resource_label:
        return resource_label
    s = str(resource_label).strip()
    if s in RESOURCE_PRICES:
        return s
    low = s.lower()
    for pk in RESOURCE_PRICES.keys():
        if pk.lower() == low:
            return pk
    return s


def _player_resources_for_leaderboard(game: Game, player_obj) -> List[Dict]:
    """Ресурсы игрока для модалки рейтинга: включённые в игре, количество > 0; ключи совпадают по смыслу с мешком."""
    if not player_obj:
        return []
    bag = getattr(player_obj, "resources", None) or {}
    enabled = list(getattr(game, "enabled_resources", None) or [])
    if not enabled:
        enabled = list(RESOURCE_PRICES.keys())
    enabled_lower = {_resource_bag_label_norm(e) for e in enabled}

    seen_lower: set = set()
    result: List[Dict] = []
    for res_name in enabled:
        n = _bag_amount_matching_label(bag, res_name)
        if n <= 0:
            continue
        display = _canonical_resource_ui_name(res_name)
        result.append({"name": display, "amount": int(round(n))})
        seen_lower.add(_resource_bag_label_norm(display))

    # Ресурсы в мешке с неточным ключом относительно enabled (устаревшие/ручные записи в БД)
    prices_keys = getattr(game, "current_prices", None) or {}
    price_names_lower = {str(k).strip().lower() for k in prices_keys.keys()}
    for key, raw in bag.items():
        try:
            n = float(raw or 0)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        display = _canonical_resource_ui_name(str(key).strip())
        dl = display.lower()
        if display not in RESOURCE_PRICES and dl not in price_names_lower:
            continue
        kn = _resource_bag_label_norm(display)
        if kn in seen_lower:
            continue
        if dl not in {e.lower() for e in enabled} and _resource_bag_label_norm(key) not in enabled_lower:
            continue
        result.append({"name": display, "amount": int(round(n))})
        seen_lower.add(kn)

    result.sort(key=lambda x: x["name"])
    return result


def _ensure_leaderboard_portfolio(game: Game, rows: List[Dict]) -> None:
    """Гарантирует поля buildings_portfolio и resources_portfolio у каждой строки лидерборда."""
    for row in rows or []:
        if not row:
            continue
        pid = row.get("player_id")
        po = game.get_player(pid) if pid else None
        if "buildings_portfolio" not in row:
            row["buildings_portfolio"] = _player_buildings_portfolio(po) if po else []
        if "resources_portfolio" not in row:
            row["resources_portfolio"] = _player_resources_for_leaderboard(game, po) if po else []


def build_enriched_leaderboard(game: Game, game_code: str) -> List[Dict]:
    """Турнирная таблица с персонажем, приростом и списком объектов (для API и WebSocket)."""
    global previous_leaderboard, previous_leaderboard_ranks

    current_leaderboard = game.get_leaderboard()
    result: List[Dict] = []

    for player in current_leaderboard:
        player_obj = game.get_player(player["player_id"])
        player_data = player.copy()

        if player_obj:
            player_data["character_name"] = getattr(player_obj, "character_name", None)
            player_data["character_image"] = getattr(player_obj, "character_image", None)
            player_data["buildings_portfolio"] = _player_buildings_portfolio(player_obj)
            player_data["resources_portfolio"] = _player_resources_for_leaderboard(game, player_obj)
        else:
            player_data["buildings_portfolio"] = []
            player_data["resources_portfolio"] = []

        player_id = player["player_id"]
        tv = float(player["total_value"])
        growth = game.player_growth_round_percent(player_id, tv)
        growth_game = game.player_growth_game_percent(player_id, tv)

        player_data["growth_percent"] = growth
        player_data["growth_round_percent"] = growth
        player_data["growth_game_percent"] = growth_game

        player_data["money"] = int(round(player_data["money"]))
        player_data["resources_value"] = int(round(player_data["resources_value"]))
        player_data["buildings_value"] = int(round(player_data["buildings_value"]))
        player_data["total_value"] = int(round(player_data["total_value"]))
        result.append(player_data)

    previous_leaderboard = current_leaderboard.copy()
    previous_leaderboard_ranks = {}
    for idx, p in enumerate(current_leaderboard):
        previous_leaderboard_ranks[p["player_id"]] = idx + 1

    try:
        _ensure_leaderboard_portfolio(game, result)
    except Exception:
        pass

    return result


@app.get("/api/leaderboard")
async def get_leaderboard(request: Request, game_code: str = Query(..., description="Код игры (6 цифр)")):
    """Получить турнирную таблицу с приростом"""
    from fastapi.responses import JSONResponse
    try:
        game = await get_game_by_code(game_code)
        result = build_enriched_leaderboard(game, game_code)
        return JSONResponse(content={"leaderboard": result})
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f"Ошибка в get_leaderboard: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Внутренняя ошибка: {str(e)}"}
        )

@app.get("/api/prices")
async def get_prices(request: Request, game_code: str = Query(..., description="Код игры (6 цифр)")):
    """Получить текущие цены с изменениями"""
    from fastapi.responses import JSONResponse
    try:
        game = await get_game_by_code(game_code)
        
        current_prices = game.current_prices
        global initial_prices
        
        # Получаем предыдущие цены
        # round_history содержит цены ПОСЛЕ обработки раунда
        # Для получения предыдущих цен нужно взять цены из предыдущего элемента истории
        previous_prices = {}
        if len(game.round_history) >= 2:
            # Берем цены из предпоследнего раунда (предыдущего)
            previous_prices = game.round_history[-2].get("prices", initial_prices.copy())
        elif len(game.round_history) == 1:
            # Если это второй раунд, предыдущие цены - начальные (до первого раунда)
            previous_prices = initial_prices.copy()
        else:
            # Первый раунд - предыдущих цен нет, используем начальные
            previous_prices = initial_prices.copy()
        
        result = []
        for resource, current_price in sorted(current_prices.items()):
            initial_price = initial_prices.get(resource, current_price)
            prev_price = previous_prices.get(resource, current_price)
            
            # Изменение от предыдущего раунда
            if prev_price > 0:
                change_from_prev = ((current_price - prev_price) / prev_price) * 100
            else:
                change_from_prev = 0
            
            # Изменение с начала игры
            if initial_price > 0:
                change_from_start = ((current_price - initial_price) / initial_price) * 100
            else:
                change_from_start = 0
            
            change_prev = round(change_from_prev, 2)
            change_start = round(change_from_start, 2)
            if abs(change_prev) < 1.0:
                change_prev = 0
            if abs(change_start) < 1.0:
                change_start = 0
            result.append({
                "resource": resource,
                "current_price": int(round(current_price)),
                "change_from_prev_percent": change_prev,
                "change_from_start_percent": change_start
            })
        
        return JSONResponse(content={"prices": result})
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f"Ошибка в get_prices: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Внутренняя ошибка: {str(e)}"}
        )

@app.get("/api/buildings")
async def get_buildings(request: Request, game_code: str = Query(..., description="Код игры (6 цифр)")):
    """Получить статистику по построенным объектам"""
    from fastapi.responses import JSONResponse
    try:
        game = await get_game_by_code(game_code)
        
        building_counts = {}
        players_with_building = {}  # Сколько игроков имеют хотя бы один такой объект
        
        players = getattr(game, 'players', [])
        for player in players:
            try:
                player_buildings = set()  # Уникальные объекты игрока
                player_buildings_list = getattr(player, 'buildings', [])
                for building in player_buildings_list:
                    try:
                        building_status = getattr(building, 'status', None)
                        if building_status and building_status.value not in ["for_sale", "sold"]:
                            building_name = getattr(building, 'name', 'unknown')
                            building_counts[building_name] = building_counts.get(building_name, 0) + 1
                            player_buildings.add(building_name)
                    except Exception as e:
                        api_logger.warning(f"Ошибка обработки объекта в get_buildings: {e}")
                        continue
                
                # Подсчитываем игроков с каждым объектом
                for building_name in player_buildings:
                    players_with_building[building_name] = players_with_building.get(building_name, 0) + 1
            except Exception as e:
                api_logger.warning(f"Ошибка обработки игрока в get_buildings: {e}")
                continue
        
        result = []
        num_players = len(players)
        current_prices = getattr(game, "current_prices", None) or {}
        game_building_costs = getattr(game, "game_building_costs", None) or {}
        enabled_buildings = list(getattr(game, "enabled_buildings", None) or list(BUILDING_COSTS.keys()))
        try:
            for building_name in sorted(enabled_buildings):
                try:
                    count = int(building_counts.get(building_name, 0))
                    players_count = int(players_with_building.get(building_name, 0))
                    players_percentage = (
                        round((players_count / num_players) * 100) if num_players > 0 else 0
                    )

                    costs = game_building_costs.get(building_name)
                    if costs is None:
                        costs = BUILDING_COSTS.get(building_name, {})
                    cost_in_coins = 0.0
                    for resource, amount in (costs or {}).items():
                        try:
                            price = float(
                                current_prices.get(resource, RESOURCE_PRICES.get(resource, 0))
                            )
                            cost_in_coins += float(amount) * price
                        except (TypeError, ValueError):
                            continue

                    result.append({
                        "name": building_name,
                        "count": count,
                        "players_percentage": players_percentage,
                        "cost_coins": int(round(cost_in_coins)),
                    })
                except Exception as e:
                    api_logger.warning(f"Ошибка обработки объекта {building_name} в get_buildings: {e}")
                    continue
        except Exception as e:
            api_logger.warning(f"Ошибка формирования результата в get_buildings: {e}")
        
        return JSONResponse(content={"buildings": result})
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f"Ошибка в get_buildings: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Внутренняя ошибка: {str(e)}"}
        )

@app.get("/api/resource/{resource_name}")
async def get_resource_details(
    request: Request,
    resource_name: str,
    game_code: str = Query(..., description="Код игры (6 цифр)")
):
    """Получить детальную информацию о ресурсе, включая историю цен и спрос/предложение"""
    game = await get_game_by_code(game_code)
    
    # Декодируем имя ресурса из URL (для кириллицы)
    resource_name = unquote(resource_name)
    
    global initial_prices
    
    # Получаем текущую цену
    current_price = game.current_prices.get(resource_name, 0)
    initial_price = initial_prices.get(resource_name, current_price)
    
    # Получаем предыдущие цены для расчета изменений
    previous_prices = {}
    if len(game.round_history) >= 2:
        previous_prices = game.round_history[-2].get("prices", initial_prices.copy())
    elif len(game.round_history) == 1:
        previous_prices = initial_prices.copy()
    else:
        previous_prices = initial_prices.copy()
    
    prev_price = previous_prices.get(resource_name, current_price)
    
    # Изменение от предыдущего раунда
    if prev_price > 0:
        change_from_prev = ((current_price - prev_price) / prev_price) * 100
    else:
        change_from_prev = 0
    
    # Изменение с начала игры
    if initial_price > 0:
        change_from_start = ((current_price - initial_price) / initial_price) * 100
    else:
        change_from_start = 0
    
    # История для графика: цена в начале раунда (раунд 1 — снимок/конфиг, без дубля текущей ценой)
    price_history = await build_resource_price_history(
        game, resource_name, float(initial_prices.get(resource_name) or current_price)
    )
    
    # Определяем уровень спроса и предложения
    players = getattr(game, 'players', []) or []
    num_players = len(players)
    previous_round_players_bought = getattr(game, 'previous_round_players_bought', {}) or {}
    previous_round_players_sold = getattr(game, 'previous_round_players_sold', {}) or {}
    players_bought = previous_round_players_bought.get(resource_name, 0)
    players_sold = previous_round_players_sold.get(resource_name, 0)
    
    # Спрос (по логике из market_dynamics.py: >75% высокий, >25% средний, иначе низкий)
    if num_players > 0:
        demand_percent = (players_bought / num_players) * 100
        if demand_percent > 75:
            demand_level = "высокий"
        elif demand_percent > 25:
            demand_level = "базовый"
        else:
            demand_level = "низкий"
    else:
        demand_level = "базовый"
    
    # Предложение (по логике из market_dynamics.py: >75% высокое, >25% среднее, иначе низкое)
    if num_players > 0:
        supply_percent = (players_sold / num_players) * 100
        if supply_percent > 75:
            supply_level = "высокое"
        elif supply_percent > 25:
            supply_level = "базовое"
        else:
            supply_level = "низкое"
    else:
        supply_level = "базовое"
    
    resource_text = ""
    try:
        current_round = int(getattr(game, "current_round", 1) or 1)
        settings_rows = await database.get_round_settings(game.game_id, current_round)
        if settings_rows:
            texts = settings_rows[0].get("resource_texts") or {}
            resource_text = (texts.get(resource_name) or "").strip()
    except Exception:
        resource_text = ""

    from fastapi.responses import JSONResponse
    return JSONResponse(content={
        "name": resource_name,
        "current_price": int(round(current_price)),
        "change_from_prev_percent": round(change_from_prev, 2),
        "change_from_start_percent": round(change_from_start, 2),
        "demand_level": demand_level,
        "supply_level": supply_level,
        "price_history": price_history,
        "resource_text": resource_text,
    })

def _active_building_counts(game: Game) -> Dict[str, int]:
    """Количество активных объектов по типам (для расчёта дохода раунда)."""
    enabled = getattr(game, "enabled_buildings", None) or list(BUILDING_INCOME.keys())
    counts: Dict[str, int] = {}
    for player in getattr(game, "players", []) or []:
        for building in getattr(player, "buildings", []) or []:
            try:
                if (
                    building.status == BuildingStatus.ACTIVE
                    and building.name in enabled
                ):
                    counts[building.name] = counts.get(building.name, 0) + 1
            except Exception:
                continue
    return counts


def _serialize_income_dict(income: Optional[Dict]) -> Dict:
    """Нормализует доход объекта для JSON (целые монеты, округлённые ресурсы)."""
    if not income:
        return {"монеты": 0, "ресурсы": {}}
    resources = {}
    for res, amt in (income.get("ресурсы") or {}).items():
        try:
            resources[res] = int(round(float(amt)))
        except (TypeError, ValueError):
            resources[res] = 0
    try:
        coins = int(round(float(income.get("монеты") or 0)))
    except (TypeError, ValueError):
        coins = 0
    return {"монеты": coins, "ресурсы": resources}


def _format_income_label(income: Dict) -> str:
    """Человекочитаемая строка дохода для проектора."""
    parts = []
    coins = income.get("монеты") or 0
    if coins:
        parts.append(f"{coins} монет")
    for res, amt in (income.get("ресурсы") or {}).items():
        if amt:
            parts.append(f"{res}: {amt}")
    return ", ".join(parts) if parts else "0"


@app.get("/api/building/{building_name}")
async def get_building_details(
    request: Request,
    building_name: str,
    game_code: str = Query(..., description="Код игры (6 цифр)")
):
    """Получить детальную информацию об объекте, включая список владельцев"""
    game = await get_game_by_code(game_code)
    building_name = unquote(building_name)

    # Подсчитываем общее количество объектов
    total_count = 0
    owners = {}  # {player_id: {name: str, count: int}}
    
    # Защита от None/пустоты
    players = getattr(game, 'players', []) or []
    
    for player in players:
        try:
            player_building_count = 0
            player_buildings = getattr(player, 'buildings', []) or []
            for building in player_buildings:
                try:
                    building_name_attr = getattr(building, 'name', None)
                    building_status = getattr(building, 'status', None)
                    if (building_name_attr == building_name and 
                        building_status and 
                        building_status.value not in ["for_sale", "sold"]):
                        total_count += 1
                        player_building_count += 1
                except Exception:
                    continue  # Пропускаем проблемные объекты
            
            if player_building_count > 0:
                player_id = getattr(player, 'id', 'unknown')
                player_name = getattr(player, 'name', 'Unknown')
                player_character_name = getattr(player, 'character_name', None)
                owners[player_id] = {
                    "name": player_character_name or player_name,
                    "character_name": player_character_name,
                    "count": player_building_count
                }
        except Exception:
            continue  # Пропускаем проблемных игроков
    
    # Подсчитываем процент игроков
    num_players = len(players)
    players_count = len(owners)
    players_percentage = round((players_count / num_players) * 100) if num_players > 0 else 0
    
    # Сортируем владельцев по количеству объектов (от большего к меньшему)
    owners_list = sorted(owners.values(), key=lambda x: x["count"], reverse=True)

    building_text = ""
    building_modifiers: Dict[str, float] = {}
    try:
        current_round = int(getattr(game, "current_round", 1) or 1)
        settings_rows = await database.get_round_settings(game.game_id, current_round)
        if settings_rows:
            texts = settings_rows[0].get("building_texts") or {}
            building_text = (texts.get(building_name) or "").strip()
            building_modifiers = settings_rows[0].get("building_modifiers") or {}
    except Exception:
        building_text = ""

    game_building_costs = getattr(game, "game_building_costs", None) or BUILDING_COSTS
    costs = dict(game_building_costs.get(building_name) or BUILDING_COSTS.get(building_name, {}))

    game_building_income = getattr(game, "game_building_income", None) or BUILDING_INCOME
    base_income = _serialize_income_dict(
        game_building_income.get(building_name, BUILDING_INCOME.get(building_name, {}))
    )

    current_round_income = {"монеты": 0, "ресурсы": {}}
    try:
        market = getattr(game, "market", None)
        if market is not None:
            counts = _active_building_counts(game)
            prices = getattr(game, "current_prices", {}) or RESOURCE_PRICES.copy()
            incomes = market.calculate_building_incomes(
                counts, prices, building_modifiers
            )
            current_round_income = _serialize_income_dict(
                incomes.get(building_name, {})
            )
    except Exception as e:
        api_logger.warning(
            f"Не удалось рассчитать доход объекта {building_name} за раунд: {e}"
        )

    from fastapi.responses import JSONResponse
    return JSONResponse(content={
        "name": building_name,
        "count": total_count,
        "players_percentage": players_percentage,
        "owners": owners_list,
        "building_text": building_text,
        "costs": costs,
        "base_income": base_income,
        "current_round_income": current_round_income,
        "current_round_income_label": _format_income_label(current_round_income),
        "base_income_label": _format_income_label(base_income),
        "current_round": int(getattr(game, "current_round", 1) or 1),
    })

async def _get_game_state_internal(game: Game, game_code: str):
    """Внутренняя функция для получения состояния игры без Request (не endpoint)"""
    try:
        
        # Получаем данные из других эндпоинтов
        # Но нужно получить их напрямую, а не через HTTP запросы
        # Используем внутренние функции
        try:
            leaderboard_data = build_enriched_leaderboard(game, game_code)
        except Exception as e:
            api_logger.warning(f"Ошибка получения leaderboard в get_game_state: {e}")
            leaderboard_data = []
        try:
            _ensure_leaderboard_portfolio(game, leaderboard_data)
        except Exception:
            pass
        
        prices_data = []
        try:
            initial_prices = RESOURCE_PRICES.copy()
            current_prices = getattr(game, 'current_prices', {})
            round_history = getattr(game, 'round_history', [])
            for resource, current_price in sorted(current_prices.items()):
                try:
                    if len(round_history) >= 2:
                        previous_prices = round_history[-2].get("prices", initial_prices.copy())
                    elif len(round_history) == 1:
                        # После первого раунда «предыдущие» — начальные цены (как в GET /api/prices)
                        previous_prices = initial_prices.copy()
                    else:
                        previous_prices = initial_prices.copy()
                    
                    previous_price = previous_prices.get(resource, initial_prices.get(resource, 0))
                    initial_price = initial_prices.get(resource, current_price)
                    price_change = current_price - previous_price
                    price_change_percent = (price_change / previous_price * 100) if previous_price > 0 else 0
                    change_from_start = ((current_price - initial_price) / initial_price * 100) if initial_price > 0 else 0
                    # Малые изменения (< 1%) показываем как 0%, чтобы не было ложных −1%
                    change_prev = round(price_change_percent, 2)
                    change_start = round(change_from_start, 2)
                    if abs(change_prev) < 1.0:
                        change_prev = 0
                    if abs(change_start) < 1.0:
                        change_start = 0
                    prices_data.append({
                        "resource": resource,
                        "current_price": int(round(current_price)),
                        "change_from_prev_percent": change_prev,
                        "change_from_start_percent": change_start
                    })
                except Exception as e:
                    api_logger.warning(f"Ошибка обработки цены {resource} в get_game_state: {e}")
                    # Продолжаем обработку других ресурсов
                    continue
        except Exception as e:
            api_logger.warning(f"Ошибка получения цен в get_game_state: {e}")
            prices_data = []
        
        buildings_data = []
        try:
            building_counts = {}
            players_with_building = {}
            players = getattr(game, 'players', [])
            num_players = len(players)

            for player in players:
                try:
                    player_types = set()
                    player_buildings_list = getattr(player, 'buildings', [])
                    for building in player_buildings_list:
                        try:
                            building_status = getattr(building, 'status', None)
                            if building_status and building_status.value not in ["for_sale", "sold"]:
                                building_name = getattr(building, 'name', 'unknown')
                                building_counts[building_name] = building_counts.get(building_name, 0) + 1
                                player_types.add(building_name)
                        except Exception as e:
                            api_logger.warning(f"Ошибка обработки объекта в get_game_state: {e}")
                            continue
                    for building_name in player_types:
                        players_with_building[building_name] = (
                            players_with_building.get(building_name, 0) + 1
                        )
                except Exception as e:
                    api_logger.warning(f"Ошибка обработки игрока в get_game_state: {e}")
                    continue

            cur_start = getattr(game, "building_players_pct_current_round_start", None) or {}
            prev_start = getattr(game, "building_players_pct_prev_round_start", None) or {}
            # Пустой prev: не сравниваем с нулём (иначе ложный «рост» для любого ненулевого current)
            has_prev = bool(prev_start)

            for building_name, count in sorted(building_counts.items()):
                owners_count = players_with_building.get(building_name, 0)
                players_percentage = (
                    round((owners_count / num_players) * 100) if num_players > 0 else 0
                )
                pc = int(cur_start.get(building_name, 0))
                pp = int(prev_start.get(building_name, 0))
                if not has_prev:
                    players_pct_trend = "same"
                elif pc > pp:
                    players_pct_trend = "up"
                elif pc < pp:
                    players_pct_trend = "down"
                else:
                    players_pct_trend = "same"
                buildings_data.append({
                    "name": building_name,
                    "count": count,
                    "players_percentage": players_percentage,
                    "players_pct_trend": players_pct_trend,
                })
        except Exception as e:
            api_logger.warning(f"Ошибка получения объектов в get_game_state: {e}")
            buildings_data = []
        
        ready_count, ready_total = game.count_ready_next_round()
        state = {
            "current_round": getattr(game, 'current_round', 1),
            "num_players": len(getattr(game, 'players', [])),
            "ready_next_round_count": ready_count,
            "ready_next_round_total": ready_total,
            "round_trading_locked": bool(getattr(game, 'round_trading_locked', False)),
            "leaderboard": leaderboard_data,
            "prices": prices_data,
            "buildings": buildings_data
        }
        return state
    except Exception as e:
        api_logger.error(f"Ошибка в _get_game_state_internal: {e}", exc_info=True)
        raise

@app.get("/api/game-state")
async def get_game_state(request: Request, game_code: str = Query(..., description="Код игры (6 цифр)")):
    """Получить полное состояние игры"""
    from fastapi.responses import JSONResponse
    try:
        game = await get_game_by_code(game_code)
        state = await _get_game_state_internal(game, game_code)
        return JSONResponse(content=state)
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f"Ошибка в get_game_state: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Внутренняя ошибка: {str(e)}"}
        )

@app.post("/api/game/set-round")
async def set_round(
    request: Request,
    set_round_request: SetRoundRequest,
    game_code: str = Query(..., description="Код игры (6 цифр)")
):
    """Установить текущий раунд"""
    try:
        game = await get_game_by_code(game_code)
        
        round_number = set_round_request.round
        
        # Если устанавливаем раунд 1, просто устанавливаем его
        if round_number == 1:
            game.current_round = 1
            game.start_round()
            game.rebuild_building_pct_round_starts_from_current_players(clear_prev=True)
            await game._persist_building_pct_baselines_to_config()
            game.player_capitalization_by_round_entry = {}
            if game.players:
                game.capture_player_capitalization_at_round_entry(1)
                await game.persist_player_round_entry_capitalization_snapshots_async()
        else:
            # Для других раундов нужно обработать предыдущие раунды
            # чтобы применить события и обновить цены
            while game.current_round < round_number:
                await game.process_round()
        
        await game.save_to_database()
        # Уведомляем всех клиентов (веб, миниапп, админка) об обновлении
        await broadcast_update(game_code=game_code)
        from fastapi.responses import JSONResponse
        return JSONResponse(content={
            "success": True,
            "current_round": game.current_round
        })
    except HTTPException:
        raise
    except Exception as e:
        print(f"Ошибка в set_round: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка: {str(e)}")

@app.post("/api/game/finish-round")
async def finish_round(request: Request, game_code: str = Query(..., description="Код игры (6 цифр)")):
    """Заблокировать торговлю у всех до следующего раунда (ведущий)."""
    try:
        game = await get_game_by_code(game_code)
        result = await game.finish_round_trading_lock()
        await game.save_to_database()
        await broadcast_update(game_code=game_code)
        from fastapi.responses import JSONResponse
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f"finish-round: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка: {type(e).__name__}")


@app.post("/api/game/next-round")
async def next_round(request: Request, game_code: str = Query(..., description="Код игры (6 цифр)")):
    """Перейти к следующему раунду"""
    try:
        game = await get_game_by_code(game_code)
        result = await game.process_round()
        await game.save_to_database()
        await broadcast_update(game_code=game_code)
        from fastapi.responses import JSONResponse
        ready_count, ready_total = game.count_ready_next_round()
        return JSONResponse(content={
            "success": True,
            "current_round": game.current_round,
            "events": result.get("events"),
            "ready_next_round_count": ready_count,
            "ready_next_round_total": ready_total,
        })
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка сервера: {type(e).__name__}")

@app.post("/api/game/new-game")
async def create_new_game(
    request: Request,
    num_players: int = Query(30, description="Количество игроков"),
    company_name: Optional[str] = Query(None, description="Название компании")
):
    """Создать новую игру"""
    try:
        # Создаем новую игру с кодом
        game_id = await database.create_game_with_code(
            num_players=num_players,
            company_name=company_name
        )
        
        # Получаем данные созданной игры напрямую из БД, избегая рекурсии
        from db_config import is_postgresql, get_sqlite_path
        import aiosqlite
        
        if is_postgresql():
            pool = await database.init_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT game_code, current_round FROM games WHERE id = $1", game_id)
                game_code = row['game_code'] if row else None
                current_round = row['current_round'] if row else 1
        else:
            db_path = get_sqlite_path()
            async with aiosqlite.connect(db_path) as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.execute("SELECT game_code, current_round FROM games WHERE id = ?", (game_id,))
                row = await cursor.fetchone()
                game_code = row['game_code'] if row else None
                current_round = row['current_round'] if row else 1
        
        # Отправляем обновление всем WebSocket клиентам для этой игры
        await broadcast_update(game_code=game_code)
        
        from fastapi.responses import JSONResponse
        return JSONResponse(content={
            "success": True,
            "message": "Новая игра создана",
            "game_id": game_id,
            "game_code": game_code,
            "current_round": current_round
        })
    except Exception as e:
        print(f"Ошибка создания новой игры: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Ошибка создания новой игры: {str(e)}")

@app.get("/api/game/snapshots")
async def get_available_snapshots(request: Request, game_code: str = Query(..., description="Код игры (6 цифр)")):
    """Получить список доступных снимков"""
    game = await get_game_by_code(game_code)
    
    snapshots = await database.get_available_snapshots(game.game_id)
    
    from fastapi.responses import JSONResponse
    return JSONResponse(content={
        "snapshots": snapshots,
        "current_round": game.current_round
    })

@app.post("/api/game/rollback")
async def rollback_to_round(
    request: Request,
    rollback_request: RollbackRequest,
    game_code: str = Query(..., description="Код игры (6 цифр)")
):
    """Откатить игру к снимку (0 = базовое состояние из админки)."""
    game = await get_game_by_code(game_code)
    
    target_round = rollback_request.round_number
    
    if target_round >= game.current_round:
        raise HTTPException(
            status_code=400,
            detail=f"Нельзя откатиться на раунд {target_round}, текущий раунд {game.current_round}"
        )
    
    snapshot = await database.load_game_snapshot(game.game_id, target_round)
    if not snapshot:
        raise HTTPException(
            status_code=404,
            detail=f"Снимок для раунда {target_round} не найден. Для отката к началу создайте игру заново."
        )
    
    # Очищаем в БД данные по раундам больше target_round
    await database.clear_rounds_after(game.game_id, target_round)
    
    game.restore_from_snapshot(snapshot)
    if not game.players and target_round == 0:
        player_rows = await database.load_all_players(game.game_id)
        for row in player_rows:
            p = Player(
                id=row["id"],
                name=row.get("name") or "Игрок",
                money=float(STARTING_MONEY),
            )
            if row.get("character_name"):
                p.character_name = row["character_name"]
            if row.get("character_image"):
                p.character_image = row["character_image"]
            p.resources = {}
            p.buildings = []
            game.players.append(p)
    if target_round == 0:
        game.reset_players_economy_to_start()
    game.reset_round_tracking()
    game.trim_player_capitalization_round_snapshots_above(int(game.current_round))
    if (
        game.players
        and int(game.current_round) not in game.player_capitalization_by_round_entry
    ):
        game.capture_player_capitalization_at_round_entry(int(game.current_round))
        await game.persist_player_round_entry_capitalization_snapshots_async()
    await game.save_to_database()
    await game._persist_building_pct_baselines_to_config()
    if target_round == 0:
        await database.save_game_snapshot(game.game_id, 0, game.create_snapshot())
        clear_capitalization_history_for_game_code(game_code)
    await broadcast_update(game_code=game_code)
    
    from fastapi.responses import JSONResponse
    return JSONResponse(content={
        "success": True,
        "message": f"Игра откачена к снимку раунда {target_round}",
        "current_round": game.current_round
    })

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, game_code: str = None):
    """WebSocket для обновлений в реальном времени"""
    await websocket.accept()
    # Читаем game_code из query string (FastAPI не подставляет его в параметр для WebSocket)
    if game_code is None:
        game_code = websocket.query_params.get("game_code")
    
    # Валидация game_code
    if game_code:
        if len(game_code) != 6 or not game_code.isdigit():
            await websocket.close(code=1008, reason="Invalid game_code format")
            return
    else:
        # Для обратной совместимости используем None как ключ
        game_code = None
    
    # Добавляем соединение в соответствующую группу
    if game_code not in active_connections:
        active_connections[game_code] = []
    active_connections[game_code].append(websocket)
    
    # Обновляем метрики (общее количество соединений)
    total_connections = sum(len(conns) for conns in active_connections.values())
    update_websocket_connections(total_connections)
    
    try:
        while True:
            # Отправляем обновление каждую секунду
            if game_code:
                try:
                    game = await get_game_by_code(game_code)
                    if game:
                        state = await _get_game_state_internal(game, game_code)
                        await websocket.send_json(state)
                except Exception as e:
                    api_logger.error(f"Ошибка в WebSocket для game_code {game_code}: {e}", exc_info=True)
            else:
                # Для соединений без game_code не отправляем обновления
                # (можно добавить логику для отправки обновлений всех игр, но это не нужно)
                pass
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    finally:
        # Удаляем соединение из группы
        if game_code in active_connections and websocket in active_connections[game_code]:
            active_connections[game_code].remove(websocket)
            # Удаляем пустую группу
            if not active_connections[game_code]:
                del active_connections[game_code]
        
        # Обновляем метрики
        total_connections = sum(len(conns) for conns in active_connections.values())
        update_websocket_connections(total_connections)

async def broadcast_update(game_code: str = None):
    """Отправить обновление всем подключенным клиентам для конкретной игры"""
    if not game_code:
        api_logger.warning("broadcast_update вызван без game_code, пропускаем")
        return
    
    # Проверяем, есть ли соединения для этой игры
    if game_code not in active_connections or not active_connections[game_code]:
        return  # Нет активных соединений для этой игры
    
    # Получаем состояние игры
    try:
        game = await get_game_by_code(game_code)
        state = await _get_game_state_internal(game, game_code)
    except Exception as e:
        api_logger.error(f"Ошибка в broadcast_update при получении состояния игры {game_code}: {e}", exc_info=True)
        return
    
    # Отправляем обновление только соединениям для этой игры
    disconnected = []
    for connection in active_connections[game_code]:
        try:
            await connection.send_json(state)
        except Exception as e:
            api_logger.warning(f"Ошибка отправки обновления WebSocket для {game_code}: {e}")
            disconnected.append(connection)
    
    # Удаляем отключенные соединения
    for conn in disconnected:
        if conn in active_connections[game_code]:
            active_connections[game_code].remove(conn)
    
    # Удаляем пустую группу
    if game_code in active_connections and not active_connections[game_code]:
        del active_connections[game_code]

def clear_capitalization_history_for_game_code(game_code: str) -> None:
    """Сброс кэша прироста/капитализации по коду игры (после «Начать сначала»)."""
    global player_capitalization_history
    prefix = f"{game_code}:"
    for k in [x for x in player_capitalization_history if x.startswith(prefix)]:
        del player_capitalization_history[k]

# Ответ /api/miniapp/player/state нельзя кэшировать (WebView/прокси иначе показывают старое после «Начать сначала»)
_MINIAPP_STATE_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}

# ========== TELEGRAM MINI APP API ==========

def verify_telegram_auth(init_data: str) -> Optional[Dict]:
    """
    Проверка авторизации через Telegram WebApp API
    В продакшене нужно использовать секретный ключ бота
    Для тестирования упрощенная проверка
    """
    # Проверяем тестовый режим
    is_load_test = os.getenv("LOAD_TEST_MODE", "false").lower() == "true"
    is_test_mode = not os.getenv("TELEGRAM_BOT_TOKEN", "") or is_load_test
    
    # Тестовый режим - упрощенная проверка
    if is_test_mode or is_load_test or init_data == 'test_init_data':
        # Пытаемся извлечь user_id из initData
        try:
            from urllib.parse import parse_qs, unquote
            import json
            import re
            
            # Парсим query string
            parsed = parse_qs(unquote(init_data))
            
            # Пытаемся получить user из данных
            if 'user' in parsed:
                user_str = parsed['user'][0]
                user_data = json.loads(unquote(user_str))
                if 'id' in user_data:
                    return user_data
            
            # Если не удалось распарсить, пробуем извлечь из строки напрямую
            if 'user=' in init_data:
                # Ищем паттерн "id":число
                match = re.search(r'"id"\s*:\s*(\d+)', init_data)
                if match:
                    user_id = int(match.group(1))
                    return {
                        'id': user_id,
                        'first_name': f'Тестовый {user_id}',
                        'last_name': 'Игрок',
                        'username': f'test_player_{user_id}',
                        'language_code': 'ru',
                        'photo_url': 'https://via.placeholder.com/200'
                    }
        except Exception:
            pass
        
        # Дефолтный тестовый пользователь
        return {
            'id': 12345,
            'first_name': 'Тестовый',
            'last_name': 'Игрок',
            'username': 'test_player',
            'language_code': 'ru',
            'photo_url': 'https://via.placeholder.com/200'
        }
    
    try:
        # Парсим init_data (может быть query string или просто строка)
        if 'user=' in init_data:
            params = parse_qs(init_data)
            # Извлекаем данные пользователя
            user_str = params.get('user', [None])[0]
            if user_str:
                user = json.loads(unquote(user_str))
                return user
        else:
            # Если init_data - это просто JSON строка с user
            try:
                user = json.loads(init_data)
                if 'id' in user:
                    return user
            except:
                pass
        
        return None
    except Exception as e:
        print(f"Ошибка проверки авторизации: {e}")
        return None

def get_player_id_from_telegram(init_data: str) -> Optional[str]:
    """Получить player_id из Telegram данных"""
    # Проверяем, находимся ли мы в тестовом режиме
    import os
    is_load_test = os.getenv("LOAD_TEST_MODE", "false").lower() == "true"
    is_test_mode = not os.getenv("TELEGRAM_BOT_TOKEN", "") or is_load_test
    
    # В тестовом режиме упрощаем авторизацию
    if is_test_mode or is_load_test:
        # Пытаемся извлечь user_id из initData
        try:
            from urllib.parse import parse_qs, unquote
            import json
            
            # Парсим query string
            parsed = parse_qs(unquote(init_data))
            
            # Пытаемся получить user из данных
            if 'user' in parsed:
                user_str = parsed['user'][0]
                user_data = json.loads(unquote(user_str))
                if 'id' in user_data:
                    user_id = user_data['id']
                    return f"load_test_player_{user_id}"
            
            # Если не удалось распарсить, пробуем извлечь из строки напрямую
            if 'user=' in init_data:
                # Ищем паттерн "id":число
                import re
                match = re.search(r'"id"\s*:\s*(\d+)', init_data)
                if match:
                    user_id = match.group(1)
                    return f"load_test_player_{user_id}"
            
            # Если ничего не помогло, используем дефолтный ID
            return "load_test_player_1"
        except Exception as e:
            # В случае любой ошибки в тестовом режиме возвращаем дефолтный ID
            return "load_test_player_1"
    
    # Обычный режим - полная проверка
    try:
        # Если initData содержит user=, извлекаем его
        if 'user=' in init_data or '%3D' in init_data:  # %3D это URL-encoded =
            # Парсим query string
            from urllib.parse import parse_qs, unquote
            parsed = parse_qs(unquote(init_data))
            if 'user' in parsed:
                user_str = parsed['user'][0]
                import json
                user_data = json.loads(unquote(user_str))
                if 'id' in user_data:
                    user_id = user_data['id']
                    return f"tg_{user_id}"
    except Exception as e:
        pass
    
    # Пытаемся верифицировать через Telegram
    try:
        # Используем новую функцию из security.telegram_auth
        from security.telegram_auth import verify_telegram_init_data
        try:
            data = verify_telegram_init_data(init_data)
            if data and data.get("user") and data["user"].get("id"):
                user_id = data["user"]["id"]
                return f"tg_{user_id}"
        except ValueError:
            # Если проверка не прошла, пробуем старую функцию для обратной совместимости
            user = verify_telegram_auth(init_data)
            if user and user.get('id'):
                if is_test_mode or is_load_test:
                    return f"load_test_player_{user.get('id')}"
                return f"tg_{user.get('id')}"
    except:
        pass
    
    return None

@app.get("/api/miniapp/player/state")
async def get_player_state(
    request: Request,
    game_code: str = Query(..., description="Код игры (6 цифр)"),
    x_telegram_init_data: Optional[str] = Header(None)
):
    """Получить состояние игрока"""
    try:
        game = await get_game_by_code(game_code)
        
        # В тестовом режиме разрешаем работу без заголовка
        if not x_telegram_init_data:
            x_telegram_init_data = 'test_init_data'
        
        try:
            player_id = get_player_id_from_telegram(x_telegram_init_data)
            if not player_id:
                # В тестовом режиме используем дефолтный ID
                player_id = "tg_12345"
        except Exception as e:
            print(f"Ошибка получения player_id: {e}")
            player_id = "tg_12345"  # Fallback для тестового режима
        
        player = game.get_player(player_id)
        if not player:
            # Игрок не найден - нужно авторизоваться
            from fastapi.responses import JSONResponse
            return JSONResponse(
                content={
                    "player_id": None,
                    "nickname": None,
                    "photo_url": None,
                    "character_name": None,
                    "character_image": None,
                    "money": 0,
                    "resources": {},
                    "buildings": []
                },
                headers=_MINIAPP_STATE_NO_CACHE,
            )
        
        # Формируем ответ
        buildings_data = []
        for building in player.buildings:
            buildings_data.append({
                "id": building.id,
                "name": building.name,
                "status": building.status.value
            })
        
        # Рассчитываем капитализацию (как в get_leaderboard)
        try:
            current_prices = getattr(game, 'current_prices', {})
            player_resources = getattr(player, 'resources', {})
            resources_value = sum(
                amount * current_prices.get(resource, 0)
                for resource, amount in player_resources.items()
            )
        except Exception as e:
            api_logger.warning(f"Ошибка расчета стоимости ресурсов в get_player_state: {e}")
            resources_value = 0
        
        try:
            player_buildings = getattr(player, 'buildings', [])
            buildings_value = sum(
                game.building_value_for_portfolio(b)
                for b in player_buildings
            )
        except Exception as e:
            api_logger.warning(f"Ошибка расчета стоимости объектов в get_player_state: {e}")
            buildings_value = 0
        
        player_money = getattr(player, 'money', 0)
        total_value = player_money + resources_value + buildings_value
        
        growth_round = game.player_growth_round_percent(player_id, float(total_value))
        growth_game = game.player_growth_game_percent(player_id, float(total_value))
        
        # Добавляем стоимость каждого объекта
        buildings_with_value = []
        try:
            player_buildings = getattr(player, 'buildings', [])
            for building in buildings_data:
                try:
                    building_id = building.get("id")
                    building_obj = next((b for b in player_buildings if getattr(b, 'id', None) == building_id), None)
                    if building_obj:
                        try:
                            building["value"] = int(
                                round(game.building_value_for_portfolio(building_obj))
                            )
                        except Exception as e:
                            api_logger.warning(f"Ошибка расчета стоимости объекта {building_id} в get_player_state: {e}")
                            building["value"] = 0
                    else:
                        building["value"] = 0
                    buildings_with_value.append(building)
                except Exception as e:
                    api_logger.warning(f"Ошибка обработки объекта в get_player_state: {e}")
                    building["value"] = 0
                    buildings_with_value.append(building)
        except Exception as e:
            api_logger.warning(f"Ошибка обработки объектов в get_player_state: {e}")
            buildings_with_value = buildings_data
        
        from fastapi.responses import JSONResponse
        try:
            player_resources = getattr(player, 'resources', {})
            return JSONResponse(
                content={
                    "player_id": getattr(player, 'id', player_id),
                    "name": getattr(player, 'name', None),
                    "nickname": getattr(player, 'character_name', None) or getattr(player, 'nickname', None),
                    "photo_url": getattr(player, 'character_image', None) or getattr(player, 'photo_url', None),
                    "character_name": getattr(player, 'character_name', None),
                    "character_image": getattr(player, 'character_image', None),
                    "money": int(round(getattr(player, 'money', 0))),
                    "resources": {k: int(v) for k, v in player_resources.items()},
                    "buildings": buildings_with_value,
                    "capitalization": int(round(total_value)),
                    "growth_round_percent": round(growth_round, 2),
                    "growth_game_percent": round(growth_game, 2),
                    "trading_locked": game.is_trading_locked_for_player(player_id),
                },
                headers=_MINIAPP_STATE_NO_CACHE,
            )
        except Exception as e:
            api_logger.error(f"Ошибка формирования ответа в get_player_state: {e}", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"detail": f"Внутренняя ошибка: {str(e)}"},
                headers=_MINIAPP_STATE_NO_CACHE,
            )
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f"Ошибка в get_player_state: {e}", exc_info=True)
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=500,
            content={"detail": f"Внутренняя ошибка: {str(e)}"},
            headers=_MINIAPP_STATE_NO_CACHE,
        )

@app.post("/api/miniapp/player/ready-next-round")
async def miniapp_player_ready_next_round(
    request: Request,
    game_code: str = Query(..., description="Код игры (6 цифр)"),
    x_telegram_init_data: Optional[str] = Header(None),
):
    """Игрок подтверждает готовность к следующему раунду (блокируется только его торговля)."""
    try:
        game = await get_game_by_code(game_code)
        if not x_telegram_init_data:
            x_telegram_init_data = "test_init_data"
        player_id = get_player_id_from_telegram(x_telegram_init_data) or "tg_12345"
        result = await game.mark_player_ready_next_round(player_id)
        if not result.get("success"):
            raise HTTPException(status_code=404, detail=result.get("message", "Игрок не найден"))
        await broadcast_update(game_code=game_code)
        from fastapi.responses import JSONResponse
        return JSONResponse(content=result, headers=_MINIAPP_STATE_NO_CACHE)
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f"ready-next-round: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка: {type(e).__name__}")


@app.get("/api/miniapp/characters")
async def get_miniapp_characters(
    game_code: str = Query(..., description="Код игры (6 цифр)")
):
    """Получить список персонажей игры (настроенных в админке) для выбора в мини-аппе"""
    try:
        game = await get_game_by_code(game_code)
        game_id = getattr(game, "game_id", None) or getattr(game, "id", None)
        if not game_id:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        characters = await database.get_game_characters(game_id)
        return {"success": True, "characters": characters}
    except HTTPException as he:
        return JSONResponse(status_code=he.status_code, content={"error": he.detail})
    except Exception as e:
        api_logger.error(f"Ошибка получения персонажей для мини-аппа: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/miniapp/characters/taken")
async def get_taken_characters(
    request: Request,
    game_code: str = Query(..., description="Код игры (6 цифр)")
):
    """Получить список уже выбранных персонажей"""
    try:
        game = await get_game_by_code(game_code)
        
        taken_characters = []
        players = getattr(game, 'players', []) or []
        for player in players:
            try:
                player_character_name = getattr(player, 'character_name', None)
                if player_character_name:
                    player_character_image = getattr(player, 'character_image', None)
                    taken_characters.append({
                        "name": player_character_name,
                        "image": player_character_image or ""
                    })
            except Exception:
                continue  # Пропускаем проблемных игроков
        
        from fastapi.responses import JSONResponse
        return JSONResponse(content={"taken_characters": taken_characters})
    except Exception as e:
        print(f"Ошибка в get_taken_characters: {e}")
        import traceback
        traceback.print_exc()
        from fastapi.responses import JSONResponse
        return JSONResponse(content={"taken_characters": []})  # Возвращаем пустой список при ошибке

@app.post("/api/miniapp/player/auth")
async def save_player_auth(
    request: Request,
    auth_request: PlayerAuthRequest,
    game_code: str = Query(..., description="Код игры (6 цифр)"),
    x_telegram_init_data: Optional[str] = Header(None)
):
    """Сохранить данные авторизации игрока (никнейм и фото)"""
    try:
        game = await get_game_by_code(game_code)
        
        # В тестовом режиме разрешаем работу без заголовка
        if not x_telegram_init_data:
            x_telegram_init_data = 'test_init_data'
        
        try:
            player_id = get_player_id_from_telegram(x_telegram_init_data)
            if not player_id:
                # В тестовом режиме используем дефолтный ID
                player_id = "tg_12345"
        except Exception as e:
            print(f"Ошибка получения player_id: {e}")
            player_id = "tg_12345"  # Fallback для тестового режима
        
        # Валидация: должно быть указано либо character_name, либо nickname
        try:
            auth_request.validate_has_name_or_nickname()
        except ValueError as e:
            from fastapi.responses import JSONResponse
            return JSONResponse(content={"success": False, "message": str(e)})
        
        # Поддержка как старого формата (nickname/photo_url), так и нового (character_name/character_image)
        character_name = auth_request.character_name
        character_image = auth_request.character_image
        nickname = auth_request.nickname  # Для обратной совместимости
        photo_url = auth_request.photo_url  # Для обратной совместимости
        
        # Если используется новый формат (выбор персонажа из списка игры)
        if character_name:
            # Проверяем, что персонаж входит в список, настроенный админом для этой игры
            game_characters = await database.get_game_characters(game.game_id)
            allowed_names = {c.get("name") for c in game_characters if c.get("name")}
            if allowed_names and character_name not in allowed_names:
                return JSONResponse(
                    content={"success": False, "message": "Этот персонаж не доступен в данной игре. Выберите персонажа из списка."},
                    status_code=400
                )
            # Проверяем, не выбран ли этот персонаж уже другим игроком
            players = getattr(game, 'players', []) or []
            for player in players:
                try:
                    player_id_attr = getattr(player, 'id', None)
                    player_character_name = getattr(player, 'character_name', None)
                    if player_id_attr and player_id_attr != player_id and player_character_name == character_name:
                        from fastapi.responses import JSONResponse
                        return JSONResponse(content={"success": False, "message": "Этот персонаж уже выбран другим игроком"})
                except Exception:
                    continue  # Пропускаем проблемных игроков
        # Если используется старый формат
        elif nickname:
            character_name = nickname
            character_image = photo_url
        
        # Проверяем, существует ли игрок
        player = game.get_player(player_id)
        if not player:
            # Создаем нового игрока
            try:
                # Используем новую функцию проверки Telegram auth
                telegram_data = verify_telegram_init_data(x_telegram_init_data)
                # ИСПРАВЛЕНИЕ: Проверяем, что telegram_data не None
                if telegram_data and telegram_data.get("user"):
                    user = telegram_data.get("user")
                    if is_valid_telegram_user(user):
                        default_name = user.get('first_name', user.get('username', 'Игрок'))
                    else:
                        default_name = 'Игрок'
                else:
                    # В тестовом режиме используем дефолтное имя
                    default_name = 'Игрок'
            except (ValueError, Exception) as e:
                # В тестовом режиме используем дефолтное имя
                print(f"Ошибка получения данных пользователя: {e}")
                default_name = 'Игрок'
            try:
                await game.add_player(player_id, default_name)
            except Exception as e:
                raise
            player = game.get_player(player_id)
            if not player:
                raise Exception("Игрок не найден после создания")
        
        # ИСПРАВЛЕНИЕ: Проверяем, что character_name и character_image не None
        if character_name:
            player.character_name = character_name
        if character_image:
            player.character_image = character_image
        # Для обратной совместимости также обновляем nickname и photo_url
        if character_name and not player.nickname:
            player.nickname = character_name
        if character_image and not player.photo_url:
            player.photo_url = character_image
        
        # Сохраняем изменения в БД
        try:
            await game.save_to_database()
        except Exception as e:
            raise
        
        from fastapi.responses import JSONResponse
        return JSONResponse(content={
            "success": True,
            "message": "Персонаж выбран успешно",
            "player_id": player_id,
            "character_name": character_name or "",
            "character_image": character_image or "",
            "nickname": character_name or "",  # Для обратной совместимости
            "photo_url": character_image or ""  # Для обратной совместимости
        })
    except HTTPException:
        raise
    except Exception as e:
        api_logger.error(f"Ошибка сохранения персонажа: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": f"Ошибка сервера: {str(e)}"}
        )

@app.get("/api/miniapp/prices")
async def get_miniapp_prices(game_code: str = Query(..., description="Код игры (6 цифр)")):
    """Получить текущие цены с изменениями"""
    game = await get_game_by_code(game_code)
    
    result = []
    initial_prices = RESOURCE_PRICES.copy()
    
    for resource, current_price in sorted(game.current_prices.items()):
        # Получаем предыдущую цену
        if len(game.round_history) >= 2:
            previous_prices = game.round_history[-2].get("prices", initial_prices.copy())
        elif len(game.round_history) == 1:
            previous_prices = initial_prices.copy()
        else:
            previous_prices = initial_prices.copy()
        
        prev_price = previous_prices.get(resource, current_price)
        initial_price = initial_prices.get(resource, current_price)
        
        # Изменение от предыдущего раунда
        if prev_price > 0:
            change_from_prev = ((current_price - prev_price) / prev_price) * 100
        else:
            change_from_prev = 0
        
        # Изменение с начала игры
        if initial_price > 0:
            change_from_start = ((current_price - initial_price) / initial_price) * 100
        else:
            change_from_start = 0
        
        change_prev = round(change_from_prev, 2)
        change_start = round(change_from_start, 2)
        if abs(change_prev) < 1.0:
            change_prev = 0
        if abs(change_start) < 1.0:
            change_start = 0
        result.append({
            "resource": resource,
            "current_price": int(round(current_price)),
            "change_from_prev_percent": change_prev,
            "change_from_start_percent": change_start
        })
    
    from fastapi.responses import JSONResponse
    return JSONResponse(content={"prices": result})

@app.get("/api/miniapp/leaderboard")
async def get_miniapp_leaderboard(game_code: str = Query(..., description="Код игры (6 цифр)")):
    """Получить рейтинг игроков с информацией об изменении позиций и капитализации"""
    game = await get_game_by_code(game_code)
    
    leaderboard = game.get_leaderboard()
    
    # Используем сохраненные предыдущие позиции
    global previous_leaderboard_ranks
    
    # Добавляем nickname, информацию об изменении позиции и росте капитализации
    leaderboard_data = []
    for idx, player_data in enumerate(leaderboard):
        player = game.get_player(player_data["player_id"])
        if player:
            current_rank = idx + 1
            prev_rank = previous_leaderboard_ranks.get(player_data["player_id"])
            
            # Определяем изменение позиции
            rank_change = None  # None = без изменений, "up" = поднялся, "down" = опустился
            if prev_rank:
                if current_rank < prev_rank:
                    rank_change = "up"
                elif current_rank > prev_rank:
                    rank_change = "down"
                # else: rank_change остается None (без изменений)
            
            total_value = float(player_data["total_value"])
            pid = player_data["player_id"]
            growth_round = game.player_growth_round_percent(pid, total_value)
            growth_game = game.player_growth_game_percent(pid, total_value)
            
            leaderboard_data.append({
                "player_id": player_data["player_id"],
                "name": player_data["name"],
                "character_name": getattr(player, 'character_name', None),
                "nickname": player.nickname,
                "character_image": getattr(player, "character_image", None),
                "photo_url": getattr(player, "photo_url", None),
                "buildings_portfolio": _player_buildings_portfolio(player),
                "resources_portfolio": _player_resources_for_leaderboard(game, player),
                "money": int(round(float(player_data.get("money", 0)))),
                "resources_value": int(round(float(player_data.get("resources_value", 0)))),
                "buildings_value": int(round(float(player_data.get("buildings_value", 0)))),
                "total_value": int(round(player_data["total_value"])),
                "rank_change": rank_change,
                "growth_round_percent": growth_round,
                "growth_game_percent": growth_game
            })
    
    # Обновляем сохраненные позиции для следующего запроса
    previous_leaderboard_ranks = {}
    for idx, player_data in enumerate(leaderboard):
        previous_leaderboard_ranks[player_data["player_id"]] = idx + 1
    
    from fastapi.responses import JSONResponse
    return JSONResponse(content={"leaderboard": leaderboard_data})

@app.get("/api/miniapp/round-info")
async def get_round_info(
    request: Request,
    game_code: str = Query(..., description="Код игры (6 цифр)")
):
    """Получить информацию о текущем раунде (в т.ч. для архивированных игр — чтобы раунд в миниаппе продолжал обновляться)."""
    game = await get_game_by_code(game_code, allow_archived=True)
    
    from fastapi.responses import JSONResponse
    response = JSONResponse(content={
        "current_round": game.current_round,
        "num_players": len(game.players)
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get("/api/miniapp/round-events-content")
async def get_miniapp_round_events_content(
    game_code: str = Query(..., description="Код игры (6 цифр)"),
):
    """Текст и картинка события для текущего раунда (админка → вкладка «События»)."""
    game = await get_game_by_code(game_code, allow_archived=True)
    cr = getattr(game, "current_round", None) or 1
    try:
        cr_int = int(cr)
    except (TypeError, ValueError):
        cr_int = 1
    if cr_int < 1:
        cr_int = 1
    if cr_int > 10:
        cr_int = 10

    rows = await database.list_round_events_admin(game.game_id, cr_int)
    event_text = ""
    image_url = ""
    if rows:
        event_text = (rows[0].get("event_text") or "").strip()
        image_url = (rows[0].get("image_url") or "").strip()

    from fastapi.responses import JSONResponse
    response = JSONResponse(
        content={
            "success": True,
            "round_number": cr_int,
            "event_text": event_text,
            "image_url": image_url,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/miniapp/buildings")
async def get_available_buildings(
    game_code: str = Query(..., description="Код игры (6 цифр)"),
    request: Request = None,
    x_telegram_init_data: Optional[str] = Header(None)
):
    """Получить список доступных объектов для строительства"""
    game = await get_game_by_code(game_code)
    
    if not x_telegram_init_data:
        raise HTTPException(status_code=401, detail="Не авторизован")
    
    player_id = get_player_id_from_telegram(x_telegram_init_data)
    if not player_id:
        raise HTTPException(status_code=401, detail="Неверная авторизация")
    
    player = game.get_player(player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    
    result = []
    game_building_costs = getattr(game, "game_building_costs", None) or BUILDING_COSTS
    enabled_buildings = list(getattr(game, "enabled_buildings", None) or list(BUILDING_COSTS.keys()))
    for building_name in enabled_buildings:
        costs = game_building_costs.get(building_name) or BUILDING_COSTS.get(building_name, {})
        can_build = player.has_resources(costs)
        cost = game.calculate_building_cost(building_name)
        
        # Формируем описание стоимости
        cost_details = []
        for resource, amount in costs.items():
            cost_details.append(f"{amount} {resource}")
        
        result.append({
            "name": building_name,
            "cost": int(round(cost)),
            "cost_details": ", ".join(cost_details),
            "can_build": can_build
        })
    
    from fastapi.responses import JSONResponse
    return JSONResponse(content={"buildings": result})

@app.post("/api/miniapp/player/buy-resource")
async def buy_resource_miniapp(
    request: Request,
    buy_request: BuyResourceRequest,
    game_code: str = Query(..., description="Код игры (6 цифр)"),
    x_telegram_init_data: Optional[str] = Header(None)
):
    """Купить ресурс"""
    game = await get_game_by_code(game_code)
    # В тестовом режиме разрешаем работу без заголовка (как в get_player_state и save_player_auth)
    if not x_telegram_init_data:
        x_telegram_init_data = 'test_init_data'
    player_id = get_player_id_from_telegram(x_telegram_init_data)
    if not player_id:
        raise HTTPException(status_code=401, detail="Неверная авторизация")
    # Конфиг нужен для buy_resource (enabled_resources, current_prices)
    if not getattr(game, "enabled_resources", None):
        await game.load_game_config()
    player = game.get_player(player_id)
    if not player:
        raise HTTPException(
            status_code=404,
            detail="Игрок не найден. Сначала выберите персонажа в мини-аппе."
        )
    
    resource = buy_request.resource
    quantity = buy_request.quantity
    result = await game.buy_resource(player_id, resource, quantity)
    result["cost"] = int(round(result.get("cost", 0)))
    return result

@app.post("/api/miniapp/player/sell-resource")
async def sell_resource_miniapp(
    request: Request,
    sell_request: SellResourceRequest,
    game_code: str = Query(..., description="Код игры (6 цифр)"),
    x_telegram_init_data: Optional[str] = Header(None)
):
    """Продать ресурс"""
    game = await get_game_by_code(game_code)
    if not x_telegram_init_data:
        x_telegram_init_data = 'test_init_data'
    player_id = get_player_id_from_telegram(x_telegram_init_data)
    if not player_id:
        raise HTTPException(status_code=401, detail="Неверная авторизация")
    
    if not getattr(game, "enabled_resources", None):
        await game.load_game_config()
    
    player = game.get_player(player_id)
    if not player:
        raise HTTPException(
            status_code=404,
            detail="Игрок не найден. Сначала выберите персонажа в мини-аппе."
        )
    
    resource = sell_request.resource
    quantity = sell_request.quantity
    
    result = await game.sell_resource(player_id, resource, quantity)
    result["income"] = int(round(result.get("income", 0)))
    
    return result

@app.post("/api/miniapp/player/build")
async def build_miniapp(
    request: Request,
    build_request: BuildBuildingRequest,
    game_code: str = Query(..., description="Код игры (6 цифр)"),
    x_telegram_init_data: Optional[str] = Header(None)
):
    """Построить объект"""
    game = await get_game_by_code(game_code)
    if not x_telegram_init_data:
        x_telegram_init_data = 'test_init_data'
    player_id = get_player_id_from_telegram(x_telegram_init_data)
    if not player_id:
        raise HTTPException(status_code=401, detail="Неверная авторизация")
    
    player = game.get_player(player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    
    building_name = build_request.building_name
    
    result = await game.start_building(player_id, building_name)
    return result

@app.post("/api/miniapp/player/sell-building")
async def sell_building_miniapp(
    request: Request,
    sell_building_request: SellBuildingRequest,
    game_code: str = Query(..., description="Код игры (6 цифр)"),
    x_telegram_init_data: Optional[str] = Header(None)
):
    """Продать объект"""
    game = await get_game_by_code(game_code)
    if not x_telegram_init_data:
        x_telegram_init_data = 'test_init_data'
    player_id = get_player_id_from_telegram(x_telegram_init_data)
    if not player_id:
        raise HTTPException(status_code=401, detail="Неверная авторизация")
    
    player = game.get_player(player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    
    building_id = sell_building_request.building_id
    
    result = await game.put_building_for_sale(player_id, building_id)
    return result

# ========== ТЕСТОВЫЕ API ENDPOINTS ==========

def get_test_player_id(x_telegram_init_data: Optional[str], user_id: Optional[int] = None) -> str:
    """Получить player_id для тестового режима"""
    # Если передан user_id напрямую (из initDataUnsafe), используем его
    if user_id:
        return f"tg_{user_id}"
    
    if x_telegram_init_data == 'test_init_data':
        # В тестовом режиме используем фиксированный ID или из заголовка
        return "tg_12345"
    return get_player_id_from_telegram(x_telegram_init_data) or "tg_12345"

@app.post("/api/miniapp/test/reset")
async def test_reset_game(
    game_code: str = Query(..., description="Код игры (6 цифр)"),
    request: Request = None,
    x_telegram_init_data: Optional[str] = Header(None)
):
    """Сбросить тестовую игру"""
    game = await get_game_by_code(game_code)
    
    # Создаем новую тестовую игру напрямую без load_from_db, чтобы избежать рекурсии
    from game_engine import Game as GameClass
    from market_dynamics import MarketDynamics
    from game_events import EventSystem
    from game_config import RESOURCE_PRICES
    
    new_game = GameClass.__new__(GameClass)
    new_game.game_id = game.game_id
    new_game.num_players = 30
    new_game.players = []
    new_game.current_prices = RESOURCE_PRICES.copy()
    new_game.current_round = 1
    new_game.market = MarketDynamics(30)
    new_game.event_system = EventSystem()
    new_game.previous_round_players_bought = {}
    new_game.previous_round_players_sold = {}
    new_game.current_round_players_bought = {}
    new_game.current_round_players_sold = {}
    new_game.round_history = []
    new_game._initialized = True
    new_game._load_from_db = False
    await new_game.add_player("tg_12345", "Тестовый Игрок")
    test_player = new_game.get_player("tg_12345")
    if test_player:
        test_player.nickname = "Тестовый Игрок"
        test_player.photo_url = "https://via.placeholder.com/200"
        test_player.add_resource("дерево", 10)
        test_player.add_resource("камень", 10)
        test_player.add_resource("железо", 5)
        test_player.money = 2000
    
    for i in range(2, 6):
        await new_game.add_player(f"player{i}", f"Игрок {i}")
    
    if test_player:
        await new_game.buy_resource("tg_12345", "железо", 5)
        await new_game.buy_resource("tg_12345", "рабы", 3)
        await new_game.start_building("tg_12345", "Лесоповал")
    
    await new_game.process_round()
    await new_game.save_to_database()
    
    from fastapi.responses import JSONResponse
    return JSONResponse(content={"success": True, "message": "Игра сброшена"})

@app.post("/api/miniapp/test/add-money")
async def test_add_money(
    game_code: str = Query(..., description="Код игры (6 цифр)"),
    request: Request = None,
    x_telegram_init_data: Optional[str] = Header(None)
):
    """Добавить деньги тестовому игроку"""
    game = await get_game_by_code(game_code)
    
    player_id = get_test_player_id(x_telegram_init_data)
    player = game.get_player(player_id)
    
    if not player:
        from fastapi.responses import JSONResponse
        return JSONResponse(content={"success": False, "message": "Игрок не найден"})
    
    player.money += 1000
    await game.save_to_database()
    from fastapi.responses import JSONResponse
    return JSONResponse(content={"success": True, "message": "Добавлено 1000 монет", "new_balance": int(round(player.money))})

@app.post("/api/miniapp/test/add-resources")
async def test_add_resources(
    game_code: str = Query(..., description="Код игры (6 цифр)"),
    request: Request = None,
    x_telegram_init_data: Optional[str] = Header(None)
):
    """Добавить ресурсы тестовому игроку"""
    game = await get_game_by_code(game_code)
    
    player_id = get_test_player_id(x_telegram_init_data)
    player = game.get_player(player_id)
    
    if not player:
        from fastapi.responses import JSONResponse
        return JSONResponse(content={"success": False, "message": "Игрок не найден"})
    
    resources = ["камень", "дерево", "железо", "скот", "овощи", "рабы", "золото", "зерно", "рыба"]
    for resource in resources:
        player.add_resource(resource, 10)
    
    await game.save_to_database()
    from fastapi.responses import JSONResponse
    return JSONResponse(content={"success": True, "message": "Добавлено по 10 каждого ресурса"})

@app.get("/api/miniapp/market/building/{building_name}")
async def get_market_building_details(
    building_name: str,
    game_code: str = Query(..., description="Код игры (6 цифр)"),
    request: Request = None,
    x_telegram_init_data: Optional[str] = Header(None)
):
    """Получить детальную информацию об объекте на бирже (данные на предыдущий раунд)"""
    game = await get_game_by_code(game_code)
    if not x_telegram_init_data:
        x_telegram_init_data = 'test_init_data'
    player_id = get_player_id_from_telegram(x_telegram_init_data)
    if not player_id:
        raise HTTPException(status_code=401, detail="Неверная авторизация")
    
    player = game.get_player(player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    
    # Декодируем название объекта
    building_name = unquote(building_name)
    
    # Получаем данные на предыдущий раунд из истории
    # Если история пуста, используем текущие данные
    if len(game.round_history) > 0:
        # Берем данные из последнего завершенного раунда
        last_round_data = game.round_history[-1]
        # В истории нет информации о зданиях, поэтому используем текущие данные
        # но это данные на момент начала текущего раунда (т.е. на конец предыдущего)
        pass
    
    # Подсчитываем количество объектов на бирже (исключая объекты на продаже)
    building_count = 0
    players_with_building = set()
    owners_list = []  # Список владельцев с количеством объектов
    
    # Защита от None/пустоты
    players = getattr(game, 'players', []) or []
    
    for p in players:
        try:
            player_buildings_list = getattr(p, 'buildings', []) or []
            player_buildings = []
            for b in player_buildings_list:
                try:
                    building_name_attr = getattr(b, 'name', None)
                    building_status = getattr(b, 'status', None)
                    if (building_name_attr == building_name and 
                        building_status and 
                        building_status.value not in ["for_sale", "sold"]):
                        player_buildings.append(b)
                except Exception:
                    continue  # Пропускаем проблемные объекты
            
            if player_buildings:
                building_count += len(player_buildings)
                player_id = getattr(p, 'id', 'unknown')
                players_with_building.add(player_id)
                player_name = getattr(p, 'character_name', None) or getattr(p, 'nickname', None) or getattr(p, 'name', 'Игрок')
                owners_list.append({
                    "player_id": player_id,
                    "name": player_name,
                    "count": len(player_buildings)
                })
        except Exception:
            continue  # Пропускаем проблемных игроков
    
    num_players = len(players)
    players_percentage = round((len(players_with_building) / num_players) * 100) if num_players > 0 else 0
    
    # Получаем стоимость объекта в ресурсах (как в GET /api/buildings)
    from game_config import BUILDING_COSTS, RESOURCE_PRICES
    game_costs_map = getattr(game, "game_building_costs", None) or {}
    building_costs = game_costs_map.get(building_name)
    if building_costs is None:
        building_costs = BUILDING_COSTS.get(building_name, {})
    current_prices = getattr(game, "current_prices", None) or {}
    cost_in_coins = sum(
        float(amount)
        * float(current_prices.get(resource, RESOURCE_PRICES.get(resource, 0)))
        for resource, amount in (building_costs or {}).items()
    )
    
    # Сколько таких объектов у игрока
    player_buildings = [b for b in player.buildings if b.name == building_name]
    player_count = len(player_buildings)
    
    # Получаем информацию о доходе объекта
    from game_config import BUILDING_INCOME
    building_income = BUILDING_INCOME.get(building_name, {"монеты": 0, "ресурсы": {}})
    
    from fastapi.responses import JSONResponse
    return JSONResponse(content={
        "name": building_name,
        "count": building_count,
        "players_percentage": players_percentage,
        "cost_resources": building_costs,
        "cost_coins": int(round(cost_in_coins)),
        "player_count": player_count,
        "owners": owners_list,
        "income": building_income
    })

@app.get("/api/miniapp/player/building/{building_name}")
async def get_player_building_details(
    building_name: str,
    game_code: str = Query(..., description="Код игры (6 цифр)"),
    status: Optional[str] = Query(
        None, description="Статус группы (если не передан building_id)"
    ),
    building_id: Optional[str] = Query(
        None, description="ID конкретного экземпляра объекта у игрока"
    ),
    x_telegram_init_data: Optional[str] = Header(None)
):
    """Получить детальную информацию об объекте игрока"""
    game = await get_game_by_code(game_code)
    
    if not x_telegram_init_data:
        x_telegram_init_data = 'test_init_data'
    
    player_id = get_player_id_from_telegram(x_telegram_init_data)
    if not player_id:
        raise HTTPException(status_code=401, detail="Неверная авторизация")
    
    player = game.get_player(player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    
    # Декодируем название объекта
    building_name = unquote(building_name)
    
    # Находим все объекты этого типа у игрока
    player_buildings = [b for b in player.buildings if b.name == building_name]
    if not player_buildings:
        raise HTTPException(status_code=404, detail="Объект не найден")
    
    single_instance = bool(building_id)

    if building_id:
        building = next(
            (
                b
                for b in player_buildings
                if str(getattr(b, "id", "")) == str(building_id)
            ),
            None,
        )
        if not building:
            raise HTTPException(status_code=404, detail="Объект не найден")
        count = 1
    else:
        buildings_by_status = {}
        for b in player_buildings:
            st = b.status.value
            if st not in buildings_by_status:
                buildings_by_status[st] = []
            buildings_by_status[st].append(b)

        if status:
            buildings = buildings_by_status.get(status, [])
        else:
            buildings = (
                list(buildings_by_status.values())[0] if buildings_by_status else []
            )

        if not buildings:
            raise HTTPException(
                status_code=404, detail="Объект с таким статусом не найден"
            )

        building = buildings[0]
        count = len(buildings)

    # Рассчитываем капитализацию
    # Для объектов "на продаже" используем зафиксированную цену продажи
    # Для активных объектов - текущую стоимость
    if building.status.value == "for_sale" and building.sale_price is not None:
        building_value = building.sale_price
    elif building.status.value == "sold":
        # Проданный объект - стоимость 0
        building_value = 0
    elif building.status.value != "for_sale" and building.status.value != "sold":
        building_value = game.calculate_building_sale_price(building)
    else:
        # Если объект на продаже, но sale_price не установлен (не должно происходить)
        building_value = 0
    total_capitalization = building_value * count
    
    # Рассчитываем реальные доходы из истории раундов
    # Доход за предыдущий раунд (из последнего завершенного раунда)
    # Для объектов "на продаже" показываем доходы так же, как для активных (объект еще не продан)
    # Проданные объекты (SOLD) не приносят доход
    income_per_round = {"монеты": 0, "ресурсы": {}}
    if len(game.round_history) > 0 and building.status.value == "active":
        # Берем доход из последнего завершенного раунда
        last_round_income = game.round_history[-1].get("income", {})
        player_income = last_round_income.get("income_distributed", {}).get(player_id, {})
        
        # Получаем базовый доход из конфига для этого объекта
        from game_config import BUILDING_INCOME
        income_config = BUILDING_INCOME.get(building_name, {"монеты": 0, "ресурсы": {}})
        
        # Считаем, сколько объектов этого типа было активных в предыдущем раунде
        # Только ACTIVE объекты приносят доход
        if single_instance:
            active_buildings_count = (
                1 if building.status.value == "active" else 0
            )
        else:
            active_buildings_count = len(
                [
                    b
                    for b in player_buildings
                    if b.status.value == "active"
                ]
            )
        
        if active_buildings_count > 0:
            # Доход за раунд (на все объекты этого типа)
            income_per_round = {
                "монеты": income_config.get("монеты", 0) * active_buildings_count,
                "ресурсы": {k: v * active_buildings_count for k, v in income_config.get("ресурсы", {}).items()}
            }
    
    # Доход за всю игру - суммируем реально выплаченные доходы из завершенных раундов
    # (не включая текущий раунд, так как он еще не завершен)
    income_per_game = {"монеты": 0, "ресурсы": {}}
    
    # Определяем, с какого раунда объект стал активным
    # Только ACTIVE объекты приносят доход
    active_from_round = None
    if building.status.value == "active" and hasattr(building, 'completed_round') and building.completed_round:
        active_from_round = building.completed_round
    elif building.status.value == "active":
        # Если нет информации, считаем что активен с раунда 1
        active_from_round = 1
    
    if active_from_round:
        # Получаем базовый доход из конфига для расчета пропорции
        from game_config import BUILDING_INCOME
        income_config = BUILDING_INCOME.get(building_name, {"монеты": 0, "ресурсы": {}})
        
        # Суммируем доходы только из завершенных раундов (round_history)
        # round_history содержит только завершенные раунды, текущий раунд еще не завершен
        for round_data in game.round_history:
            round_num = round_data.get("round", 0)
            
            # Проверяем, был ли объект активен в этом раунде
            # Объект активен, если round_num >= active_from_round
            if round_num >= active_from_round:
                # Получаем реально выплаченные доходы игрока из этого раунда
                round_income = round_data.get("income", {})
                player_round_income = round_income.get("income_distributed", {}).get(player_id, {})
                
                # Получаем реальные выплаченные доходы
                real_coins = player_round_income.get("монеты", 0)
                real_resources = player_round_income.get("ресурсы", {})
                
                # Рассчитываем долю этого объекта в общем доходе игрока
                # Для этого считаем, сколько объектов этого типа было активных в том раунде
                # (используем текущее количество как приближение)
                # Только ACTIVE объекты приносят доход
                if single_instance:
                    active_count = (
                        1 if building.status.value == "active" else 0
                    )
                else:
                    active_count = len(
                        [
                            b
                            for b in player_buildings
                            if b.status.value == "active"
                        ]
                    )
                
                # Считаем общий базовый доход от всех активных объектов игрока в том раунде
                # (для расчета пропорции)
                total_base_income_coins = 0
                total_base_income_resources = {}
                
                # Проходим по всем активным объектам игрока
                for b in player_buildings:
                    if b.status.value == "active":
                        b_income = BUILDING_INCOME.get(b.name, {"монеты": 0, "ресурсы": {}})
                        total_base_income_coins += b_income.get("монеты", 0)
                        for res, amt in b_income.get("ресурсы", {}).items():
                            if res not in total_base_income_resources:
                                total_base_income_resources[res] = 0
                            total_base_income_resources[res] += amt
                
                # Рассчитываем долю этого объекта в общем доходе
                if total_base_income_coins > 0 or any(total_base_income_resources.values()):
                    # Доля монет
                    base_income_coins = income_config.get("монеты", 0) * active_count
                    if total_base_income_coins > 0:
                        coins_share = base_income_coins / total_base_income_coins
                        income_per_game["монеты"] += real_coins * coins_share
                    
                    # Доля ресурсов
                    for resource, amount in income_config.get("ресурсы", {}).items():
                        base_income_resource = amount * active_count
                        total_base = total_base_income_resources.get(resource, 0)
                        if total_base > 0:
                            resource_share = base_income_resource / total_base
                            real_amount = real_resources.get(resource, 0)
                            if resource not in income_per_game["ресурсы"]:
                                income_per_game["ресурсы"][resource] = 0
                            income_per_game["ресурсы"][resource] += real_amount * resource_share
                else:
                    # Если нет базового дохода, используем упрощенный расчет
                    if active_count > 0:
                        income_per_game["монеты"] += income_config.get("монеты", 0) * active_count
                        for resource, amount in income_config.get("ресурсы", {}).items():
                            if resource not in income_per_game["ресурсы"]:
                                income_per_game["ресурсы"][resource] = 0
                            income_per_game["ресурсы"][resource] += amount * active_count
    
    # Рассчитываем изменение капитализации
    # Для упрощения: используем базовую стоимость объекта
    from game_config import BUILDING_COSTS, RESOURCE_PRICES
    game_costs_map = getattr(game, "game_building_costs", None) or {}
    building_cost = game_costs_map.get(building_name) or BUILDING_COSTS.get(building_name, {})
    game_resource_prices = getattr(game, "game_resource_prices", None) or RESOURCE_PRICES
    base_cost = sum(
        amount * game_resource_prices.get(resource, RESOURCE_PRICES.get(resource, 0))
        for resource, amount in building_cost.items()
    )
    base_capitalization = base_cost * count
    
    # Изменение капитализации за раунд и за игру
    if base_capitalization > 0:
        change_round = ((total_capitalization - base_capitalization) / base_capitalization) * 100
    else:
        change_round = 0
    
    change_game = change_round  # Для упрощения используем то же значение
    
    # Получаем базовый доход из конфига (как на бирже)
    from game_config import BUILDING_INCOME
    building_income = BUILDING_INCOME.get(building_name, {"монеты": 0, "ресурсы": {}})
    
    from fastapi.responses import JSONResponse
    return JSONResponse(content={
        "name": building_name,
        "id": getattr(building, "id", None),
        "status": building.status.value,
        "count": count,
        "capitalization": int(round(total_capitalization)),
        "change_round_percent": round(change_round, 2),
        "change_game_percent": round(change_game, 2),
        "income_per_round": income_per_round,
        "income_per_game": income_per_game,
        "income": building_income  # Базовый доход из конфига (как на бирже)
    })

@app.post("/api/miniapp/test/create-players")
async def create_test_players(game_code: str = Query(..., description="Код игры (6 цифр)")):
    """Создать 10 тестовых игроков для мультиплеерного тестирования"""
    game = await get_game_by_code(game_code)
    
    created_players = []
    for i in range(1, 11):
        player_id = f"tg_{10000 + i}"
        player_name = f"Игрок {i}"
        
        # Проверяем, существует ли игрок
        if not game.get_player(player_id):
            await game.add_player(player_id, player_name)
            player = game.get_player(player_id)
            if player:
                player.nickname = player_name
                created_players.append(player_id)
    
    await game.save_to_database()
    from fastapi.responses import JSONResponse
    return JSONResponse(content={
        "success": True,
        "created": created_players,
        "total": len(created_players)
    })


@app.post("/api/miniapp/test/next-round")
async def test_next_round(
    game_code: str = Query(..., description="Код игры (6 цифр)"),
    request: Request = None,
    x_telegram_init_data: Optional[str] = Header(None)
):
    """Перейти к следующему раунду"""
    game = await get_game_by_code(game_code)
    
    # Симулируем действия игроков
    player_id = get_test_player_id(x_telegram_init_data)
    player = game.get_player(player_id)
    
    if player:
        # Тестовый игрок покупает случайный ресурс
        import random
        resources = list(game.current_prices.keys())
        if resources:
            resource = random.choice(resources)
            amount = random.randint(1, 5)
            if player.money >= amount * game.current_prices[resource]:
                await game.buy_resource(player_id, resource, amount)
    
    # Обрабатываем раунд
    result = await game.process_round()
    await game.save_to_database()
    
    from fastapi.responses import JSONResponse
    return JSONResponse(content={
        "success": True,
        "message": "Раунд завершен",
        "round": game.current_round,
        "events": result.get("events")
    })

# Обработка favicon.ico (чтобы убрать 404 ошибки)
@app.get("/favicon.ico")
async def favicon():
    """Возвращает пустой ответ для favicon"""
    from fastapi.responses import Response
    return Response(content="", media_type="image/x-icon")

# ========== МОНИТОРИНГ ==========

@app.get("/health")
async def health_check():
    """Health check endpoint - общий статус здоровья приложения"""
    health = await get_health_status()
    status_code = 200 if health["status"] == "healthy" else 503
    from fastapi.responses import JSONResponse
    return JSONResponse(content=health, status_code=status_code)

@app.get("/health/live")
async def liveness_check():
    """Liveness probe - проверка, что приложение запущено"""
    from fastapi.responses import JSONResponse
    return JSONResponse(content={"status": "alive", "timestamp": datetime.now().isoformat()})

@app.get("/health/ready")
async def readiness_check():
    """Readiness probe - проверка готовности к обработке запросов"""
    health = await get_health_status()
    # Готовность зависит от состояния БД
    is_ready = health["database"]["status"] == "healthy"
    status_code = 200 if is_ready else 503
    from fastapi.responses import JSONResponse
    return JSONResponse(content={
        "status": "ready" if is_ready else "not_ready",
        "database": health["database"]["status"],
        "timestamp": datetime.now().isoformat()
    }, status_code=status_code)

@app.get("/metrics")
async def metrics_endpoint():
    """Endpoint для получения метрик приложения"""
    return get_metrics()

@app.get("/metrics/prometheus")
async def prometheus_metrics():
    """Prometheus-совместимый формат метрик"""
    from fastapi.responses import PlainTextResponse
    metrics = get_metrics()
    lines = []
    
    # Метрики запросов
    lines.append(f"# HELP http_requests_total Total number of HTTP requests")
    lines.append(f"# TYPE http_requests_total counter")
    lines.append(f"http_requests_total {metrics['requests']['total']}")
    
    # Метрики по эндпоинтам
    for endpoint, count in metrics['requests']['by_endpoint'].items():
        endpoint_escaped = endpoint.replace(' ', '_').replace('/', '_').replace('{', '').replace('}', '')
        lines.append(f'http_requests_by_endpoint{{endpoint="{endpoint_escaped}"}} {count}')
    
    # Метрики по статусам
    for status, count in metrics['requests']['by_status'].items():
        lines.append(f'http_requests_by_status{{status="{status}"}} {count}')
    
    # Метрики времени ответа
    if metrics['requests']['response_time_stats']:
        stats = metrics['requests']['response_time_stats']
        lines.append(f"# HELP http_request_duration_seconds HTTP request duration")
        lines.append(f"# TYPE http_request_duration_seconds histogram")
        lines.append(f"http_request_duration_seconds_avg {stats['avg'] / 1000}")
        lines.append(f"http_request_duration_seconds_p95 {stats['p95'] / 1000}")
    
    # Метрики ошибок
    lines.append(f"# HELP http_errors_total Total number of HTTP errors")
    lines.append(f"# TYPE http_errors_total counter")
    lines.append(f"http_errors_total {metrics['errors']['total']}")
    
    # Метрики БД
    lines.append(f"# HELP db_queries_total Total number of database queries")
    lines.append(f"# TYPE db_queries_total counter")
    lines.append(f"db_queries_total {metrics['database']['queries_total']}")
    
    # Метрики приложения
    lines.append(f"# HELP app_active_games Number of active games")
    lines.append(f"# TYPE app_active_games gauge")
    lines.append(f"app_active_games {metrics['application']['active_games']}")
    
    lines.append(f"# HELP app_active_players Number of active players")
    lines.append(f"# TYPE app_active_players gauge")
    lines.append(f"app_active_players {metrics['application']['active_players']}")
    
    lines.append(f"# HELP app_websocket_connections Number of WebSocket connections")
    lines.append(f"# TYPE app_websocket_connections gauge")
    lines.append(f"app_websocket_connections {metrics['application']['websocket_connections']}")
    
    lines.append(f"# HELP app_uptime_seconds Application uptime in seconds")
    lines.append(f"# TYPE app_uptime_seconds gauge")
    lines.append(f"app_uptime_seconds {metrics['application']['uptime_seconds']}")
    
    # Системные метрики
    if metrics['system']:
        lines.append(f"# HELP system_cpu_percent CPU usage percentage")
        lines.append(f"# TYPE system_cpu_percent gauge")
        lines.append(f"system_cpu_percent {metrics['system']['cpu_percent']}")
        
        lines.append(f"# HELP system_memory_mb Memory usage in MB")
        lines.append(f"# TYPE system_memory_mb gauge")
        lines.append(f"system_memory_mb {metrics['system']['memory_mb']}")
        
        lines.append(f"# HELP system_memory_percent Memory usage percentage")
        lines.append(f"# TYPE system_memory_percent gauge")
        lines.append(f"system_memory_percent {metrics['system']['memory_percent']}")
    
    return PlainTextResponse(content="\n".join(lines))

# Подключаем статические файлы
# ВАЖНО: более специфичный путь должен быть ПЕРВЫМ, иначе /static перехватит все запросы
# Создаем директорию для видео, если её нет (видео загружаются отдельно)
import os
os.makedirs("static/videos", exist_ok=True)
# Папка для изображений, загруженных через админку (на Railway монтируется как Volume)
os.makedirs("static/images/uploads", exist_ok=True)
app.mount("/static/videos", StaticFiles(directory="static/videos"), name="videos")
app.mount("/static", StaticFiles(directory="static"), name="static")
# Папка design (макеты, иконки для миниаппа)
app.mount("/design", StaticFiles(directory="design"), name="design")

# ========== АДМИН-ПАНЕЛЬ ==========

# Простая система аутентификации через пароль из env
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")  # По умолчанию для разработки
IMGBB_API_KEY = os.getenv("IMGBB_API_KEY", "")  # API ключ для ImgBB
ADMIN_TOKENS: Dict[str, datetime] = {}  # {token: expiration_time}

import secrets

def generate_admin_token() -> str:
    """Генерирует токен для админа"""
    token = secrets.token_urlsafe(32)
    ADMIN_TOKENS[token] = datetime.now() + timedelta(hours=24)  # Токен действует 24 часа
    return token

def verify_admin_token(token: Optional[str]) -> bool:
    """Проверяет токен админа"""
    if not token:
        return False
    if token not in ADMIN_TOKENS:
        return False
    if datetime.now() > ADMIN_TOKENS[token]:
        del ADMIN_TOKENS[token]
        return False
    return True

def get_admin_token(request: Request) -> Optional[str]:
    """Получает токен из заголовка Authorization"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None

@app.get("/admin", response_class=HTMLResponse)
async def admin_panel():
    """Админ-панель"""
    with open("templates/admin.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/api/admin/login")
async def admin_login(request: Request):
    """Авторизация администратора"""
    try:
        data = await request.json()
        password = data.get("password", "")
        
        if password == ADMIN_PASSWORD:
            token = generate_admin_token()
            return {"success": True, "token": token}
        else:
            return JSONResponse(
                status_code=401,
                content={"success": False, "error": "Неверный пароль"}
            )
    except Exception as e:
        api_logger.error(f"Ошибка авторизации админа: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": "Ошибка сервера"}
        )

@app.get("/api/admin/imgbb-api-key")
async def get_imgbb_api_key(request: Request):
    """Получить API ключ ImgBB для загрузки изображений"""
    token = get_admin_token(request)
    if not verify_admin_token(token):
        return JSONResponse(status_code=401, content={"error": "Не авторизован"})
    
    imgbb_api_key = os.getenv("IMGBB_API_KEY")
    if not imgbb_api_key:
        api_logger.error("IMGBB_API_KEY не установлен в переменных окружения")
        return JSONResponse(status_code=500, content={"error": "API ключ ImgBB не настроен на сервере"})
    
    return {"api_key": imgbb_api_key}

UPLOAD_IMAGE_DIR = os.path.join("static", "images", "uploads")
UPLOAD_ALLOWED_EXTENSIONS = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "webp": "webp", "gif": "gif"}
UPLOAD_MAX_BYTES = 5 * 1024 * 1024

@app.post("/api/admin/upload-image")
async def upload_admin_image(request: Request):
    """Сохранить изображение (персонаж, событие раунда) на сервере и вернуть путь к нему"""
    token = get_admin_token(request)
    if not verify_admin_token(token):
        return JSONResponse(status_code=401, content={"error": "Не авторизован"})

    try:
        data = await request.json()
        original_name = (data.get("filename") or "").strip()
        content = data.get("content") or ""

        extension = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
        if extension not in UPLOAD_ALLOWED_EXTENSIONS:
            return JSONResponse(
                status_code=400,
                content={"error": "Поддерживаются только PNG, JPG, WEBP и GIF"}
            )

        if "," in content:
            content = content.split(",", 1)[1]

        try:
            blob = base64.b64decode(content, validate=True)
        except Exception:
            return JSONResponse(status_code=400, content={"error": "Некорректные данные файла"})

        if not blob:
            return JSONResponse(status_code=400, content={"error": "Файл пустой"})
        if len(blob) > UPLOAD_MAX_BYTES:
            return JSONResponse(status_code=400, content={"error": "Размер файла не должен превышать 5 МБ"})

        os.makedirs(UPLOAD_IMAGE_DIR, exist_ok=True)
        # Имя генерируем сами: исходное может содержать кириллицу или путь
        safe_name = f"{secrets.token_urlsafe(12)}.{UPLOAD_ALLOWED_EXTENSIONS[extension]}"
        with open(os.path.join(UPLOAD_IMAGE_DIR, safe_name), "wb") as f:
            f.write(blob)

        api_logger.info(f"Загружено изображение: {safe_name} ({len(blob)} байт)")
        return {"success": True, "url": f"/static/images/uploads/{safe_name}"}
    except Exception as e:
        api_logger.error(f"Ошибка загрузки изображения: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/admin/check")
async def admin_check(request: Request):
    """Проверка токена админа"""
    token = get_admin_token(request)
    if verify_admin_token(token):
        return {"success": True}
    return JSONResponse(
        status_code=401,
        content={"success": False, "error": "Неверный токен"}
    )

@app.get("/api/admin/games/active")
async def get_active_games(request: Request):
    """Получить список активных игр"""
    token = get_admin_token(request)
    if not verify_admin_token(token):
        return JSONResponse(status_code=401, content={"error": "Не авторизован"})
    
    try:
        from db_config import is_postgresql, get_sqlite_path
        import aiosqlite
        
        games = []
        if is_postgresql():
            pool = await database.init_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, game_code, current_round, num_players, status, created_at, updated_at
                    FROM games
                    WHERE status = 'active'
                    ORDER BY created_at DESC
                """)
                for row in rows:
                    games.append({
                        "id": row["id"],
                        "game_code": row["game_code"],
                        "current_round": row["current_round"],
                        "num_players": row["num_players"],
                        "status": row["status"],
                        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None
                    })
        else:
            db_path = get_sqlite_path()
            async with aiosqlite.connect(db_path) as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.execute("""
                    SELECT id, game_code, current_round, num_players, status, created_at, updated_at
                    FROM games
                    WHERE status = 'active'
                    ORDER BY created_at DESC
                """)
                rows = await cursor.fetchall()
                for row in rows:
                    games.append({
                        "id": row["id"],
                        "game_code": row["game_code"],
                        "current_round": row["current_round"],
                        "num_players": row["num_players"],
                        "status": row["status"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"]
                    })
        
        return {"games": games}
    except Exception as e:
        api_logger.error(f"Ошибка получения активных игр: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/admin/games/archived")
async def get_archived_games(request: Request):
    """Получить список архивных игр"""
    token = get_admin_token(request)
    if not verify_admin_token(token):
        return JSONResponse(status_code=401, content={"error": "Не авторизован"})
    
    try:
        from db_config import is_postgresql, get_sqlite_path
        import aiosqlite
        
        games = []
        if is_postgresql():
            pool = await database.init_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, game_code, current_round, num_players, status, created_at, updated_at
                    FROM games
                    WHERE status = 'archived'
                    ORDER BY updated_at DESC
                """)
                for row in rows:
                    games.append({
                        "id": row["id"],
                        "game_code": row["game_code"],
                        "current_round": row["current_round"],
                        "num_players": row["num_players"],
                        "status": row["status"],
                        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                        "archived_at": row["updated_at"].isoformat() if row["updated_at"] else None
                    })
        else:
            db_path = get_sqlite_path()
            async with aiosqlite.connect(db_path) as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.execute("""
                    SELECT id, game_code, current_round, num_players, status, created_at, updated_at
                    FROM games
                    WHERE status = 'archived'
                    ORDER BY updated_at DESC
                """)
                rows = await cursor.fetchall()
                for row in rows:
                    games.append({
                        "id": row["id"],
                        "game_code": row["game_code"],
                        "current_round": row["current_round"],
                        "num_players": row["num_players"],
                        "status": row["status"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "archived_at": row["updated_at"]
                    })
        
        return {"games": games}
    except Exception as e:
        api_logger.error(f"Ошибка получения архивных игр: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/admin/games/create")
async def admin_create_game(request: Request):
    """Создать новую игру через админ-панель"""
    token = get_admin_token(request)
    if not verify_admin_token(token):
        return JSONResponse(status_code=401, content={"error": "Не авторизован"})
    
    try:
        data = await request.json()
        num_players = data.get("num_players", 10)
        company_name = data.get("company_name")
        
        # Валидация num_players
        if not isinstance(num_players, int) or num_players < 5 or num_players > 30:
            return JSONResponse(
                status_code=400,
                content={"error": "Количество игроков должно быть от 5 до 30"}
            )
        
        # create_game_with_code возвращает (game_id, game_code)
        game_id, game_code = await database.create_game_with_code(
            num_players=num_players,
            company_name=company_name
        )
        
        return {"success": True, "game_id": game_id, "game_code": game_code}
    except Exception as e:
        api_logger.error(f"Ошибка создания игры через админку: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/admin/games/{game_code}")
async def get_game_info_admin(game_code: str, request: Request):
    """Получить информацию об игре для админки"""
    token = get_admin_token(request)
    if not verify_admin_token(token):
        return JSONResponse(status_code=401, content={"error": "Не авторизован"})
    
    try:
        game = await get_game_by_code(game_code)
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        return {
            "id": game.game_id,
            "game_code": game_code,
            "current_round": game.current_round,
            "num_players": len(game.players),
            "status": "archived" if hasattr(game, 'status') and game.status == 'archived' else "active",
            "story_id": await database.get_game_story_id(game.game_id) or "",
        }
    except Exception as e:
        api_logger.error(f"Ошибка получения информации об игре: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/admin/games/{game_code}/archive")
async def archive_game(game_code: str, request: Request):
    """Архивировать игру"""
    token = get_admin_token(request)
    if not verify_admin_token(token):
        return JSONResponse(status_code=401, content={"error": "Не авторизован"})
    
    try:
        # Получаем игру по коду
        game = await get_game_by_code(game_code)
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        # Получаем game_id
        game_id = getattr(game, 'game_id', None) or getattr(game, 'id', None)
        if not game_id:
            # Если game_id нет в объекте, получаем из БД по game_code
            from db_config import is_postgresql, get_sqlite_path
            import aiosqlite
            if is_postgresql():
                pool = await database.init_pool()
                async with pool.acquire() as conn:
                    game_id = await conn.fetchval("SELECT id FROM games WHERE game_code = $1", game_code)
            else:
                db_path = get_sqlite_path()
                async with aiosqlite.connect(db_path) as conn:
                    conn.row_factory = aiosqlite.Row
                    cursor = await conn.execute("SELECT id FROM games WHERE game_code = ?", (game_code,))
                    row = await cursor.fetchone()
                    game_id = row["id"] if row else None
        
        if not game_id:
            return JSONResponse(status_code=404, content={"error": "ID игры не найден"})
        
        # Обновляем статус через database.update_game_status
        await database.update_game_status(game_id, 'archived')
        
        return {"success": True, "message": "Игра перемещена в архив"}
    except Exception as e:
        api_logger.error(f"Ошибка архивирования игры: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/admin/games/{game_code}/players")
async def get_game_players_admin(game_code: str, request: Request):
    """Получить список игроков игры"""
    token = get_admin_token(request)
    if not verify_admin_token(token):
        return JSONResponse(status_code=401, content={"error": "Не авторизован"})
    
    try:
        game = await get_game_by_code(game_code)
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        players = []
        for player in game.players:
            # Player использует 'id', а не 'player_id'
            player_id = getattr(player, 'id', None) or getattr(player, 'player_id', None)
            players.append({
                "player_id": player_id,
                "name": player.name,
                "character_name": getattr(player, 'character_name', None),
                "character_image": getattr(player, 'character_image', None)
            })
        
        return {"players": players}
    except Exception as e:
        api_logger.error(f"Ошибка получения игроков: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/admin/games/{game_code}/players")
async def add_game_player(game_code: str, request: Request):
    """Добавить игрока в игру"""
    token = get_admin_token(request)
    if not verify_admin_token(token):
        return JSONResponse(status_code=401, content={"error": "Не авторизован"})
    
    try:
        data = await request.json()
        name = data.get("name")
        avatar_url = data.get("avatar_url")
        
        if not name:
            return JSONResponse(status_code=400, content={"error": "Имя игрока обязательно"})
        
        game = await get_game_by_code(game_code)
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        # Генерируем player_id
        player_id = f"admin_{secrets.token_urlsafe(8)}"
        
        # Добавляем игрока в игру
        from game_engine import Player
        player = Player(player_id, name)
        if avatar_url:
            player.character_image = avatar_url
        game.players.append(player)
        
        # Сохраняем игрока в БД
        await database.save_player(
            player_id=player_id,
            game_id=game.game_id,
            name=name,
            character_name=name,
            character_image=avatar_url or None
        )
        
        return {"success": True, "player_id": player_id}
    except Exception as e:
        api_logger.error(f"Ошибка добавления игрока: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.put("/api/admin/games/{game_code}/players/{player_id}")
async def update_game_player(game_code: str, player_id: str, request: Request):
    """Обновить данные игрока"""
    token = get_admin_token(request)
    if not verify_admin_token(token):
        return JSONResponse(status_code=401, content={"error": "Не авторизован"})
    
    try:
        data = await request.json()
        name = data.get("name")
        character_name = data.get("character_name")
        character_image = data.get("character_image")
        
        if not name or not character_name or not character_image:
            return JSONResponse(status_code=400, content={"error": "Все поля обязательны"})
        
        game = await get_game_by_code(game_code)
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        # Находим игрока
        player = None
        for p in game.players:
            p_id = getattr(p, 'id', None) or getattr(p, 'player_id', None)
            if str(p_id) == str(player_id):
                player = p
                break
        
        if not player:
            return JSONResponse(status_code=404, content={"error": "Игрок не найден"})
        
        # Обновляем данные
        player.name = name
        player.character_name = character_name
        player.character_image = character_image
        
        # Сохраняем в БД
        await database.save_player(
            game_id=game.game_id,
            player_id=player_id,
            name=name,
            money=getattr(player, 'money', 1000),
            character_name=character_name,
            character_image=character_image
        )
        
        return {"success": True, "message": "Игрок обновлен"}
    except Exception as e:
        api_logger.error(f"Ошибка обновления игрока: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/admin/games/{game_code}/characters")
async def get_game_characters_admin(game_code: str, request: Request):
    """Получить список персонажей игры"""
    if not verify_admin_token(get_admin_token(request)):
        return JSONResponse(status_code=401, content={"error": "Неавторизован"})
    
    try:
        try:
            game = await get_game_by_code(game_code)
        except HTTPException as he:
            return JSONResponse(status_code=he.status_code, content={"error": he.detail})
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        # Получаем game_id из объекта game
        game_id = getattr(game, 'game_id', None) or getattr(game, 'id', None)
        if not game_id:
            # Если game_id нет в объекте, получаем из БД по game_code
            from db_config import is_postgresql, get_sqlite_path
            import aiosqlite
            if is_postgresql():
                pool = await database.init_pool()
                async with pool.acquire() as conn:
                    game_id = await conn.fetchval("SELECT id FROM games WHERE game_code = $1", game_code)
            else:
                db_path = get_sqlite_path()
                async with aiosqlite.connect(db_path) as conn:
                    conn.row_factory = aiosqlite.Row
                    cursor = await conn.execute("SELECT id FROM games WHERE game_code = ?", (game_code,))
                    row = await cursor.fetchone()
                    game_id = row["id"] if row else None
        
        if not game_id:
            return JSONResponse(status_code=404, content={"error": "ID игры не найден"})
        
        characters = await database.get_game_characters(game_id)
        return {"success": True, "characters": characters}
    except Exception as e:
        api_logger.error(f"Ошибка получения персонажей: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/admin/games/{game_code}/characters")
async def add_game_character(game_code: str, request: Request):
    """Добавить персонажа в игру"""
    if not verify_admin_token(get_admin_token(request)):
        return JSONResponse(status_code=401, content={"error": "Неавторизован"})
    
    try:
        try:
            game = await get_game_by_code(game_code)
        except HTTPException as he:
            return JSONResponse(status_code=he.status_code, content={"error": he.detail})
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        data = await request.json()
        character_name = data.get("character_name", "").strip()
        character_image = data.get("character_image", "").strip()
        character_description = data.get("character_description", "").strip() or None
        
        if not character_name or not character_image:
            return JSONResponse(status_code=400, content={"error": "Имя и изображение персонажа обязательны"})
        
        await database.save_game_character(
            game_id=game.game_id,
            character_name=character_name,
            character_image=character_image,
            character_description=character_description
        )
        
        return {"success": True, "message": "Персонаж добавлен"}
    except Exception as e:
        api_logger.error(f"Ошибка добавления персонажа: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.delete("/api/admin/games/{game_code}/characters/{character_name}")
async def delete_game_character_admin(game_code: str, character_name: str, request: Request):
    """Удалить персонажа из игры"""
    if not verify_admin_token(get_admin_token(request)):
        return JSONResponse(status_code=401, content={"error": "Неавторизован"})
    
    try:
        try:
            game = await get_game_by_code(game_code)
        except HTTPException as he:
            return JSONResponse(status_code=he.status_code, content={"error": he.detail})
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        character_name = unquote(character_name)
        success = await database.delete_game_character(game.game_id, character_name)
        
        if success:
            return {"success": True, "message": "Персонаж удален"}
        else:
            return JSONResponse(status_code=404, content={"error": "Персонаж не найден"})
    except Exception as e:
        api_logger.error(f"Ошибка удаления персонажа: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/admin/games/{game_code}/round-content")
async def get_round_content_admin(game_code: str, request: Request):
    """Получить контент для раундов"""
    if not verify_admin_token(get_admin_token(request)):
        return JSONResponse(status_code=401, content={"error": "Неавторизован"})
    
    try:
        try:
            game = await get_game_by_code(game_code)
        except HTTPException as he:
            return JSONResponse(status_code=he.status_code, content={"error": he.detail})
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        # Получаем game_id из объекта game
        game_id = getattr(game, 'game_id', None) or getattr(game, 'id', None)
        if not game_id:
            # Если game_id нет в объекте, получаем из БД по game_code
            from db_config import is_postgresql, get_sqlite_path
            import aiosqlite
            if is_postgresql():
                pool = await database.init_pool()
                async with pool.acquire() as conn:
                    game_id = await conn.fetchval("SELECT id FROM games WHERE game_code = $1", game_code)
            else:
                db_path = get_sqlite_path()
                async with aiosqlite.connect(db_path) as conn:
                    conn.row_factory = aiosqlite.Row
                    cursor = await conn.execute("SELECT id FROM games WHERE game_code = ?", (game_code,))
                    row = await cursor.fetchone()
                    game_id = row["id"] if row else None
        
        if not game_id:
            return JSONResponse(status_code=404, content={"error": "ID игры не найден"})
        
        # Загружаем контент из БД
        content = await database.get_round_content(game_id)
        return {"success": True, "content": content}
    except Exception as e:
        api_logger.error(f"Ошибка получения контента раундов: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/admin/games/{game_code}/round-content/{round_number}")
async def save_round_content_admin(game_code: str, round_number: int, request: Request):
    """Сохранить контент для раунда"""
    if not verify_admin_token(get_admin_token(request)):
        return JSONResponse(status_code=401, content={"error": "Неавторизован"})
    
    try:
        try:
            game = await get_game_by_code(game_code)
        except HTTPException as he:
            return JSONResponse(status_code=he.status_code, content={"error": he.detail})
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        data = await request.json()
        content_url = data.get("content_url", "").strip()
        content_type = data.get("content_type", "video")
        
        if not content_url:
            return JSONResponse(status_code=400, content={"error": "URL контента обязателен"})
        
        # Получаем game_id из объекта game
        game_id = getattr(game, 'game_id', None) or getattr(game, 'id', None)
        if not game_id:
            # Если game_id нет в объекте, получаем из БД по game_code
            from db_config import is_postgresql, get_sqlite_path
            import aiosqlite
            if is_postgresql():
                pool = await database.init_pool()
                async with pool.acquire() as conn:
                    game_id = await conn.fetchval("SELECT id FROM games WHERE game_code = $1", game_code)
            else:
                db_path = get_sqlite_path()
                async with aiosqlite.connect(db_path) as conn:
                    conn.row_factory = aiosqlite.Row
                    cursor = await conn.execute("SELECT id FROM games WHERE game_code = ?", (game_code,))
                    row = await cursor.fetchone()
                    game_id = row["id"] if row else None
        
        if not game_id:
            return JSONResponse(status_code=404, content={"error": "ID игры не найден"})
        
        # Валидация round_number (0 = интро, 1–10 = раунды)
        if not isinstance(round_number, int) or round_number < 0 or round_number > 10:
            return JSONResponse(status_code=400, content={"error": "Номер раунда должен быть от 0 до 10 (0 — интро)"})
        
        await database.save_round_content(game_id, round_number, content_url, content_type)
        return {"success": True, "message": "Контент сохранен"}
    except Exception as e:
        api_logger.error(f"Ошибка сохранения контента: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/admin/games/{game_code}/round-events")
async def get_round_events_admin(game_code: str, request: Request):
    """Текст и картинка события для каждого раунда (настройка в админке)."""
    if not verify_admin_token(get_admin_token(request)):
        return JSONResponse(status_code=401, content={"error": "Неавторизован"})
    try:
        try:
            game = await get_game_by_code(game_code)
        except HTTPException as he:
            return JSONResponse(status_code=he.status_code, content={"error": he.detail})
        events = await database.list_round_events_admin(game.game_id)
        return {"success": True, "events": events}
    except Exception as e:
        api_logger.error(f"Ошибка получения событий раундов: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/admin/games/{game_code}/web-events")
async def get_web_events_admin(game_code: str, request: Request):
    """События ВЕБ для проектора (текст, картинки, подсветка ресурсов/объектов)."""
    if not verify_admin_token(get_admin_token(request)):
        return JSONResponse(status_code=401, content={"error": "Неавторизован"})
    try:
        game = await get_game_by_code(game_code)
        events = await database.list_round_web_events_admin(game.game_id)
        return {"success": True, "events": events}
    except HTTPException as he:
        return JSONResponse(status_code=he.status_code, content={"error": he.detail})
    except Exception as e:
        api_logger.error(f"Ошибка получения событий ВЕБ: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/admin/games/{game_code}/web-events/{round_number}")
async def save_web_event_admin(game_code: str, round_number: int, request: Request):
    """Сохранить событие ВЕБ для раунда (1–10)."""
    if not verify_admin_token(get_admin_token(request)):
        return JSONResponse(status_code=401, content={"error": "Неавторизован"})
    try:
        game = await get_game_by_code(game_code)
        if not isinstance(round_number, int) or round_number < 1 or round_number > 10:
            return JSONResponse(
                status_code=400,
                content={"error": "Номер раунда должен быть от 1 до 10"},
            )
        data = await request.json()
        event_text = data.get("event_text", "") or ""
        image_url = data.get("image_url", "") or ""
        bg_image_url = data.get("bg_image_url", "") or ""
        highlight_resources = data.get("highlight_resources") or []
        highlight_buildings = data.get("highlight_buildings") or []
        if not isinstance(highlight_resources, list):
            highlight_resources = []
        if not isinstance(highlight_buildings, list):
            highlight_buildings = []
        await database.save_round_web_event(
            game.game_id,
            round_number,
            str(event_text),
            str(image_url),
            str(bg_image_url),
            [str(x) for x in highlight_resources],
            [str(x) for x in highlight_buildings],
        )
        return {"success": True, "message": "Событие ВЕБ сохранено"}
    except HTTPException as he:
        return JSONResponse(status_code=he.status_code, content={"error": he.detail})
    except Exception as e:
        api_logger.error(f"Ошибка сохранения события ВЕБ: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/admin/games/{game_code}/round-events/{round_number}")
async def save_round_event_admin(game_code: str, round_number: int, request: Request):
    """Сохранить текст/изображение события для раунда (1–10). Оба пустые — очищают только поля админки."""
    if not verify_admin_token(get_admin_token(request)):
        return JSONResponse(status_code=401, content={"error": "Неавторизован"})
    try:
        try:
            game = await get_game_by_code(game_code)
        except HTTPException as he:
            return JSONResponse(status_code=he.status_code, content={"error": he.detail})
        if not isinstance(round_number, int) or round_number < 1 or round_number > 10:
            return JSONResponse(
                status_code=400,
                content={"error": "Номер раунда должен быть от 1 до 10"},
            )
        data = await request.json()
        event_text = data.get("event_text", "")
        image_url = data.get("image_url", "")
        if event_text is None:
            event_text = ""
        if image_url is None:
            image_url = ""
        await database.save_round_event_admin(game.game_id, round_number, str(event_text), str(image_url))
        return {"success": True, "message": "Событие сохранено"}
    except Exception as e:
        api_logger.error(f"Ошибка сохранения события раунда: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/admin/games/{game_code}/round-settings")
async def get_round_settings_admin(game_code: str, request: Request):
    """Получить настройки раундов"""
    if not verify_admin_token(get_admin_token(request)):
        return JSONResponse(status_code=401, content={"error": "Неавторизован"})
    
    try:
        try:
            game = await get_game_by_code(game_code)
        except HTTPException as he:
            return JSONResponse(status_code=he.status_code, content={"error": he.detail})
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        settings = await database.get_round_settings(game.game_id)
        return {"success": True, "settings": settings}
    except Exception as e:
        api_logger.error(f"Ошибка получения настроек раундов: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/admin/games/{game_code}/round-settings/{round_number}")
async def save_round_settings_admin(game_code: str, round_number: int, request: Request):
    """Сохранить настройки раунда"""
    if not verify_admin_token(get_admin_token(request)):
        return JSONResponse(status_code=401, content={"error": "Неавторизован"})
    
    try:
        try:
            game = await get_game_by_code(game_code)
        except HTTPException as he:
            return JSONResponse(status_code=he.status_code, content={"error": he.detail})
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        data = await request.json()
        resource_modifiers = data.get("resource_modifiers", {})
        building_modifiers = data.get("building_modifiers", {})
        resource_texts = data.get("resource_texts", {})
        building_texts = data.get("building_texts", {})
        
        await database.save_round_settings(
            game.game_id, round_number,
            resource_modifiers, building_modifiers,
            resource_texts=resource_texts,
            building_texts=building_texts,
        )
        return {"success": True, "message": "Настройки сохранены"}
    except Exception as e:
        api_logger.error(f"Ошибка сохранения настроек: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


def _load_admin_story_json(story_id: str) -> Dict:
    """Загрузить JSON сюжета из static/stories (только story-N)."""
    story_id = (story_id or "").strip()
    if not re.fullmatch(r"story-\d+", story_id):
        raise ValueError("Некорректный идентификатор сюжета")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    story_path = os.path.join(base_dir, "static", "stories", f"{story_id}.json")
    if not os.path.isfile(story_path):
        raise FileNotFoundError(f"Сюжет «{story_id}» не найден")
    with open(story_path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.post("/api/admin/games/{game_code}/apply-story")
async def apply_story_admin(game_code: str, request: Request):
    """Применить сюжет ко всей игре: раунды, события, события ВЕБ + сохранить story_id."""
    if not verify_admin_token(get_admin_token(request)):
        return JSONResponse(status_code=401, content={"error": "Неавторизован"})
    try:
        game = await get_game_by_code(game_code)
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})

        data = await request.json()
        story_id = (data.get("story_id") or "").strip()
        if not story_id:
            return JSONResponse(status_code=400, content={"error": "story_id обязателен"})

        try:
            story = _load_admin_story_json(story_id)
        except FileNotFoundError as e:
            return JSONResponse(status_code=404, content={"error": str(e)})
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})

        stats = await database.apply_story_to_game(game.game_id, story)
        await database.set_game_story_id(game.game_id, story_id)

        return {
            "success": True,
            "story_id": story_id,
            "story_title": story.get("title") or story_id,
            "stats": stats,
            "message": "Сюжет применён и сохранён",
        }
    except HTTPException as he:
        return JSONResponse(status_code=he.status_code, content={"error": he.detail})
    except Exception as e:
        api_logger.error(f"Ошибка применения сюжета: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/admin/games/{game_code}/config")
async def get_game_config_admin(game_code: str, request: Request):
    """Получить конфигурацию игры (ресурсы и объекты)"""
    if not verify_admin_token(get_admin_token(request)):
        return JSONResponse(status_code=401, content={"error": "Неавторизован"})
    
    try:
        try:
            game = await get_game_by_code(game_code)
        except HTTPException as he:
            return JSONResponse(status_code=he.status_code, content={"error": he.detail})
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        # Загружаем конфигурацию из БД
        config = await database.get_game_config(game.game_id)
        
        # Если конфигурации нет, возвращаем дефолтную
        if not config:
            from game_config import RESOURCE_PRICES, BUILDING_COSTS, BUILDING_INCOME
            config = {
                "enabled_resources": list(RESOURCE_PRICES.keys()),
                "enabled_buildings": list(BUILDING_COSTS.keys()),
                "resource_prices": RESOURCE_PRICES.copy(),
                "building_costs": BUILDING_COSTS.copy(),
                "building_income": BUILDING_INCOME.copy()
            }
        
        return {"success": True, "config": config}
    except Exception as e:
        api_logger.error(f"Ошибка получения конфигурации: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/admin/games/{game_code}/config")
async def save_game_config_admin(game_code: str, request: Request):
    """Сохранить конфигурацию игры (ресурсы и объекты)"""
    if not verify_admin_token(get_admin_token(request)):
        return JSONResponse(status_code=401, content={"error": "Неавторизован"})
    
    try:
        try:
            game = await get_game_by_code(game_code)
        except HTTPException as he:
            return JSONResponse(status_code=he.status_code, content={"error": he.detail})
        if not game:
            return JSONResponse(status_code=404, content={"error": "Игра не найдена"})
        
        data = await request.json()
        config = data.get("config", {})
        
        # Валидация конфигурации
        if not isinstance(config, dict):
            return JSONResponse(status_code=400, content={"error": "Неверный формат конфигурации"})
        
        enabled_resources = config.get("enabled_resources", [])
        enabled_buildings = config.get("enabled_buildings", [])
        resource_prices = config.get("resource_prices", {})
        building_costs = config.get("building_costs", {})
        building_income = config.get("building_income", {})
        
        # Валидация: должна быть хотя бы одна опция
        if not enabled_resources or len(enabled_resources) == 0:
            return JSONResponse(status_code=400, content={"error": "Должен быть выбран хотя бы один ресурс"})
        
        if not enabled_buildings or len(enabled_buildings) == 0:
            return JSONResponse(status_code=400, content={"error": "Должен быть выбран хотя бы один объект"})
        
        # Валидация resource_prices
        if resource_prices:
            if not isinstance(resource_prices, dict):
                return JSONResponse(status_code=400, content={"error": "resource_prices должен быть объектом"})
            
            for resource, price in resource_prices.items():
                if resource not in enabled_resources:
                    return JSONResponse(status_code=400, content={"error": f"Ресурс '{resource}' в resource_prices не включен в enabled_resources"})
                if not isinstance(price, (int, float)) or price <= 0 or math.isnan(price):
                    return JSONResponse(status_code=400, content={"error": f"Цена на ресурс '{resource}' должна быть положительным числом"})
        
        # Валидация building_costs
        if building_costs:
            if not isinstance(building_costs, dict):
                return JSONResponse(status_code=400, content={"error": "building_costs должен быть объектом"})
            
            for building, costs in building_costs.items():
                if building not in enabled_buildings:
                    return JSONResponse(status_code=400, content={"error": f"Объект '{building}' в building_costs не включен в enabled_buildings"})
                if not isinstance(costs, dict):
                    return JSONResponse(status_code=400, content={"error": f"Стоимость объекта '{building}' должна быть объектом"})
                
                for resource, amount in costs.items():
                    if resource not in enabled_resources:
                        return JSONResponse(status_code=400, content={"error": f"Ресурс '{resource}' в стоимости объекта '{building}' не включен в enabled_resources"})
                    if not isinstance(amount, (int, float)) or amount < 0 or math.isnan(amount):
                        return JSONResponse(status_code=400, content={"error": f"Количество ресурса '{resource}' для объекта '{building}' должно быть неотрицательным числом"})
        
        # Валидация building_income
        if building_income:
            if not isinstance(building_income, dict):
                return JSONResponse(status_code=400, content={"error": "building_income должен быть объектом"})
            
            for building, income in building_income.items():
                if building not in enabled_buildings:
                    return JSONResponse(status_code=400, content={"error": f"Объект '{building}' в building_income не включен в enabled_buildings"})
                if not isinstance(income, dict):
                    return JSONResponse(status_code=400, content={"error": f"Доход объекта '{building}' должен быть объектом"})
                
                # Валидация монет
                if "монеты" in income:
                    coins = income["монеты"]
                    if not isinstance(coins, (int, float)) or coins < 0 or math.isnan(coins):
                        return JSONResponse(status_code=400, content={"error": f"Доход в монетах для объекта '{building}' должен быть неотрицательным числом"})
                
                # Валидация ресурсов
                if "ресурсы" in income:
                    resources = income["ресурсы"]
                    if not isinstance(resources, dict):
                        return JSONResponse(status_code=400, content={"error": f"Ресурсы в доходе объекта '{building}' должны быть объектом"})
                    
                    for resource, amount in resources.items():
                        if resource not in enabled_resources:
                            return JSONResponse(status_code=400, content={"error": f"Ресурс '{resource}' в доходе объекта '{building}' не включен в enabled_resources"})
                        if not isinstance(amount, (int, float)) or amount < 0 or math.isnan(amount):
                            return JSONResponse(status_code=400, content={"error": f"Количество ресурса '{resource}' в доходе объекта '{building}' должно быть неотрицательным числом"})
        
        # Не затираем служебные ключи (тренд проектора)
        existing_cfg = await database.get_game_config(game.game_id) or {}
        preserved = existing_cfg.get(PROJECTOR_PCT_BASELINES_CONFIG_KEY)
        if isinstance(preserved, dict):
            config[PROJECTOR_PCT_BASELINES_CONFIG_KEY] = preserved
        
        # Сохраняем конфигурацию в БД
        await database.save_game_config(game.game_id, config)
        
        return {"success": True, "message": "Конфигурация сохранена"}
    except Exception as e:
        api_logger.error(f"Ошибка сохранения конфигурации: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})
