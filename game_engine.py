"""
Игровой движок для "Королевская биржа"
Управляет игровым процессом, раундами, действиями игроков
"""
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum
import math
from game_config import (
    RESOURCE_PRICES, BUILDING_COSTS, BUILDING_INCOME, 
    BUILDING_CONSTRUCTION_TIME, STARTING_MONEY
)
from game_events import EventSystem
from market_dynamics import MarketDynamics
import database

# Служебный ключ в games.config_data: опорные % игроков для тренда на проекторе (переживает рестарт сервера)
PROJECTOR_PCT_BASELINES_CONFIG_KEY = "_projector_building_pct_baselines"
# Капитализация игроков (total_value как в get_leaderboard) на входе в раунд R — один снимок сразу после переключения current_round на R
PLAYER_CAPITALIZATION_BY_ROUND_ENTRY_KEY = "_player_capitalization_round_entry_snapshots"


class BuildingStatus(Enum):
    """Статусы объектов"""
    BUILDING = "building"  # Строится
    ACTIVE = "active"  # Активен, приносит доход (после стройки сразу, без промежуточного статуса)
    FOR_SALE = "for_sale"  # Выставлен на продажу
    SOLD = "sold"  # Продан (не приносит доход, остается в портфеле)


def parse_building_status(s: str) -> BuildingStatus:
    """Статус из БД/снимка. Устаревшее значение 'completed' -> active."""
    if s == "completed":
        return BuildingStatus.ACTIVE
    return BuildingStatus(s)


@dataclass
class Building:
    """Объект игрока"""
    id: str
    name: str
    started_round: int  # Раунд начала строительства
    completed_round: int  # Раунд завершения (started_round + 1)
    status: BuildingStatus = BuildingStatus.BUILDING
    sale_round: Optional[int] = None  # Раунд выставления на продажу
    sale_price: Optional[float] = None  # Цена продажи (фиксируется при выставлении)


@dataclass
class Player:
    """Игрок"""
    id: str
    name: str
    money: float = STARTING_MONEY
    resources: Dict[str, int] = field(default_factory=dict)
    buildings: List[Building] = field(default_factory=list)
    nickname: Optional[str] = None  # Никнейм для игры (устаревшее, используйте character_name)
    photo_url: Optional[str] = None  # URL фото профиля (устаревшее, используйте character_image)
    character_name: Optional[str] = None  # Имя выбранного персонажа
    character_image: Optional[str] = None  # Путь к изображению персонажа
    ready_next_round: bool = False
    ready_next_round_for: Optional[int] = None
    
    def get_resource(self, resource: str) -> int:
        """Получить количество ресурса"""
        return self.resources.get(resource, 0)
    
    def add_resource(self, resource: str, amount: int):
        """Добавить ресурс"""
        if resource not in self.resources:
            self.resources[resource] = 0
        self.resources[resource] += amount
    
    def remove_resource(self, resource: str, amount: int) -> bool:
        """Удалить ресурс (если достаточно)"""
        if self.get_resource(resource) >= amount:
            self.resources[resource] -= amount
            if self.resources[resource] == 0:
                del self.resources[resource]
            return True
        return False
    
    def has_resources(self, costs: Dict[str, int]) -> bool:
        """Проверить, достаточно ли ресурсов"""
        for resource, amount in costs.items():
            if self.get_resource(resource) < amount:
                return False
        return True
    
    def remove_resources(self, costs: Dict[str, int]) -> bool:
        """Удалить ресурсы (если достаточно)"""
        if not self.has_resources(costs):
            return False
        for resource, amount in costs.items():
            self.remove_resource(resource, amount)
        return True
    
    def get_building(self, building_id: str) -> Optional[Building]:
        """Найти объект по ID"""
        for building in self.buildings:
            if building.id == building_id:
                return building
        return None
    
    def remove_building(self, building_id: str) -> bool:
        """Удалить объект"""
        initial_count = len(self.buildings)
        self.buildings = [b for b in self.buildings if b.id != building_id]
        return len(self.buildings) < initial_count


class Game:
    """Игровой движок"""
    
    def __init__(self, num_players: int = 10, game_id: Optional[int] = None, load_from_db: bool = False):
        """
        Args:
            num_players: Количество игроков
            game_id: ID игры в БД (если None, создается новая)
            load_from_db: Загрузить состояние из БД
        """
        self.num_players = num_players
        self.current_round = 1
        self.players: List[Player] = []
        
        # ID игры в БД
        if game_id is None:
            # Создаем новую игру в БД (синхронно, т.к. это в __init__)
            # Используем синхронную обертку для обратной совместимости
            from database import create_game_sync
            self.game_id = create_game_sync(num_players)
        else:
            self.game_id = game_id
        
        # Состояние рынка (будет обновлено в load_game_config)
        self.current_prices = {}
        self.previous_round_players_bought: Dict[str, int] = {}
        self.previous_round_players_sold: Dict[str, int] = {}
        
        # Системы (market будет обновлен в load_game_config с конфигурацией)
        self.market = MarketDynamics(num_players)
        self.event_system = EventSystem()
        
        # История раундов
        self.round_history: List[Dict] = []
        
        # Отслеживание действий текущего раунда (для расчета спроса/предложения)
        self.current_round_players_bought: Dict[str, set] = {}  # {ресурс: set(player_ids)}
        self.current_round_players_sold: Dict[str, set] = {}    # {ресурс: set(player_ids)}
        
        # Флаг для асинхронной инициализации
        self._initialized = False
        self._load_from_db = load_from_db
        
        # Конфигурация игры (ресурсы и объекты)
        # Загружается из БД в initialize()
        self.enabled_resources: List[str] = list(RESOURCE_PRICES.keys())
        self.enabled_buildings: List[str] = list(BUILDING_COSTS.keys())
        self.game_resource_prices: Dict[str, int] = RESOURCE_PRICES.copy()
        self.game_building_costs: Dict[str, Dict[str, int]] = BUILDING_COSTS.copy()
        self.game_building_income: Dict[str, Dict] = BUILDING_INCOME.copy()
        # % игроков с хотя бы одним объектом типа (округлённо), на границах раундов — для тренда на проекторе
        self.building_players_pct_prev_round_start: Dict[str, int] = {}
        self.building_players_pct_current_round_start: Dict[str, int] = {}
        self.player_capitalization_by_round_entry: Dict[int, Dict[str, float]] = {}
        self.round_trading_locked: bool = False
    
    def is_trading_locked_for_player(self, player_id: str) -> bool:
        if self.round_trading_locked:
            return True
        player = self.get_player(player_id)
        if not player:
            return False
        if not player.ready_next_round:
            return False
        return player.ready_next_round_for == self.current_round
    
    def trading_lock_message_for_player(self, player_id: str) -> str:
        if self.round_trading_locked:
            return "Торговля закрыта до следующего раунда"
        return "Вы подтвердили переход к следующему раунду — торговля недоступна"
    
    def count_ready_next_round(self) -> tuple:
        total = len(self.players)
        ready = sum(
            1
            for p in self.players
            if p.ready_next_round and p.ready_next_round_for == self.current_round
        )
        return ready, total
    
    async def mark_player_ready_next_round(self, player_id: str) -> Dict:
        player = self.get_player(player_id)
        if not player:
            return {"success": False, "message": "Игрок не найден"}
        if self.is_trading_locked_for_player(player_id):
            return {"success": True, "message": "Уже готов"}
        player.ready_next_round = True
        player.ready_next_round_for = self.current_round
        await database.set_player_ready_next_round(player.id, True, self.current_round)
        await self.save_player_to_db(player)
        ready, total = self.count_ready_next_round()
        return {
            "success": True,
            "ready_next_round_count": ready,
            "ready_next_round_total": total,
        }
    
    async def finish_round_trading_lock(self) -> Dict:
        self.round_trading_locked = True
        await database.set_game_round_trading_locked(self.game_id, True)
        ready, total = self.count_ready_next_round()
        return {
            "success": True,
            "round_trading_locked": True,
            "ready_next_round_count": ready,
            "ready_next_round_total": total,
        }
    
    async def clear_round_transition_lock_state(self) -> None:
        self.round_trading_locked = False
        await database.set_game_round_trading_locked(self.game_id, False)
        for player in self.players:
            player.ready_next_round = False
            player.ready_next_round_for = None
        await database.clear_all_players_ready_next_round(self.game_id)
    
    async def initialize(self):
        """Асинхронная инициализация игры (загрузка/сохранение в БД)"""
        if self._initialized:
            return
        
        # Загружаем конфигурацию из БД
        await self.load_game_config()
        
        # Загружаем состояние из БД, если нужно
        if self._load_from_db:
            await self.load_from_database()
        else:
            # Новая игра: применяем коэффициенты раунда 1 (цена = база × коэф. раунда 1)
            await self.phase_events(1)
            # Сохраняем начальное состояние
            await self.save_to_database()
            # Создаем снимок начального состояния (раунд 0)
            snapshot = self.create_snapshot()
            from database import save_game_snapshot as _save_game_snapshot_async
            await _save_game_snapshot_async(self.game_id, 0, snapshot)
            # Тренд на проекторе: сравнение начала раунда 1 с снимком 0 (до первого process_round — «плашка», если совпадают)
            self.building_players_pct_prev_round_start = Game.compute_building_players_pct_from_snapshot(snapshot)
            self.building_players_pct_current_round_start = Game.compute_building_players_pct_by_name(self.players)
            await self._persist_building_pct_baselines_to_config()
        
        await self.ensure_player_round_entry_capitalization_baseline_async()
        self._initialized = True
    
    async def load_game_config(self):
        """Загрузить конфигурацию игры из БД"""
        try:
            config = await database.get_game_config(self.game_id)
            if config:
                # Обновляем конфигурацию из БД
                self.enabled_resources = config.get("enabled_resources", list(RESOURCE_PRICES.keys()))
                self.enabled_buildings = config.get("enabled_buildings", list(BUILDING_COSTS.keys()))
                
                # Если есть кастомные цены/стоимости, используем их
                if "resource_prices" in config:
                    self.game_resource_prices = config["resource_prices"]
                if "building_costs" in config:
                    self.game_building_costs = config["building_costs"]
                if "building_income" in config:
                    self.game_building_income = config["building_income"]
                
                # Обновляем текущие цены с учетом конфигурации
                # Оставляем только включенные ресурсы
                filtered_prices = {}
                for resource in self.enabled_resources:
                    if resource in self.game_resource_prices:
                        filtered_prices[resource] = self.game_resource_prices[resource]
                self.current_prices = filtered_prices
                
                # Обновляем market с конфигурацией игры
                self.market.base_prices = self.game_resource_prices.copy()
                self.market.base_incomes = self.game_building_income.copy()
        except Exception as e:
            # Если ошибка загрузки, используем дефолтную конфигурацию
            print(f"Ошибка загрузки конфигурации игры: {e}")
            self.enabled_resources = list(RESOURCE_PRICES.keys())
            self.enabled_buildings = list(BUILDING_COSTS.keys())
            self.game_resource_prices = RESOURCE_PRICES.copy()
            self.game_building_costs = BUILDING_COSTS.copy()
            self.game_building_income = BUILDING_INCOME.copy()
            self.current_prices = RESOURCE_PRICES.copy()
    
    async def save_to_database(self):
        """Сохранить полное состояние игры в БД (асинхронно)"""
        # Используем прямые SQL запросы, избегая рекурсии в синхронных обертках
        from db_config import is_postgresql, get_sqlite_path
        import aiosqlite
        
        if is_postgresql():
            pool = await database.init_pool()
            async with pool.acquire() as conn:
                # Одна транзакция: иначе при пулe asyncpg часть шагов может откатиться при смене/возврате соединения
                async with conn.transaction():
                    # Сохраняем состояние игры
                    await conn.execute(
                        "UPDATE games SET current_round = $1, round_trading_locked = $2, updated_at = CURRENT_TIMESTAMP WHERE id = $3",
                        self.current_round, bool(self.round_trading_locked), self.game_id
                    )
                    
                    # Сохраняем текущие цены
                    await conn.execute("DELETE FROM current_prices WHERE game_id = $1", self.game_id)
                    for resource_name, price in self.current_prices.items():
                        await conn.execute(
                            "INSERT INTO current_prices (game_id, resource_name, price) VALUES ($1, $2, $3)",
                            self.game_id, resource_name, price
                        )
                    
                    await conn.execute("DELETE FROM buildings WHERE game_id = $1", self.game_id)
                    
                    # Сохраняем всех игроков
                    for player in self.players:
                        await conn.execute("""
                            INSERT INTO players 
                            (id, game_id, name, character_name, character_image, money, ready_next_round, ready_next_round_for)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                            ON CONFLICT (id) DO UPDATE SET
                                game_id = EXCLUDED.game_id,
                                name = EXCLUDED.name,
                                character_name = EXCLUDED.character_name,
                                character_image = EXCLUDED.character_image,
                                money = EXCLUDED.money,
                                ready_next_round = EXCLUDED.ready_next_round,
                                ready_next_round_for = EXCLUDED.ready_next_round_for
                        """, player.id, self.game_id, player.name,
                            getattr(player, 'character_name', None),
                            getattr(player, 'character_image', None),
                            player.money,
                            bool(getattr(player, 'ready_next_round', False)),
                            getattr(player, 'ready_next_round_for', None))
                        
                        # Сохраняем ресурсы игрока
                        await conn.execute("DELETE FROM player_resources WHERE player_id = $1", player.id)
                        for resource_name, amount in player.resources.items():
                            if amount > 0:
                                await conn.execute(
                                    "INSERT INTO player_resources (player_id, resource_name, amount) VALUES ($1, $2, $3)",
                                    player.id, resource_name, amount
                                )
                        
                        # Сохраняем объекты игрока
                        for building in player.buildings:
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
                            """, building.id, player.id, self.game_id, building.name,
                                building.started_round, building.completed_round,
                                building.status.value,
                                building.sale_round, building.sale_price)
        else:  # SQLite
            db_path = get_sqlite_path()
            async with aiosqlite.connect(db_path) as conn:
                # Сохраняем состояние игры
                await conn.execute(
                    "UPDATE games SET current_round = ?, round_trading_locked = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (self.current_round, 1 if self.round_trading_locked else 0, self.game_id)
                )
                
                # Сохраняем текущие цены
                await conn.execute("DELETE FROM current_prices WHERE game_id = ?", (self.game_id,))
                for resource_name, price in self.current_prices.items():
                    await conn.execute(
                        "INSERT INTO current_prices (game_id, resource_name, price) VALUES (?, ?, ?)",
                        (self.game_id, resource_name, price)
                    )
                
                await conn.execute("DELETE FROM buildings WHERE game_id = ?", (self.game_id,))
                
                # Сохраняем всех игроков
                for player in self.players:
                    await conn.execute("""
                        INSERT OR REPLACE INTO players 
                        (id, game_id, name, character_name, character_image, money, ready_next_round, ready_next_round_for)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (player.id, self.game_id, player.name,
                        getattr(player, 'character_name', None),
                        getattr(player, 'character_image', None),
                        player.money,
                        1 if getattr(player, 'ready_next_round', False) else 0,
                        getattr(player, 'ready_next_round_for', None)))
                    
                    # Сохраняем ресурсы игрока
                    await conn.execute("DELETE FROM player_resources WHERE player_id = ?", (player.id,))
                    for resource_name, amount in player.resources.items():
                        if amount > 0:
                            await conn.execute(
                                "INSERT INTO player_resources (player_id, resource_name, amount) VALUES (?, ?, ?)",
                                (player.id, resource_name, amount)
                            )
                    
                    # Сохраняем объекты игрока
                    for building in player.buildings:
                        await conn.execute("""
                            INSERT OR REPLACE INTO buildings 
                            (id, player_id, game_id, name, started_round, completed_round, status, sale_round, sale_price)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (building.id, player.id, self.game_id, building.name,
                            building.started_round, building.completed_round,
                            building.status.value,
                            building.sale_round, building.sale_price))
                
                await conn.commit()
    
    async def load_from_database(self):
        """Загрузить полное состояние игры из БД (асинхронно)"""
        # Загружаем состояние игры
        game_data = await database.load_game(self.game_id)
        if game_data:
            self.current_round = game_data['current_round']
            self.num_players = game_data['num_players']
        
        # Загружаем текущие цены
        self.current_prices = await database.load_current_prices(self.game_id)
        if not self.current_prices:
            # Используем конфигурацию игры (уже загружена в load_game_config)
            self.current_prices = self.game_resource_prices.copy()
            # Фильтруем только включенные ресурсы
            filtered_prices = {}
            for resource in self.enabled_resources:
                if resource in self.current_prices:
                    filtered_prices[resource] = self.current_prices[resource]
            self.current_prices = filtered_prices
        
        # Загружаем игроков
        players_data = await database.load_all_players(self.game_id)
        self.players = []
        for p_data in players_data:
            player = Player(
                id=p_data['id'],
                name=p_data['name'],
                money=p_data['money']
            )
            # Добавляем атрибуты персонажа, если есть
            if p_data.get('character_name'):
                player.character_name = p_data['character_name']
            if p_data.get('character_image'):
                player.character_image = p_data['character_image']
            
            # Загружаем ресурсы
            resources = await database.load_player_resources(p_data['id'])
            player.resources = resources
            
            # Загружаем объекты (только для текущей игры)
            buildings_data = await database.load_player_buildings(p_data['id'], self.game_id)
            for b_data in buildings_data:
                s = b_data['status']
                b_status = parse_building_status(str(s))
                building = Building(
                    id=b_data['id'],
                    name=b_data['name'],
                    started_round=b_data['started_round'],
                    completed_round=b_data['completed_round'],
                    status=b_status,
                    sale_round=b_data.get('sale_round'),
                    sale_price=b_data.get('sale_price')
                )
                player.buildings.append(building)
            
            self.players.append(player)
        
        # Загружаем данные о спросе/предложении из предыдущего раунда
        if self.current_round > 1:
            self.previous_round_players_bought, self.previous_round_players_sold = \
                await database.load_round_actions(self.game_id, self.current_round - 1)
        hydrated = await self.hydrate_building_pct_baselines_from_config()
        if not hydrated:
            self.rebuild_building_pct_round_starts_from_current_players(clear_prev=True)
        await self._persist_building_pct_baselines_to_config()
    
    async def save_player_to_db(self, player: Player):
        """Сохранить игрока в БД (асинхронно)"""
        # Используем прямые SQL запросы, избегая рекурсии
        from db_config import is_postgresql, get_sqlite_path
        import aiosqlite
        
        if is_postgresql():
            pool = await database.init_pool()
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO players 
                    (id, game_id, name, character_name, character_image, money, ready_next_round, ready_next_round_for)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (id) DO UPDATE SET
                        game_id = EXCLUDED.game_id,
                        name = EXCLUDED.name,
                        character_name = EXCLUDED.character_name,
                        character_image = EXCLUDED.character_image,
                        money = EXCLUDED.money,
                        ready_next_round = EXCLUDED.ready_next_round,
                        ready_next_round_for = EXCLUDED.ready_next_round_for
                """, player.id, self.game_id, player.name,
                    getattr(player, 'character_name', None),
                    getattr(player, 'character_image', None),
                    player.money,
                    bool(getattr(player, 'ready_next_round', False)),
                    getattr(player, 'ready_next_round_for', None))
                
                await conn.execute("DELETE FROM player_resources WHERE player_id = $1", player.id)
                for resource_name, amount in player.resources.items():
                    if amount > 0:
                        await conn.execute(
                            "INSERT INTO player_resources (player_id, resource_name, amount) VALUES ($1, $2, $3)",
                            player.id, resource_name, amount
                        )
        else:  # SQLite
            db_path = get_sqlite_path()
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute("""
                    INSERT OR REPLACE INTO players 
                    (id, game_id, name, character_name, character_image, money, ready_next_round, ready_next_round_for)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (player.id, self.game_id, player.name,
                    getattr(player, 'character_name', None),
                    getattr(player, 'character_image', None),
                    player.money,
                    1 if getattr(player, 'ready_next_round', False) else 0,
                    getattr(player, 'ready_next_round_for', None)))
                
                await conn.execute("DELETE FROM player_resources WHERE player_id = ?", (player.id,))
                for resource_name, amount in player.resources.items():
                    if amount > 0:
                        await conn.execute(
                            "INSERT INTO player_resources (player_id, resource_name, amount) VALUES (?, ?, ?)",
                            (player.id, resource_name, amount)
                        )
                await conn.commit()
    
    async def save_building_to_db(self, building: Building, player_id: str):
        """Сохранить объект в БД (асинхронно)"""
        # Используем прямые SQL запросы, избегая рекурсии
        from db_config import is_postgresql, get_sqlite_path
        import aiosqlite
        
        if is_postgresql():
            pool = await database.init_pool()
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
                """, building.id, player_id, self.game_id, building.name,
                    building.started_round, building.completed_round,
                    building.status.value,
                    building.sale_round, building.sale_price)
        else:  # SQLite
            db_path = get_sqlite_path()
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute("""
                    INSERT OR REPLACE INTO buildings 
                    (id, player_id, game_id, name, started_round, completed_round, status, sale_round, sale_price)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (building.id, player_id, self.game_id, building.name,
                    building.started_round, building.completed_round,
                    building.status.value,
                    building.sale_round, building.sale_price))
                await conn.commit()
    
    async def add_player(self, player_id: str, player_name: str) -> bool:
        """Добавить игрока (асинхронно)"""
        if len(self.players) >= self.num_players:
            return False
        if any(p.id == player_id for p in self.players):
            return False  # Игрок уже существует
        
        player = Player(id=player_id, name=player_name)
        self.players.append(player)
        # Сохраняем в БД
        await self.save_player_to_db(player)
        return True
    
    def get_player(self, player_id: str) -> Optional[Player]:
        """Получить игрока по ID"""
        sid = str(player_id) if player_id is not None else None
        if sid is None:
            return None
        for player in self.players:
            if str(player.id) == sid:
                return player
        return None
    
    def calculate_building_cost(self, building_name: str) -> float:
        """Рассчитать стоимость объекта в монетах по текущим ценам"""
        # Проверяем, включен ли объект в конфигурации
        if building_name not in self.enabled_buildings:
            return 0.0  # Объект недоступен
        
        costs = self.game_building_costs.get(building_name, {})
        total = 0
        for resource, amount in costs.items():
            # Проверяем, включен ли ресурс в конфигурации
            if resource in self.enabled_resources:
                total += amount * self.current_prices.get(resource, 0)
        return total
    
    def calculate_building_sale_price(self, building: Building) -> float:
        """Рассчитать цену продажи объекта (по текущим ценам ресурсов)"""
        try:
            # Защита от None
            building_name = getattr(building, 'name', None)
            if not building_name:
                return 0.0
            
            # Проверяем, включен ли объект в конфигурации
            if building_name not in self.enabled_buildings:
                return 0.0
            
            costs = self.game_building_costs.get(building_name, {})
            if not costs:
                return 0.0
            
            # Защита от None для current_prices
            if not self.current_prices:
                self.current_prices = self.game_resource_prices.copy()
            
            total = 0.0
            for resource, amount in costs.items():
                # Проверяем, включен ли ресурс в конфигурации
                if resource not in self.enabled_resources:
                    continue
                try:
                    price = self.current_prices.get(resource, 0)
                    total += amount * price
                except Exception:
                    continue  # Пропускаем проблемные ресурсы
            
            return total
        except Exception:
            return 0.0  # В случае любой ошибки возвращаем 0

    def building_value_for_portfolio(self, building: Building) -> float:
        """
        Стоимость объекта в капитализации (суммарно с деньгами и ресурсами):
        продан — 0 (выручка в money); на продаже — фиксированный sale_price;
        иначе — текущая оценка по рынку ресурсов.
        """
        try:
            st = getattr(building, "status", None)
            if st == BuildingStatus.SOLD:
                return 0.0
            if st == BuildingStatus.FOR_SALE:
                sp = getattr(building, "sale_price", None)
                if sp is not None:
                    return float(sp)
                return self.calculate_building_sale_price(building)
            return self.calculate_building_sale_price(building)
        except Exception:
            return 0.0
    
    # ========== ДЕЙСТВИЯ ИГРОКОВ ==========
    
    async def buy_resource(self, player_id: str, resource: str, amount: int) -> Dict:
        """
        Купить ресурс
        
        Returns:
            {"success": bool, "message": str, "cost": float}
        """
        player = self.get_player(player_id)
        if not player:
            return {"success": False, "message": "Игрок не найден"}
        
        if self.is_trading_locked_for_player(player_id):
            return {"success": False, "message": self.trading_lock_message_for_player(player_id)}
        
        # Проверяем, включен ли ресурс в конфигурации
        if resource not in self.enabled_resources:
            return {"success": False, "message": "Ресурс недоступен в этой игре"}
        
        if amount <= 0:
            return {"success": False, "message": "Количество должно быть положительным"}
        
        cost = amount * self.current_prices[resource]
        
        if player.money < cost:
            return {"success": False, "message": f"Недостаточно денег. Нужно {cost:.2f}, есть {player.money:.2f}"}
        
        # Покупаем
        player.money -= cost
        player.add_resource(resource, amount)
        
        # Отслеживаем для расчета спроса
        if resource not in self.current_round_players_bought:
            self.current_round_players_bought[resource] = set()
        self.current_round_players_bought[resource].add(player_id)
        
        # Сохраняем действие в БД (используем прямые SQL запросы, избегая рекурсии)
        from db_config import is_postgresql, get_sqlite_path
        import aiosqlite
        
        if is_postgresql():
            pool = await database.init_pool()
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO player_actions 
                    (game_id, round_number, player_id, action_type, resource_name, amount)
                    VALUES ($1, $2, $3, $4, $5, $6)
                """, self.game_id, self.current_round, player_id, 'buy', resource, amount)
        else:  # SQLite
            db_path = get_sqlite_path()
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute("""
                    INSERT INTO player_actions 
                    (game_id, round_number, player_id, action_type, resource_name, amount)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (self.game_id, self.current_round, player_id, 'buy', resource, amount))
                await conn.commit()
        
        # Сохраняем изменения игрока в БД
        await self.save_player_to_db(player)
        
        return {"success": True, "message": f"Куплено {amount} {resource} за {cost:.2f} монет", "cost": cost}
    
    async def sell_resource(self, player_id: str, resource: str, amount: int) -> Dict:
        """
        Продать ресурс
        
        Returns:
            {"success": bool, "message": str, "income": float}
        """
        player = self.get_player(player_id)
        if not player:
            return {"success": False, "message": "Игрок не найден"}
        
        if self.is_trading_locked_for_player(player_id):
            return {"success": False, "message": self.trading_lock_message_for_player(player_id)}
        
        # Проверяем, включен ли ресурс в конфигурации
        if resource not in self.enabled_resources:
            return {"success": False, "message": "Ресурс недоступен в этой игре"}
        
        if amount <= 0:
            return {"success": False, "message": "Количество должно быть положительным"}
        
        if not player.remove_resource(resource, amount):
            return {"success": False, "message": f"Недостаточно {resource}"}
        
        income = amount * self.current_prices[resource]
        player.money += income
        
        # Отслеживаем для расчета предложения
        if resource not in self.current_round_players_sold:
            self.current_round_players_sold[resource] = set()
        self.current_round_players_sold[resource].add(player_id)
        
        # Сохраняем действие в БД (используем прямые SQL запросы, избегая рекурсии)
        from db_config import is_postgresql, get_sqlite_path
        import aiosqlite
        
        if is_postgresql():
            pool = await database.init_pool()
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO player_actions 
                    (game_id, round_number, player_id, action_type, resource_name, amount)
                    VALUES ($1, $2, $3, $4, $5, $6)
                """, self.game_id, self.current_round, player_id, 'sell', resource, amount)
        else:  # SQLite
            db_path = get_sqlite_path()
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute("""
                    INSERT INTO player_actions 
                    (game_id, round_number, player_id, action_type, resource_name, amount)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (self.game_id, self.current_round, player_id, 'sell', resource, amount))
                await conn.commit()
        
        # Сохраняем изменения игрока в БД
        await self.save_player_to_db(player)
        
        return {"success": True, "message": f"Продано {amount} {resource} за {income:.2f} монет", "income": income}
    
    async def start_building(self, player_id: str, building_name: str) -> Dict:
        """
        Начать строительство объекта (асинхронно)
        
        Returns:
            {"success": bool, "message": str, "building_id": str}
        """
        player = self.get_player(player_id)
        if not player:
            return {"success": False, "message": "Игрок не найден"}
        
        if self.is_trading_locked_for_player(player_id):
            return {"success": False, "message": self.trading_lock_message_for_player(player_id)}
        
        # Проверяем, включен ли объект в конфигурации
        if building_name not in self.enabled_buildings:
            return {"success": False, "message": "Объект недоступен в этой игре"}
        
        # Используем конфигурацию игры (может быть кастомная)
        costs = self.game_building_costs.get(building_name, {})
        if not costs:
            return {"success": False, "message": "Объект не найден в конфигурации"}
        
        if not player.has_resources(costs):
            return {"success": False, "message": "Недостаточно ресурсов"}
        
        # Списываем ресурсы
        player.remove_resources(costs)
        
        # Создаем объект
        building_id = f"{player_id}_{building_name}_{self.current_round}_{len(player.buildings)}"
        building = Building(
            id=building_id,
            name=building_name,
            started_round=self.current_round,
            completed_round=self.current_round + 1,  # Завершится в следующем раунде
            status=BuildingStatus.BUILDING
        )
        player.buildings.append(building)
        
        # Сохраняем объект в БД
        await self.save_building_to_db(building, player_id)
        # Сохраняем изменения игрока в БД
        await self.save_player_to_db(player)
        
        return {"success": True, "message": f"Начато строительство {building_name}", "building_id": building_id}
    
    async def put_building_for_sale(self, player_id: str, building_id: str) -> Dict:
        """
        Выставить объект на продажу (асинхронно)
        
        Returns:
            {"success": bool, "message": str, "sale_price": float}
        """
        player = self.get_player(player_id)
        if not player:
            return {"success": False, "message": "Игрок не найден"}
        
        if self.is_trading_locked_for_player(player_id):
            return {"success": False, "message": self.trading_lock_message_for_player(player_id)}
        
        building = player.get_building(building_id)
        if not building:
            return {"success": False, "message": "Объект не найден"}
        
        if building.status == BuildingStatus.FOR_SALE:
            return {"success": False, "message": "Объект уже выставлен на продажу"}
        
        if building.status == BuildingStatus.SOLD:
            return {"success": False, "message": "Объект уже продан"}
        
        if building.status == BuildingStatus.BUILDING:
            return {"success": False, "message": "Нельзя продать объект, который еще строится"}
        
        # Фиксируем цену продажи (по текущим ценам ресурсов)
        sale_price = self.calculate_building_sale_price(building)
        building.status = BuildingStatus.FOR_SALE
        building.sale_round = self.current_round
        building.sale_price = sale_price
        
        # Сохраняем изменения в БД
        await self.save_building_to_db(building, player_id)
        
        return {"success": True, "message": f"Объект выставлен на продажу за {sale_price:.2f} монет", "sale_price": sale_price}
    
    # ========== ФАЗЫ РАУНДА ==========
    
    async def phase_events(self, round_number: int = None) -> Dict:
        """
        Фаза 1: Обновление цен на ресурсы по коэффициентам из админки.
        Цены пересчитываются только коэффициентами из round_settings (без спроса, предложения и событий).
        Раунд 1: цена = базовая цена из админки × коэффициент раунда 1.
        Раунд N: цена = цена раунда N-1 × коэффициент раунда N.
        """
        if round_number is None:
            round_number = self.current_round
        
        # Загружаем настройки раунда из БД (коэффициенты из админки)
        coefficients = {}
        building_mods = {}
        try:
            db_round_settings_list = await database.get_round_settings(self.game_id, round_number)
            if db_round_settings_list:
                db_round_settings = db_round_settings_list[0]
                coefficients = db_round_settings.get("resource_modifiers", {})
                building_mods = db_round_settings.get("building_modifiers", {})
        except Exception as e:
            print(f"Ошибка загрузки настроек раунда из БД: {e}")
        
        # Предыдущие цены: для раунда 1 — базовые из админки, иначе текущие
        if round_number == 1:
            previous_prices = self.game_resource_prices.copy()
        else:
            previous_prices = self.current_prices.copy()
        
        # Новые цены = предыдущие × коэффициент (если коэффициент не задан — 1.0, цена не меняется)
        new_prices = {}
        for resource in previous_prices:
            coef = coefficients.get(resource, 1.0)
            new_prices[resource] = round(previous_prices[resource] * coef, 2)
        
        self.current_prices = new_prices
        
        return {
            "events": None,
            "resource_modifiers": coefficients,
            "building_modifiers": building_mods,
            "prices_changed": True,
            "new_prices": new_prices.copy()
        }
    
    async def phase_income(self, building_modifiers: Dict[str, float]) -> Dict:
        """
        Фаза 2: Начисление доходов (асинхронно)
        Продажа объектов и начисление дохода от активных объектов
        """
        income_results = {
            "buildings_sold": [],
            "income_distributed": {}
        }
        
        # 1) Продажа объектов из предыдущего раунда
        # Объекты, выставленные на продажу в предыдущем раунде, продаются сейчас
        # Объект выставляется в раунде N, продается в раунде N+1
        # Логика: объект с sale_round = N должен продаться в раунде N+1
        # В начале раунда N+1 current_round = N+1 (увеличен в конце предыдущего process_round)
        # Поэтому проверяем: sale_round < current_round (N < N+1 = True)
        for player in self.players:
            for building in player.buildings:  # Проходим по всем объектам
                if (building.status == BuildingStatus.FOR_SALE and 
                    building.sale_round is not None):
                    # Объект должен быть продан, если он был выставлен в предыдущем раунде
                    # ВАЖНО: current_round увеличивается в конце process_round(), поэтому здесь он еще старый
                    # Объект выставляется в раунде N (sale_round = N), должен продаться в раунде N+1
                    # В начале process_round() для раунда N+1, current_round = N+1 (увеличен в конце предыдущего)
                    # Но в phase_income() current_round еще не увеличен, поэтому проверяем sale_round < current_round
                    # Например: sale_round=2, current_round=3 -> объект продается сейчас (выставлен в раунде 2, продается в раунде 3)
                    # sale_round=3, current_round=3 -> объект НЕ продается (только что выставлен в текущем раунде)
                    # После увеличения current_round до 4: sale_round=3, current_round=4 -> объект продается
                    # ИСПРАВЛЕНИЕ: current_round увеличивается в конце process_round(), поэтому в phase_income() он еще старый
                    # Но когда мы обрабатываем раунд N+1, current_round уже N+1 (увеличен в конце предыдущего process_round)
                    # Поэтому проверяем: sale_round < current_round (объект выставляется в N, продается в N+1)
                    # Например: sale_round=3, current_round=4 -> объект продается (выставлен в раунде 3, продается в раунде 4)
                    # ИСПРАВЛЕНИЕ: current_round увеличивается в конце process_round(), поэтому когда мы обрабатываем раунд N+1,
                    # current_round уже N+1, но в phase_income() он еще не увеличен. Поэтому нужно проверять sale_round < current_round + 1
                    # Например: объект выставлен в раунде 3 (sale_round=3), обрабатываем раунд 4 (current_round=4 в начале process_round)
                    # В phase_income() current_round еще 4, sale_round=3, проверка: 3 < 4+1 = 3 < 5 = True -> объект продается
                    if building.sale_round < self.current_round + 1:
                        # Продаем объект (выставлен в предыдущем раунде)
                        # Вместо удаления меняем статус на SOLD - объект остается в портфеле
                        player.money += building.sale_price
                        building.status = BuildingStatus.SOLD
                        # Сохраняем изменения в БД
                        await self.save_building_to_db(building, player.id)
                        income_results["buildings_sold"].append({
                            "player_id": player.id,
                            "building_name": building.name,
                            "sale_price": building.sale_price
                        })
                        print(f"✅ Объект {building.id} продан у игрока {player.id}, статус изменен на SOLD")
        
        # 2. Начисление дохода от активных объектов
        # Считаем количество каждого типа объектов (только ACTIVE)
        building_counts = {}
        for player in self.players:
            for building in player.buildings:
                if building.status == BuildingStatus.ACTIVE:
                    building_counts[building.name] = building_counts.get(building.name, 0) + 1
        
        # Фильтруем building_counts - только включенные объекты
        filtered_building_counts = {}
        for building_name, count in building_counts.items():
            if building_name in self.enabled_buildings:
                filtered_building_counts[building_name] = count
        
        # Рассчитываем доходы с учетом насыщения и событий
        new_incomes = self.market.calculate_building_incomes(
            filtered_building_counts,
            self.current_prices,
            building_modifiers
        )
        
        # Начисляем доходы игрокам
        for player in self.players:
            player_income = {"монеты": 0, "ресурсы": {}}
            
            for building in player.buildings:
                if building.status == BuildingStatus.ACTIVE:
                    # Проверяем, включен ли объект в конфигурации
                    if building.name not in self.enabled_buildings:
                        continue  # Пропускаем объекты, не включенные в конфигурацию
                    
                    income = new_incomes.get(building.name, {"монеты": 0, "ресурсы": {}})
                    
                    # Монеты - округляем до целых
                    coins = income.get("монеты", 0)
                    coins = int(round(coins))
                    player.money += coins
                    player_income["монеты"] += coins
                    
                    # Ресурсы - округляем вверх, чтобы не терять доход
                    # Фильтруем только включенные в конфигурацию ресурсы
                    for resource, amount in income.get("ресурсы", {}).items():
                        # Пропускаем ресурсы, не включенные в конфигурацию
                        if resource not in self.enabled_resources:
                            continue
                        
                        # Используем math.ceil для округления вверх (чтобы не терять доход)
                        # Если amount > 0, округляем вверх, иначе используем обычное округление
                        if amount > 0:
                            rounded_amount = math.ceil(amount)
                        else:
                            rounded_amount = int(round(amount))
                        player.add_resource(resource, rounded_amount)
                        if resource not in player_income["ресурсы"]:
                            player_income["ресурсы"][resource] = 0
                        player_income["ресурсы"][resource] += rounded_amount
            
            income_results["income_distributed"][player.id] = player_income
            
            # Сохраняем изменения игрока в БД
            await self.save_player_to_db(player)
        
        return income_results
    
    def phase_purchases(self) -> Dict:
        """
        Фаза 3: Закупки
        Игроки совершают действия (покупка, продажа, строительство)
        Эта фаза собирает данные о спросе и предложении для следующего раунда
        """
        # Преобразуем sets в количество игроков
        players_bought = {
            resource: len(player_ids) 
            for resource, player_ids in self.current_round_players_bought.items()
        }
        players_sold = {
            resource: len(player_ids) 
            for resource, player_ids in self.current_round_players_sold.items()
        }
        
        return {
            "players_bought": players_bought,
            "players_sold": players_sold
        }
    
    async def update_state(self, players_bought: Dict[str, int], players_sold: Dict[str, int]):
        """
        Фаза 4: Обновление состояния для следующего раунда (асинхронно)
        """
        # Сохраняем данные для следующего раунда
        self.previous_round_players_bought = players_bought.copy()
        self.previous_round_players_sold = players_sold.copy()
        
        # Обновляем статусы объектов
        # ВАЖНО: current_round увеличивается в конце process_round(), поэтому здесь он еще старый
        # Но мы проверяем для следующего раунда, поэтому используем current_round + 1
        for player in self.players:
            for building in player.buildings:
                if building.status == BuildingStatus.BUILDING:
                    # Готово, когда current_round + 1 >= completed_round; сразу active (доход со следующей фазы income)
                    if (self.current_round + 1) >= building.completed_round:
                        building.status = BuildingStatus.ACTIVE
                        await self.save_building_to_db(building, player.id)
    
    def start_round(self):
        """Начать новый раунд (сбросить отслеживание действий)"""
        self.current_round_players_bought = {}
        self.current_round_players_sold = {}
    
    @staticmethod
    def compute_building_players_pct_by_name(players: List[Player]) -> Dict[str, int]:
        """Доля игроков (0–100), у которых есть хотя бы один объект типа (не for_sale / sold)."""
        players_with_building: Dict[str, int] = {}
        num_players = len(players) if players else 0
        if num_players == 0:
            return {}
        for player in players:
            types_seen: set = set()
            for building in getattr(player, "buildings", None) or []:
                st = getattr(building, "status", None)
                if st is None:
                    continue
                val = st.value if isinstance(st, BuildingStatus) else str(st)
                if val in ("for_sale", "sold"):
                    continue
                name = getattr(building, "name", None) or "unknown"
                types_seen.add(name)
            for name in types_seen:
                players_with_building[name] = players_with_building.get(name, 0) + 1
        return {
            name: round((cnt / num_players) * 100)
            for name, cnt in players_with_building.items()
        }
    
    @staticmethod
    def compute_building_players_pct_from_snapshot(snapshot: Optional[Dict[str, Any]]) -> Dict[str, int]:
        """То же по полю players из create_snapshot / load_game_snapshot."""
        if not snapshot or not snapshot.get("players"):
            return {}
        plist = snapshot["players"]
        num_players = len(plist) if plist else 0
        if num_players == 0:
            return {}
        players_with_building: Dict[str, int] = {}
        for p in plist:
            types_seen: set = set()
            for b in p.get("buildings") or []:
                val = str(b.get("status") or "")
                if val in ("for_sale", "sold"):
                    continue
                name = b.get("name") or "unknown"
                types_seen.add(name)
            for name in types_seen:
                players_with_building[name] = players_with_building.get(name, 0) + 1
        return {
            name: round((cnt / num_players) * 100)
            for name, cnt in players_with_building.items()
        }
    
    @staticmethod
    def _normalize_pct_baselines_map(raw: Any) -> Dict[str, int]:
        if not isinstance(raw, dict):
            return {}
        out: Dict[str, int] = {}
        for k, v in raw.items():
            try:
                out[str(k)] = int(round(float(v)))
            except (TypeError, ValueError):
                continue
        return out
    
    async def hydrate_building_pct_baselines_from_config(self) -> bool:
        """
        Восстановить building_players_pct_* из games.config_data.
        Возвращает True только если блок есть и at_round совпадает с текущим раундом.
        """
        try:
            cfg = await database.get_game_config(self.game_id)
        except Exception:
            return False
        if cfg is None or not isinstance(cfg, dict):
            return False
        block = cfg.get(PROJECTOR_PCT_BASELINES_CONFIG_KEY)
        if not isinstance(block, dict):
            return False
        try:
            at_round = int(block.get("at_round"))
        except (TypeError, ValueError):
            return False
        if at_round != int(self.current_round):
            return False
        self.building_players_pct_prev_round_start = Game._normalize_pct_baselines_map(block.get("prev"))
        self.building_players_pct_current_round_start = Game._normalize_pct_baselines_map(block.get("current"))
        return True
    
    async def _persist_building_pct_baselines_to_config(self) -> None:
        """Сохранить опорные % в games.config_data (слияние с существующим JSON)."""
        try:
            cfg = await database.get_game_config(self.game_id)
        except Exception:
            cfg = None
        if not cfg or not isinstance(cfg, dict):
            merged: Dict[str, Any] = {}
        else:
            merged = dict(cfg)
        merged[PROJECTOR_PCT_BASELINES_CONFIG_KEY] = {
            "prev": dict(self.building_players_pct_prev_round_start),
            "current": dict(self.building_players_pct_current_round_start),
            "at_round": int(self.current_round),
        }
        await database.save_game_config(self.game_id, merged)
    
    def rebuild_building_pct_round_starts_from_current_players(self, clear_prev: bool = True) -> None:
        """Сбросить опорные % для тренда на проекторе (откат, раунд 1 вручную, старт с нуля)."""
        if clear_prev:
            self.building_players_pct_prev_round_start = {}
        self.building_players_pct_current_round_start = Game.compute_building_players_pct_by_name(self.players)
    
    async def process_round(self) -> Dict:
        """
        Обработать полный раунд (асинхронно)
        Вызывается после того, как все игроки совершили действия
        
        Returns:
            Результаты раунда
        """
        round_result = {
            "round": self.current_round,
            "events": None,
            "income": None,
            "prices": self.current_prices.copy()
        }
        
        # Создаем снимок состояния ПЕРЕД обработкой раунда
        # Снимок содержит состояние на начало текущего раунда
        snapshot = self.create_snapshot()
        # Сохраняем снимок через прямые SQL запросы, избегая рекурсии
        from db_config import is_postgresql, get_sqlite_path
        import aiosqlite
        import json
        
        if is_postgresql():
            pool = await database.init_pool()
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO game_snapshots 
                    (game_id, round_number, snapshot_data)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (game_id, round_number) DO UPDATE SET
                        snapshot_data = EXCLUDED.snapshot_data
                """, self.game_id, self.current_round, json.dumps(snapshot))
        else:  # SQLite
            db_path = get_sqlite_path()
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute("""
                    INSERT OR REPLACE INTO game_snapshots 
                    (game_id, round_number, snapshot_data)
                    VALUES (?, ?, ?)
                """, (self.game_id, self.current_round, json.dumps(snapshot)))
                await conn.commit()
        
        # Фаза 1: События
        # События применяются для СЛЕДУЮЩЕГО раунда (current_round + 1)
        # Например, если мы обрабатываем раунд 1, события применяются для раунда 2
        next_round = self.current_round + 1
        events_result = await self.phase_events(next_round)
        round_result["events"] = events_result["events"]
        round_result["prices"] = events_result["new_prices"]
        
        # Фаза 2: Начисление доходов
        building_modifiers = events_result.get("building_modifiers", {})
        income_result = await self.phase_income(building_modifiers)
        round_result["income"] = income_result
        
        # Фаза 3: Закупки (собираем данные о спросе/предложении)
        purchases_result = self.phase_purchases()
        players_bought = purchases_result["players_bought"]
        players_sold = purchases_result["players_sold"]
        
        # Фаза 4: Обновление состояния
        await self.update_state(players_bought, players_sold)
        
        # Сохраняем историю
        self.round_history.append(round_result)
        
        # Сохраняем события раунда в БД (используем прямые SQL запросы, избегая рекурсии)
        from db_config import is_postgresql, get_sqlite_path
        import aiosqlite
        
        if is_postgresql():
            pool = await database.init_pool()
            async with pool.acquire() as conn:
                if round_result.get("events"):
                    events = round_result["events"]
                    await conn.execute("""
                        INSERT INTO round_events 
                        (game_id, round_number, positive_event, negative_event, positive2_event)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (game_id, round_number) DO UPDATE SET
                            positive_event = EXCLUDED.positive_event,
                            negative_event = EXCLUDED.negative_event,
                            positive2_event = EXCLUDED.positive2_event
                    """, self.game_id, next_round,
                        events.get("positive"), events.get("negative"), events.get("positive2"))
                
                # Сохраняем цены раунда в БД (история)
                for resource_name, price in round_result["prices"].items():
                    await conn.execute("""
                        INSERT INTO resource_prices 
                        (game_id, round_number, resource_name, price)
                        VALUES ($1, $2, $3, $4)
                        ON CONFLICT (game_id, round_number, resource_name) DO UPDATE SET
                            price = EXCLUDED.price
                    """, self.game_id, next_round, resource_name, price)
                
                # Очищаем действия текущего раунда в БД (они уже обработаны)
                await conn.execute(
                    "DELETE FROM player_actions WHERE game_id = $1 AND round_number = $2",
                    self.game_id, self.current_round - 1
                )
        else:  # SQLite
            db_path = get_sqlite_path()
            async with aiosqlite.connect(db_path) as conn:
                if round_result.get("events"):
                    events = round_result["events"]
                    await conn.execute("""
                        INSERT OR REPLACE INTO round_events 
                        (game_id, round_number, positive_event, negative_event, positive2_event)
                        VALUES (?, ?, ?, ?, ?)
                    """, (self.game_id, next_round,
                        events.get("positive"), events.get("negative"), events.get("positive2")))
                
                # Сохраняем цены раунда в БД (история)
                for resource_name, price in round_result["prices"].items():
                    await conn.execute("""
                        INSERT OR REPLACE INTO resource_prices 
                        (game_id, round_number, resource_name, price)
                        VALUES (?, ?, ?, ?)
                    """, (self.game_id, next_round, resource_name, price))
                
                # Очищаем действия текущего раунда в БД (они уже обработаны)
                await conn.execute(
                    "DELETE FROM player_actions WHERE game_id = ? AND round_number = ?",
                    (self.game_id, self.current_round - 1)
                )
                await conn.commit()
        
        # Переходим к следующему раунду
        self.current_round += 1
        
        await self.clear_round_transition_lock_state()
        
        # Сбрасываем отслеживание для следующего раунда
        self.start_round()
        
        # Тренд % игроков на проекторе: начало нового раунда vs начало предыдущего (стабильно до следующего process_round)
        new_start = Game.compute_building_players_pct_by_name(self.players)
        self.building_players_pct_prev_round_start = dict(self.building_players_pct_current_round_start)
        self.building_players_pct_current_round_start = new_start
        await self._persist_building_pct_baselines_to_config()
        
        # Снимок капитализации на вход в новый текущий раунд (после current_round += 1)
        self.capture_player_capitalization_at_round_entry(int(self.current_round))
        await self.persist_player_round_entry_capitalization_snapshots_async()
        
        # Сохраняем полное состояние в БД
        await self.save_to_database()
        
        return round_result
    
    def leaderboard_player_totals_by_id(self) -> Dict[str, float]:
        """total_value каждого игрока как в get_leaderboard (ключ — str(player_id))."""
        return {
            str(row["player_id"]): float(row["total_value"])
            for row in self.get_leaderboard()
        }
    
    def capture_player_capitalization_at_round_entry(self, round_no: int) -> None:
        """Зафиксировать капитализацию игроков на входе в раунд ``round_no``."""
        try:
            r = int(round_no)
        except (TypeError, ValueError):
            return
        if not self.players:
            self.player_capitalization_by_round_entry.pop(r, None)
            return
        self.player_capitalization_by_round_entry[r] = self.leaderboard_player_totals_by_id()
    
    @staticmethod
    def parse_round_entry_snapshots_from_config(fragment: Any) -> Dict[int, Dict[str, float]]:
        """Разобрать JSON из games.config_data в словарь {раунд: {player_id: total}}."""
        if not fragment or not isinstance(fragment, dict):
            return {}
        out: Dict[int, Dict[str, float]] = {}
        for rk, raw_map in fragment.items():
            try:
                r = int(rk)
            except (TypeError, ValueError):
                continue
            if not isinstance(raw_map, dict):
                continue
            inner: Dict[str, float] = {}
            for pk, val in raw_map.items():
                try:
                    inner[str(pk)] = float(val)
                except (TypeError, ValueError):
                    continue
            if inner:
                out[r] = inner
        return out
    
    def trim_player_capitalization_round_snapshots_above(self, max_round: int) -> None:
        """Удалить снимки раундов строго больше ``max_round`` (откат)."""
        try:
            mx = int(max_round)
        except (TypeError, ValueError):
            return
        self.player_capitalization_by_round_entry = {
            r: d
            for r, d in self.player_capitalization_by_round_entry.items()
            if r <= mx
        }
    
    async def hydrate_player_round_entry_cap_from_db(self) -> None:
        """Загрузить снимки из games.config_data."""
        try:
            cfg = await database.get_game_config(self.game_id)
        except Exception:
            cfg = None
        frag = cfg.get(PLAYER_CAPITALIZATION_BY_ROUND_ENTRY_KEY) if isinstance(cfg, dict) else {}
        self.player_capitalization_by_round_entry = Game.parse_round_entry_snapshots_from_config(frag)
    
    async def persist_player_round_entry_capitalization_snapshots_async(self) -> None:
        """Сохранить снимки в games.config_data (слияние)."""
        try:
            cfg = await database.get_game_config(self.game_id)
        except Exception:
            cfg = None
        merged: Dict[str, Any] = dict(cfg) if isinstance(cfg, dict) else {}
        serial: Dict[str, Dict[str, float]] = {}
        for r, pmap in sorted(self.player_capitalization_by_round_entry.items()):
            serial[str(r)] = {str(k): float(v) for k, v in pmap.items()}
        merged[PLAYER_CAPITALIZATION_BY_ROUND_ENTRY_KEY] = serial
        await database.save_game_config(self.game_id, merged)
    
    async def ensure_player_round_entry_capitalization_baseline_async(self) -> None:
        """Загрузить снимки, подрезать под текущий раунд; при отсутствии снимка — записать текущее."""
        await self.hydrate_player_round_entry_cap_from_db()
        r = int(getattr(self, "current_round", 1) or 1)
        self.trim_player_capitalization_round_snapshots_above(r)
        if self.players and r not in self.player_capitalization_by_round_entry:
            self.capture_player_capitalization_at_round_entry(r)
            await self.persist_player_round_entry_capitalization_snapshots_async()
    
    def player_growth_round_percent(self, player_id: str, current_total_value: float) -> float:
        """
        Доходность за последнее переключение раунда: снимок(текущий раунд) / снимок(прошлый) - 1.
        Снимок текущего раунда задаётся на входе после process_round (при желании можно подставить live total).
        """
        try:
            R = int(self.current_round)
        except (TypeError, ValueError):
            return 0.0
        if R < 2:
            return 0.0
        pid = str(player_id)
        by_r = self.player_capitalization_by_round_entry
        cur_map = by_r.get(R) or {}
        prev_map = by_r.get(R - 1) or {}
        cap_prev = prev_map.get(pid)
        if cap_prev is None or cap_prev <= 0:
            return 0.0
        cap_cur = cur_map.get(pid)
        if cap_cur is None:
            cap_cur = float(current_total_value)
        return round(((cap_cur / cap_prev) - 1.0) * 100.0, 2)
    
    def player_growth_game_percent(self, player_id: str, current_total_value: float) -> float:
        """Прирост относительно снимка на входе в раунд 1 (если есть)."""
        pid = str(player_id)
        first_map = self.player_capitalization_by_round_entry.get(1) or {}
        first = first_map.get(pid)
        if first is None or first <= 0:
            return 0.0
        try:
            return round(((float(current_total_value) / first) - 1.0) * 100.0, 2)
        except Exception:
            return 0.0
    
    def create_snapshot(self) -> Dict:
        """Создать снимок текущего состояния игры"""
        snapshot = {
            "game_id": self.game_id,
            "current_round": self.current_round,
            "current_prices": self.current_prices.copy(),
            "round_trading_locked": bool(self.round_trading_locked),
            "players": []
        }
        
        for player in self.players:
            player_snapshot = {
                "id": player.id,
                "name": player.name,
                "money": player.money,
                "character_name": getattr(player, 'character_name', None),
                "character_image": getattr(player, 'character_image', None),
                "ready_next_round": bool(getattr(player, 'ready_next_round', False)),
                "ready_next_round_for": getattr(player, 'ready_next_round_for', None),
                "resources": player.resources.copy(),
                "buildings": []
            }
            
            for building in player.buildings:
                building_snapshot = {
                    "id": building.id,
                    "name": building.name,
                    "started_round": building.started_round,
                    "completed_round": building.completed_round,
                    "status": building.status.value,
                    "sale_round": building.sale_round,
                    "sale_price": building.sale_price
                }
                player_snapshot["buildings"].append(building_snapshot)
            
            snapshot["players"].append(player_snapshot)
        
        return snapshot
    
    def restore_from_snapshot(self, snapshot: Dict):
        """Восстановить состояние игры из снимка"""
        self.current_round = snapshot["current_round"]
        self.current_prices = snapshot["current_prices"].copy()
        self.round_trading_locked = bool(snapshot.get("round_trading_locked", False))
        
        # Восстанавливаем игроков
        self.players = []
        for p_data in snapshot["players"]:
            player = Player(
                id=p_data["id"],
                name=p_data["name"],
                money=p_data["money"]
            )
            if p_data.get("character_name"):
                player.character_name = p_data["character_name"]
            if p_data.get("character_image"):
                player.character_image = p_data["character_image"]
            player.ready_next_round = bool(p_data.get("ready_next_round", False))
            player.ready_next_round_for = p_data.get("ready_next_round_for")
            
            player.resources = p_data["resources"].copy()
            
            for b_data in p_data["buildings"]:
                s = b_data["status"]
                b_status = parse_building_status(str(s))
                building = Building(
                    id=b_data["id"],
                    name=b_data["name"],
                    started_round=b_data["started_round"],
                    completed_round=b_data["completed_round"],
                    status=b_status,
                    sale_round=b_data.get("sale_round"),
                    sale_price=b_data.get("sale_price")
                )
                player.buildings.append(building)
            
            self.players.append(player)
        self.rebuild_building_pct_round_starts_from_current_players(clear_prev=True)
    
    def reset_players_economy_to_start(self) -> None:
        """Сброс денег, ресурсов и объектов к старту. Персонаж, id, имя не меняем."""
        for player in self.players:
            player.money = float(STARTING_MONEY)
            player.resources = {}
            player.buildings = []
        self.rebuild_building_pct_round_starts_from_current_players(clear_prev=True)
    
    def reset_round_tracking(self):
        """Сбросить историю раундов и счётчики действий после отката к снимку."""
        self.round_history = []
        self.previous_round_players_bought = {}
        self.previous_round_players_sold = {}
        self.current_round_players_bought = {}
        self.current_round_players_sold = {}
        self.start_round()
    
    def get_leaderboard(self) -> List[Dict]:
        """Получить турнирную таблицу"""
        players_data = []
        
        # Защита от None/пустоты
        if not self.players:
            return []
        
        # Защита от None для current_prices
        if not self.current_prices:
            # Используем конфигурацию игры
            self.current_prices = self.game_resource_prices.copy()
            # Фильтруем только включенные ресурсы
            filtered_prices = {}
            for resource in self.enabled_resources:
                if resource in self.current_prices:
                    filtered_prices[resource] = self.current_prices[resource]
            self.current_prices = filtered_prices
        
        for player in self.players:
            try:
                # Защита от None для атрибутов игрока
                player_resources = getattr(player, 'resources', {}) or {}
                player_buildings = getattr(player, 'buildings', []) or []
                player_money = getattr(player, 'money', 0) or 0
                player_id = getattr(player, 'id', 'unknown')
                player_name = getattr(player, 'name', 'Unknown')
                
                # Считаем стоимость всех ресурсов
                # Учитываем только включенные в конфигурацию ресурсы
                try:
                    resources_value = sum(
                        amount * self.current_prices.get(resource, 0)
                        for resource, amount in player_resources.items()
                        if resource in self.enabled_resources
                    )
                except Exception:
                    resources_value = 0
                
                # Считаем стоимость всех объектов (for_sale — по sale_price, sold — 0)
                buildings_value = 0
                for building in player_buildings:
                    try:
                        buildings_value += self.building_value_for_portfolio(building)
                    except Exception:
                        continue  # Пропускаем проблемные объекты
                
                total_value = player_money + resources_value + buildings_value
                
                players_data.append({
                    "player_id": player_id,
                    "name": player_name,
                    "money": round(player_money, 2),
                    "resources_value": round(resources_value, 2),
                    "buildings_value": round(buildings_value, 2),
                    "total_value": round(total_value, 2)
                })
            except Exception:
                continue  # Пропускаем проблемных игроков
        
        # Сортируем по общей стоимости
        try:
            players_data.sort(key=lambda x: x.get("total_value", 0), reverse=True)
        except Exception:
            pass  # Если сортировка не удалась, возвращаем как есть
        
        return players_data
    
    def get_player_state(self, player_id: str) -> Optional[Dict]:
        """Получить полное состояние игрока"""
        player = self.get_player(player_id)
        if not player:
            return None
        
        buildings_data = []
        for building in player.buildings:
            buildings_data.append({
                "id": building.id,
                "name": building.name,
                "status": building.status.value,
                "started_round": building.started_round,
                "completed_round": building.completed_round,
                "sale_round": building.sale_round,
                "sale_price": building.sale_price
            })
        
        return {
            "player_id": player.id,
            "name": player.name,
            "money": round(player.money, 2),
            "resources": player.resources.copy(),
            "buildings": buildings_data,
            "current_prices": self.current_prices.copy(),
            "current_round": self.current_round
        }

