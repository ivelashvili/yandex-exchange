// Telegram WebApp API
// Используем существующий window.Telegram.WebApp, если он есть (например, из miniapp_test.html)
// Иначе создаем fallback объект
let tg;
if (window.Telegram?.WebApp) {
    tg = window.Telegram.WebApp;
} else {
    tg = {
        initData: 'test_init_data',
        initDataUnsafe: { 
            user: { 
                id: 12345, 
                first_name: 'Тестовый', 
                username: 'test',
                photo_url: 'https://via.placeholder.com/200'
            } 
        },
        ready: () => {},
        expand: () => {}
    };
}

// Инициализация Telegram WebApp (если доступно)
if (window.Telegram?.WebApp) {
    tg.ready();
    tg.expand();
}

// Глобальные переменные
let playerState = null;
let prices = [];
let currentRound = 1;
let playerId = null;
let playerName = null;
let isAuthorized = false;
let telegramUser = null;
let updateInterval = null; // Интервал для обновления данных
let currentResource = null; // Текущий ресурс на странице ресурса
let currentBuilding = null; // Текущий объект на странице объекта
let currentBuildingStatus = null; // Статус текущего объекта
let currentBuildingId = null; // ID экземпляра (портфель: одна строка — один объект)
let previousScreen = 'portfolio'; // Предыдущий экран для кнопки "Назад"
let originScreen = 'portfolio'; // Исходный экран (портфель или биржа), откуда зашли в ресурс
let selectedCharacter = null; // Выбранный персонаж для подтверждения
let gameCode = null; // Код игры (6 цифр)
let mockTelegramUserId = null; // DEV-only: mock user id из URL/sessionStorage для теста разных вкладок

function getMockTelegramUserId() {
    if (mockTelegramUserId !== null) {
        return mockTelegramUserId;
    }
    const urlParams = new URLSearchParams(window.location.search);
    const fromUrl = urlParams.get('mock_user_id');
    const fromSession = sessionStorage.getItem('mock_user_id');
    const raw = fromUrl || fromSession;
    if (!raw) {
        mockTelegramUserId = '';
        return '';
    }
    const normalized = String(raw).trim();
    if (!/^\d+$/.test(normalized)) {
        mockTelegramUserId = '';
        return '';
    }
    mockTelegramUserId = normalized;
    sessionStorage.setItem('mock_user_id', normalized);
    return normalized;
}

function buildMockTelegramInitData(userId) {
    const user = {
        id: Number(userId),
        first_name: `Mock${userId}`,
        username: `mock_${userId}`
    };
    return `user=${encodeURIComponent(JSON.stringify(user))}`;
}

function getTelegramInitDataHeader() {
    const realInitData = tg.initData;
    const mockId = getMockTelegramUserId();
    if (mockId && (!realInitData || realInitData === 'test_init_data')) {
        return buildMockTelegramInitData(mockId);
    }
    return realInitData || 'test_init_data';
}

/** Позиции прокрутки при уходе с экрана: window, .container и сам .screen (для overflow) */
const screenScrollPositions = Object.create(null);

function captureActiveScreenScroll() {
    const active = document.querySelector('.screen.active');
    if (!active || !active.id) return;
    const name = active.id.replace(/-screen$/, '');
    if (!name) return;
    const container = document.querySelector('.container');
    screenScrollPositions[name] = {
        w: window.scrollY || document.documentElement.scrollTop || 0,
        c: container ? container.scrollTop : 0,
        s: active.scrollTop || 0
    };
}

function applyTargetScreenScroll(screenName, targetScreen, restoreScroll) {
    const container = document.querySelector('.container');
    function setPos(w, c, s) {
        window.scrollTo(0, w);
        if (container) container.scrollTop = c;
        if (targetScreen) targetScreen.scrollTop = s;
    }
    if (restoreScroll && screenScrollPositions[screenName] != null) {
        const pos = screenScrollPositions[screenName];
        requestAnimationFrame(() => {
            requestAnimationFrame(() => {
                setPos(pos.w, pos.c, pos.s);
            });
        });
    } else {
        setPos(0, 0, 0);
    }
}

/** Кэш строк рейтинга для карточки игрока (клик по строке) */
let miniappLeaderboardCache = [];
let currentMiniappPlayerModalIndex = 0;
let miniappPlayerModalEl = null;

// ========== GAME CODE HELPERS ==========

/**
 * Валидация кода игры (6 цифр, 100000-999999)
 */
function validateGameCode(code) {
    if (!code || typeof code !== 'string') {
        return false;
    }
    // Проверяем, что это 6 цифр
    const codeRegex = /^\d{6}$/;
    if (!codeRegex.test(code)) {
        return false;
    }
    // Проверяем диапазон
    const codeNum = parseInt(code, 10);
    return codeNum >= 100000 && codeNum <= 999999;
}

/**
 * Сохранить код игры в localStorage
 */
function saveGameCode(code) {
    if (validateGameCode(code)) {
        localStorage.setItem('game_code', code);
        gameCode = code;
        return true;
    }
    return false;
}

/**
 * Получить код игры из localStorage
 */
function getGameCode() {
    if (gameCode) {
        return gameCode;
    }
    const saved = localStorage.getItem('game_code');
    if (saved && validateGameCode(saved)) {
        gameCode = saved;
        return saved;
    }
    return null;
}

/**
 * Очистить сохраненный код игры
 */
function clearGameCode() {
    localStorage.removeItem('game_code');
    gameCode = null;
}

/**
 * Добавить game_code к URL как query-параметр
 */
function addGameCodeToUrl(url) {
    const code = getGameCode();
    if (!code) {
        return url;
    }
    const separator = url.includes('?') ? '&' : '?';
    return `${url}${separator}game_code=${encodeURIComponent(code)}`;
}

/**
 * Проверить существование игры по коду
 */
async function checkGameExists(code) {
    if (!validateGameCode(code)) {
        return { exists: false, error: 'Неверный формат кода' };
    }
    
    try {
        const response = await fetch(`/api/miniapp/round-info?game_code=${encodeURIComponent(code)}`, {
            headers: {
                'X-Telegram-Init-Data': getTelegramInitDataHeader()
            }
        });
        
        if (response.status === 404) {
            return { exists: false, error: 'Игра с таким кодом не найдена' };
        }
        
        if (!response.ok) {
            return { exists: false, error: 'Ошибка проверки игры' };
        }
        
        return { exists: true };
    } catch (error) {
        console.error('Ошибка проверки игры:', error);
        return { exists: false, error: 'Ошибка сети. Проверьте подключение.' };
    }
}

// Список персонажей (33 персонажа)
const characters = [
    { name: 'Алексей Пермский', image: '/static/images/characters/Алексей Пермский.png' },
    { name: 'Анастасия Барабинская', image: '/static/images/characters/Анастасия Барабинская.png' },
    { name: 'Анастасия Бердская', image: '/static/images/characters/Анастасия Бердская.png' },
    { name: 'Анастасия Шеврикская', image: '/static/images/characters/Анастасия Шеврикская.png' },
    { name: 'Арсений Жестокий', image: '/static/images/characters/Арсений Жестокий.png' },
    { name: 'Артемий Строитель', image: '/static/images/characters/Артемий Строитель.png' },
    { name: 'Аслан Акбайский', image: '/static/images/characters/Аслан Акбайский.png' },
    { name: 'Ахмед Бакийский', image: '/static/images/characters/Ахмед Бакийский.png' },
    { name: 'Бато Даши Цыден', image: '/static/images/characters/Бато Даши Цыден.png' },
    { name: 'Валерия Караболта', image: '/static/images/characters/Валерия Караболта.png' },
    { name: 'Виктория Мудрая', image: '/static/images/characters/Виктория Мудрая.png' },
    { name: 'Всеволод Умный', image: '/static/images/characters/Всеволод Умный.png' },
    { name: 'Дарья Великая', image: '/static/images/characters/Дарья Великая.png' },
    { name: 'Денис Бийский', image: '/static/images/characters/Денис Бийский.png' },
    { name: 'Ева Прородительница', image: '/static/images/characters/Ева Прородительница.png' },
    { name: 'Евгений Панасенков', image: '/static/images/characters/Евгений Панасенков.png' },
    { name: 'Евгений Тогучинский', image: '/static/images/characters/Евгений Тогучинский.png' },
    { name: 'Елена Жан-Мазова', image: '/static/images/characters/Елена Жан-Мазова.png' },
    { name: 'Жан Владлен', image: '/static/images/characters/Жан Владлен.png' },
    { name: 'Жан Дони', image: '/static/images/characters/Жан Дони.png' },
    { name: 'Игорь Ангарский', image: '/static/images/characters/Игорь Ангарский.png' },
    { name: 'Игорь Лысков', image: '/static/images/characters/Игорь Лысков.png' },
    { name: 'Кирилл Великолепный', image: '/static/images/characters/Кирилл Великолепный.png' },
    { name: 'Кирилл Храбрый', image: '/static/images/characters/Кирилл Храбрый.png' },
    { name: 'Клуни', image: '/static/images/characters/Клуни.png' },
    { name: 'Леван Картлийский', image: '/static/images/characters/Леван Картлийский.png' },
    { name: 'Маргарита Матерь', image: '/static/images/characters/Маргарита Матерь.png' },
    { name: 'Мария Светлая', image: '/static/images/characters/Мария Светлая.png' },
    { name: 'Месси Леонель', image: '/static/images/characters/Месси Леонель.png' },
    { name: 'Михаил Заебийский', image: '/static/images/characters/Михаил Заебийский.png' },
    { name: 'Петр Братский', image: '/static/images/characters/Петр Братский.png' },
    { name: 'Татьяна Пермская', image: '/static/images/characters/Татьяна Пермская.png' },
    { name: 'Юрий Каркаде', image: '/static/images/characters/Юрий Каркаде.png' }
];

// ========== GAME CODE SCREEN FUNCTIONS ==========

/**
 * Показать экран ввода кода игры
 */
function showGameCodeScreen(errorMessage = null) {
    const gameCodeScreen = document.getElementById('game-code-screen');
    const mainContainer = document.getElementById('main-container');
    const characterScreen = document.getElementById('character-selection-screen');
    
    // Скрываем все остальные экраны
    if (mainContainer) {
        mainContainer.style.display = 'none';
    }
    if (characterScreen) {
        characterScreen.style.display = 'none';
    }
    
    // Показываем экран ввода кода
    if (gameCodeScreen) {
        gameCodeScreen.style.display = 'flex';
        
        // Очищаем поле ввода
        const input = document.getElementById('game-code-input');
        if (input) {
            input.value = '';
            input.disabled = false;
            input.readOnly = false;
            // Фокус с небольшой задержкой для надежности
            setTimeout(() => {
                input.focus();
            }, 100);
        }
        
        // Показываем ошибку, если есть
        const errorDiv = document.getElementById('game-code-error');
        if (errorDiv) {
            if (errorMessage) {
                errorDiv.textContent = errorMessage;
                errorDiv.style.display = 'block';
            } else {
                errorDiv.style.display = 'none';
            }
        }
        
        // Показываем кнопку "Сменить игру", если есть сохраненный код
        const changeBtn = document.getElementById('game-code-change');
        if (changeBtn) {
            changeBtn.style.display = getGameCode() ? 'block' : 'none';
        }
    }
}

/**
 * Обработка отправки кода игры
 */
async function submitGameCode() {
    const input = document.getElementById('game-code-input');
    const errorDiv = document.getElementById('game-code-error');
    const submitBtn = document.getElementById('game-code-submit');
    
    if (!input) return;
    
    const code = input.value.trim();
    
    // Валидация на фронтенде
    if (!validateGameCode(code)) {
        if (errorDiv) {
            errorDiv.textContent = 'Код должен состоять из 6 цифр (100000-999999)';
            errorDiv.style.display = 'block';
        }
        input.focus();
        return;
    }
    
    // Отключаем кнопку на время проверки
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Проверка...';
    }
    
    if (errorDiv) {
        errorDiv.style.display = 'none';
    }
    
    // Проверяем существование игры
    const checkResult = await checkGameExists(code);
    
    if (!checkResult.exists) {
        // Показываем ошибку
        if (errorDiv) {
            errorDiv.textContent = checkResult.error || 'Игра не найдена';
            errorDiv.style.display = 'block';
        }
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Войти в игру';
        }
        input.focus();
        return;
    }
    
    // Сохраняем код и переходим к авторизации
    saveGameCode(code);
    
    if (submitBtn) {
        submitBtn.textContent = 'Успешно!';
    }
    
    // Небольшая задержка для визуального подтверждения
    await new Promise(resolve => setTimeout(resolve, 500));
    
    // Скрываем экран ввода кода
    const gameCodeScreen = document.getElementById('game-code-screen');
    if (gameCodeScreen) {
        gameCodeScreen.style.display = 'none';
    }
    
    // Переходим к проверке авторизации
    await checkAuth();
}

// Просмотр аватара в полноэкранном режиме
function openAvatarView() {
    const avatarImg = document.getElementById('player-avatar');
    const modal = document.getElementById('avatar-view-modal');
    const viewImage = document.getElementById('avatar-view-image');
    if (!avatarImg || !modal || !viewImage) return;
    if (avatarImg.style.display === 'none' || !avatarImg.src) return;
    viewImage.src = avatarImg.src;
    modal.classList.add('is-open');
    modal.setAttribute('aria-hidden', 'false');
}
function closeAvatarView() {
    const modal = document.getElementById('avatar-view-modal');
    if (!modal) return;
    modal.classList.remove('is-open');
    modal.setAttribute('aria-hidden', 'true');
}
document.addEventListener('DOMContentLoaded', () => {
    const clickArea = document.getElementById('player-avatar-click-area');
    const closeBtn = document.getElementById('avatar-view-close');
    const modal = document.getElementById('avatar-view-modal');
    const backdrop = modal ? modal.querySelector('.avatar-view-backdrop') : null;
    if (clickArea) {
        clickArea.addEventListener('click', openAvatarView);
        clickArea.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openAvatarView(); } });
    }
    if (closeBtn) closeBtn.addEventListener('click', closeAvatarView);
    if (backdrop) backdrop.addEventListener('click', closeAvatarView);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && modal && modal.classList.contains('is-open')) closeAvatarView(); });
});

document.addEventListener('DOMContentLoaded', () => {
    miniappPlayerModalEl = document.getElementById('miniapp-player-modal');
    const closeBtn = document.getElementById('miniapp-player-modal-close');
    const backdrop = miniappPlayerModalEl ? miniappPlayerModalEl.querySelector('.miniapp-player-modal-backdrop') : null;
    const left = document.getElementById('miniapp-player-modal-nav-left');
    const right = document.getElementById('miniapp-player-modal-nav-right');
    if (closeBtn) closeBtn.addEventListener('click', closeMiniappPlayerModal);
    if (backdrop) backdrop.addEventListener('click', closeMiniappPlayerModal);
    if (left) left.addEventListener('click', () => navigateMiniappPlayerModal(-1));
    if (right) right.addEventListener('click', () => navigateMiniappPlayerModal(1));
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape' || !miniappPlayerModalEl) return;
        if (miniappPlayerModalEl.classList.contains('miniapp-player-modal--open')) {
            closeMiniappPlayerModal();
        }
    });
});

document.addEventListener('DOMContentLoaded', () => {
    const nextRoundBtn = document.getElementById('miniapp-next-round-btn');
    const modal = document.getElementById('miniapp-next-round-modal');
    const backdrop = document.getElementById('miniapp-next-round-modal-backdrop');
    const noBtn = document.getElementById('miniapp-next-round-modal-no');
    const yesBtn = document.getElementById('miniapp-next-round-modal-yes');
    if (nextRoundBtn) {
        nextRoundBtn.addEventListener('click', () => openMiniappNextRoundConfirmModal());
    }
    if (noBtn) noBtn.addEventListener('click', () => closeMiniappNextRoundConfirmModal());
    if (backdrop) backdrop.addEventListener('click', () => closeMiniappNextRoundConfirmModal());
    if (yesBtn) {
        yesBtn.addEventListener('click', async () => {
            closeMiniappNextRoundConfirmModal();
            try {
                const response = await fetch(addGameCodeToUrl('/api/miniapp/player/ready-next-round'), {
                    method: 'POST',
                    headers: {
                        'X-Telegram-Init-Data': getTelegramInitDataHeader(),
                    },
                });
                if (!response.ok) {
                    showToast('Не удалось подтвердить готовность', 'error');
                    return;
                }
                await loadPlayerState();
            } catch (e) {
                console.error('ready-next-round:', e);
                showToast('Ошибка сети', 'error');
            }
        });
    }
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape' || !modal) return;
        if (modal.classList.contains('miniapp-next-round-modal--open')) {
            closeMiniappNextRoundConfirmModal();
        }
    });
});

document.addEventListener('DOMContentLoaded', () => {
    const eventsBtn = document.getElementById('portfolio-round-events-btn');
    const reModal = document.getElementById('miniapp-round-events-modal');
    const reBackdrop = document.getElementById('miniapp-round-events-modal-backdrop');
    const reClose = document.getElementById('miniapp-round-events-modal-close');
    if (eventsBtn) eventsBtn.addEventListener('click', () => openMiniappRoundEventsModal());
    if (reClose) reClose.addEventListener('click', () => closeMiniappRoundEventsModal());
    if (reBackdrop) reBackdrop.addEventListener('click', () => closeMiniappRoundEventsModal());
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape' || !reModal) return;
        if (reModal.classList.contains('miniapp-round-events-modal--open')) {
            closeMiniappRoundEventsModal();
        }
    });
});

// Обработка Enter в поле ввода кода
document.addEventListener('DOMContentLoaded', () => {
    const gameCodeInput = document.getElementById('game-code-input');
    if (gameCodeInput) {
        gameCodeInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                submitGameCode();
            }
        });
        
        // Ограничиваем ввод только цифрами
        gameCodeInput.addEventListener('input', (e) => {
            e.target.value = e.target.value.replace(/\D/g, '');
        });
    }
});

// Инициализация
document.addEventListener('DOMContentLoaded', async () => {
    try {
        // Читаем game_code из URL (например /miniapp?game_code=123456), если есть
        const urlParams = new URLSearchParams(window.location.search);
        const urlGameCode = urlParams.get('game_code');
        if (urlGameCode && validateGameCode(urlGameCode)) {
            saveGameCode(urlGameCode);
        }
        // Получаем данные пользователя из Telegram
        telegramUser = tg.initDataUnsafe?.user;
        if (!telegramUser) {
            const mockId = getMockTelegramUserId();
            if (mockId) {
                telegramUser = { id: Number(mockId), first_name: `Mock${mockId}`, username: `mock_${mockId}` };
            } else {
                console.warn('Telegram user not found, using default test mode user');
                telegramUser = { id: 12345, first_name: 'Тестовый', username: 'test' };
            }
        }

        playerId = `tg_${telegramUser.id}`;
        
        // Скрываем все экраны
        const mainContainer = document.getElementById('main-container');
        const characterScreen = document.getElementById('character-selection-screen');
        const gameCodeScreen = document.getElementById('game-code-screen');
        
        if (mainContainer) {
            mainContainer.style.display = 'none';
        }
        if (characterScreen) {
            characterScreen.style.display = 'none';
        }
        // Убеждаемся, что экран ввода кода скрыт по умолчанию
        if (gameCodeScreen) {
            gameCodeScreen.style.display = 'none';
        }
        
        // Проверяем наличие сохраненного кода игры
        const savedCode = getGameCode();
        if (savedCode) {
            // Проверяем, существует ли игра
            const checkResult = await checkGameExists(savedCode);
            if (checkResult.exists) {
                // Игра существует, переходим к проверке авторизации
                await checkAuth();
            } else {
                // Игра не найдена, показываем экран ввода кода
                showGameCodeScreen(checkResult.error);
            }
        } else {
            // Кода нет, показываем экран ввода
            showGameCodeScreen();
        }
    } catch (error) {
        console.error('Ошибка инициализации:', error);
        showToast('Ошибка загрузки данных', 'error');
        showGameCodeScreen('Ошибка инициализации');
    }
});

// Проверка авторизации
async function checkAuth() {
    const code = getGameCode();
    if (!code) {
        showGameCodeScreen('Код игры не найден');
        return;
    }
    
    try {
        const response = await fetch(addGameCodeToUrl('/api/miniapp/player/state'), {
            cache: 'no-store',
            headers: {
                'X-Telegram-Init-Data': getTelegramInitDataHeader(),
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache'
            }
        });

        if (!response.ok) {
            console.error('Ошибка проверки авторизации:', response.status, response.statusText);
            
            // Если 404 - игра не найдена, возвращаемся к экрану ввода кода
            if (response.status === 404) {
                clearGameCode();
                showGameCodeScreen('Игра не найдена. Проверьте код игры.');
                return;
            }
            
            // Если 400 - неверный формат кода
            if (response.status === 400) {
                clearGameCode();
                showGameCodeScreen('Неверный формат кода игры.');
                return;
            }
            
            // В тестовом режиме продолжаем работу
            const mainContainer = document.getElementById('main-container');
            if (mainContainer) {
                mainContainer.style.display = 'block';
                initializeScreens();
            }
            // Показываем выбор персонажа вместо несуществующей функции showAuthModal
            showCharacterSelection();
            return;
        }

        const data = await response.json();
        console.log('Данные игрока:', data);
        
        // Если игрок не найден или нет персонажа - показываем окно выбора персонажа
        if (!data.character_name) {
            // Показываем контейнер даже если нет авторизации (для тестового режима)
            const mainContainer = document.getElementById('main-container');
            if (mainContainer) {
                mainContainer.style.display = 'block';
                initializeScreens();
            }
            showCharacterSelection();
            return;
        }

        // Игрок авторизован
        isAuthorized = true;
        playerName = data.character_name || data.nickname;
        const mainContainer = document.getElementById('main-container');
        if (mainContainer) {
            mainContainer.style.display = 'block';
            // Инициализируем экраны
            initializeScreens();
        }
        
        // Загружаем начальные данные
        await loadPlayerState();
        await loadPrices();
        await loadRoundInfo();

        // Обновляем данные каждые 2 секунды (очищаем предыдущий интервал, если есть)
        if (updateInterval) {
            clearInterval(updateInterval);
        }
        updateInterval = setInterval(async () => {
            try { await loadPlayerState(); } catch (e) {}
            try { await loadPrices(); } catch (e) {}
            try { await loadRoundInfo(); } catch (e) {}
        }, 2000);
    } catch (error) {
        console.error('Ошибка проверки авторизации:', error);
        // В случае ошибки показываем контейнер для тестового режима
        const mainContainer = document.getElementById('main-container');
        if (mainContainer) {
            mainContainer.style.display = 'block';
            initializeScreens();
        }
        // Если игрок не найден, показываем окно выбора персонажа
        showCharacterSelection();
    }
}

// Показать окно выбора персонажа (персонажи загружаются из API — список, настроенный админом для этой игры)
async function showCharacterSelection() {
    const screen = document.getElementById('character-selection-screen');
    const grid = document.getElementById('character-selection-grid');
    const mainContainer = document.getElementById('main-container');
    
    if (!screen || !grid) {
        console.error('Элементы страницы выбора персонажа не найдены');
        return;
    }
    
    // Скрываем основной контейнер
    if (mainContainer) {
        mainContainer.style.display = 'none';
    }
    
    screen.style.display = 'block';
    grid.innerHTML = '<div style="text-align: center; padding: 20px;">Загрузка...</div>';
    
    try {
        // Получаем список персонажей игры (настроенных в админке)
        const charactersResponse = await fetch(addGameCodeToUrl('/api/miniapp/characters'));
        if (!charactersResponse.ok) {
            grid.innerHTML = '<div style="text-align: center; padding: 20px; color: #3a2a1a;">Персонажи для этой игры не настроены. Обратитесь к организатору.</div>';
            return;
        }
        const charactersData = await charactersResponse.json();
        const characters = (charactersData.characters || []);
        if (characters.length === 0) {
            grid.innerHTML = '<div style="text-align: center; padding: 20px; color: #3a2a1a;">Персонажи для этой игры не настроены. Обратитесь к организатору.</div>';
            return;
        }

        // Получаем список уже выбранных персонажей
        const takenResponse = await fetch(addGameCodeToUrl('/api/miniapp/characters/taken'));
        if (!takenResponse.ok) {
            grid.innerHTML = '<div style="text-align: center; padding: 20px; color: #8b4513;">Ошибка загрузки. Попробуйте обновить страницу.</div>';
            return;
        }
        const takenData = await takenResponse.json();
        const takenCharacterNames = new Set((takenData.taken_characters || []).map(c => c.name));
        
        // Показываем только тех, кто еще не выбран
        const availableCharacters = characters.filter(c => !takenCharacterNames.has(c.name));
        
        grid.innerHTML = '';
        
        if (availableCharacters.length === 0) {
            grid.innerHTML = '<div style="text-align: center; padding: 20px; color: #3a2a1a;">Все персонажи уже выбраны</div>';
            return;
        }
        
        // Создаем карточки персонажей (name, image — как отдаёт API)
        availableCharacters.forEach(character => {
            const card = document.createElement('div');
            card.className = 'character-card';
            const name = character.name || '';
            const image = character.image || '';
            // Экранируем кавычки и специальные символы для безопасного использования в HTML
            const escapedName = name.replace(/'/g, "\\'").replace(/"/g, '&quot;');
            const escapedImage = image.replace(/'/g, "\\'").replace(/"/g, '&quot;');
            
            card.innerHTML = `
                <div class="character-card-avatar">
                    <img src="${escapedImage}" alt="${escapedName}" class="character-avatar-img" onerror="this.style.background='#ddd';this.onerror=null;">
                </div>
                <div class="character-card-name">${name}</div>
                <button class="character-card-select-btn" onclick="selectCharacter('${escapedName}', '${escapedImage}')">Выбрать</button>
            `;
            grid.appendChild(card);
        });
    } catch (error) {
        console.error('Ошибка загрузки списка персонажей:', error);
        grid.innerHTML = '<div style="text-align: center; padding: 20px; color: #8b4513;">Ошибка загрузки. Попробуйте обновить страницу.</div>';
    }
}

// Выбрать персонажа
function selectCharacter(name, image) {
    selectedCharacter = { name, image };
    
    // Показываем модальное окно подтверждения
    const confirmModal = document.getElementById('character-confirm-modal');
    const confirmAvatar = document.getElementById('confirm-character-avatar');
    const confirmName = document.getElementById('confirm-character-name');
    
    if (confirmModal && confirmAvatar && confirmName) {
        confirmAvatar.src = image;
        confirmName.textContent = name;
        confirmModal.style.display = 'flex';
    }
}

// Отменить выбор персонажа
function cancelCharacterSelection() {
    const confirmModal = document.getElementById('character-confirm-modal');
    if (confirmModal) {
        confirmModal.style.display = 'none';
    }
    selectedCharacter = null;
}

// Подтвердить выбор персонажа
async function confirmCharacterSelection() {
    if (!selectedCharacter) {
        showToast('Персонаж не выбран', 'error');
        return;
    }

    showLoading(true);

    try {
        const response = await fetch(addGameCodeToUrl('/api/miniapp/player/auth'), {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Telegram-Init-Data': getTelegramInitDataHeader()
            },
            body: JSON.stringify({
                character_name: selectedCharacter.name,
                character_image: selectedCharacter.image
            })
        });

        const data = await response.json();

        if (data.success) {
            isAuthorized = true;
            playerName = selectedCharacter.name;
            
            // Скрываем экран выбора персонажа
            const screen = document.getElementById('character-selection-screen');
            const confirmModal = document.getElementById('character-confirm-modal');
            if (screen) screen.style.display = 'none';
            if (confirmModal) confirmModal.style.display = 'none';
            
            // Показываем основной контейнер
            const mainContainer = document.getElementById('main-container');
            if (mainContainer) {
                mainContainer.style.display = 'block';
            }
            
            // Инициализируем экраны
            initializeScreens();
            
            // Загружаем начальные данные
            await loadPlayerState();
            await loadPrices();
            await loadRoundInfo();

            // Обновляем данные каждые 2 секунды (очищаем предыдущий интервал, если есть)
            if (updateInterval) {
                clearInterval(updateInterval);
            }
            updateInterval = setInterval(async () => {
                try { await loadPlayerState(); } catch (e) {}
                try { await loadPrices(); } catch (e) {}
                try { await loadRoundInfo(); } catch (e) {}
            }, 2000);
        } else {
            const errorMessage = data.message || 'Ошибка сохранения данных';
            showToast(errorMessage, 'error');
            
            // Если персонаж уже выбран, обновляем список персонажей
            if (errorMessage.includes('уже выбран')) {
                // Обновляем список, чтобы убрать уже выбранного персонажа
                setTimeout(() => {
                    showCharacterSelection();
                }, 1000);
            }
        }
    } catch (error) {
        console.error('Ошибка сохранения данных:', error);
        showToast('Ошибка сохранения данных', 'error');
    } finally {
        showLoading(false);
        selectedCharacter = null;
    }
}

// Делаем функции доступными глобально для тестового режима
window.loadPlayerState = loadPlayerState;
window.loadPrices = loadPrices;
window.addGameCodeToUrl = addGameCodeToUrl;
window.loadRoundInfo = loadRoundInfo;
window.showScreen = showScreen;
window.showCharacterSelection = showCharacterSelection;
window.selectCharacter = selectCharacter;
window.cancelCharacterSelection = cancelCharacterSelection;
window.confirmCharacterSelection = confirmCharacterSelection;
window.showResourceScreen = showResourceScreen;
window.goBackFromResource = goBackFromResource;
window.showBuyResourceScreen = showBuyResourceScreen;
window.showSellResourceScreen = showSellResourceScreen;
window.increaseBuyQuantity = increaseBuyQuantity;
window.decreaseBuyQuantity = decreaseBuyQuantity;
window.updateBuyTotal = updateBuyTotal;
window.increaseSellQuantity = increaseSellQuantity;
window.decreaseSellQuantity = decreaseSellQuantity;
window.updateSellTotal = updateSellTotal;
window.confirmBuyResource = confirmBuyResource;
window.confirmSellResource = confirmSellResource;
window.goBackFromBuyResource = goBackFromBuyResource;
window.goBackFromSellResource = goBackFromSellResource;
window.showPortfolioBuildingScreen = showPortfolioBuildingScreen;
window.goBackFromPortfolioBuilding = goBackFromPortfolioBuilding;
window.showSellBuildingScreen = showSellBuildingScreen;
window.showMarketBuildingScreen = showMarketBuildingScreen;
window.goBackFromMarketBuilding = goBackFromMarketBuilding;
window.showBuildBuildingScreenFromMarket = showBuildBuildingScreenFromMarket;
window.showSellBuildingScreenFromMarket = showSellBuildingScreenFromMarket;
window.increaseBuildQuantity = increaseBuildQuantity;
window.decreaseBuildQuantity = decreaseBuildQuantity;
window.updateBuildTotal = updateBuildTotal;
window.confirmBuildBuilding = confirmBuildBuilding;
window.buyAllResourcesForBuilding = buyAllResourcesForBuilding;
window.goBackFromBuildBuilding = goBackFromBuildBuilding;
window.increaseSellBuildingQuantity = increaseSellBuildingQuantity;
window.decreaseSellBuildingQuantity = decreaseSellBuildingQuantity;
window.updateSellBuildingTotal = updateSellBuildingTotal;
window.confirmSellBuilding = confirmSellBuilding;
window.goBackFromSellBuilding = goBackFromSellBuilding;

// Загрузка состояния игрока
async function loadPlayerState() {
    try {
        const response = await fetch(addGameCodeToUrl('/api/miniapp/player/state'), {
            cache: 'no-store',
            headers: {
                'X-Telegram-Init-Data': getTelegramInitDataHeader(),
                'Cache-Control': 'no-cache',
                'Pragma': 'no-cache'
            }
        });

        if (!response.ok) {
            if (response.status === 404) {
                clearGameCode();
                showGameCodeScreen('Игра не найдена. Проверьте код игры.');
                return;
            }
            if (response.status === 400) {
                clearGameCode();
                showGameCodeScreen('Неверный формат кода игры.');
                return;
            }
            throw new Error('Ошибка загрузки состояния');
        }

        const data = await response.json();
        playerState = data;
        syncTradingLockUi();

        // Обновляем UI
        try {
            updatePlayerInfo();
            updateResources();
            updateBuildings();
        } catch (uiError) {
            console.error('Ошибка обновления UI:', uiError);
        }
    } catch (error) {
        console.error('Ошибка загрузки состояния игрока:', error);
        // В тестовом режиме показываем контейнер даже при ошибке
        const mainContainer = document.getElementById('main-container');
        if (mainContainer && mainContainer.style.display === 'none') {
            mainContainer.style.display = 'block';
            initializeScreens();
        }
    }
}

// Загрузка цен
async function loadPrices() {
    try {
        const response = await fetch(addGameCodeToUrl('/api/miniapp/prices'));
        if (!response.ok) return;

        const data = await response.json();
        prices = data.prices || [];

        updatePrices();
    } catch (error) {
        console.error('Ошибка загрузки цен:', error);
    }
}

// Загрузка информации о раунде (cache: no-store чтобы при смене раунда на вебе/админке отображалось актуальное значение)
async function loadRoundInfo() {
    try {
        const url = addGameCodeToUrl('/api/miniapp/round-info');
        const response = await fetch(url, {
            cache: 'no-store',
            headers: { 'Cache-Control': 'no-cache', 'Pragma': 'no-cache' }
        });
        const data = response.ok ? await response.json() : null;
        if (!response.ok) return;
        currentRound = data.current_round || 1;
        const el = document.getElementById('current-round');
        if (el) el.textContent = currentRound;
    } catch (error) {
        console.error('Ошибка загрузки информации о раунде:', error);
    }
}

function openMiniappNextRoundConfirmModal() {
    const modal = document.getElementById('miniapp-next-round-modal');
    if (!modal) return;
    modal.classList.add('miniapp-next-round-modal--open');
    modal.setAttribute('aria-hidden', 'false');
}

function closeMiniappNextRoundConfirmModal() {
    const modal = document.getElementById('miniapp-next-round-modal');
    if (!modal) return;
    modal.classList.remove('miniapp-next-round-modal--open');
    modal.setAttribute('aria-hidden', 'true');
}

function isTradingLocked() {
    return !!(playerState && playerState.trading_locked);
}

function syncTradingLockUi() {
    const locked = isTradingLocked();
    document.body.classList.toggle('miniapp-trading-locked', locked);
    const nextBtn = document.getElementById('miniapp-next-round-btn');
    if (nextBtn) {
        nextBtn.disabled = locked;
        nextBtn.setAttribute('aria-disabled', locked ? 'true' : 'false');
    }
}

function guardTradingAction() {
    if (!isTradingLocked()) return true;
    return false;
}

// Переключение раунда на вебе; в миниапе «Да» — готовность к следующему раунду.

async function openMiniappRoundEventsModal() {
    try {
        const response = await fetch(addGameCodeToUrl('/api/miniapp/round-events-content'), {
            cache: 'no-store',
            headers: { 'Cache-Control': 'no-cache', 'Pragma': 'no-cache' },
        });
        const data = response.ok ? await response.json() : null;
        if (!data || data.success !== true) {
            showToast('Не удалось загрузить событие раунда', 'error');
            return;
        }

        const imgWrap = document.getElementById('miniapp-round-events-modal-image-wrap');
        const imgEl = document.getElementById('miniapp-round-events-modal-image');
        const textEl = document.getElementById('miniapp-round-events-modal-text');
        const emptyEl = document.getElementById('miniapp-round-events-modal-empty');
        const rn = data.round_number != null ? data.round_number : currentRound;

        const t = (data.event_text || '').trim();
        const u = (data.image_url || '').trim();

        if (textEl) {
            textEl.textContent = t;
            textEl.style.display = t ? 'block' : 'none';
        }
        if (emptyEl) emptyEl.style.display = !t && !u ? 'block' : 'none';

        if (imgWrap && imgEl) {
            if (u) {
                imgWrap.style.display = 'block';
                imgEl.alt = 'Событие раунда ' + rn;
                imgEl.onerror = function () {
                    imgWrap.style.display = 'none';
                };
                imgEl.src = u;
            } else {
                imgWrap.style.display = 'none';
                imgEl.removeAttribute('src');
            }
        }

        const modal = document.getElementById('miniapp-round-events-modal');
        if (modal) {
            modal.classList.add('miniapp-round-events-modal--open');
            modal.setAttribute('aria-hidden', 'false');
        }
    } catch (err) {
        console.error('openMiniappRoundEventsModal:', err);
        showToast('Ошибка загрузки события', 'error');
    }
}

function closeMiniappRoundEventsModal() {
    const modal = document.getElementById('miniapp-round-events-modal');
    if (!modal) return;
    modal.classList.remove('miniapp-round-events-modal--open');
    modal.setAttribute('aria-hidden', 'true');
}

// Обновление информации об игроке
function updatePlayerInfo() {
    try {
        if (!playerState) {
            console.warn('playerState is null in updatePlayerInfo');
            return;
        }
        
        // Обновляем имя игрока
        const playerNameEl = document.getElementById('player-name');
        const playerName = playerState.character_name || playerState.nickname;
        if (playerNameEl && playerName) {
            playerNameEl.textContent = playerName;
        }
        
        // Обновляем аватар
        const avatarImg = document.getElementById('player-avatar');
        const avatarPlaceholder = document.getElementById('player-avatar-placeholder');
        
        if (avatarImg && avatarPlaceholder) {
            const avatarUrl = playerState.character_image || playerState.photo_url;
            if (avatarUrl) {
                avatarImg.src = avatarUrl;
                avatarImg.style.display = 'block';
                avatarPlaceholder.style.display = 'none';
            } else {
                avatarImg.style.display = 'none';
                avatarPlaceholder.style.display = 'flex';
            }
        }
        
        // Обновляем капитализацию
        const capitalization = playerState.total_capitalization || playerState.capitalization || 0;
        const capitalizationEl = document.getElementById('capitalization-value');
        if (capitalizationEl) {
            capitalizationEl.textContent = Math.round(capitalization).toLocaleString('ru-RU');
        }
        
        // Монеты на счете (карточка баланса на экране портфеля)
        const balanceEl = document.getElementById('portfolio-balance-value');
        if (balanceEl) {
            balanceEl.textContent = Math.round(playerState.money || 0).toLocaleString('ru-RU');
        }

        // Обновляем приросты (прогресс-бар на экране портфеля)
        const growthRound = playerState.growth_from_prev_round_percent || playerState.growth_round_percent || 0;
        const growthGame = playerState.growth_from_start_percent || playerState.growth_game_percent || 0;
        
        const growthRoundEl = document.getElementById('growth-round');
        const growthGameEl = document.getElementById('growth-game');
        const growthWrapRound = document.getElementById('portfolio-growth-round-wrap');
        const growthWrapGame = document.getElementById('portfolio-growth-game-wrap');
        
        if (growthRoundEl) growthRoundEl.textContent = Math.round(growthRound) + '%';
        if (growthGameEl) growthGameEl.textContent = Math.round(growthGame) + '%';
        
        if (growthWrapRound) {
            growthWrapRound.className = 'portfolio-growth-segment portfolio-growth-round ' + (growthRound >= 0 ? 'positive' : 'negative');
        }
        if (growthWrapGame) {
            growthWrapGame.className = 'portfolio-growth-segment portfolio-growth-game ' + (growthGame >= 0 ? 'positive' : 'negative');
        }
    } catch (error) {
        console.error('Ошибка обновления информации об игроке:', error);
    }
}

// Обновление ресурсов
function updateResources() {
    try {
        if (!playerState) {
            console.warn('playerState is null in updateResources');
            return;
        }
        
        if (!playerState.resources) {
            console.warn('playerState.resources is null in updateResources');
            return;
        }

        const list = document.getElementById('resources-list');
        if (!list) {
            console.error('resources-list element not found!');
            return;
        }
        
        list.innerHTML = '';

    // Ресурсы (монеты вынесены в карточку «монет на счете»)
    const resourceNames = ['камень', 'дерево', 'железо', 'скот', 'овощи', 'рабы', 'золото', 'зерно', 'рыба'];
    
    resourceNames.forEach(resource => {
        const amount = playerState.resources[resource] || 0;
        if (amount === 0) return;
        
        const price = prices.find(p => p.resource === resource);
        const priceValue = price ? price.current_price : 0;
        const totalValue = amount * priceValue;
        // На странице портфеля — иконки из design/icons
        const imagePath = miniappResourceIconUrl(resource);
        
        const resourceItem = document.createElement('div');
        resourceItem.className = 'resource-item portfolio-resource-row';
        resourceItem.style.cursor = 'pointer';
        resourceItem.onclick = () => showResourceScreen(resource);
        resourceItem.innerHTML = `
            <div class="resource-info">
                <div class="resource-left">
                    <img src="${imagePath}" alt="${capitalizeFirst(resource)}" class="resource-icon" onerror="this.style.display='none'">
                    <span class="resource-name">${capitalizeFirst(resource)}</span>
                </div>
                <div class="resource-right">
                    <span class="resource-value resource-value-small">${totalValue.toLocaleString('ru-RU')}</span>
                    <span class="resource-amount">${amount.toLocaleString('ru-RU')}</span>
                </div>
            </div>
        `;
        list.appendChild(resourceItem);
    });
    
        if (list.children.length === 0) {
            list.innerHTML = '<div class="portfolio-empty-message">У вас пока нет ресурсов</div>';
        }
    } catch (error) {
        console.error('Ошибка обновления ресурсов:', error);
    }
}

// Обновление объектов
function updateBuildings() {
    try {
        if (!playerState) {
            console.warn('playerState is null in updateBuildings');
            return;
        }
        
        if (!playerState.buildings) {
            console.warn('playerState.buildings is null in updateBuildings');
            return;
        }

        const list = document.getElementById('buildings-list');
        if (!list) {
            console.error('buildings-list element not found!');
            return;
        }
        
        list.innerHTML = '';

    // В портфеле не показываем проданные объекты (остаются в данных игры)
    const buildingsVisible = playerState.buildings.filter(function (b) {
        return b && b.status !== 'sold';
    });

    if (buildingsVisible.length === 0) {
        list.innerHTML = '<div class="portfolio-empty-message">У вас пока нет объектов</div>';
        return;
    }

    // По одной строке на каждый экземпляр объекта
    buildingsVisible.forEach(b => {
        const statusText = getStatusText(b.status);
        const portfolioBuildingIconPath = miniappBuildingIconUrlForName(b.name);
        const buildingItem = document.createElement('div');
        buildingItem.className = 'building-item portfolio-building-row';
        buildingItem.style.cursor = 'pointer';
        const bid = b.id != null ? String(b.id) : '';
        buildingItem.onclick = () => showPortfolioBuildingScreen(b.name, b.status, bid);
        buildingItem.innerHTML = `
            <div class="building-info">
                <div class="building-left">
                    <img src="${portfolioBuildingIconPath}" alt="${b.name}" class="building-icon" onerror="this.style.display='none'">
                    <span class="building-name">${b.name}</span>
                </div>
                <div class="building-right">
                    <span class="building-status-pill ${b.status}">${statusText}</span>
                </div>
            </div>
        `;
        list.appendChild(buildingItem);
    });
    } catch (error) {
        console.error('Ошибка обновления объектов:', error);
    }
}

function getStatusText(status) {
    const s = (status == null) ? '' : String(status);
    const statusMap = {
        building: 'Строится',
        active: 'Активен',
        for_sale: 'Продается',
        sold: 'Продан',
        // устаревшее значение в кэше/старых ответах
        completed: 'Активен',
    };
    return statusMap[s] || status;
}

// Инициализация экранов
function initializeScreens() {
    // Убеждаемся, что портфель активен по умолчанию
    const portfolioScreen = document.getElementById('portfolio-screen');
    if (portfolioScreen) {
        // Скрываем все экраны
        document.querySelectorAll('.screen').forEach(screen => {
            screen.classList.remove('active');
        });
        // Активируем портфель
        portfolioScreen.classList.add('active');
        
        // Обновляем активную кнопку навигации и иконки
        document.querySelectorAll('.nav-btn').forEach(btn => {
            btn.classList.remove('active');
        });
        const navBtn = document.getElementById('nav-portfolio');
        if (navBtn) {
            navBtn.classList.add('active');
        }
        updateNavIcons('portfolio');
    }
}

/** Файл в design/icons с URL-encoding (корректно на Linux и в Telegram WebView). */
function miniappDesignIconsUrl(filename) {
    return '/design/icons/' + encodeURIComponent(filename);
}

/** Иконка ресурса — как на веб-проекторе (script.js): первая буква заглавная + .png, ключ нормализуем. */
function miniappResourceIconUrl(resourceKey) {
    if (resourceKey == null || resourceKey === '') return '';
    const r = String(resourceKey).trim().toLowerCase();
    if (!r) return '';
    const resourceIconName = r.charAt(0).toUpperCase() + r.slice(1) + '.png';
    return miniappDesignIconsUrl(resourceIconName);
}

/** Большое изображение на карточке ресурса: design/картинки ресурсов/<имя как у ресурса>.png */
function miniappResourceCardImageUrl(resourceKey, filenameVariant) {
    if (resourceKey == null || resourceKey === '') return '';
    const dir = 'картинки ресурсов';
    let stem = filenameVariant != null && filenameVariant !== ''
        ? String(filenameVariant).trim()
        : String(resourceKey).trim().toLowerCase();
    stem = stem.replace(/\.png$/i, '');
    const file = stem + '.png';
    return '/design/' + encodeURIComponent(dir) + '/' + encodeURIComponent(file);
}

// Пути иконок меню (design/icons): активная и неактивная версия для каждого раздела
const NAV_ICONS = {
    portfolio: { active: miniappDesignIconsUrl('Портфель.png'), inactive: miniappDesignIconsUrl('Портфель(1).png') },
    market: { active: miniappDesignIconsUrl('Биржа.png'), inactive: miniappDesignIconsUrl('Биржа(1).png') },
    leaderboard: { active: miniappDesignIconsUrl('Рейтинг.png'), inactive: miniappDesignIconsUrl('Рейтинг(1).png') },
    settings: { active: miniappDesignIconsUrl('Настройки.png'), inactive: miniappDesignIconsUrl('Настройки(1).png') }
};

function updateNavIcons(activeScreenName) {
    const mainScreens = ['portfolio', 'market', 'leaderboard', 'settings'];
    mainScreens.forEach(name => {
        const btn = document.getElementById('nav-' + name);
        if (!btn) return;
        const img = btn.querySelector('img.nav-icon');
        if (!img || !NAV_ICONS[name]) return;
        img.src = (name === activeScreenName) ? NAV_ICONS[name].active : NAV_ICONS[name].inactive;
    });
}

function showRulesScreen() {
    showScreen('rules');
}
function goBackFromRules() {
    showScreen('settings', { restoreScroll: true });
}

// Навигация между экранами (options.restoreScroll — вернуть прокрутку при «Назад»)
function showScreen(screenName, options) {
    const opts = options && typeof options === 'object' ? options : {};
    
    // Убеждаемся, что main-container отображается ПЕРЕД поиском элементов
    const mainContainer = document.getElementById('main-container');
    const wasHidden = mainContainer && mainContainer.style.display === 'none';
    if (wasHidden) {
        mainContainer.style.display = 'block';
    }
    
    // Если контейнер был скрыт, используем requestAnimationFrame для гарантии обновления DOM
    if (wasHidden) {
        requestAnimationFrame(() => {
            performScreenSwitch(screenName, mainContainer, opts);
        });
    } else {
        performScreenSwitch(screenName, mainContainer, opts);
    }
}

function performScreenSwitch(screenName, mainContainer, options) {
    const opts = options && typeof options === 'object' ? options : {};
    // Пока виден текущий экран — запоминаем прокрутку ухода
    captureActiveScreenScroll();

    // Сохраняем предыдущий экран (если это не экран ресурса и не страницы успешных действий)
    if (screenName !== 'resource' && 
        !screenName.startsWith('success-') && 
        !screenName.startsWith('error-')) {
        previousScreen = screenName;
    }
    
    // ВСЕГДА используем document для поиска элементов, даже если они внутри скрытого контейнера
    // getElementById и querySelectorAll работают из document независимо от видимости элементов
    
    // Скрываем все экраны (ищем во всем документе)
    document.querySelectorAll('.screen').forEach(screen => {
        screen.classList.remove('active');
    });
    
    // Показываем нужный экран (getElementById работает из document, даже для скрытых элементов)
    const screenId = `${screenName}-screen`;
    const targetScreen = document.getElementById(screenId);
    
    if (targetScreen) {
        targetScreen.classList.add('active');
        applyTargetScreenScroll(screenName, targetScreen, opts.restoreScroll === true);
    } else {
        console.error('Screen not found:', screenId);
    }
    
    // Обновляем активную кнопку навигации и иконки (только для основных экранов)
    const mainScreens = ['portfolio', 'market', 'leaderboard', 'settings'];
    if (mainScreens.includes(screenName)) {
        document.querySelectorAll('.nav-btn').forEach(btn => {
            btn.classList.remove('active');
        });
        const navBtn = document.getElementById(`nav-${screenName}`);
        if (navBtn) {
            navBtn.classList.add('active');
        }
        updateNavIcons(screenName);
    }
    
    // Загружаем данные для экрана, если нужно
    if (screenName === 'leaderboard') {
        loadLeaderboard();
    } else if (screenName === 'market') {
        loadPrices();
        updateMarketBuildings();
    }
}

// Загрузка рейтинга
async function loadLeaderboard() {
    try {
        const response = await fetch(addGameCodeToUrl('/api/miniapp/leaderboard'));
        if (!response.ok) return;

        const data = await response.json();
        updateLeaderboard(data.leaderboard || []);
    } catch (error) {
        console.error('Ошибка загрузки рейтинга:', error);
    }
}

// Обновление рейтинга
function updateLeaderboard(leaderboard) {
    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    }

    function escAttr(s) {
        return esc(s).replace(/"/g, '&quot;');
    }

    const list = document.getElementById('leaderboard-list');
    list.innerHTML = '';
    miniappLeaderboardCache = Array.isArray(leaderboard) ? leaderboard.slice() : [];

    if (leaderboard.length === 0) {
        list.innerHTML = '<div style="text-align: center; color: #3a2a1a; padding: 20px;">Рейтинг пуст</div>';
        return;
    }

    leaderboard.forEach((player, index) => {
        const rank = index + 1;
        const displayName = player.character_name || player.nickname || player.name || 'Игрок';
        const initialLetter = (displayName.trim().charAt(0) || '?').toUpperCase();

        const playerItem = document.createElement('div');
        playerItem.className = 'leaderboard-item leaderboard-item--clickable';
        playerItem.setAttribute('role', 'button');
        playerItem.tabIndex = 0;
        playerItem.addEventListener('click', () => openMiniappPlayerModal(index));
        playerItem.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                openMiniappPlayerModal(index);
            }
        });

        let arrowHtml = '';
        if (player.rank_change === 'up') {
            arrowHtml = '<span class="leaderboard-arrow leaderboard-arrow-up">↑</span>';
        } else if (player.rank_change === 'down') {
            arrowHtml = '<span class="leaderboard-arrow leaderboard-arrow-down">↓</span>';
        } else {
            arrowHtml = '<span class="leaderboard-arrow-empty"></span>';
        }

        const growthRound = player.growth_round_percent || 0;
        const growthGame = player.growth_game_percent || 0;
        const growthRoundClass = growthRound >= 0 ? 'positive' : 'negative';
        const growthGameClass = growthGame >= 0 ? 'positive' : 'negative';

        const portfolio = player.buildings_portfolio || [];
        const seenBuildingNames = new Set();
        const buildingIconParts = [];
        for (let bi = 0; bi < portfolio.length; bi++) {
            const row = portfolio[bi];
            const bname = row && row.name;
            if (!bname || seenBuildingNames.has(bname)) continue;
            seenBuildingNames.add(bname);
            const iconSrc = miniappBuildingIconUrlForName(bname);
            buildingIconParts.push(
                `<img class="leaderboard-building-icon" src="${iconSrc}" alt="" title="${escAttr(bname)}" loading="lazy" onerror="this.style.display='none'" />`
            );
        }
        const buildingIconsHtml = buildingIconParts.length
            ? `<div class="leaderboard-building-icons">${buildingIconParts.join('')}</div>`
            : '<div class="leaderboard-building-icons leaderboard-building-icons--empty" aria-hidden="true"></div>';

        playerItem.innerHTML = `
            <div class="leaderboard-avatar-cell" aria-hidden="true">
                <img class="leaderboard-avatar" alt="" />
                <div class="leaderboard-avatar-fallback">${esc(initialLetter)}</div>
            </div>
            <div class="leaderboard-info">
                <div class="leaderboard-name-row">
                    <div class="leaderboard-name">${esc(displayName)}</div>
                    <div class="leaderboard-arrow-slot">${arrowHtml}</div>
                </div>
                <div class="leaderboard-capitalization">${Math.round(player.total_value || 0).toLocaleString('ru-RU')}</div>
                <div class="leaderboard-growth-row">
                    <div class="leaderboard-growth">
                        <div class="leaderboard-growth-box ${growthRoundClass}">
                            <div class="leaderboard-growth-value">${Math.round(growthRound)}%</div>
                        </div>
                        <div class="leaderboard-growth-box ${growthGameClass}">
                            <div class="leaderboard-growth-value">${Math.round(growthGame)}%</div>
                        </div>
                    </div>
                    ${buildingIconsHtml}
                </div>
            </div>
            <div class="leaderboard-rank-side">
                <span class="leaderboard-rank-number">${rank}</span>
            </div>
        `;

        const avatarImg = playerItem.querySelector('.leaderboard-avatar');
        const avatarFb = playerItem.querySelector('.leaderboard-avatar-fallback');
        const avatarUrl = player.character_image || player.photo_url || '';
        if (avatarImg && avatarFb) {
            if (avatarUrl) {
                avatarFb.style.display = 'flex';
                avatarImg.style.display = 'none';
                avatarImg.onload = function () {
                    avatarImg.style.display = 'block';
                    avatarFb.style.display = 'none';
                };
                avatarImg.onerror = function () {
                    avatarImg.style.display = 'none';
                    avatarFb.style.display = 'flex';
                };
                avatarImg.src = avatarUrl;
                if (avatarImg.complete && avatarImg.naturalWidth > 0) {
                    avatarImg.style.display = 'block';
                    avatarFb.style.display = 'none';
                }
            } else {
                avatarImg.style.display = 'none';
                avatarFb.style.display = 'flex';
            }
        }

        list.appendChild(playerItem);
    });
}

function miniappBuildingIconUrlForName(buildingName) {
    if (!buildingName) return '';
    return miniappDesignIconsUrl(buildingName + '.png');
}

/** Имена файлов в design/картинки для веба (регистр как на диске, Linux case-sensitive). */
const MINIAPP_BUILDING_WEB_FILES = {
    'Лесоповал': 'лесоповал.png',
    'Каменоломня': 'каменоломня.png',
    'Рыболовня': 'рыболовня.png',
    'Трактир': 'Трактир.png',
    'Теплицы': 'теплицы.png',
    'Посевные поля': 'Посевные поля.png',
    'Ферма': 'ферма.png',
    'Постоялый двор': 'постоялый двор.png',
    'Кузнечная': 'кузнечная.png',
    'Золотой рудник': 'золотой рудник.png',
    'Куртизанские палатки': 'куртизанские палатки.png'
};

/** Картинки карточек объектов на бирже: design/картинки для карточек объектов/<имя в нижнем регистре> (1).png */
function miniappMarketBuildingCardImageUrl(buildingName) {
    if (!buildingName) return '';
    const dir = 'картинки для карточек объектов';
    const file = buildingName.toLowerCase() + ' (1).png';
    return '/design/' + encodeURIComponent(dir) + '/' + encodeURIComponent(file);
}

/** Страница объекта на бирже / портфеле: design/картинки для веба/… */
function miniappMarketBuildingWebImageUrl(buildingName) {
    if (!buildingName) return '';
    const dir = 'картинки для веба';
    const file = MINIAPP_BUILDING_WEB_FILES[buildingName] || (buildingName + '.png');
    return '/design/' + encodeURIComponent(dir) + '/' + encodeURIComponent(file);
}

/** Подписи статусов как на вебе (карточка игрока в турнирной таблице) */
function miniappPortfolioStatusText(status) {
    return getStatusText(status);
}

function fillMiniappPlayerModalCard(player, rankIndex) {
    const name = player.character_name || player.name || 'Игрок';
    const money = typeof player.money === 'number' ? player.money : parseFloat(player.money) || 0;
    const totalVal = player.total_value != null && player.total_value !== ''
        ? Number(player.total_value)
        : NaN;
    const displayCapitalization = Number.isFinite(totalVal) ? totalVal : money;
    const gr = player.growth_round_percent != null ? player.growth_round_percent : (player.growth_percent || 0);
    const gg = player.growth_game_percent != null ? player.growth_game_percent : 0;

    const nameEl = document.getElementById('miniapp-player-modal-name');
    const moneyValueEl = document.getElementById('miniapp-player-modal-money-value');
    const rankNumEl = document.getElementById('miniapp-player-modal-rank-num');
    const pillRound = document.getElementById('miniapp-player-modal-pill-round');
    const pillGame = document.getElementById('miniapp-player-modal-pill-game');
    const listEl = document.getElementById('miniapp-player-modal-buildings');
    const imgEl = document.getElementById('miniapp-player-modal-avatar');
    const fbEl = document.getElementById('miniapp-player-modal-avatar-fallback');

    if (nameEl) nameEl.textContent = name;
    if (rankNumEl) rankNumEl.textContent = String(rankIndex + 1);
    if (moneyValueEl) moneyValueEl.textContent = Math.round(displayCapitalization).toLocaleString('ru-RU');

    if (pillRound) {
        const signR = gr > 0 ? '+' : '';
        pillRound.textContent = `${signR}${Math.round(gr)}%`;
        pillRound.classList.toggle('negative', gr < 0);
    }
    if (pillGame) {
        const signG = gg > 0 ? '+' : '';
        pillGame.textContent = `${signG}${Math.round(gg)}%`;
        pillGame.classList.toggle('negative', gg < 0);
    }

    const heroEl = document.getElementById('miniapp-player-modal-hero');
    const heroBlur = document.getElementById('miniapp-player-modal-hero-blur');

    function setMiniappPlayerHeroBackdrop(hasPhoto, srcForBackdrop) {
        if (!heroEl || !heroBlur) return;
        if (hasPhoto && srcForBackdrop) {
            heroEl.classList.remove('miniapp-player-modal-hero--no-photo');
            const safe = String(srcForBackdrop).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
            heroBlur.style.backgroundImage = `url("${safe}")`;
        } else {
            heroEl.classList.add('miniapp-player-modal-hero--no-photo');
            heroBlur.style.backgroundImage = '';
        }
    }

    const photo = player.character_image || player.photo_url || '';
    const photoTrimmed = typeof photo === 'string' ? photo.trim() : '';
    if (photoTrimmed && imgEl && fbEl) {
        setMiniappPlayerHeroBackdrop(true, photoTrimmed);
        imgEl.onload = () => {
            imgEl.style.display = 'block';
            fbEl.style.display = 'none';
        };
        imgEl.onerror = () => {
            imgEl.style.display = 'none';
            fbEl.style.display = 'flex';
            fbEl.textContent = (name.charAt(0) || '?').toUpperCase();
            setMiniappPlayerHeroBackdrop(false);
        };
        imgEl.src = photoTrimmed;
        if (imgEl.complete && imgEl.naturalWidth > 0) {
            imgEl.style.display = 'block';
            fbEl.style.display = 'none';
        }
    } else if (imgEl && fbEl) {
        setMiniappPlayerHeroBackdrop(false);
        imgEl.removeAttribute('src');
        imgEl.style.display = 'none';
        fbEl.style.display = 'flex';
        fbEl.textContent = (name.charAt(0) || '?').toUpperCase();
    }

    if (listEl) {
        listEl.innerHTML = '';
        const portfolio = (player.buildings_portfolio || []).filter(function (row) {
            return row && row.status !== 'sold';
        });
        if (portfolio.length > 0) {
            portfolio.forEach((row) => {
                const li = document.createElement('li');
                const icon = document.createElement('img');
                icon.className = 'miniapp-player-modal-building-icon';
                icon.alt = '';
                icon.src = miniappBuildingIconUrlForName(row.name);
                icon.onerror = () => { icon.style.visibility = 'hidden'; };
                const label = document.createElement('span');
                label.className = 'miniapp-player-modal-building-name';
                label.textContent = row.name;
                const pill = document.createElement('span');
                pill.className = 'building-status-pill ' + (row.status || '');
                pill.textContent = miniappPortfolioStatusText(row.status);
                li.appendChild(icon);
                li.appendChild(label);
                li.appendChild(pill);
                listEl.appendChild(li);
            });
        } else {
            const li = document.createElement('li');
            li.className = 'miniapp-player-modal-buildings-empty';
            li.textContent = 'Нет объектов';
            listEl.appendChild(li);
        }
    }

    const resourcesListEl = document.getElementById('miniapp-player-modal-resources');
    if (resourcesListEl) {
        resourcesListEl.innerHTML = '';
        const rp = player.resources_portfolio || [];
        if (rp.length > 0) {
            rp.forEach((row) => {
                const rname = row && row.name;
                if (!rname) return;
                const amount = typeof row.amount === 'number' ? row.amount : parseFloat(row.amount) || 0;
                const li = document.createElement('li');
                const icon = document.createElement('img');
                icon.className = 'miniapp-player-modal-building-icon';
                icon.alt = '';
                icon.src = miniappResourceIconUrl(rname);
                icon.onerror = () => { icon.style.visibility = 'hidden'; };
                const label = document.createElement('span');
                label.className = 'miniapp-player-modal-building-name';
                label.textContent = capitalizeFirst(rname);
                const amtEl = document.createElement('span');
                amtEl.className = 'miniapp-player-modal-resource-amount';
                amtEl.textContent = Math.round(amount).toLocaleString('ru-RU');
                li.appendChild(icon);
                li.appendChild(label);
                li.appendChild(amtEl);
                resourcesListEl.appendChild(li);
            });
        } else {
            const li = document.createElement('li');
            li.className = 'miniapp-player-modal-buildings-empty';
            li.textContent = 'Нет ресурсов';
            resourcesListEl.appendChild(li);
        }
    }
}

function miniappPlayerModalRowNeedsLeaderboardRefresh(row) {
    if (!row) return true;
    if (!Object.prototype.hasOwnProperty.call(row, 'buildings_portfolio')) return true;
    if (!Object.prototype.hasOwnProperty.call(row, 'resources_portfolio')) return true;
    const rp = row.resources_portfolio;
    if (Array.isArray(rp) && rp.length > 0) return false;
    const rv = row.resources_value != null ? Number(row.resources_value) : NaN;
    return Number.isFinite(rv) && rv > 0;
}

function fillMiniappPlayerModalFromIndex(index) {
    const base = miniappLeaderboardCache[index];
    if (!base) return;
    if (!miniappPlayerModalRowNeedsLeaderboardRefresh(base) &&
        Object.prototype.hasOwnProperty.call(base, 'buildings_portfolio')) {
        fillMiniappPlayerModalCard(base, index);
        return;
    }
    fillMiniappPlayerModalCard(base, index);
    fetch(addGameCodeToUrl('/api/miniapp/leaderboard'))
        .then(function (res) { return res.json(); })
        .then(function (data) {
            const lb = data && data.leaderboard;
            if (lb && lb.length) {
                const pid = base.player_id;
                const row = lb.find(function (p) {
                    return String(p.player_id) === String(pid);
                });
                if (row) {
                    miniappLeaderboardCache[index] = Object.assign({}, base, row);
                }
            }
            fillMiniappPlayerModalCard(miniappLeaderboardCache[index], index);
        })
        .catch(function () {});
}

function updateMiniappPlayerModalNavButtons() {
    const left = document.getElementById('miniapp-player-modal-nav-left');
    const right = document.getElementById('miniapp-player-modal-nav-right');
    const n = miniappLeaderboardCache.length;
    if (left) left.disabled = n <= 1 || currentMiniappPlayerModalIndex <= 0;
    if (right) right.disabled = n <= 1 || currentMiniappPlayerModalIndex >= n - 1;
}

function openMiniappPlayerModal(playerIndex) {
    if (!miniappLeaderboardCache.length || playerIndex < 0 || playerIndex >= miniappLeaderboardCache.length) {
        return;
    }
    currentMiniappPlayerModalIndex = playerIndex;
    fillMiniappPlayerModalFromIndex(currentMiniappPlayerModalIndex);
    updateMiniappPlayerModalNavButtons();
    if (miniappPlayerModalEl) {
        miniappPlayerModalEl.classList.add('miniapp-player-modal--open');
        miniappPlayerModalEl.setAttribute('aria-hidden', 'false');
    }
}

function closeMiniappPlayerModal() {
    if (miniappPlayerModalEl) {
        miniappPlayerModalEl.classList.remove('miniapp-player-modal--open');
        miniappPlayerModalEl.setAttribute('aria-hidden', 'true');
    }
}

function navigateMiniappPlayerModal(delta) {
    const n = miniappLeaderboardCache.length;
    if (n <= 1) return;
    let next = currentMiniappPlayerModalIndex + delta;
    if (next < 0 || next >= n) return;
    currentMiniappPlayerModalIndex = next;
    fillMiniappPlayerModalFromIndex(currentMiniappPlayerModalIndex);
    updateMiniappPlayerModalNavButtons();
}

// Обновление цен (для страницы Биржа)
function updatePrices() {
    const list = document.getElementById('resources-market-list');
    if (!list) return; // Если элемент не найден, выходим
    
    list.innerHTML = '';

    prices.forEach(price => {
        const item = document.createElement('div');
        item.className = 'resource-market-item';
        
        const changeRoundClass = price.change_from_prev_percent >= 0 ? 'positive' : 'negative';
        const changeRoundSign = price.change_from_prev_percent >= 0 ? '+' : '';
        const changeGameClass = price.change_from_start_percent >= 0 ? 'positive' : 'negative';
        const changeGameSign = price.change_from_start_percent >= 0 ? '+' : '';
        
        const iconPath = miniappResourceIconUrl(price.resource);
        item.style.cursor = 'pointer';
        item.onclick = () => showResourceScreen(price.resource);
        item.innerHTML = `
            <div class="resource-market-left">
                <img src="${iconPath}" alt="${capitalizeFirst(price.resource)}" class="resource-market-icon" onerror="this.style.display='none'">
                <span class="resource-market-name">${capitalizeFirst(price.resource)}</span>
            </div>
            <div class="resource-market-right">
                <div class="resource-market-changes">
                    <div class="resource-market-change-box ${changeRoundClass}">
                        <span class="resource-market-change-value">${changeRoundSign}${Math.round(price.change_from_prev_percent)}%</span>
                    </div>
                    <div class="resource-market-change-box ${changeGameClass}">
                        <span class="resource-market-change-value">${changeGameSign}${Math.round(price.change_from_start_percent)}%</span>
                    </div>
                </div>
                <span class="resource-market-price">${price.current_price.toLocaleString('ru-RU')}</span>
            </div>
        `;
        list.appendChild(item);
    });
}

// Обновление объектов на странице Биржа
async function updateMarketBuildings() {
    const grid = document.getElementById('buildings-market-grid');
    if (!grid) return;
    
    grid.innerHTML = '';
    
    // Все возможные объекты из конфига (Куртизанские палатки — последней)
    const allBuildings = [
        'Лесоповал', 'Каменоломня', 'Рыболовня', 'Трактир', 
        'Теплицы', 'Посевные поля', 'Ферма', 'Постоялый двор',
        'Кузнечная', 'Золотой рудник', 'Куртизанские палатки'
    ];
    
    try {
        // Берем оценку из того же API, что и внутри карточки объекта
        const details = await Promise.all(allBuildings.map(async (buildingName) => {
            try {
                const response = await fetch(
                    addGameCodeToUrl(`/api/miniapp/market/building/${encodeURIComponent(buildingName)}`),
                    {
                        headers: {
                            'X-Telegram-Init-Data': getTelegramInitDataHeader()
                        }
                    }
                );
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const data = await response.json();
                return {
                    buildingName,
                    costCoins: typeof data.cost_coins === 'number' ? data.cost_coins : 0
                };
            } catch (error) {
                console.warn(`Не удалось получить оценку для ${buildingName}:`, error);
                return { buildingName, costCoins: 0 };
            }
        }));
        
        details.forEach(({ buildingName, costCoins }) => {
            const card = document.createElement('div');
            card.className = 'building-market-card';
            card.style.cursor = 'pointer';
            card.onclick = () => showMarketBuildingScreen(buildingName);
            card.innerHTML = `
                <div class="building-market-name">${buildingName}</div>
                <img src="${miniappMarketBuildingCardImageUrl(buildingName)}" alt="${buildingName}" class="building-market-image" onerror="this.style.display='none'">
                <div class="building-market-stats">
                    <span class="building-market-value">${costCoins.toLocaleString('ru-RU')}</span>
                </div>
            `;
            grid.appendChild(card);
        });
    } catch (error) {
        console.error('Ошибка загрузки объектов:', error);
    }
}

// Показ страницы ресурса
async function showResourceScreen(resourceName) {
    currentResource = resourceName;
    
    // Сохраняем текущий активный экран как предыдущий и исходный
    const activeScreen = document.querySelector('.screen.active');
    if (activeScreen && activeScreen.id !== 'resource-screen') {
        const screenName = activeScreen.id.replace('-screen', '');
        previousScreen = screenName;
        // Сохраняем исходный экран только если это портфель или биржа
        if (screenName === 'portfolio' || screenName === 'market') {
            originScreen = screenName;
        }
    }
    
    // Загружаем данные ресурса
    try {
        const response = await fetch(addGameCodeToUrl(`/api/resource/${encodeURIComponent(resourceName)}`));
        if (!response.ok) {
            throw new Error('Ошибка загрузки данных ресурса');
        }
        
        const data = await response.json();
        
        // Заполняем данные на странице
        document.getElementById('resource-screen-name').textContent = capitalizeFirst(resourceName);
        
        // Картинка (папка design/картинки ресурсов; имя файла как у ресурса; иначе — иконка)
        const imageEl = document.getElementById('resource-screen-image');
        const rn = String(resourceName).trim();
        const rnLower = rn.toLowerCase();
        const altFileBase = capitalizeFirst(rnLower);
        let phase = 0;
        imageEl.alt = capitalizeFirst(resourceName);
        imageEl.style.display = 'block';
        imageEl.onerror = function () {
            phase += 1;
            if (phase === 1) {
                this.src = miniappResourceCardImageUrl(resourceName, altFileBase);
            } else if (phase === 2) {
                this.src = miniappResourceIconUrl(resourceName);
                this.onerror = function () {
                    this.style.display = 'none';
                };
            }
        };
        imageEl.src = miniappResourceCardImageUrl(resourceName, rnLower);
        
        // Цена
        document.getElementById('resource-screen-price').textContent = `${data.current_price.toLocaleString('ru-RU')}`;
        
        // Изменения
        const changeRound = data.change_from_prev_percent || 0;
        const changeGame = data.change_from_start_percent || 0;
        const changeRoundClass = changeRound >= 0 ? 'positive' : 'negative';
        const changeGameClass = changeGame >= 0 ? 'positive' : 'negative';
        const changeRoundSign = changeRound >= 0 ? '+' : '';
        const changeGameSign = changeGame >= 0 ? '+' : '';
        
        const changeRoundEl = document.getElementById('resource-screen-change-round');
        const changeGameEl = document.getElementById('resource-screen-change-game');
        const changeRoundBox = document.getElementById('resource-screen-change-round-box');
        const changeGameBox = document.getElementById('resource-screen-change-game-box');
        
        changeRoundEl.textContent = `${changeRoundSign}${Math.round(changeRound)}%`;
        changeGameEl.textContent = `${changeGameSign}${Math.round(changeGame)}%`;
        changeRoundBox.className = `resource-screen-change-box ${changeRoundClass}`;
        changeGameBox.className = `resource-screen-change-box ${changeGameClass}`;
        
        // Количество ресурса у игрока
        const amount = playerState?.resources?.[resourceName] || 0;
        document.getElementById('resource-screen-available-value').textContent = amount.toLocaleString('ru-RU');
        
        // График
        setTimeout(() => {
            drawPriceChartForResource(data.price_history);
        }, 100);
        
        // Показываем экран
        showScreen('resource');
    } catch (error) {
        console.error('Ошибка загрузки ресурса:', error);
        showToast('Ошибка загрузки данных ресурса', 'error');
    }
}

// Возврат назад со страницы ресурса
function goBackFromResource() {
    // Всегда возвращаемся на исходный экран (портфель или биржа)
    showScreen(originScreen, { restoreScroll: true });
    currentResource = null;
}

// Отрисовка графика цены для страницы ресурса
function drawPriceChartForResource(priceHistory) {
    const canvas = document.getElementById('resource-screen-chart');
    if (!canvas) {
        console.error('Canvas не найден');
        return;
    }
    
    const ctx = canvas.getContext('2d');
    
    // Устанавливаем размер canvas
    const container = canvas.parentElement;
    if (container) {
        canvas.width = container.clientWidth - 20;
    } else {
        canvas.width = canvas.offsetWidth || 400;
    }
    canvas.height = 300;
    
    if (!priceHistory || priceHistory.length === 0) {
        ctx.fillStyle = '#3a2a1a';
        ctx.font = "16px 'San Francisco', -apple-system, sans-serif";
        ctx.textAlign = 'center';
        ctx.fillText('Нет данных для графика', canvas.width / 2, canvas.height / 2);
        return;
    }
    
    // Очищаем canvas
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    
    // Параметры графика
    const padding = 40;
    const chartWidth = canvas.width - padding * 2;
    const chartHeight = canvas.height - padding * 2;
    
    // Один ряд точек по номеру раунда (последняя цена побеждает), сортировка по round
    const byRound = new Map();
    for (const p of priceHistory) {
        if (!p || typeof p.round !== 'number' || p.round < 1) continue;
        byRound.set(p.round, { round: p.round, price: Number(p.price) });
    }
    let standardizedHistory = Array.from(byRound.keys())
        .sort((a, b) => a - b)
        .map((r) => byRound.get(r));
    if (standardizedHistory.length === 0) {
        ctx.fillStyle = '#3a2a1a';
        ctx.font = "16px 'San Francisco', -apple-system, sans-serif";
        ctx.textAlign = 'center';
        ctx.fillText('Нет данных для графика', canvas.width / 2, canvas.height / 2);
        return;
    }
    
    // Находим минимальное и максимальное значение цены
    const prices = standardizedHistory.map(h => h.price);
    const minPrice = 0;
    const maxPrice = Math.max(...prices);
    const priceRange = maxPrice - minPrice || 1;
    
    // Рисуем оси
    ctx.strokeStyle = '#8b4513';
    ctx.lineWidth = 2;
    
    // Ось X
    ctx.beginPath();
    ctx.moveTo(padding, canvas.height - padding);
    ctx.lineTo(canvas.width - padding, canvas.height - padding);
    ctx.stroke();
    
    // Ось Y
    ctx.beginPath();
    ctx.moveTo(padding, padding);
    ctx.lineTo(padding, canvas.height - padding);
    ctx.stroke();
    
    // Рисуем сетку
    ctx.strokeStyle = '#8b4513';
    ctx.lineWidth = 1;
    ctx.setLineDash([5, 5]);
    
    const gridLines = 5;
    for (let i = 0; i <= gridLines; i++) {
        const y = padding + (chartHeight / gridLines) * i;
        const price = maxPrice - (priceRange / gridLines) * i;
        
        ctx.beginPath();
        ctx.moveTo(padding, y);
        ctx.lineTo(canvas.width - padding, y);
        ctx.stroke();
        
        ctx.fillStyle = '#3a2a1a';
        ctx.font = "12px 'San Francisco', -apple-system, sans-serif";
        ctx.textAlign = 'right';
        ctx.fillText(Math.round(price).toString(), padding - 10, y + 4);
    }
    
    ctx.setLineDash([]);
    
    const n = standardizedHistory.length;
    // Первая точка — у вертикальной оси (x = padding), дальше равномерно до правого края сетки
    function xForIndex(index) {
        if (n <= 1) return padding;
        return padding + (chartWidth / (n - 1)) * index;
    }
    function yForPrice(price) {
        return padding + chartHeight - ((price - minPrice) / priceRange) * chartHeight;
    }
    
    // Линия только от 2-й точки (раньше — только маркер без соединительной линии)
    if (n >= 2) {
        ctx.strokeStyle = '#006400';
        ctx.lineWidth = 3;
        ctx.beginPath();
        standardizedHistory.forEach((point, index) => {
            const x = xForIndex(index);
            const y = yForPrice(point.price);
            if (index === 0) {
                ctx.moveTo(x, y);
            } else {
                ctx.lineTo(x, y);
            }
        });
        ctx.stroke();
    }
    
    // Точки и подписи номеров раундов под осью X (у каждой точки свой round)
    ctx.fillStyle = '#006400';
    const labelStride = n > 12 ? Math.ceil(n / 8) : 1;
    standardizedHistory.forEach((point, index) => {
        const x = xForIndex(index);
        const y = yForPrice(point.price);
        ctx.beginPath();
        ctx.arc(x, y, 4, 0, Math.PI * 2);
        ctx.fill();
        const showLabel =
            n <= 12 || index === 0 || index === n - 1 || index % labelStride === 0;
        if (showLabel) {
            ctx.fillStyle = '#3a2a1a';
            ctx.font = "11px 'San Francisco', -apple-system, sans-serif";
            ctx.textAlign = 'center';
            ctx.fillText(String(point.round), x, canvas.height - padding + 20);
            ctx.fillStyle = '#006400';
        }
    });
    
    // Подписи осей
    ctx.fillStyle = '#3a2a1a';
    ctx.font = "14px 'San Francisco', -apple-system, sans-serif";
    ctx.textAlign = 'center';
    ctx.fillText('Раунд', canvas.width / 2, canvas.height - 10);
    
    ctx.save();
    ctx.translate(15, canvas.height / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.fillText('Цена (монеты)', 0, 0);
    ctx.restore();
}

// Показ экрана покупки ресурса
function showBuyResourceScreen() {
    if (!currentResource) return;
    
    // Сохраняем предыдущий экран (это всегда resource)
    previousScreen = 'resource';
    
    // Находим цену ресурса
    const price = prices.find(p => p.resource === currentResource);
    if (!price) {
        showToast('Ошибка: цена ресурса не найдена', 'error');
        return;
    }
    
    // Заполняем данные
    document.getElementById('buy-resource-screen-name').textContent = `Купить ${capitalizeFirst(currentResource)}`;
    
    // Картинка
    const imagePath = miniappResourceIconUrl(currentResource);
    const imageEl = document.getElementById('buy-resource-image');
    imageEl.src = imagePath;
    imageEl.alt = capitalizeFirst(currentResource);
    imageEl.onerror = function() {
        this.style.display = 'none';
    };
    
    // Цена
    document.getElementById('buy-resource-price-value').textContent = price.current_price.toLocaleString('ru-RU');
    
    // Доступное количество (у вас есть)
    const amount = playerState?.resources?.[currentResource] || 0;
    document.getElementById('buy-resource-available-value').textContent = amount.toLocaleString('ru-RU');
    
    // Монеты
    const money = playerState?.money || 0;
    document.getElementById('buy-resource-money-value').textContent = money.toLocaleString('ru-RU');
    
    // Количество
    document.getElementById('buy-resource-quantity-input').value = 1;
    document.getElementById('buy-resource-quantity-input').max = '';
    updateBuyTotal();
    
    // Показываем экран
    showScreen('buy-resource');
}

// Показ экрана продажи ресурса
function showSellResourceScreen() {
    if (!currentResource) return;
    
    // Сохраняем предыдущий экран
    previousScreen = 'resource';
    
    // Находим цену и количество ресурса
    const price = prices.find(p => p.resource === currentResource);
    if (!price) {
        showToast('Ошибка: цена ресурса не найдена', 'error');
        return;
    }
    
    const amount = playerState?.resources?.[currentResource] || 0;
    if (amount === 0) {
        showToast('У вас нет этого ресурса', 'error');
        return;
    }
    
    // Заполняем данные
    document.getElementById('sell-resource-screen-name').textContent = `Продать ${capitalizeFirst(currentResource)}`;
    
    // Картинка
    const imagePath = miniappResourceIconUrl(currentResource);
    const imageEl = document.getElementById('sell-resource-image');
    imageEl.src = imagePath;
    imageEl.alt = capitalizeFirst(currentResource);
    imageEl.onerror = function() {
        this.style.display = 'none';
    };
    
    // Цена
    document.getElementById('sell-resource-price-value').textContent = price.current_price.toLocaleString('ru-RU');
    
    // Доступное количество
    document.getElementById('sell-resource-available-value').textContent = amount.toLocaleString('ru-RU');
    
    // Монеты
    const money = playerState?.money || 0;
    document.getElementById('sell-resource-money-value').textContent = money.toLocaleString('ru-RU');
    
    // Количество
    const quantityInput = document.getElementById('sell-resource-quantity-input');
    quantityInput.value = 1;
    quantityInput.max = amount;
    updateSellTotal();
    
    // Показываем экран
    showScreen('sell-resource');
}

// Управление количеством при покупке
function increaseBuyQuantity() {
    const input = document.getElementById('buy-resource-quantity-input');
    const currentValue = parseInt(input.value) || 1;
    input.value = currentValue + 1;
    updateBuyTotal();
}

function decreaseBuyQuantity() {
    const input = document.getElementById('buy-resource-quantity-input');
    const currentValue = parseInt(input.value) || 1;
    if (currentValue > 1) {
        input.value = currentValue - 1;
        updateBuyTotal();
    }
}

function updateBuyTotal() {
    const quantity = parseInt(document.getElementById('buy-resource-quantity-input').value) || 1;
    const price = prices.find(p => p.resource === currentResource);
    if (price) {
        const total = quantity * price.current_price;
        document.getElementById('buy-resource-total-value').textContent = total.toLocaleString('ru-RU');
    }
}

// Управление количеством при продаже
function increaseSellQuantity() {
    const input = document.getElementById('sell-resource-quantity-input');
    const max = parseInt(input.max) || 1;
    const currentValue = parseInt(input.value) || 1;
    if (currentValue < max) {
        input.value = currentValue + 1;
        updateSellTotal();
    }
}

function decreaseSellQuantity() {
    const input = document.getElementById('sell-resource-quantity-input');
    const currentValue = parseInt(input.value) || 1;
    if (currentValue > 1) {
        input.value = currentValue - 1;
        updateSellTotal();
    }
}

function updateSellTotal() {
    const quantity = parseInt(document.getElementById('sell-resource-quantity-input').value) || 1;
    const price = prices.find(p => p.resource === currentResource);
    if (price) {
        const total = quantity * price.current_price;
        document.getElementById('sell-resource-total-value').textContent = total.toLocaleString('ru-RU');
    }
}

// Подтверждение покупки
async function confirmBuyResource() {
    if (!guardTradingAction()) return;
    if (!currentResource) return;
    
    const quantity = parseInt(document.getElementById('buy-resource-quantity-input').value) || 1;
    if (quantity < 1) {
        showToast('Количество должно быть больше 0', 'error');
        return;
    }
    
    try {
        const response = await fetch(addGameCodeToUrl('/api/miniapp/player/buy-resource'), {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Telegram-Init-Data': getTelegramInitDataHeader()
            },
            body: JSON.stringify({
                resource: currentResource,
                quantity: quantity
            })
        });
        
        const data = await response.json();
        
        if (response.ok && data.success) {
            const price = prices.find(p => p.resource === currentResource);
            const cost = quantity * (price ? price.current_price : 0);
            
            showToast('Ресурс успешно куплен!', 'success');
            
            // Обновляем данные
            await loadPlayerState();
            await loadPrices();
            
            // Обновляем количество монет на экране покупки, если он все еще открыт
            const moneyEl = document.getElementById('buy-resource-money-value');
            if (moneyEl && playerState) {
                moneyEl.textContent = (playerState.money || 0).toLocaleString('ru-RU');
            }
        } else {
            // Проверяем, это ошибка недостаточности средств?
            if (data.message && (data.message.includes('недостаточно') || data.message.includes('Недостаточно'))) {
                showToast(data.message || 'Недостаточно средств', 'error');
            } else {
                showToast(data.message || 'Ошибка при покупке ресурса', 'error');
            }
        }
    } catch (error) {
        console.error('Ошибка покупки ресурса:', error);
        showToast('Ошибка при покупке ресурса', 'error');
    }
}

// Подтверждение продажи
async function confirmSellResource() {
    if (!guardTradingAction()) return;
    if (!currentResource) return;
    
    const quantity = parseInt(document.getElementById('sell-resource-quantity-input').value) || 1;
    if (quantity < 1) {
        showToast('Количество должно быть больше 0', 'error');
        return;
    }
    
    const max = parseInt(document.getElementById('sell-resource-quantity-input').max) || 0;
    if (quantity > max) {
        showToast('Недостаточно ресурсов', 'error');
        return;
    }
    
    try {
        const response = await fetch(addGameCodeToUrl('/api/miniapp/player/sell-resource'), {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Telegram-Init-Data': getTelegramInitDataHeader()
            },
            body: JSON.stringify({
                resource: currentResource,
                quantity: quantity
            })
        });
        
        const data = await response.json();
        
        if (response.ok && data.success) {
            const price = prices.find(p => p.resource === currentResource);
            const revenue = quantity * (price ? price.current_price : 0);
            
            showToast('Ресурс успешно продан!', 'success');
            
            // Обновляем данные
            await loadPlayerState();
            await loadPrices();
            
            // Обновляем количество монет на экране продажи, если он все еще открыт
            const moneyEl = document.getElementById('sell-resource-money-value');
            if (moneyEl && playerState) {
                moneyEl.textContent = (playerState.money || 0).toLocaleString('ru-RU');
            }
        } else {
            showToast(data.message || 'Ошибка при продаже ресурса', 'error');
        }
    } catch (error) {
        console.error('Ошибка продажи ресурса:', error);
        showToast('Ошибка при продаже ресурса', 'error');
    }
}

// Возврат назад с экранов покупки/продажи
function goBackFromBuyResource() {
    // Всегда возвращаемся на страницу ресурса
    showScreen('resource', { restoreScroll: true });
}

function goBackFromSellResource() {
    // Всегда возвращаемся на страницу ресурса
    showScreen('resource', { restoreScroll: true });
}

// Показ страницы объекта из портфеля
async function showPortfolioBuildingScreen(buildingName, buildingStatus, buildingId) {
    currentBuilding = buildingName;
    currentBuildingStatus = buildingStatus;
    currentBuildingId = buildingId != null && buildingId !== '' ? String(buildingId) : null;
    
    // Сохраняем текущий активный экран как предыдущий
    const activeScreen = document.querySelector('.screen.active');
    if (activeScreen && activeScreen.id !== 'portfolio-building-screen') {
        const screenName = activeScreen.id.replace('-screen', '');
        previousScreen = screenName;
        // Сохраняем исходный экран только если это портфель
        if (screenName === 'portfolio') {
            originScreen = screenName;
        }
    }
    
    // Загружаем данные объекта
    try {
        let buildingUrl = addGameCodeToUrl(
            `/api/miniapp/player/building/${encodeURIComponent(buildingName)}?status=${encodeURIComponent(buildingStatus)}`
        );
        if (currentBuildingId) {
            buildingUrl += `&building_id=${encodeURIComponent(currentBuildingId)}`;
        }
        const response = await fetch(buildingUrl, {
            headers: {
                'X-Telegram-Init-Data': getTelegramInitDataHeader()
            }
        });
        
        if (!response.ok) {
            throw new Error('Ошибка загрузки данных объекта');
        }
        
        const data = await response.json();
        
        // Заполняем данные на странице
        document.getElementById('portfolio-building-screen-name').textContent = buildingName;
        
        // Картинка
        const imagePath = miniappMarketBuildingWebImageUrl(buildingName);
        const imageEl = document.getElementById('portfolio-building-image');
        imageEl.src = imagePath;
        imageEl.alt = buildingName;
        imageEl.onerror = function() {
            this.style.display = 'none';
        };
        imageEl.style.display = 'block';
        
        // Статус (с классом для стилизации)
        const statusEl = document.getElementById('portfolio-building-status');
        statusEl.textContent = getStatusText(buildingStatus);
        statusEl.className = `portfolio-building-status ${buildingStatus}`;
        
        // Капитализация
        document.getElementById('portfolio-building-capitalization').textContent = data.capitalization.toLocaleString('ru-RU');
        
        // Изменения капитализации
        const changeRound = data.change_round_percent || 0;
        const changeGame = data.change_game_percent || 0;
        const changeRoundClass = changeRound >= 0 ? 'positive' : 'negative';
        const changeGameClass = changeGame >= 0 ? 'positive' : 'negative';
        const changeRoundSign = changeRound >= 0 ? '+' : '';
        const changeGameSign = changeGame >= 0 ? '+' : '';
        
        const changeRoundEl = document.getElementById('portfolio-building-change-round');
        const changeGameEl = document.getElementById('portfolio-building-change-game');
        const changeRoundBox = document.getElementById('portfolio-building-change-round-box');
        const changeGameBox = document.getElementById('portfolio-building-change-game-box');
        
        changeRoundEl.textContent = `${changeRoundSign}${Math.round(changeRound)}%`;
        changeGameEl.textContent = `${changeGameSign}${Math.round(changeGame)}%`;
        changeRoundBox.className = `portfolio-building-change-box ${changeRoundClass}`;
        changeGameBox.className = `portfolio-building-change-box ${changeGameClass}`;
        
        // Доход за раунд (базовый доход из конфига)
        const incomeEl = document.getElementById('portfolio-building-income');
        if (incomeEl) {
            incomeEl.innerHTML = '';
            if (data.income) {
                const incomeItems = [];
                
                // Монеты
                if (data.income.монеты && data.income.монеты > 0) {
                    incomeItems.push(`${data.income.монеты.toLocaleString('ru-RU')} монет`);
                }
                
                // Ресурсы
                if (data.income.ресурсы && Object.keys(data.income.ресурсы).length > 0) {
                    Object.entries(data.income.ресурсы).forEach(([resource, amount]) => {
                        if (amount > 0) {
                            incomeItems.push(`${amount.toLocaleString('ru-RU')} ${capitalizeFirst(resource)}`);
                        }
                    });
                }
                
                if (incomeItems.length > 0) {
                    incomeItems.forEach(item => {
                        const incomeItem = document.createElement('div');
                        incomeItem.className = 'portfolio-building-income-value';
                        incomeItem.textContent = item;
                        incomeEl.appendChild(incomeItem);
                    });
                } else {
                    const noIncome = document.createElement('div');
                    noIncome.className = 'portfolio-building-income-value';
                    noIncome.textContent = 'Нет дохода';
                    incomeEl.appendChild(noIncome);
                }
            }
        }
        
        // Показываем экран
        showScreen('portfolio-building');
    } catch (error) {
        console.error('Ошибка загрузки объекта:', error);
        showToast('Ошибка загрузки данных объекта', 'error');
    }
}

// Форматирование дохода для отображения
function formatIncome(income) {
    let html = '';
    
    if (income.monеты > 0) {
        html += `<span class="income-coins">${income.mонеты.toLocaleString('ru-RU')} монет</span>`;
    }
    
    const resources = Object.entries(income.ресурсы || {});
    if (resources.length > 0) {
        if (html) html += ', ';
        html += resources.map(([resource, amount]) => 
            `<span class="income-resource">${amount.toLocaleString('ru-RU')} ${capitalizeFirst(resource)}</span>`
        ).join(', ');
    }
    
    if (!html) {
        html = '<span class="income-none">0</span>';
    }
    
    return html;
}

// Возврат назад со страницы объекта из портфеля
function goBackFromPortfolioBuilding() {
    // Всегда возвращаемся на портфель
    showScreen('portfolio', { restoreScroll: true });
    currentBuilding = null;
    currentBuildingStatus = null;
    currentBuildingId = null;
}

// Показ страницы объекта на бирже
async function showMarketBuildingScreen(buildingName) {
    currentBuilding = buildingName;
    currentBuildingId = null;
    
    // Сохраняем текущий активный экран как предыдущий
    const activeScreen = document.querySelector('.screen.active');
    if (activeScreen && activeScreen.id !== 'market-building-screen') {
        const screenName = activeScreen.id.replace('-screen', '');
        previousScreen = screenName;
        // Сохраняем исходный экран (биржа)
        if (screenName === 'market') {
            originScreen = screenName;
        }
    }
    
    // Загружаем данные объекта
    try {
        const response = await fetch(addGameCodeToUrl(`/api/miniapp/market/building/${encodeURIComponent(buildingName)}`), {
            headers: {
                'X-Telegram-Init-Data': getTelegramInitDataHeader()
            }
        });
        
        if (!response.ok) {
            throw new Error('Ошибка загрузки данных объекта');
        }
        
        const data = await response.json();
        
        // Сначала показываем экран, чтобы элементы были доступны в DOM
        showScreen('market-building');
        
        // Ждем обновления DOM - увеличиваем задержку
        await new Promise(resolve => setTimeout(resolve, 100));
        
        // Проверяем, что страница действительно отображена
        const screenEl = document.getElementById('market-building-screen');
        if (!screenEl || !screenEl.classList.contains('active')) {
            console.error('Страница market-building-screen не активна!');
        }
        
        // Заполняем данные на странице
        document.getElementById('market-building-screen-name').textContent = buildingName;
        
        // Картинка (папка design/картинки для веба, имя файла = название объекта)
        const imagePath = miniappMarketBuildingWebImageUrl(buildingName);
        const imageEl = document.getElementById('market-building-image');
        imageEl.src = imagePath;
        imageEl.alt = buildingName;
        imageEl.onerror = function() {
            this.style.display = 'none';
        };
        imageEl.style.display = 'block';
        
        // Количество построенных объектов
        document.getElementById('market-building-count').textContent = data.count.toLocaleString('ru-RU');
        
        // Процент игроков
        document.getElementById('market-building-percentage').textContent = `${data.players_percentage}%`;
        
        // Стоимость в ресурсах (построчно с буллитами)
        const costResourcesEl = document.getElementById('market-building-cost-resources');
        costResourcesEl.innerHTML = '';
        if (data.cost_resources && Object.keys(data.cost_resources).length > 0) {
            Object.entries(data.cost_resources).forEach(([resource, amount]) => {
                const li = document.createElement('li');
                li.className = 'market-building-cost-resource-item';
                li.textContent = `${amount} ${resource}`;
                costResourcesEl.appendChild(li);
            });
        } else {
            const li = document.createElement('li');
            li.textContent = 'Нет данных';
            costResourcesEl.appendChild(li);
        }
        
        // Оценка в монетах (только цифра, выровнена по правому краю)
        document.getElementById('market-building-cost-coins').textContent = data.cost_coins.toLocaleString('ru-RU');
        
        // Приносит ресурса (в том же стиле, что и "Стоимость в ресурсах")
        let baseResourceListEl = document.getElementById('market-building-base-resource-list');
        
        // Если элемент не найден, создаем его динамически
        if (!baseResourceListEl) {
            console.log('Элемент не найден, создаем динамически...');
            // Находим блок "Оценка в монетах"
            const costCoinsItem = document.getElementById('market-building-cost-coins')?.closest('.market-building-stat-item');
            if (costCoinsItem && costCoinsItem.parentElement) {
                // Создаем новый блок
                const newBlock = document.createElement('div');
                newBlock.className = 'market-building-stat-item market-building-base-resource-item';
                newBlock.innerHTML = `
                    <div class="market-building-cost-resources-label">Приносит ресурса:</div>
                    <ul class="market-building-cost-resources-list" id="market-building-base-resource-list">
                        <!-- Заполняется через JS -->
                    </ul>
                `;
                // Вставляем после блока "Оценка в монетах"
                costCoinsItem.parentElement.insertBefore(newBlock, costCoinsItem.nextSibling);
                baseResourceListEl = document.getElementById('market-building-base-resource-list');
                console.log('Элемент создан динамически:', !!baseResourceListEl);
            } else {
                console.error('Не удалось найти место для вставки элемента');
            }
        }
        
        console.log('Поиск элемента market-building-base-resource-list:', !!baseResourceListEl);
        console.log('Данные income:', data.income);
        
        if (baseResourceListEl) {
            baseResourceListEl.innerHTML = '';
            if (data.income) {
                const resourceItems = [];
                
                // Монеты
                if (data.income.монеты && data.income.монеты > 0) {
                    resourceItems.push({ type: 'монеты', amount: data.income.монеты });
                }
                
                // Ресурсы
                if (data.income.ресурсы && Object.keys(data.income.ресурсы).length > 0) {
                    Object.entries(data.income.ресурсы).forEach(([resource, amount]) => {
                        if (amount > 0) {
                            resourceItems.push({ type: resource, amount: amount });
                        }
                    });
                }
                
                console.log('Ресурсы для отображения:', resourceItems);
                
                if (resourceItems.length > 0) {
                    resourceItems.forEach(item => {
                        const li = document.createElement('li');
                        li.className = 'market-building-cost-resource-item';
                        if (item.type === 'монеты') {
                            li.textContent = `${item.amount.toLocaleString('ru-RU')} монет`;
                        } else {
                            li.textContent = `${item.amount.toLocaleString('ru-RU')} ${item.type}`;
                        }
                        baseResourceListEl.appendChild(li);
                    });
                    console.log('Блок "Приносит ресурса" заполнен, элементов:', resourceItems.length);
                } else {
                    const li = document.createElement('li');
                    li.textContent = 'Нет данных';
                    baseResourceListEl.appendChild(li);
                    console.log('Блок "Приносит ресурса": нет ресурсов для отображения');
                }
            } else {
                const li = document.createElement('li');
                li.textContent = 'Нет данных';
                baseResourceListEl.appendChild(li);
                console.log('Блок "Приносит ресурса": data.income отсутствует');
            }
        } else {
            console.error('Элемент market-building-base-resource-list не найден!');
            // Попробуем найти через querySelector
            const screenEl = document.getElementById('market-building-screen');
            if (screenEl) {
                const foundEl = screenEl.querySelector('#market-building-base-resource-list');
                console.log('Поиск через querySelector:', !!foundEl);
            }
        }
        
        // Количество у игрока
        document.getElementById('market-building-player-count').textContent = data.player_count.toLocaleString('ru-RU');
        
        // Доход за раунд
        const incomeEl = document.getElementById('market-building-income');
        if (incomeEl) {
            incomeEl.innerHTML = '';
            if (data.income) {
                const incomeItems = [];
                
                // Монеты
                if (data.income.монеты && data.income.монеты > 0) {
                    incomeItems.push(`${data.income.монеты.toLocaleString('ru-RU')} монет`);
                }
                
                // Ресурсы
                if (data.income.ресурсы && Object.keys(data.income.ресурсы).length > 0) {
                    Object.entries(data.income.ресурсы).forEach(([resource, amount]) => {
                        if (amount > 0) {
                            incomeItems.push(`${amount.toLocaleString('ru-RU')} ${capitalizeFirst(resource)}`);
                        }
                    });
                }
                
                if (incomeItems.length > 0) {
                    incomeItems.forEach(item => {
                        const incomeItem = document.createElement('div');
                        incomeItem.className = 'market-building-income-value';
                        incomeItem.textContent = item;
                        incomeEl.appendChild(incomeItem);
                    });
                } else {
                    const noIncome = document.createElement('div');
                    noIncome.className = 'market-building-income-value';
                    noIncome.textContent = 'Нет дохода';
                    incomeEl.appendChild(noIncome);
                }
            }
        } else {
            console.error('Элемент market-building-income не найден');
        }
        
        // Список владельцев
        const ownersListEl = document.getElementById('market-building-owners-list');
        ownersListEl.innerHTML = '';
        if (data.owners && data.owners.length > 0) {
            data.owners.forEach(owner => {
                const ownerItem = document.createElement('div');
                ownerItem.className = 'market-building-owner-item';
                ownerItem.innerHTML = `
                    <span class="market-building-owner-name">${owner.name}</span>
                    <span class="market-building-owner-count">${owner.count}</span>
                `;
                ownersListEl.appendChild(ownerItem);
            });
        } else {
            ownersListEl.innerHTML = '<div class="market-building-no-owners">Нет владельцев</div>';
        }
    } catch (error) {
        console.error('Ошибка загрузки данных объекта:', error);
        showToast('Ошибка загрузки данных объекта', 'error');
    }
}

// Возврат назад со страницы объекта на бирже
function goBackFromMarketBuilding() {
    // Возвращаемся на биржу
    showScreen('market', { restoreScroll: true });
    currentBuilding = null;
}

// Показ экрана строительства объекта с биржи
async function showBuildBuildingScreenFromMarket() {
    if (!currentBuilding) return;
    
    // Сохраняем предыдущий экран
    previousScreen = 'market-building';
    
    try {
        // Загружаем данные объекта для получения стоимости
        const response = await fetch(addGameCodeToUrl(`/api/miniapp/market/building/${encodeURIComponent(currentBuilding)}`), {
            headers: {
                'X-Telegram-Init-Data': getTelegramInitDataHeader()
            }
        });
        
        if (!response.ok) {
            throw new Error('Ошибка загрузки данных объекта');
        }
        
        const data = await response.json();
        
        // Заполняем данные на странице
        document.getElementById('build-building-screen-name').textContent = `Построить ${currentBuilding}`;
        
        // Картинка
        const imagePath = miniappMarketBuildingWebImageUrl(currentBuilding);
        const imageEl = document.getElementById('build-building-image');
        imageEl.src = imagePath;
        imageEl.alt = currentBuilding;
        imageEl.onerror = function() {
            this.style.display = 'none';
        };
        imageEl.style.display = 'block';
        
        // Доступные ресурсы у игрока (построчно с буллитами)
        const availableResourcesEl = document.getElementById('build-building-available-resources');
        availableResourcesEl.innerHTML = '';
        if (data.cost_resources && Object.keys(data.cost_resources).length > 0) {
            Object.entries(data.cost_resources).forEach(([resource, requiredAmount]) => {
                const availableAmount = playerState?.resources?.[resource] || 0;
                const canBuild = availableAmount >= requiredAmount;
                const li = document.createElement('li');
                li.className = canBuild ? 'build-building-available-resource-item' : 'build-building-available-resource-item insufficient';
                li.textContent = `${availableAmount}/${requiredAmount} ${resource}`;
                availableResourcesEl.appendChild(li);
            });
        }
        
        // Сбрасываем количество на 1
        document.getElementById('build-building-quantity-input').value = 1;
        updateBuildTotal();
        
        // Показываем экран
        showScreen('build-building');
    } catch (error) {
        console.error('Ошибка загрузки данных объекта:', error);
        showToast('Ошибка загрузки данных объекта', 'error');
    }
}

// Показ экрана продажи объекта с биржи
async function showSellBuildingScreenFromMarket() {
    if (!currentBuilding) return;
    
    // Сохраняем предыдущий экран
    previousScreen = 'market-building';
    
    // Находим объекты этого типа у игрока
    const buildings = playerState?.buildings || [];
    const matchingBuildings = buildings.filter(b => b.name === currentBuilding && b.status !== 'for_sale');
    
    if (matchingBuildings.length === 0) {
        showToast('У вас нет таких объектов для продажи', 'error');
        return;
    }
    
    try {
        // Получаем цену продажи одного объекта
        // Используем стоимость из конфига для расчета
        const response = await fetch(addGameCodeToUrl(`/api/miniapp/market/building/${encodeURIComponent(currentBuilding)}`), {
            headers: {
                'X-Telegram-Init-Data': getTelegramInitDataHeader()
            }
        });
        
        if (!response.ok) {
            throw new Error('Ошибка загрузки данных объекта');
        }
        
        const data = await response.json();
        
        // Заполняем данные на странице
        document.getElementById('sell-building-screen-name').textContent = `Продать ${currentBuilding}`;
        
        // Картинка
        const imagePath = miniappMarketBuildingWebImageUrl(currentBuilding);
        const imageEl = document.getElementById('sell-building-image');
        imageEl.src = imagePath;
        imageEl.alt = currentBuilding;
        imageEl.onerror = function() {
            this.style.display = 'none';
        };
        imageEl.style.display = 'block';
        
        // Цена за объект (стоимость в монетах)
        document.getElementById('sell-building-price-value').textContent = data.cost_coins.toLocaleString('ru-RU');
        
        // Количество у игрока
        const availableCount = matchingBuildings.length;
        document.getElementById('sell-building-available-value').textContent = availableCount;
        document.getElementById('sell-building-quantity-input').max = availableCount;
        document.getElementById('sell-building-quantity-input').value = 1;
        
        // Обновляем итоговую сумму
        updateSellBuildingTotal();
        
        // Показываем экран
        showScreen('sell-building');
    } catch (error) {
        console.error('Ошибка загрузки данных объекта:', error);
        showToast('Ошибка загрузки данных объекта', 'error');
    }
}

// Купить все недостающие ресурсы для строительства
async function buyAllResourcesForBuilding() {
    if (!currentBuilding) return;
    
    const quantity = parseInt(document.getElementById('build-building-quantity-input').value) || 1;
    if (quantity < 1) return;
    
    try {
        const response = await fetch(addGameCodeToUrl(`/api/miniapp/market/building/${encodeURIComponent(currentBuilding)}`), {
            headers: { 'X-Telegram-Init-Data': getTelegramInitDataHeader() }
        });
        if (!response.ok) throw new Error('Ошибка загрузки данных');
        const data = await response.json();
        
        const costResources = data.cost_resources || {};
        if (Object.keys(costResources).length === 0) {
            showToast('Для этого объекта не нужны ресурсы', 'info');
            return;
        }
        
        const resourcesToBuy = [];
        for (const [resource, requiredAmount] of Object.entries(costResources)) {
            const needed = requiredAmount * quantity;
            const available = playerState?.resources?.[resource] || 0;
            const toBuy = Math.max(0, needed - available);
            if (toBuy > 0) resourcesToBuy.push({ resource, quantity: toBuy });
        }
        
        if (resourcesToBuy.length === 0) {
            showToast('У вас достаточно ресурсов', 'info');
            return;
        }
        
        showLoading(true);
        let bought = 0;
        for (const { resource, quantity: qty } of resourcesToBuy) {
            const res = await fetch(addGameCodeToUrl('/api/miniapp/player/buy-resource'), {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Telegram-Init-Data': getTelegramInitDataHeader()
                },
                body: JSON.stringify({ resource, quantity: qty })
            });
            const result = await res.json();
            if (result.success) bought++;
        }
        
        await loadPlayerState();
        
        // Обновляем список ресурсов на экране
        const availableResourcesEl = document.getElementById('build-building-available-resources');
        if (availableResourcesEl) {
            availableResourcesEl.innerHTML = '';
            Object.entries(costResources).forEach(([resource, requiredAmount]) => {
                const availableAmount = playerState?.resources?.[resource] || 0;
                const canBuild = availableAmount >= requiredAmount * quantity;
                const li = document.createElement('li');
                li.className = canBuild ? 'build-building-available-resource-item' : 'build-building-available-resource-item insufficient';
                li.textContent = `${availableAmount}/${requiredAmount * quantity} ${resource}`;
                availableResourcesEl.appendChild(li);
            });
        }
        
        showToast(bought > 0 ? `Куплено ресурсов: ${bought}` : 'Не удалось купить ресурсы', bought > 0 ? 'success' : 'error');
    } catch (error) {
        console.error('Ошибка покупки ресурсов:', error);
        showToast('Ошибка при покупке ресурсов', 'error');
    } finally {
        showLoading(false);
    }
}

// Управление количеством при строительстве
function increaseBuildQuantity() {
    const input = document.getElementById('build-building-quantity-input');
    const currentValue = parseInt(input.value) || 1;
    input.value = currentValue + 1;
    updateBuildTotal();
}

function decreaseBuildQuantity() {
    const input = document.getElementById('build-building-quantity-input');
    const currentValue = parseInt(input.value) || 1;
    if (currentValue > 1) {
        input.value = currentValue - 1;
        updateBuildTotal();
    }
}

function updateBuildTotal() {
    // Блок "Итого" удален, функция оставлена для совместимости
    // но больше не обновляет UI
}

// Подтверждение строительства объекта
async function confirmBuildBuilding() {
    if (!guardTradingAction()) return;
    if (!currentBuilding) return;
    
    const quantity = parseInt(document.getElementById('build-building-quantity-input').value) || 1;
    if (quantity < 1) {
        showToast('Количество должно быть больше 0', 'error');
        return;
    }
    
    showLoading(true);
    
    try {
        // Строим объекты по одному
        let successCount = 0;
        let failedCount = 0;
        
        let lastErrorData = null;
        
        for (let i = 0; i < quantity; i++) {
            const response = await fetch(addGameCodeToUrl('/api/miniapp/player/build'), {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Telegram-Init-Data': getTelegramInitDataHeader()
                },
                body: JSON.stringify({
                    building_name: currentBuilding
                })
            });
            
            const data = await response.json();
            if (data.success) {
                successCount++;
            } else {
                failedCount++;
                lastErrorData = data; // Сохраняем последнюю ошибку
            }
        }
        
        if (successCount > 0) {
            showToast(successCount === 1 ? 'Объект начал строиться!' : 'Объекты начали строиться!', 'success');
            
            // Обновляем данные
            await loadPlayerState();
        } else {
            // Проверяем, это ошибка недостаточности ресурсов?
            if (failedCount > 0 && lastErrorData) {
                const errorMessage = lastErrorData.message || '';
                
                // Проверяем, содержит ли сообщение информацию о недостаточности ресурсов
                if (errorMessage.toLowerCase().includes('недостаточно') || 
                    errorMessage.toLowerCase().includes('ресурс')) {
                    // Получаем детали стоимости объекта
                    try {
                        const buildingResponse = await fetch(addGameCodeToUrl('/api/miniapp/market/building/' + encodeURIComponent(currentBuilding)), {
                            headers: {
                                'X-Telegram-Init-Data': getTelegramInitDataHeader()
                            }
                        });
                        const buildingData = await buildingResponse.json();
                        
                        if (buildingData.cost_in_resources) {
                            const missingResources = [];
                            const playerResources = playerState?.resources || {};
                            
                            for (const [resource, needed] of Object.entries(buildingData.cost_in_resources)) {
                                const available = playerResources[resource] || 0;
                                if (available < needed) {
                                    missingResources.push({
                                        label: capitalizeFirst(resource),
                                        value: `Нужно: ${needed}, есть: ${available}, не хватает: ${needed - available}`
                                    });
                                }
                            }
                            
                            if (missingResources.length > 0) {
                                showToast('Недостаточно ресурсов', 'error');
                            } else {
                                showToast(errorMessage || 'Ошибка строительства объекта', 'error');
                            }
                        } else {
                            showToast(errorMessage || 'Ошибка строительства объекта', 'error');
                        }
                    } catch (error) {
                        showToast(errorMessage || 'Ошибка строительства объекта', 'error');
                    }
                } else {
                    showToast(errorMessage || 'Ошибка строительства объекта', 'error');
                }
            } else {
                showToast('Ошибка строительства объекта', 'error');
            }
        }
    } catch (error) {
        console.error('Ошибка строительства объекта:', error);
        showToast('Ошибка при строительстве объекта', 'error');
    } finally {
        showLoading(false);
    }
}

// Возврат назад со страницы строительства
function goBackFromBuildBuilding() {
    showScreen('market-building', { restoreScroll: true });
}

// Управление количеством при продаже объекта
function increaseSellBuildingQuantity() {
    const input = document.getElementById('sell-building-quantity-input');
    const currentValue = parseInt(input.value) || 1;
    const max = parseInt(input.max) || 0;
    if (currentValue < max) {
        input.value = currentValue + 1;
        updateSellBuildingTotal();
    }
}

function decreaseSellBuildingQuantity() {
    const input = document.getElementById('sell-building-quantity-input');
    const currentValue = parseInt(input.value) || 1;
    if (currentValue > 1) {
        input.value = currentValue - 1;
        updateSellBuildingTotal();
    }
}

function updateSellBuildingTotal() {
    if (!currentBuilding) return;
    
    const quantity = parseInt(document.getElementById('sell-building-quantity-input').value) || 1;
    const pricePerBuilding = parseInt(document.getElementById('sell-building-price-value').textContent.replace(/\s/g, '')) || 0;
    const total = pricePerBuilding * quantity;
    
    document.getElementById('sell-building-total-value').textContent = total.toLocaleString('ru-RU');
}

// Подтверждение продажи объекта
async function confirmSellBuilding() {
    if (!guardTradingAction()) return;
    if (!currentBuilding) return;
    
    const quantity = parseInt(document.getElementById('sell-building-quantity-input').value) || 1;
    if (quantity < 1) {
        showToast('Количество должно быть больше 0', 'error');
        return;
    }
    
    const max = parseInt(document.getElementById('sell-building-quantity-input').max) || 0;
    if (quantity > max) {
        showToast('Недостаточно объектов для продажи', 'error');
        return;
    }
    
    showLoading(true);
    
    try {
        // Находим объекты для продажи
        const buildings = playerState?.buildings || [];
        const matchingBuildings = buildings.filter(b => 
            b.name === currentBuilding && b.status !== 'for_sale'
        ).slice(0, quantity);
        
        if (matchingBuildings.length === 0) {
            showToast('Объекты не найдены', 'error');
            return;
        }
        
        // Продаем объекты
        const promises = matchingBuildings.map(building => 
            fetch(addGameCodeToUrl('/api/miniapp/player/sell-building'), {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Telegram-Init-Data': getTelegramInitDataHeader()
                },
                body: JSON.stringify({
                    building_id: building.id
                })
            })
        );
        
        const responses = await Promise.all(promises);
        const results = await Promise.all(responses.map(r => r.json()));
        
        const allSuccess = results.every(r => r.success);
        
        if (allSuccess) {
            // Суммируем цены продажи
            const totalPrice = results.reduce((sum, r) => sum + (r.sale_price || 0), 0);
            
            showToast('Объект выставлен на продажу!', 'success');
            
            // Обновляем данные
            await loadPlayerState();
        } else {
            const failed = results.filter(r => !r.success);
            showToast(`Ошибка при продаже ${failed.length} объектов`, 'error');
        }
    } catch (error) {
        console.error('Ошибка продажи объектов:', error);
        showToast('Ошибка при продаже объектов', 'error');
    } finally {
        showLoading(false);
    }
}

// Возврат назад со страницы продажи
function goBackFromSellBuilding() {
    showScreen('market-building', { restoreScroll: true });
}

// Показ экрана продажи объекта
function showSellBuildingScreen() {
    if (!currentBuilding) return;
    if (!currentBuildingStatus && !currentBuildingId) return;

    const buildings = playerState?.buildings || [];
    let matchingBuildings;
    if (currentBuildingId) {
        matchingBuildings = buildings.filter(
            b => String(b.id) === String(currentBuildingId)
        );
    } else {
        matchingBuildings = buildings.filter(
            b =>
                b.name === currentBuilding &&
                b.status === currentBuildingStatus
        );
    }

    if (matchingBuildings.length === 0) {
        showToast('Объект не найден', 'error');
        return;
    }

    sellBuildingsFromPortfolio(matchingBuildings);
}

// Продажа объектов из портфеля
async function sellBuildingsFromPortfolio(buildings) {
    if (buildings.length === 0) return;
    
    showLoading(true);
    
    try {
        // Продаем все объекты
        const promises = buildings.map(building => 
            fetch(addGameCodeToUrl('/api/miniapp/player/sell-building'), {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Telegram-Init-Data': getTelegramInitDataHeader()
                },
                body: JSON.stringify({
                    building_id: building.id
                })
            })
        );
        
        const responses = await Promise.all(promises);
        const results = await Promise.all(responses.map(r => r.json()));
        
        const allSuccess = results.every(r => r.success);
        
        if (allSuccess) {
            // Суммируем цены продажи
            const totalPrice = results.reduce((sum, r) => sum + (r.sale_price || 0), 0);
            const buildingName = buildings[0]?.name || currentBuilding || 'Объект';
            
            showToast('Объект выставлен на продажу!', 'success');
            
            // Обновляем данные
            await loadPlayerState();
        } else {
            const failed = results.filter(r => !r.success);
            showToast(`Ошибка при продаже ${failed.length} объектов`, 'error');
        }
    } catch (error) {
        console.error('Ошибка продажи объектов:', error);
        showToast('Ошибка при продаже объектов', 'error');
    } finally {
        showLoading(false);
    }
}

// Вспомогательные функции

function showLoading(show) {
    document.getElementById('loading-overlay').style.display = show ? 'flex' : 'none';
}

function showToast(message, type = 'success') {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.className = `toast ${type} show`;
    
    setTimeout(() => {
        toast.classList.remove('show');
    }, 3000);
}

function capitalizeFirst(str) {
    return str.charAt(0).toUpperCase() + str.slice(1);
}

// Экспорт функций для использования в HTML
window.submitGameCode = submitGameCode;
window.showGameCodeScreen = showGameCodeScreen;
window.showRulesScreen = showRulesScreen;
window.goBackFromRules = goBackFromRules;
window.selectCharacter = selectCharacter;

// Закрытие модальных окон при клике вне их
window.onclick = function(event) {
    const modals = ['buy-resource-modal', 'sell-resource-modal', 'build-modal', 'sell-building-modal'];
    modals.forEach(modalId => {
        const modal = document.getElementById(modalId);
        if (event.target === modal) {
            modal.style.display = 'none';
        }
    });
}

