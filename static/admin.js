// Админ-панель JavaScript

let currentGameId = null;
let currentGameCode = null;
let adminToken = null;
/** Выбранный сюжет по коду игры (синхронизируется с БД после «Применить»). */
const adminSelectedStoryByGame = {};

// ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

/**
 * Экранирование HTML для предотвращения XSS
 */
function escapeHtml(unsafe) {
    if (unsafe == null) return '';
    return String(unsafe)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

// ========== АВТОРИЗАЦИЯ ==========

document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const password = document.getElementById('admin-password').value;
    const errorDiv = document.getElementById('login-error');
    
    try {
        const response = await fetch('/api/admin/login', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ password })
        });
        
        const data = await response.json();
        
        if (response.ok && data.success) {
            adminToken = data.token;
            localStorage.setItem('admin_token', adminToken);
            
            showAdminPanel();
        } else {
            errorDiv.textContent = data.error || 'Неверный пароль';
            errorDiv.style.display = 'block';
        }
    } catch (error) {
        errorDiv.textContent = 'Ошибка подключения к серверу';
        errorDiv.style.display = 'block';
    }
});

// Проверка сохраненного токена при загрузке
window.addEventListener('DOMContentLoaded', () => {
    const savedToken = localStorage.getItem('admin_token');
    if (savedToken) {
        adminToken = savedToken;
        // Проверяем токен
        checkAdminAuth();
    }
});

async function checkAdminAuth() {
    try {
        const response = await fetch('/api/admin/check', {
            headers: {
                'Authorization': `Bearer ${adminToken}`
            }
        });
        
        if (response.ok) {
            showAdminPanel();
        } else {
            localStorage.removeItem('admin_token');
            adminToken = null;
        }
    } catch (error) {
        localStorage.removeItem('admin_token');
        adminToken = null;
    }
}

function showAdminPanel() {
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('admin-panel').style.display = 'block';
    loadActiveGames();
}

document.getElementById('logout-btn').addEventListener('click', () => {
    localStorage.removeItem('admin_token');
    adminToken = null;
    document.getElementById('login-screen').style.display = 'flex';
    document.getElementById('admin-panel').style.display = 'none';
});

// ========== НАВИГАЦИЯ ==========

document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', () => {
        const section = item.dataset.section;
        
        // Обновляем активное состояние
        document.querySelectorAll('.nav-item').forEach(nav => nav.classList.remove('active'));
        item.classList.add('active');
        
        // Показываем нужную секцию
        document.querySelectorAll('.admin-section').forEach(sec => sec.classList.remove('active'));
        document.getElementById(`section-${section}`).classList.add('active');
        
        // Загружаем данные для секции
        if (section === 'games') {
            loadActiveGames();
        } else if (section === 'archive') {
            loadArchiveGames();
        }
    });
});

// ========== УПРАВЛЕНИЕ ИГРАМИ ==========

async function loadActiveGames() {
    const gamesList = document.getElementById('games-list');
    gamesList.innerHTML = '<div class="loading">Загрузка...</div>';
    
    try {
        const response = await fetch('/api/admin/games/active', {
            headers: {
                'Authorization': `Bearer ${adminToken}`
            }
        });
        
        if (!response.ok) throw new Error('Ошибка загрузки игр');
        
        const data = await response.json();
        const games = data.games || [];
        
        if (games.length === 0) {
            gamesList.innerHTML = '<div class="loading">Нет активных игр</div>';
            return;
        }
        
        gamesList.innerHTML = games.map(game => {
            const safeGameCode = escapeHtml(game.game_code || '');
            const safeGameId = game.id || 0;
            return `
            <div class="game-card" onclick="openGameModal('${safeGameCode}', ${safeGameId})">
                <div class="game-card-header">
                    <span class="game-code">${safeGameCode}</span>
                    <span class="game-status active">Активна</span>
                </div>
                <div class="game-info">
                    <div class="game-info-item">
                        <span class="game-info-label">Раунд:</span>
                        <span class="game-info-value">${game.current_round || 1}</span>
                    </div>
                    <div class="game-info-item">
                        <span class="game-info-label">Игроков:</span>
                        <span class="game-info-value">${game.num_players || 0}</span>
                    </div>
                    <div class="game-info-item">
                        <span class="game-info-label">Создана:</span>
                        <span class="game-info-value">${game.created_at ? new Date(game.created_at).toLocaleDateString('ru-RU') : '-'}</span>
                    </div>
                </div>
            </div>
        `;
        }).join('');
    } catch (error) {
        console.error('Ошибка загрузки активных игр:', error);
        gamesList.innerHTML = `<div class="error-message">Ошибка загрузки: ${error.message || 'Неизвестная ошибка'}</div>`;
    }
}

async function loadArchiveGames() {
    const archiveList = document.getElementById('archive-list');
    archiveList.innerHTML = '<div class="loading">Загрузка...</div>';
    
    try {
        const response = await fetch('/api/admin/games/archived', {
            headers: {
                'Authorization': `Bearer ${adminToken}`
            }
        });
        
        if (!response.ok) throw new Error('Ошибка загрузки архива');
        
        const data = await response.json();
        const games = data.games || [];
        
        if (games.length === 0) {
            archiveList.innerHTML = '<div class="loading">Архив пуст</div>';
            return;
        }
        
        archiveList.innerHTML = games.map(game => {
            const safeGameCode = escapeHtml(game.game_code || '');
            const safeGameId = game.id || 0;
            const archiveDate = game.archived_at || game.updated_at;
            return `
            <div class="game-card" onclick="openGameModal('${safeGameCode}', ${safeGameId})">
                <div class="game-card-header">
                    <span class="game-code">${safeGameCode}</span>
                    <span class="game-status archived">Архив</span>
                </div>
                <div class="game-info">
                    <div class="game-info-item">
                        <span class="game-info-label">Раунд:</span>
                        <span class="game-info-value">${game.current_round || 1}</span>
                    </div>
                    <div class="game-info-item">
                        <span class="game-info-label">Игроков:</span>
                        <span class="game-info-value">${game.num_players || 0}</span>
                    </div>
                    <div class="game-info-item">
                        <span class="game-info-label">Завершена:</span>
                        <span class="game-info-value">${archiveDate ? new Date(archiveDate).toLocaleDateString('ru-RU') : '-'}</span>
                    </div>
                </div>
            </div>
        `;
        }).join('');
    } catch (error) {
        console.error('Ошибка загрузки архива игр:', error);
        archiveList.innerHTML = `<div class="error-message">Ошибка загрузки: ${error.message || 'Неизвестная ошибка'}</div>`;
    }
}

// ========== СОЗДАНИЕ ИГРЫ ==========

document.getElementById('create-game-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const numPlayersInput = document.getElementById('game-num-players');
    const numPlayers = parseInt(numPlayersInput.value);
    const companyName = document.getElementById('game-company-name').value || null;
    const errorDiv = document.getElementById('create-game-error');
    const successDiv = document.getElementById('create-game-success');
    
    errorDiv.style.display = 'none';
    successDiv.style.display = 'none';
    
    // Валидация на клиенте
    if (isNaN(numPlayers) || numPlayers < 5 || numPlayers > 30) {
        errorDiv.textContent = 'Количество игроков должно быть от 5 до 30';
        errorDiv.style.display = 'block';
        numPlayersInput.focus();
        return;
    }
    
    try {
        const response = await fetch('/api/admin/games/create', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${adminToken}`
            },
            body: JSON.stringify({
                num_players: numPlayers,
                company_name: companyName
            })
        });
        
        const data = await response.json();
        
        if (response.ok) {
            successDiv.textContent = `Игра создана! Код: ${data.game_code}`;
            successDiv.style.display = 'block';
            document.getElementById('create-game-form').reset();
            document.getElementById('game-num-players').value = 10;
            loadActiveGames();
        } else {
            errorDiv.textContent = data.error || 'Ошибка создания игры';
            errorDiv.style.display = 'block';
        }
    } catch (error) {
        errorDiv.textContent = `Ошибка подключения к серверу: ${error.message}`;
        errorDiv.style.display = 'block';
        console.error('Ошибка создания игры:', error);
    }
});

// ========== МОДАЛЬНОЕ ОКНО ИГРЫ ==========

function openGameModal(gameCode, gameId) {
    currentGameCode = gameCode;
    currentGameId = gameId;
    
    document.getElementById('game-modal').style.display = 'flex';
    
    // Активируем первую вкладку
    document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
    document.querySelector('.tab-btn[data-tab="info"]').classList.add('active');
    document.getElementById('tab-info').classList.add('active');

    loadGameInfo().then(() => {
        loadGamePlayers();
        loadRoundContent();
        loadRoundEvents();
    });
}

function closeGameModal() {
    document.getElementById('game-modal').style.display = 'none';
    currentGameId = null;
    currentGameCode = null;
}

// Вкладки в модальном окне
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        
        btn.classList.add('active');
        document.getElementById(`tab-${tab}`).classList.add('active');
        
        // Загружаем данные для вкладки
        if (tab === 'players') {
            loadGamePlayers();
        } else if (tab === 'content') {
            loadRoundContent();
        } else if (tab === 'events') {
            loadRoundEvents();
        } else if (tab === 'web-events') {
            loadWebEventsAdmin();
        } else if (tab === 'rounds') {
            loadRoundSettings();
        }
    });
});

async function loadGameInfo() {
    if (!currentGameCode) {
        console.error('currentGameCode не установлен');
        return;
    }
    
    const modalGameCode = document.getElementById('modal-game-code');
    const modalGameId = document.getElementById('modal-game-id');
    const modalGameRound = document.getElementById('modal-game-round');
    const modalGamePlayersCount = document.getElementById('modal-game-players-count');
    const modalGameStatus = document.getElementById('modal-game-status');
    const archiveBtn = document.getElementById('archive-game-btn');
    
    if (!modalGameCode || !modalGameId || !modalGameRound || !modalGamePlayersCount || !modalGameStatus) {
        console.error('Не найдены элементы DOM для отображения информации об игре');
        return;
    }
    
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}`, {
            headers: {
                'Authorization': `Bearer ${adminToken}`
            }
        });
        
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.error || `Ошибка загрузки информации (${response.status})`);
        }
        
        const data = await response.json();
        // Обрабатываем оба формата: {success: true, game: {...}} или прямой объект
        const game = data.game || data;

        if (currentGameCode) {
            adminSelectedStoryByGame[currentGameCode] = game.story_id || '';
        }
        
        modalGameCode.textContent = game.game_code || '-';
        modalGameId.textContent = game.game_id || game.id || '-';
        modalGameRound.textContent = game.current_round || 1;
        modalGamePlayersCount.textContent = game.num_players || 0;
        modalGameStatus.textContent = game.status === 'archived' ? 'Архив' : 'Активна';
        
        // Показываем/скрываем кнопку архивирования
        if (archiveBtn) {
            if (game.status === 'archived') {
                archiveBtn.style.display = 'none';
            } else {
                archiveBtn.style.display = 'block';
            }
        }
    } catch (error) {
        console.error('Ошибка загрузки информации об игре:', error);
        if (modalGameCode) {
            modalGameCode.textContent = 'Ошибка';
        }
        if (modalGameStatus) {
            modalGameStatus.textContent = 'Ошибка загрузки';
            modalGameStatus.style.color = '#d32f2f';
        }
    }
}

/** Загружает список персонажей игры (каталог для экрана выбора). Не путать с участниками игры (players). */
async function loadGamePlayers() {
    if (!currentGameCode) {
        console.error('currentGameCode не установлен');
        return;
    }
    
    const playersList = document.getElementById('players-list');
    playersList.innerHTML = '<div class="loading">Загрузка...</div>';
    
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/characters`, {
            headers: {
                'Authorization': `Bearer ${adminToken}`
            }
        });
        
        if (!response.ok) throw new Error('Ошибка загрузки персонажей');
        
        const data = await response.json();
        const characters = data.characters || [];
        
        if (characters.length === 0) {
            playersList.innerHTML = '<div class="loading">Нет персонажей. Добавьте персонажей — они появятся на экране выбора в игре.</div>';
            return;
        }
        
        playersList.innerHTML = characters.map(char => {
            const name = char.name || char.character_name || 'Без имени';
            const safeName = escapeHtml(name);
            const safeNameForOnclick = safeName.replace(/'/g, "\\'").replace(/"/g, '&quot;');
            const avatarUrl = (char.image || char.character_image || '').trim() || '/static/images/logo.png';
            const safeAvatarUrl = escapeHtml(avatarUrl);
            
            return `
            <div class="player-card" onclick="editCharacter('${safeNameForOnclick}')" data-character-name="${safeName}">
                <img src="${safeAvatarUrl}" 
                     alt="${safeName}" 
                     class="player-avatar"
                     onerror="this.onerror=null; this.src='/static/images/logo.png'">
                <div class="player-info">
                    <div class="player-name">${safeName}</div>
                    <div class="player-id">Персонаж для выбора</div>
                </div>
                <button class="btn btn-danger btn-sm" onclick="event.stopPropagation(); deleteCharacter('${safeNameForOnclick}')" title="Удалить персонажа">Удалить</button>
            </div>
        `;
        }).join('');
    } catch (error) {
        console.error('Ошибка загрузки персонажей:', error);
        playersList.innerHTML = `<div class="error-message">Ошибка загрузки персонажей: ${error.message || 'Неизвестная ошибка'}</div>`;
    }
}

async function loadRoundContent() {
    if (!currentGameCode) {
        console.error('currentGameCode не установлен');
        return;
    }
    
    const contentList = document.getElementById('round-content-list');
    contentList.innerHTML = '<div class="loading">Загрузка...</div>';
    
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/round-content`, {
            headers: {
                'Authorization': `Bearer ${adminToken}`
            }
        });
        
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.error || `Ошибка загрузки контента (${response.status})`);
        }
        
        const data = await response.json();
        const rounds = data.content || data.rounds || [];
        
        // Создаем карту существующих раундов (включая интро — round_number 0)
        const roundsMap = {};
        rounds.forEach(round => {
            roundsMap[round.round_number] = round;
        });
        
        const introRound = roundsMap[0];
        const introUrl = introRound ? (introRound.content_url || '').replace(/"/g, '&quot;').replace(/'/g, '&#39;') : '';
        const introBlock = `
            <div class="round-content-item">
                <div class="round-content-header">
                    <span class="round-number">Интро</span>
                </div>
                <form class="round-content-form" onsubmit="saveRoundContent(event, 0)">
                    <div class="form-group">
                        <label>URL видео или ссылка на контент:</label>
                        <input type="url" class="form-input" 
                               id="round-0-content" 
                               value="${introUrl}"
                               placeholder="https://example.com/intro.mp4 или ссылка на YouTube/RuTube">
                    </div>
                    <button type="submit" class="btn btn-primary">Сохранить</button>
                </form>
            </div>
        `;
        
        const roundsBlocks = Array.from({length: 10}, (_, i) => {
            const roundNum = i + 1;
            const round = roundsMap[roundNum];
            const contentUrl = round ? (round.content_url || '').replace(/"/g, '&quot;').replace(/'/g, '&#39;') : '';
            return `
                <div class="round-content-item">
                    <div class="round-content-header">
                        <span class="round-number">Раунд ${roundNum}</span>
                    </div>
                    <form class="round-content-form" onsubmit="saveRoundContent(event, ${roundNum})">
                        <div class="form-group">
                            <label>URL видео или ссылка на контент:</label>
                            <input type="url" class="form-input" 
                                   id="round-${roundNum}-content" 
                                   value="${contentUrl}"
                                   placeholder="https://example.com/video.mp4 или /static/videos/round-${roundNum}.mp4">
                        </div>
                        <button type="submit" class="btn btn-primary">Сохранить</button>
                    </form>
                </div>
            `;
        }).join('');
        
        contentList.innerHTML = introBlock + roundsBlocks;
    } catch (error) {
        console.error('Ошибка загрузки контента раундов:', error);
        contentList.innerHTML = `<div class="error-message">Ошибка загрузки контента: ${error.message || 'Неизвестная ошибка'}</div>`;
    }
}

async function loadRoundEvents() {
    if (!currentGameCode) {
        console.error('currentGameCode не установлен');
        return;
    }
    const listEl = document.getElementById('round-events-list');
    if (!listEl) return;
    listEl.innerHTML = '<div class="loading">Загрузка...</div>';
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/round-events`, {
            headers: { 'Authorization': `Bearer ${adminToken}` },
        });
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.error || `Ошибка загрузки (${response.status})`);
        }
        const data = await response.json();
        const rows = data.events || [];
        const map = {};
        rows.forEach((row) => {
            map[row.round_number] = row;
        });

        const storyPickerHtml = buildAdminStoryPickerHtml({
            selectId: 'admin-events-story-select',
            applyButtonId: 'admin-events-story-apply',
            selectedStoryId: getAdminSelectedStoryId(),
            hint: ADMIN_STORY_APPLY_HINT,
        });

        const roundsHtml = Array.from({ length: 10 }, (_, i) => {
            const roundNum = i + 1;
            const row = map[roundNum] || {};
            const rawText = row.event_text != null ? String(row.event_text) : '';
            const safeText = escapeHtml(rawText);
            const rawUrl = row.image_url != null ? String(row.image_url) : '';
            const safeUrlAttr = rawUrl.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
            const previewSrc = rawUrl ? escapeHtml(rawUrl.trim()) : '';
            const previewHtml = previewSrc
                ? `<div class="round-event-preview" id="event-r-${roundNum}-preview"><img src="${previewSrc}" alt="" class="round-event-preview-img" onerror="this.parentNode.innerHTML=''"></div>`
                : `<div class="round-event-preview" id="event-r-${roundNum}-preview"></div>`;
            return `
                <details class="admin-collapsible round-settings-collapsible">
                    <summary>Раунд ${roundNum}</summary>
                    <form class="round-content-form round-settings-form" onsubmit="saveRoundEvent(event, ${roundNum})">
                    <div class="form-group">
                        <label>Текст события:</label>
                        <textarea class="form-input round-event-textarea" id="event-r-${roundNum}-text" rows="4" placeholder="Описание события для этого раунда">${safeText}</textarea>
                    </div>
                    <div class="form-group">
                        <label>Картинка (URL):</label>
                        <input type="url" class="form-input" id="event-r-${roundNum}-image" value="${safeUrlAttr}"
                               placeholder="https://... или загрузите файл ниже"
                               oninput="updateRoundEventPreview(${roundNum})">
                    </div>
                    <div class="form-group round-event-upload-row">
                        <input type="file" id="event-r-${roundNum}-file" accept="image/*" style="display:none"
                               onchange="onRoundEventImageChosen(${roundNum}, this)">
                        <button type="button" class="btn btn-secondary" onclick="document.getElementById('event-r-${roundNum}-file').click()">Загрузить картинку</button>
                        <span class="round-event-upload-status" id="event-r-${roundNum}-upload-status"></span>
                    </div>
                    ${previewHtml}
                    <button type="submit" class="btn btn-primary">Сохранить</button>
                    </form>
                </details>`;
        }).join('');

        listEl.innerHTML = storyPickerHtml + roundsHtml;
        bindAdminStoryPicker('events');
    } catch (error) {
        console.error('Ошибка загрузки событий раундов:', error);
        listEl.innerHTML = `<div class="error-message">Ошибка загрузки: ${error.message || 'Неизвестная ошибка'}</div>`;
    }
}

function updateRoundEventPreview(roundNum) {
    const imgInput = document.getElementById(`event-r-${roundNum}-image`);
    const box = document.getElementById(`event-r-${roundNum}-preview`);
    if (!box || !imgInput) return;
    const url = (imgInput.value || '').trim();
    if (!url) {
        box.innerHTML = '';
        return;
    }
    const safe = escapeHtml(url);
    box.innerHTML = `<img src="${safe}" alt="" class="round-event-preview-img" onerror="this.style.display='none'">`;
}

async function onRoundEventImageChosen(roundNum, fileInput) {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    const status = document.getElementById(`event-r-${roundNum}-upload-status`);
    if (status) status.textContent = 'Загрузка...';
    try {
        const url = await uploadImageToServer(file);
        const imgField = document.getElementById(`event-r-${roundNum}-image`);
        if (imgField) imgField.value = url;
        if (status) status.textContent = 'Готово';
        updateRoundEventPreview(roundNum);
    } catch (error) {
        console.error('Ошибка загрузки картинки события:', error);
        if (status) status.textContent = '';
        alert(error.message || 'Ошибка загрузки изображения');
    }
    fileInput.value = '';
}

async function saveRoundEvent(event, roundNumber) {
    event.preventDefault();
    const textEl = document.getElementById(`event-r-${roundNumber}-text`);
    const imgEl = document.getElementById(`event-r-${roundNumber}-image`);
    const event_text = textEl ? textEl.value : '';
    const image_url = imgEl ? (imgEl.value || '').trim() : '';
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/round-events/${roundNumber}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${adminToken}`,
            },
            body: JSON.stringify({ event_text, image_url }),
        });
        if (response.ok) {
            alert('Событие сохранено!');
        } else {
            const errorData = await response.json().catch(() => ({}));
            alert(errorData.error || 'Ошибка сохранения');
        }
    } catch (error) {
        console.error('Ошибка сохранения события:', error);
        alert(`Ошибка подключения: ${error.message || 'Неизвестная ошибка'}`);
    }
}

const ADMIN_ALL_RESOURCES = ['камень', 'дерево', 'железо', 'скот', 'овощи', 'рабы', 'золото', 'зерно', 'рыба'];
const ADMIN_ALL_BUILDINGS = [
    'Лесоповал', 'Каменоломня', 'Теплицы', 'Трактир', 'Посевные поля', 'Рыболовня',
    'Кузнечная', 'Ферма', 'Постоялый двор', 'Куртизанские палатки', 'Золотой рудник',
];

/** Список сюжетов в админке (данные — static/stories/{id}.json). */
const ADMIN_AVAILABLE_STORIES = [
    { id: '', label: '— не выбран —' },
    { id: 'story-1', label: 'Сюжет 1' },
    { id: 'story-2', label: 'Сюжет 2 (черновик)' },
    { id: 'story-3', label: 'Сюжет 3 (черновик)' },
];

const ADMIN_STORY_APPLY_HINT = 'Применяет сюжет ко всей игре (раунды, события, подсветка ресурсов/объектов) и сохраняет в БД.';

function getAdminSelectedStoryId() {
    if (!currentGameCode) return '';
    return adminSelectedStoryByGame[currentGameCode] || '';
}

function setAdminSelectedStoryId(storyId) {
    if (!currentGameCode) return;
    adminSelectedStoryByGame[currentGameCode] = storyId || '';
    syncAdminStorySelects(storyId);
}

function reloadActiveAdminTab() {
    const activeBtn = document.querySelector('.tab-btn.active');
    const tab = activeBtn ? activeBtn.dataset.tab : null;
    if (tab === 'rounds') loadRoundSettings();
    else if (tab === 'events') loadRoundEvents();
    else if (tab === 'web-events') loadWebEventsAdmin();
}

function syncAdminStorySelects(storyId) {
    ['admin-story-select', 'admin-events-story-select', 'admin-web-events-story-select'].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.value = storyId || '';
    });
}

async function applyAdminStory(scope) {
    const selectId = scope === 'events'
        ? 'admin-events-story-select'
        : scope === 'web-events'
            ? 'admin-web-events-story-select'
            : 'admin-story-select';
    const selectEl = document.getElementById(selectId);
    if (!selectEl || !currentGameCode) return;
    const storyId = (selectEl.value || '').trim();
    if (!storyId) {
        alert('Выберите сюжет из списка');
        return;
    }
    if (!confirm('Применить сюжет ко всей игре?\n\nБудут обновлены и сохранены в БД:\n• настройки раундов (коэффициенты и комментарии)\n• тексты событий (mini app и ВЕБ)\n• подсветка ресурсов и объектов на экране ведущего\n\nКартинки и фон в событиях сохранятся.')) {
        return;
    }
    const applyBtn = document.getElementById(
        scope === 'events' ? 'admin-events-story-apply'
            : scope === 'web-events' ? 'admin-web-events-story-apply'
                : 'admin-story-apply'
    );
    const origText = applyBtn ? applyBtn.textContent : '';
    if (applyBtn) {
        applyBtn.disabled = true;
        applyBtn.textContent = 'Применение...';
    }
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/apply-story`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${adminToken}`,
            },
            body: JSON.stringify({ story_id: storyId }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.error || `Ошибка (${response.status})`);
        }
        setAdminSelectedStoryId(data.story_id || storyId);
        const stats = data.stats || {};
        alert(
            `Сюжет «${data.story_title || storyId}» применён и сохранён.\n\n` +
            `Раундов: ${stats.rounds_saved ?? 0}, событий: ${stats.events_saved ?? 0}, событий ВЕБ: ${stats.web_saved ?? 0}`
        );
        reloadActiveAdminTab();
    } catch (error) {
        console.error('applyAdminStory:', error);
        alert(error.message || 'Не удалось применить сюжет');
    } finally {
        if (applyBtn) {
            applyBtn.disabled = false;
            applyBtn.textContent = origText;
        }
    }
}

function bindAdminStoryPicker(scope) {
    const selectId = scope === 'events'
        ? 'admin-events-story-select'
        : scope === 'web-events'
            ? 'admin-web-events-story-select'
            : 'admin-story-select';
    const applyId = scope === 'events'
        ? 'admin-events-story-apply'
        : scope === 'web-events'
            ? 'admin-web-events-story-apply'
            : 'admin-story-apply';
    const selectEl = document.getElementById(selectId);
    const applyEl = document.getElementById(applyId);
    if (applyEl && !applyEl.dataset.bound) {
        applyEl.dataset.bound = '1';
        applyEl.addEventListener('click', () => applyAdminStory(scope));
    }
    if (selectEl && !selectEl.dataset.bound) {
        selectEl.dataset.bound = '1';
        selectEl.addEventListener('change', () => setAdminSelectedStoryId(selectEl.value || ''));
    }
}

function buildAdminStoryPickerHtml({
    selectId = 'admin-story-select',
    applyButtonId = '',
    selectedStoryId = '',
    hint = 'Выберите сюжет и нажмите «Применить сюжет».',
} = {}) {
    const selected = selectedStoryId || '';
    const options = ADMIN_AVAILABLE_STORIES.map((story) => {
        const sel = story.id === selected ? ' selected' : '';
        return `<option value="${escapeHtml(story.id)}"${sel}>${escapeHtml(story.label)}</option>`;
    }).join('');
    const applyBtn = applyButtonId
        ? `<button type="button" id="${applyButtonId}" class="btn btn-secondary admin-story-apply-btn">Применить сюжет</button>`
        : '';
    return `
        <details class="admin-collapsible rounds-story-picker" open>
            <summary>Выбрать сюжет</summary>
            <div class="rounds-story-picker-body">
                <div class="form-group">
                    <label for="${selectId}">Сюжет игры</label>
                    <select id="${selectId}" class="form-input admin-story-select">
                        ${options}
                    </select>
                    <p class="admin-field-hint">${escapeHtml(hint)}</p>
                    ${applyBtn}
                </div>
            </div>
        </details>`;
}

function buildRoundSettingsStoryPickerHtml(selectedStoryId) {
    return buildAdminStoryPickerHtml({
        selectId: 'admin-story-select',
        applyButtonId: 'admin-story-apply',
        selectedStoryId: selectedStoryId != null ? selectedStoryId : getAdminSelectedStoryId(),
        hint: ADMIN_STORY_APPLY_HINT,
    });
}

function buildRoundModifierFieldHtml(roundNum, kind, itemName, coefValue, textValue) {
    const safeName = escapeHtml(itemName);
    const kindPrefix = kind === 'resource' ? 'resource' : 'building';
    const coefId = `round-${roundNum}-${kindPrefix}-${itemName}`;
    const textId = `${coefId}-text`;
    const safeCoef = coefValue != null && coefValue !== '' ? escapeHtml(String(coefValue)) : '1.0';
    const safeText = escapeHtml(textValue != null ? String(textValue) : '');
    return `
        <div class="round-modifier-field">
            <div class="round-modifier-field-title">${safeName}</div>
            <div class="form-group round-modifier-coef">
                <label for="${coefId}">Коэффициент</label>
                <input type="number" step="0.01" class="form-input"
                       id="${coefId}"
                       value="${safeCoef}"
                       placeholder="1.0">
            </div>
            <div class="form-group round-modifier-text">
                <label for="${textId}">Текст</label>
                <input type="text" class="form-input round-modifier-text-input"
                       id="${textId}"
                       value="${safeText}"
                       placeholder="Комментарий к коэффициенту">
            </div>
        </div>`;
}

function buildWebEventCheckboxGrid(roundNum, fieldType, allItems, checkedItems) {
    const checkedSet = new Set(checkedItems || []);
    return allItems.map((item, index) => {
        const safe = escapeHtml(item);
        const isChecked = checkedSet.has(item) ? 'checked' : '';
        const inputId = `web-r-${roundNum}-${fieldType}-${index}`;
        return `
            <label class="admin-checkbox-label" for="${inputId}">
                <input type="checkbox" id="${inputId}" class="web-event-checkbox"
                       data-round="${roundNum}" data-kind="${fieldType}"
                       name="web-r-${roundNum}-${fieldType}" value="${safe}" ${isChecked}>
                <span>${safe}</span>
            </label>`;
    }).join('');
}

function getWebEventCheckedItems(roundNum, fieldType) {
    const inputs = document.querySelectorAll(`input[name="web-r-${roundNum}-${fieldType}"]:checked`);
    return Array.from(inputs).map((el) => el.value);
}

async function loadWebEventsAdmin() {
    if (!currentGameCode) {
        console.error('currentGameCode не установлен');
        return;
    }
    const listEl = document.getElementById('web-events-list');
    if (!listEl) return;
    listEl.innerHTML = '<div class="loading">Загрузка...</div>';
    let map = {};
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/web-events`, {
            headers: { 'Authorization': `Bearer ${adminToken}` },
        });
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.error || `Ошибка загрузки (${response.status})`);
        }
        const data = await response.json();
        (data.events || []).forEach((row) => {
            map[row.round_number] = row;
        });
    } catch (error) {
        console.error('Ошибка загрузки событий ВЕБ:', error);
        listEl.innerHTML = `<div class="error-message">Ошибка загрузки: ${error.message || 'Неизвестная ошибка'}</div>`;
        return;
    }
    const storyPickerHtml = buildAdminStoryPickerHtml({
        selectId: 'admin-web-events-story-select',
        applyButtonId: 'admin-web-events-story-apply',
        selectedStoryId: getAdminSelectedStoryId(),
        hint: ADMIN_STORY_APPLY_HINT,
    });

    const roundsHtml = Array.from({ length: 10 }, (_, i) => {
        const roundNum = i + 1;
        const row = map[roundNum] || {};
        const rawText = row.event_text != null ? String(row.event_text) : '';
        const safeText = escapeHtml(rawText);
        const rawUrl = row.image_url != null ? String(row.image_url) : '';
        const safeUrlAttr = rawUrl.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        const rawBg = row.bg_image_url != null ? String(row.bg_image_url) : '';
        const safeBgAttr = rawBg.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        const previewSrc = rawUrl ? escapeHtml(rawUrl.trim()) : '';
        const previewHtml = previewSrc
            ? `<div class="round-event-preview" id="web-r-${roundNum}-preview"><img src="${previewSrc}" alt="" class="round-event-preview-img" onerror="this.parentNode.innerHTML=''"></div>`
            : `<div class="round-event-preview" id="web-r-${roundNum}-preview"></div>`;
        const bgPreviewSrc = rawBg ? escapeHtml(rawBg.trim()) : '';
        const bgPreviewHtml = bgPreviewSrc
            ? `<div class="round-event-preview web-event-bg-preview" id="web-r-${roundNum}-bg-preview"><img src="${bgPreviewSrc}" alt="" class="web-event-bg-preview-img" onerror="this.parentNode.innerHTML=''"></div>`
            : `<div class="round-event-preview web-event-bg-preview" id="web-r-${roundNum}-bg-preview"></div>`;
        const resourcesGrid = buildWebEventCheckboxGrid(roundNum, 'resource', ADMIN_ALL_RESOURCES, row.highlight_resources || []);
        const buildingsGrid = buildWebEventCheckboxGrid(roundNum, 'building', ADMIN_ALL_BUILDINGS, row.highlight_buildings || []);
        return `
            <details class="admin-collapsible round-settings-collapsible web-events-round-item">
                <summary>Раунд ${roundNum}</summary>
                <form class="round-content-form web-events-form round-settings-form" onsubmit="saveWebRoundEvent(event, ${roundNum})">
                    <div class="form-group">
                        <label>Текст:</label>
                        <textarea class="form-input round-event-textarea" id="web-r-${roundNum}-text" rows="4"
                                  placeholder="Текст события для экрана ведущего">${safeText}</textarea>
                    </div>
                    <div class="form-group">
                        <label>Картинка (URL):</label>
                        <input type="url" class="form-input" id="web-r-${roundNum}-image" value="${safeUrlAttr}"
                               placeholder="https://... или загрузите файл ниже"
                               oninput="updateWebEventPreview(${roundNum})">
                    </div>
                    <div class="form-group round-event-upload-row">
                        <input type="file" id="web-r-${roundNum}-file" accept="image/*" style="display:none"
                               onchange="onWebEventImageChosen(${roundNum}, this)">
                        <button type="button" class="btn btn-secondary"
                                onclick="document.getElementById('web-r-${roundNum}-file').click()">Загрузить картинку</button>
                        <span class="round-event-upload-status" id="web-r-${roundNum}-upload-status"></span>
                    </div>
                    ${previewHtml}

                    <div class="form-group">
                        <label>Картинка фона (URL):</label>
                        <input type="url" class="form-input" id="web-r-${roundNum}-bg" value="${safeBgAttr}"
                               placeholder="https://... или загрузите файл ниже"
                               oninput="updateWebEventBgPreview(${roundNum})">
                    </div>
                    <div class="form-group round-event-upload-row">
                        <input type="file" id="web-r-${roundNum}-bg-file" accept="image/*" style="display:none"
                               onchange="onWebEventBgImageChosen(${roundNum}, this)">
                        <button type="button" class="btn btn-secondary"
                                onclick="document.getElementById('web-r-${roundNum}-bg-file').click()">Загрузить фон</button>
                        <span class="round-event-upload-status" id="web-r-${roundNum}-bg-upload-status"></span>
                    </div>
                    ${bgPreviewHtml}

                    <details class="admin-collapsible">
                        <summary>Ресурсы</summary>
                        <div class="admin-checkbox-grid">${resourcesGrid}</div>
                    </details>

                    <details class="admin-collapsible">
                        <summary>Объекты</summary>
                        <div class="admin-checkbox-grid">${buildingsGrid}</div>
                    </details>

                    <button type="submit" class="btn btn-primary btn-save-web-event">Сохранить</button>
                </form>
            </details>`;
    }).join('');

    listEl.innerHTML = storyPickerHtml + roundsHtml;
    bindAdminStoryPicker('web-events');
}

function updateWebEventPreview(roundNum) {
    const imgInput = document.getElementById(`web-r-${roundNum}-image`);
    const box = document.getElementById(`web-r-${roundNum}-preview`);
    if (!box || !imgInput) return;
    const url = (imgInput.value || '').trim();
    if (!url) {
        box.innerHTML = '';
        return;
    }
    const safe = escapeHtml(url);
    box.innerHTML = `<img src="${safe}" alt="" class="round-event-preview-img" onerror="this.style.display='none'">`;
}

function updateWebEventBgPreview(roundNum) {
    const imgInput = document.getElementById(`web-r-${roundNum}-bg`);
    const box = document.getElementById(`web-r-${roundNum}-bg-preview`);
    if (!box || !imgInput) return;
    const url = (imgInput.value || '').trim();
    if (!url) {
        box.innerHTML = '';
        return;
    }
    const safe = escapeHtml(url);
    box.innerHTML = `<img src="${safe}" alt="" class="web-event-bg-preview-img" onerror="this.style.display='none'">`;
}

async function onWebEventImageChosen(roundNum, fileInput) {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    const status = document.getElementById(`web-r-${roundNum}-upload-status`);
    if (status) status.textContent = 'Загрузка...';
    try {
        const url = await uploadImageToServer(file);
        const imgField = document.getElementById(`web-r-${roundNum}-image`);
        if (imgField) imgField.value = url;
        if (status) status.textContent = 'Готово';
        updateWebEventPreview(roundNum);
    } catch (error) {
        console.error('Ошибка загрузки картинки (События ВЕБ):', error);
        if (status) status.textContent = '';
        alert(error.message || 'Ошибка загрузки изображения');
    }
    fileInput.value = '';
}

async function onWebEventBgImageChosen(roundNum, fileInput) {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    const status = document.getElementById(`web-r-${roundNum}-bg-upload-status`);
    if (status) status.textContent = 'Загрузка...';
    try {
        const url = await uploadImageToServer(file);
        const imgField = document.getElementById(`web-r-${roundNum}-bg`);
        if (imgField) imgField.value = url;
        if (status) status.textContent = 'Готово';
        updateWebEventBgPreview(roundNum);
    } catch (error) {
        console.error('Ошибка загрузки фона (События ВЕБ):', error);
        if (status) status.textContent = '';
        alert(error.message || 'Ошибка загрузки изображения');
    }
    fileInput.value = '';
}

async function saveWebRoundEvent(event, roundNumber) {
    event.preventDefault();
    const textEl = document.getElementById(`web-r-${roundNumber}-text`);
    const imgEl = document.getElementById(`web-r-${roundNumber}-image`);
    const bgEl = document.getElementById(`web-r-${roundNumber}-bg`);
    const event_text = textEl ? textEl.value : '';
    const image_url = imgEl ? (imgEl.value || '').trim() : '';
    const bg_image_url = bgEl ? (bgEl.value || '').trim() : '';
    const highlight_resources = getWebEventCheckedItems(roundNumber, 'resource');
    const highlight_buildings = getWebEventCheckedItems(roundNumber, 'building');
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/web-events/${roundNumber}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${adminToken}`,
            },
            body: JSON.stringify({
                event_text,
                image_url,
                bg_image_url,
                highlight_resources,
                highlight_buildings,
            }),
        });
        if (response.ok) {
            alert('Событие ВЕБ сохранено!');
        } else {
            const errorData = await response.json().catch(() => ({}));
            alert(errorData.error || 'Ошибка сохранения');
        }
    } catch (error) {
        console.error('Ошибка сохранения события ВЕБ:', error);
        alert(`Ошибка подключения: ${error.message || 'Неизвестная ошибка'}`);
    }
}

async function saveRoundContent(event, roundNumber) {
    event.preventDefault();
    const contentUrl = document.getElementById(`round-${roundNumber}-content`).value;
    
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/round-content/${roundNumber}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${adminToken}`
            },
            body: JSON.stringify({ 
                content_url: contentUrl,
                content_type: 'video'  // По умолчанию видео
            })
        });
        
        if (response.ok) {
            alert('Контент сохранен!');
        } else {
            const errorData = await response.json().catch(() => ({}));
            alert(errorData.error || 'Ошибка сохранения контента');
        }
    } catch (error) {
        console.error('Ошибка сохранения контента:', error);
        alert(`Ошибка подключения к серверу: ${error.message || 'Неизвестная ошибка'}`);
    }
}

async function loadRoundSettings() {
    if (!currentGameCode) {
        console.error('currentGameCode не установлен');
        return;
    }
    
    const settingsDiv = document.getElementById('rounds-settings');
    settingsDiv.innerHTML = '<div class="loading">Загрузка...</div>';
    
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/round-settings`, {
            headers: {
                'Authorization': `Bearer ${adminToken}`
            }
        });
        
        if (!response.ok) {
            const errorData = await response.json().catch(() => ({}));
            throw new Error(errorData.error || `Ошибка загрузки настроек (${response.status})`);
        }
        
        const data = await response.json();
        const settings = data.settings || [];
        
        // Создаем карту существующих настроек
        const settingsMap = {};
        settings.forEach(setting => {
            settingsMap[setting.round_number] = setting;
        });
        
        const storyPickerHtml = buildRoundSettingsStoryPickerHtml(getAdminSelectedStoryId());

        const roundsHtml = Array.from({ length: 10 }, (_, i) => {
            const roundNum = i + 1;
            const setting = settingsMap[roundNum] || {};
            const resourceMods = setting.resource_modifiers || {};
            const buildingMods = setting.building_modifiers || {};
            const resourceTexts = setting.resource_texts || {};
            const buildingTexts = setting.building_texts || {};

            const resourcesGrid = ADMIN_ALL_RESOURCES.map((resource) =>
                buildRoundModifierFieldHtml(
                    roundNum,
                    'resource',
                    resource,
                    resourceMods[resource] ?? 1.0,
                    resourceTexts[resource] ?? ''
                )
            ).join('');

            const buildingsGrid = ADMIN_ALL_BUILDINGS.map((building) =>
                buildRoundModifierFieldHtml(
                    roundNum,
                    'building',
                    building,
                    buildingMods[building] ?? 1.0,
                    buildingTexts[building] ?? ''
                )
            ).join('');

            return `
                <details class="admin-collapsible round-settings-collapsible">
                    <summary>Раунд ${roundNum}</summary>
                    <form class="round-content-form round-settings-form" onsubmit="saveRoundSettings(event, ${roundNum})">
                        <h4 class="round-settings-section-title">Ресурсы</h4>
                        <div class="round-modifier-grid">${resourcesGrid}</div>
                        <h4 class="round-settings-section-title">Объекты</h4>
                        <div class="round-modifier-grid">${buildingsGrid}</div>
                        <button type="submit" class="btn btn-primary">Сохранить настройки</button>
                    </form>
                </details>`;
        }).join('');

        settingsDiv.innerHTML = storyPickerHtml + roundsHtml;
        bindAdminStoryPicker('rounds');
    } catch (error) {
        console.error('Ошибка загрузки настроек раундов:', error);
        settingsDiv.innerHTML = `<div class="error-message">Ошибка загрузки настроек: ${error.message || 'Неизвестная ошибка'}</div>`;
    }
}

async function saveRoundSettings(event, roundNumber) {
    event.preventDefault();

    const resourceModifiers = {};
    ADMIN_ALL_RESOURCES.forEach(resource => {
        const input = document.getElementById(`round-${roundNumber}-resource-${resource}`);
        if (input && input.value) {
            resourceModifiers[resource] = parseFloat(input.value) || 1.0;
        }
    });
    
    const buildingModifiers = {};
    ADMIN_ALL_BUILDINGS.forEach(building => {
        const input = document.getElementById(`round-${roundNumber}-building-${building}`);
        if (input && input.value) {
            buildingModifiers[building] = parseFloat(input.value) || 1.0;
        }
    });

    const resourceTexts = {};
    ADMIN_ALL_RESOURCES.forEach(resource => {
        const textEl = document.getElementById(`round-${roundNumber}-resource-${resource}-text`);
        if (textEl) resourceTexts[resource] = textEl.value || '';
    });

    const buildingTexts = {};
    ADMIN_ALL_BUILDINGS.forEach(building => {
        const textEl = document.getElementById(`round-${roundNumber}-building-${building}-text`);
        if (textEl) buildingTexts[building] = textEl.value || '';
    });

    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/round-settings/${roundNumber}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${adminToken}`
            },
            body: JSON.stringify({
                resource_modifiers: resourceModifiers,
                building_modifiers: buildingModifiers,
                resource_texts: resourceTexts,
                building_texts: buildingTexts,
            })
        });
        
        if (response.ok) {
            alert('Настройки сохранены!');
        } else {
            const errorData = await response.json().catch(() => ({}));
            alert(errorData.error || 'Ошибка сохранения настроек');
        }
    } catch (error) {
        console.error('Ошибка сохранения настроек раунда:', error);
        alert(`Ошибка подключения к серверу: ${error.message || 'Неизвестная ошибка'}`);
    }
}

async function saveAllGameSettings() {
    if (!currentGameCode) {
        alert('Ошибка: код игры не установлен');
        return;
    }
    const adminToken = localStorage.getItem('admin_token');
    if (!adminToken) {
        alert('Нужна авторизация');
        return;
    }
    const statusEl = document.getElementById('save-all-settings-btn');
    const origText = statusEl?.textContent || 'Сохранить все настройки';
    if (statusEl) {
        statusEl.disabled = true;
        statusEl.textContent = 'Сохранение...';
    }
    let saved = 0;
    let errors = [];
    try {
        // Сохраняем контент: интро (0) и раунды 1..10
        for (let roundNum = 0; roundNum <= 10; roundNum++) {
            const input = document.getElementById(`round-${roundNum}-content`);
            if (!input) continue;
            const contentUrl = (input.value || '').trim();
            if (!contentUrl) continue; // не отправляем пустой URL — API возвращает 400
            const label = roundNum === 0 ? 'Интро' : `Раунд ${roundNum}`;
            try {
                const res = await fetch(`/api/admin/games/${currentGameCode}/round-content/${roundNum}`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${adminToken}`
                    },
                    body: JSON.stringify({ content_url: contentUrl, content_type: 'video' })
                });
                if (res.ok) saved++; else errors.push(`${label} контент: ${(await res.json().catch(() => ({}))).error || res.status}`);
            } catch (e) {
                errors.push(`${label} контент: ${e.message}`);
            }
        }
        // Сохраняем настройки раундов (коэффициенты), если форма есть в DOM
        for (let roundNum = 1; roundNum <= 10; roundNum++) {
            const resourceInput = document.getElementById(`round-${roundNum}-resource-камень`);
            if (!resourceInput) continue;
            const resourceModifiers = {};
            ADMIN_ALL_RESOURCES.forEach(resource => {
                const input = document.getElementById(`round-${roundNum}-resource-${resource}`);
                if (input && input.value) resourceModifiers[resource] = parseFloat(input.value) || 1.0;
            });
            const buildingModifiers = {};
            ADMIN_ALL_BUILDINGS.forEach(building => {
                const input = document.getElementById(`round-${roundNum}-building-${building}`);
                if (input && input.value) buildingModifiers[building] = parseFloat(input.value) || 1.0;
            });
            const resourceTexts = {};
            ADMIN_ALL_RESOURCES.forEach(resource => {
                const textEl = document.getElementById(`round-${roundNum}-resource-${resource}-text`);
                if (textEl) resourceTexts[resource] = textEl.value || '';
            });
            const buildingTexts = {};
            ADMIN_ALL_BUILDINGS.forEach(building => {
                const textEl = document.getElementById(`round-${roundNum}-building-${building}-text`);
                if (textEl) buildingTexts[building] = textEl.value || '';
            });
            try {
                const res = await fetch(`/api/admin/games/${currentGameCode}/round-settings/${roundNum}`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${adminToken}`
                    },
                    body: JSON.stringify({
                        resource_modifiers: resourceModifiers,
                        building_modifiers: buildingModifiers,
                        resource_texts: resourceTexts,
                        building_texts: buildingTexts,
                    })
                });
                if (res.ok) saved++; else errors.push(`Раунд ${roundNum} настройки: ${(await res.json().catch(() => ({}))).error || res.status}`);
            } catch (e) {
                errors.push(`Раунд ${roundNum} настройки: ${e.message}`);
            }
        }
        // События раундов (текст + картинка), если форма была отрисована
        for (let roundNum = 1; roundNum <= 10; roundNum++) {
            const textEl = document.getElementById(`event-r-${roundNum}-text`);
            if (!textEl) continue;
            const imgEl = document.getElementById(`event-r-${roundNum}-image`);
            const event_text = textEl.value;
            const image_url = imgEl ? (imgEl.value || '').trim() : '';
            try {
                const res = await fetch(`/api/admin/games/${currentGameCode}/round-events/${roundNum}`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': `Bearer ${adminToken}`,
                    },
                    body: JSON.stringify({ event_text, image_url }),
                });
                if (res.ok) saved++;
                else errors.push(`Раунд ${roundNum} событие: ${(await res.json().catch(() => ({}))).error || res.status}`);
            } catch (e) {
                errors.push(`Раунд ${roundNum} событие: ${e.message}`);
            }
        }
        if (errors.length > 0) {
            alert('Сохранено частями. Ошибки:\n' + errors.slice(0, 5).join('\n') + (errors.length > 5 ? '\n...' : ''));
        } else {
            alert('Все настройки сохранены!');
        }
    } finally {
        if (statusEl) {
            statusEl.disabled = false;
            statusEl.textContent = origText;
        }
    }
}

document.getElementById('save-all-settings-btn')?.addEventListener('click', () => {
    saveAllGameSettings();
});

// ========== АРХИВИРОВАНИЕ ИГРЫ ==========

document.getElementById('archive-game-btn').addEventListener('click', async () => {
    if (!currentGameCode) {
        alert('Ошибка: код игры не установлен');
        return;
    }
    
    if (!confirm('Вы уверены, что хотите завершить игру и переместить её в архив?')) {
        return;
    }
    
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/archive`, {
            method: 'POST',
            headers: {
                'Authorization': `Bearer ${adminToken}`
            }
        });
        
        const data = await response.json().catch(() => ({}));
        
        if (response.ok) {
            alert(data.message || 'Игра перемещена в архив');
            closeGameModal();
            loadActiveGames();
            loadArchiveGames();
        } else {
            console.error('Ошибка архивирования:', data);
            alert(data.error || 'Ошибка архивирования игры');
        }
    } catch (error) {
        console.error('Ошибка архивирования игры:', error);
        alert(`Ошибка подключения к серверу: ${error.message || 'Неизвестная ошибка'}`);
    }
});

// ========== РЕДАКТИРОВАНИЕ ПЕРСОНАЖА (каталог для выбора) ==========

/** Открыть модалку редактирования персонажа по имени (из списка персонажей). */
async function editCharacter(characterName) {
    if (!currentGameCode) {
        alert('Ошибка: код игры не установлен');
        return;
    }
    const decoded = typeof characterName === 'string' ? characterName.replace(/\\'/g, "'").replace(/&quot;/g, '"') : characterName;
    
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/characters`, {
            headers: { 'Authorization': `Bearer ${adminToken}` }
        });
        if (!response.ok) throw new Error('Ошибка загрузки персонажей');
        const data = await response.json();
        const characters = data.characters || [];
        const char = characters.find(c => (c.name || c.character_name) === decoded);
        if (!char) {
            alert('Персонаж не найден');
            return;
        }
        const name = char.name || char.character_name || '';
        const image = char.image || char.character_image || '';
        document.getElementById('edit-character-original-name').value = name;
        document.getElementById('edit-player-character-name').value = name;
        document.getElementById('edit-player-avatar').value = image;
        const preview = document.getElementById('edit-player-avatar-preview');
        if (preview) {
            preview.src = image || '';
            preview.style.display = image ? 'block' : 'none';
        }
        document.getElementById('edit-player-modal').style.display = 'flex';
    } catch (error) {
        console.error('Ошибка загрузки персонажа:', error);
        alert(`Ошибка: ${error.message || 'Неизвестная ошибка'}`);
    }
}

/** Редактирование игрока по player_id (для обратной совместимости). */
async function editPlayer(playerId) {
    if (!currentGameCode) {
        alert('Ошибка: код игры не установлен');
        return;
    }
    
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/players`, {
            headers: {
                'Authorization': `Bearer ${adminToken}`
            }
        });
        
        if (!response.ok) {
            throw new Error('Ошибка загрузки данных игрока');
        }
        
        const data = await response.json();
        const players = data.players || [];
        const player = players.find(p => String(p.player_id) === String(playerId));
        
        if (!player) {
            alert('Игрок не найден');
            return;
        }
        
        document.getElementById('edit-character-original-name').value = '';
        document.getElementById('edit-player-id').value = playerId;
        document.getElementById('edit-player-name').value = player.name || '';
        document.getElementById('edit-player-character-name').value = player.character_name || '';
        document.getElementById('edit-player-avatar').value = player.character_image || '';
        
        const preview = document.getElementById('edit-player-avatar-preview');
        if (preview && player.character_image) {
            preview.src = player.character_image;
            preview.style.display = 'block';
        }
        
        document.getElementById('edit-player-modal').style.display = 'flex';
    } catch (error) {
        console.error('Ошибка загрузки данных игрока:', error);
        alert(`Ошибка: ${error.message || 'Неизвестная ошибка'}`);
    }
}

function closeEditPlayerModal() {
    document.getElementById('edit-player-modal').style.display = 'none';
    document.getElementById('edit-player-form').reset();
    const orig = document.getElementById('edit-character-original-name');
    if (orig) orig.value = '';
    
    const preview = document.getElementById('edit-player-avatar-preview');
    if (preview) {
        preview.style.display = 'none';
        preview.src = '';
    }
    const statusDiv = document.getElementById('edit-player-avatar-upload-status');
    if (statusDiv) {
        statusDiv.style.display = 'none';
        statusDiv.textContent = '';
    }
    const fileInput = document.getElementById('edit-player-avatar-file');
    if (fileInput) fileInput.value = '';
}

// Обработчик загрузки изображения для редактирования игрока
document.getElementById('edit-player-avatar-file')?.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    
    const urlInput = document.getElementById('edit-player-avatar');
    const statusDiv = document.getElementById('edit-player-avatar-upload-status');
    const preview = document.getElementById('edit-player-avatar-preview');
    
    if (!urlInput || !statusDiv) return;
    
    // Проверяем тип файла
    if (!file.type.startsWith('image/')) {
        statusDiv.textContent = '❌ Выберите изображение';
        statusDiv.style.color = '#d32f2f';
        statusDiv.style.display = 'block';
        return;
    }
    
    // Проверяем размер (макс 5 МБ)
    if (file.size > 5 * 1024 * 1024) {
        statusDiv.textContent = '❌ Размер файла не должен превышать 5 МБ';
        statusDiv.style.color = '#d32f2f';
        statusDiv.style.display = 'block';
        return;
    }
    
    urlInput.disabled = true;
    statusDiv.style.display = 'block';
    statusDiv.textContent = '⏳ Загрузка...';
    statusDiv.style.color = '#666';
    
    try {
        const imageUrl = await uploadImageToServer(file);
        urlInput.value = imageUrl;
        
        // Показываем успех
        statusDiv.textContent = '✅ Изображение загружено успешно';
        statusDiv.style.color = '#4caf50';
        
        // Показываем превью
        if (preview) {
            preview.src = imageUrl;
            preview.style.display = 'block';
        }
        
    } catch (error) {
        console.error('Ошибка загрузки:', error);
        statusDiv.textContent = `❌ Ошибка: ${error.message}`;
        statusDiv.style.color = '#d32f2f';
    } finally {
        urlInput.disabled = false;
    }
});

// Обработчик формы редактирования (персонаж или игрок)
document.getElementById('edit-player-form')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const originalCharacterName = document.getElementById('edit-character-original-name').value.trim();
    const playerId = document.getElementById('edit-player-id').value.trim();
    const name = document.getElementById('edit-player-name').value.trim();
    const characterName = document.getElementById('edit-player-character-name').value.trim();
    const avatar = document.getElementById('edit-player-avatar').value.trim();
    const errorDiv = document.getElementById('edit-player-error');
    
    errorDiv.style.display = 'none';
    
    // Редактирование персонажа (каталог для выбора)
    if (originalCharacterName) {
        if (!characterName || !avatar) {
            errorDiv.textContent = 'Заполните имя персонажа и URL изображения';
            errorDiv.style.display = 'block';
            return;
        }
        try {
            if (characterName !== originalCharacterName) {
                const delRes = await fetch(`/api/admin/games/${currentGameCode}/characters/${encodeURIComponent(originalCharacterName)}`, {
                    method: 'DELETE',
                    headers: { 'Authorization': `Bearer ${adminToken}` }
                });
                if (!delRes.ok) {
                    const d = await delRes.json().catch(() => ({}));
                    throw new Error(d.error || 'Не удалось удалить старую запись персонажа');
                }
            }
            const response = await fetch(`/api/admin/games/${currentGameCode}/characters`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': `Bearer ${adminToken}`
                },
                body: JSON.stringify({
                    character_name: characterName,
                    character_image: avatar
                })
            });
            const data = await response.json();
            if (response.ok) {
                closeEditPlayerModal();
                loadGamePlayers();
            } else {
                errorDiv.textContent = data.error || 'Ошибка сохранения персонажа';
                errorDiv.style.display = 'block';
            }
        } catch (error) {
            console.error('Ошибка сохранения персонажа:', error);
            errorDiv.textContent = error.message || 'Ошибка подключения к серверу';
            errorDiv.style.display = 'block';
        }
        return;
    }
    
    // Редактирование игрока (участника игры)
    if (!playerId || !name || !characterName || !avatar) {
        errorDiv.textContent = 'Заполните все поля';
        errorDiv.style.display = 'block';
        return;
    }
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/players/${playerId}`, {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${adminToken}`
            },
            body: JSON.stringify({
                name: name,
                character_name: characterName,
                character_image: avatar
            })
        });
        const data = await response.json();
        if (response.ok) {
            closeEditPlayerModal();
            loadGamePlayers();
        } else {
            errorDiv.textContent = data.error || 'Ошибка обновления игрока';
            errorDiv.style.display = 'block';
        }
    } catch (error) {
        console.error('Ошибка обновления игрока:', error);
        errorDiv.textContent = `Ошибка подключения к серверу: ${error.message || 'Неизвестная ошибка'}`;
        errorDiv.style.display = 'block';
    }
});

// ========== ДОБАВЛЕНИЕ ИГРОКА ==========

document.getElementById('add-player-btn').addEventListener('click', () => {
    document.getElementById('add-player-modal').style.display = 'flex';
});

function closeAddPlayerModal() {
    document.getElementById('add-player-modal').style.display = 'none';
    document.getElementById('add-player-form').reset();
    
    // Очищаем превью изображения
    const preview = document.getElementById('player-avatar-preview');
    if (preview) {
        preview.style.display = 'none';
        preview.src = '';
    }
    
    // Очищаем статус загрузки
    const statusDiv = document.getElementById('player-avatar-upload-status');
    if (statusDiv) {
        statusDiv.style.display = 'none';
        statusDiv.textContent = '';
    }
    
    // Очищаем файл
    const fileInput = document.getElementById('player-avatar-file');
    if (fileInput) {
        fileInput.value = '';
    }
}

// Обработчик загрузки изображения для игрока
document.getElementById('player-avatar-file')?.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    
    const urlInput = document.getElementById('player-avatar');
    const statusDiv = document.getElementById('player-avatar-upload-status');
    const preview = document.getElementById('player-avatar-preview');
    
    if (!urlInput || !statusDiv) return;
    
    // Проверяем тип файла
    if (!file.type.startsWith('image/')) {
        statusDiv.textContent = '❌ Выберите изображение';
        statusDiv.style.color = '#d32f2f';
        statusDiv.style.display = 'block';
        return;
    }
    
    // Проверяем размер (макс 5 МБ)
    if (file.size > 5 * 1024 * 1024) {
        statusDiv.textContent = '❌ Размер файла не должен превышать 5 МБ';
        statusDiv.style.color = '#d32f2f';
        statusDiv.style.display = 'block';
        return;
    }
    
    urlInput.disabled = true;
    statusDiv.style.display = 'block';
    statusDiv.textContent = '⏳ Загрузка...';
    statusDiv.style.color = '#666';
    
    try {
        const imageUrl = await uploadImageToServer(file);
        urlInput.value = imageUrl;
        
        // Показываем успех
        statusDiv.textContent = '✅ Изображение загружено успешно';
        statusDiv.style.color = '#4caf50';
        
        // Показываем превью
        if (preview) {
            preview.src = imageUrl;
            preview.style.display = 'block';
        }
        
    } catch (error) {
        console.error('Ошибка загрузки:', error);
        statusDiv.textContent = `❌ Ошибка: ${error.message}`;
        statusDiv.style.color = '#d32f2f';
    } finally {
        urlInput.disabled = false;
    }
});

document.getElementById('add-player-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const characterName = document.getElementById('player-character-name').value.trim();
    const avatar = document.getElementById('player-avatar').value.trim();
    const errorDiv = document.getElementById('add-player-error');
    
    errorDiv.style.display = 'none';
    
    if (!characterName || !avatar) {
        errorDiv.textContent = 'Заполните имя персонажа и URL изображения';
        errorDiv.style.display = 'block';
        return;
    }
    
    try {
        // Добавляем только в каталог персонажей (экран выбора). В игру участник попадёт только после выбора персонажа.
        const response = await fetch(`/api/admin/games/${currentGameCode}/characters`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${adminToken}`
            },
            body: JSON.stringify({
                character_name: characterName,
                character_image: avatar
            })
        });
        
        const data = await response.json();
        
        if (response.ok) {
            closeAddPlayerModal();
            loadGamePlayers();
        } else {
            errorDiv.textContent = data.error || 'Ошибка добавления персонажа';
            errorDiv.style.display = 'block';
        }
    } catch (error) {
        console.error('Ошибка добавления персонажа:', error);
        errorDiv.textContent = `Ошибка подключения к серверу: ${error.message || 'Неизвестная ошибка'}`;
        errorDiv.style.display = 'block';
    }
});

/** Удалить персонажа из каталога (по имени). */
async function deleteCharacter(characterName) {
    if (!characterName) return;
    const decoded = typeof characterName === 'string' ? characterName.replace(/\\'/g, "'").replace(/&quot;/g, '"') : characterName;
    if (!confirm(`Удалить персонажа «${decoded}» из списка для выбора?`)) {
        return;
    }
    
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/characters/${encodeURIComponent(decoded)}`, {
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${adminToken}`
            }
        });
        
        if (response.ok) {
            alert('Персонаж удален');
            loadGamePlayers();
        } else {
            const data = await response.json().catch(() => ({}));
            alert(data.error || 'Ошибка удаления персонажа');
        }
    } catch (error) {
        console.error('Ошибка удаления персонажа:', error);
        alert(`Ошибка подключения к серверу: ${error.message || 'Неизвестная ошибка'}`);
    }
}

/** Удалить игрока (оставлено для совместимости, в UI вкладки «Персонажи» не используется). */
async function deletePlayer(playerId) {
    if (!confirm(`Вы уверены, что хотите удалить игрока ${playerId}?`)) {
        return;
    }
    
    try {
        const response = await fetch(`/api/admin/games/${currentGameCode}/players/${playerId}`, {
            method: 'DELETE',
            headers: {
                'Authorization': `Bearer ${adminToken}`
            }
        });
        
        if (response.ok) {
            alert('Игрок удален');
            loadGamePlayers();
        } else {
            const data = await response.json().catch(() => ({}));
            alert(data.error || 'Ошибка удаления игрока');
        }
    } catch (error) {
        console.error('Ошибка удаления игрока:', error);
        alert(`Ошибка подключения к серверу: ${error.message || 'Неизвестная ошибка'}`);
    }
}

// ========== ЗАГРУЗКА ИЗОБРАЖЕНИЙ ==========

/**
 * Загрузить изображение на сервер игры.
 * Файл передаём base64 в JSON, чтобы не тянуть python-multipart на бэкенде.
 */
async function uploadImageToServer(file) {
    const content = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error('Не удалось прочитать файл'));
        reader.readAsDataURL(file);
    });
    
    const response = await fetch('/api/admin/upload-image', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${adminToken}`
        },
        body: JSON.stringify({ filename: file.name, content })
    });
    
    const data = await response.json().catch(() => ({}));
    
    if (!response.ok || !data.url) {
        throw new Error(data.error || `Ошибка загрузки (${response.status})`);
    }
    
    return data.url;
}

// Функции handleImageFileSelect и showImagePreview удалены - больше не используются
// Теперь загрузка изображений для игроков обрабатывается напрямую через обработчик player-avatar-file

// ========== УПРАВЛЕНИЕ ПЕРСОНАЖАМИ ==========
// УДАЛЕНО: Персонажи объединены с игроками
// Теперь при добавлении игрока сразу указывается имя и изображение персонажа

// Экспорт функций для использования в HTML
window.openGameModal = openGameModal;
window.closeGameModal = closeGameModal;
window.closeAddPlayerModal = closeAddPlayerModal;
window.editPlayer = editPlayer;
window.editCharacter = editCharacter;
window.closeEditPlayerModal = closeEditPlayerModal;
window.saveRoundContent = saveRoundContent;
window.saveRoundSettings = saveRoundSettings;
window.deletePlayer = deletePlayer;
window.deleteCharacter = deleteCharacter;

// Обновление списков
document.getElementById('refresh-games-btn')?.addEventListener('click', loadActiveGames);
document.getElementById('refresh-archive-btn')?.addEventListener('click', loadArchiveGames);
