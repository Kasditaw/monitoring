/**
 * LabGuard PRO v2.0 - Frontend Application Logic
 * Pure Vanilla JavaScript with Real-time WebSocket Client
 */

// --- Dynamic Reverse Proxy Base Path (Supports direct :8000 and /monitoring on Port 80) ---
const BASE_PATH = (window.location.pathname.toLowerCase().startsWith('/monitoring')) ? '/monitoring' : '';

// Transparent fetch wrapper ensuring all /api/ and /download/ calls pass through BASE_PATH,
// auto-attaches Bearer token, and handles 401 authentication expiration cleanly
const _origFetch = window.fetch;
window.fetch = async function(url, options) {
    if (typeof url === 'string' && BASE_PATH && (url.startsWith('/api/') || url.startsWith('/download/'))) {
        url = BASE_PATH + url;
    }
    const token = (typeof getAuthToken === 'function') ? getAuthToken() : (localStorage.getItem('labguard_token') || '');
    if (token) {
        options = options || {};
        options.headers = options.headers || {};
        if (options.headers instanceof Headers) {
            if (!options.headers.has('Authorization')) options.headers.set('Authorization', `Bearer ${token}`);
        } else if (Array.isArray(options.headers)) {
            options.headers.push(['Authorization', `Bearer ${token}`]);
        } else if (typeof options.headers === 'object') {
            if (!options.headers['Authorization']) options.headers['Authorization'] = `Bearer ${token}`;
        }
    }
    const resp = await _origFetch(url, options);
    if (resp.status === 401 && typeof url === 'string' && !url.includes('/api/auth/status') && !url.includes('/api/auth/login')) {
        try { localStorage.removeItem('labguard_token'); } catch (e) {}
        window.location.replace(BASE_PATH ? `${BASE_PATH}/login` : '/login');
    }
    return resp;
};

// --- State Store ---
const state = {
    ws: null,
    wsConnected: false,
    pingStartTime: 0,
    latencyMs: 0,
    reconnectAttempts: 0,
    
    clients: new Map(),          // client_id -> client object
    thumbnails: new Map(),       // client_id -> base64 string
    selectedClientIds: new Set(),// Set of selected client_id strings
    
    roomFilter: 'all',
    searchQuery: '',
    viewMode: 'grid',            // 'grid' | 'list' | 'seating'
    sortBy: localStorage.getItem('labguard_sort_by') || 'name_asc',
    soundEnabled: true,

    // Seating Plan (Denah Lab)
    seatingEditMode: true,       // Mode geser meja aktif
    seatingSnap: true,           // Snap to 20px grid
    seatingZoom: 1.0,            // 100% zoom
    seatingLayouts: new Map(),   // room -> { cid: { x, y } }
    seatingSaveTimer: null,
    
    activeInspectId: null,
    activeInspectTab: 'running',  // 'running' | 'installed' | 'updates'
    renameTargetCid: null,
    launchAppTargetSingle: null,  // client_id if launching for single PC from inspect modal, null for default
    clientInstalledApps: new Map(), // client_id -> array of {name, version, publisher, exe, install_location}
    installedAppSearchQuery: '',
    remoteClientId: null,
    remoteMode: 'interactive',    // 'interactive' | 'view'
    remoteQuality: 55,
    remoteFrameCount: 0,
    remoteFps: 0,
    remoteLastFpsTime: 0,
    remoteLastMoveTime: 0,
    remoteThrottleMs: 25,
    stats: {
        total_clients: 0,
        online_clients: 0,
        offline_clients: 0,
        locked_clients: 0,
        avg_cpu: 0,
        avg_ram: 0,
        high_cpu_count: 0
    }
};

// --- Helper: Get Clean PC Display Name (Custom Name / Alias or Hostname) ---
function getClientDisplayName(client) {
    if (!client) return '';
    if (typeof client === 'string') {
        const c = state.clients.get(client);
        if (c) {
            return (c.custom_name && c.custom_name.trim()) ? c.custom_name.trim() : (c.display_name || c.hostname || c.client_id || client);
        }
        return client;
    }
    return (client.custom_name && client.custom_name.trim()) ? client.custom_name.trim() : (client.display_name || client.hostname || client.client_id || '');
}

// --- Helper: Teacher PC Detection ---
function isTeacherClient(c) {
    if (!c) return false;
    if (Boolean(c.is_teacher)) return true;
    const cid = String(c.client_id || '').toUpperCase();
    const hname = String(c.hostname || '').toUpperCase();
    const cname = String(c.custom_name || '').toLowerCase();
    if (cid === 'DESKTOP-2PTRR2S' || cid === 'DESKTOP-G4DVHV2') return true;
    if (hname === 'DESKTOP-2PTRR2S' || hname === 'DESKTOP-G4DVHV2') return true;
    if (cname.includes('guru') || hname.toLowerCase().includes('guru') || cname.includes('teacher')) return true;
    return false;
}

// --- Helper: Natural Alphanumeric Sorter for PCs ---
function sortClients(clientList, sortMode = (state.sortBy || 'name_asc')) {
    if (!Array.isArray(clientList)) return [];
    
    return clientList.slice().sort((a, b) => {
        const isTeacherA = isTeacherClient(a);
        const isTeacherB = isTeacherClient(b);
        
        // In all sort modes (except reverse name), keep Teacher PC pinned at the top
        if (sortMode !== 'name_desc') {
            if (isTeacherA && !isTeacherB) return -1;
            if (!isTeacherA && isTeacherB) return 1;
        }

        if (sortMode === 'online_first') {
            const onA = a.is_online ? 1 : 0;
            const onB = b.is_online ? 1 : 0;
            if (onA !== onB) return onB - onA;
        } else if (sortMode === 'ip_asc') {
            const ipPartsA = (a.ip_address || '0.0.0.0').split('.').map(n => parseInt(n, 10) || 0);
            const ipPartsB = (b.ip_address || '0.0.0.0').split('.').map(n => parseInt(n, 10) || 0);
            for (let i = 0; i < 4; i++) {
                const diff = (ipPartsA[i] || 0) - (ipPartsB[i] || 0);
                if (diff !== 0) return diff;
            }
        }

        const nameA = getClientDisplayName(a);
        const nameB = getClientDisplayName(b);
        const comp = nameA.localeCompare(nameB, undefined, { numeric: true, sensitivity: 'base' });
        return (sortMode === 'name_desc') ? -comp : comp;
    });
}

// --- Reorder Grid Cards In-Place (Zero-flicker, maintains live canvas/streams) ---
function reorderGridCards() {
    const grid = document.getElementById('clientsGrid');
    if (!grid) return;
    const cards = Array.from(grid.querySelectorAll('.pc-card'));
    if (cards.length <= 1) return;

    const cardMap = new Map();
    cards.forEach(card => {
        const cid = card.id.replace('pc-card-', '');
        cardMap.set(cid, card);
    });

    const clientList = Array.from(cardMap.keys()).map(cid => state.clients.get(cid)).filter(Boolean);
    const sorted = sortClients(clientList);

    sorted.forEach(c => {
        const card = cardMap.get(c.client_id);
        if (card) {
            grid.appendChild(card);
        }
    });
}

// --- Web Audio Synthesizer for Clean Alert Tones ---
let audioCtx = null;

function getAudioContext() {
    if (!audioCtx) {
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        if (AudioContext) audioCtx = new AudioContext();
    }
    if (audioCtx && audioCtx.state === 'suspended') {
        audioCtx.resume();
    }
    return audioCtx;
}

function playSound(type = 'blip') {
    if (!state.soundEnabled) return;
    try {
        const ctx = getAudioContext();
        if (!ctx) return;
        const now = ctx.currentTime;
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);

        if (type === 'blip') {
            // Pleasant high-pitch blip for connection
            osc.frequency.setValueAtTime(587.33, now); // D5
            osc.frequency.exponentialRampToValueAtTime(880, now + 0.1); // A5
            gain.gain.setValueAtTime(0.08, now);
            gain.gain.exponentialRampToValueAtTime(0.001, now + 0.15);
            osc.start(now);
            osc.stop(now + 0.15);
        } else if (type === 'alert') {
            // Two-tone warning beep
            osc.type = 'sawtooth';
            osc.frequency.setValueAtTime(440, now);
            osc.frequency.setValueAtTime(880, now + 0.12);
            gain.gain.setValueAtTime(0.12, now);
            gain.gain.exponentialRampToValueAtTime(0.001, now + 0.28);
            osc.start(now);
            osc.stop(now + 0.28);
        } else if (type === 'success') {
            // Harmonious chord
            osc.frequency.setValueAtTime(523.25, now); // C5
            osc.frequency.setValueAtTime(659.25, now + 0.08); // E5
            osc.frequency.setValueAtTime(783.99, now + 0.16); // G5
            gain.gain.setValueAtTime(0.09, now);
            gain.gain.exponentialRampToValueAtTime(0.001, now + 0.3);
            osc.start(now);
            osc.stop(now + 0.3);
        }
    } catch (e) {
        // Audio might be blocked until user gesture
    }
}

// --- Toast Notification System ---
function showToast(message, type = 'info', duration = 3500) {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    
    let icon = 'ℹ️';
    if (type === 'success') icon = '✓';
    if (type === 'warning') icon = '⚠️';
    if (type === 'danger') icon = '🚨';

    toast.innerHTML = `<span>${icon}</span> <div>${message}</div>`;
    container.appendChild(toast);

    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        toast.style.transition = 'all 0.3s ease';
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

// --- Real-time Clock ---
function startClock() {
    const clockEl = document.getElementById('clockDisplay');
    function update() {
        const now = new Date();
        const timeStr = now.toLocaleTimeString('id-ID', { hour12: false }) + ' WIB';
        if (clockEl) clockEl.textContent = timeStr;
    }
    update();
    setInterval(update, 1000);
}

// --- WebSocket Connection ---
function getAuthToken() {
    let token = '';
    try {
        token = localStorage.getItem('labguard_token') || '';
    } catch (e) {}
    if (!token) {
        const match = document.cookie.match(/(?:^|;\s*)labguard_token=([^;]+)/);
        if (match) {
            token = decodeURIComponent(match[1]);
            try { localStorage.setItem('labguard_token', token); } catch (e) {}
        }
    }
    return token;
}

function initWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    let host = window.location.host;
    let wsPath = '/ws/admin';

    if (BASE_PATH) {
        // Reverse-proxied via Apache on port 80 (e.g. http://10.161.17.165/monitoring)
        wsPath = `${BASE_PATH}/ws/admin`;
    } else if (!host || window.location.port === '80' || window.location.port === '') {
        const hostname = window.location.hostname || 'localhost';
        host = `${hostname}:8000`;
    }
    const token = getAuthToken();
    const wsUrl = `${protocol}//${host}${wsPath}${token ? `?token=${encodeURIComponent(token)}` : ''}`;

    const statusPill = document.getElementById('serverStatusPill');
    const pulseDot = document.getElementById('serverPulseDot');
    const statusText = document.getElementById('serverStatusText');
    const latencyEl = document.getElementById('serverLatency');

    function setOnline(online) {
        state.wsConnected = online;
        if (online) {
            pulseDot.className = 'pulsing-dot online';
            statusText.textContent = 'TERHUBUNG';
            statusText.className = 'status-label text-emerald';
        } else {
            pulseDot.className = 'pulsing-dot offline';
            statusText.textContent = 'TERPUTUS';
            statusText.className = 'status-label text-rose';
            latencyEl.textContent = '--';
        }
    }

    try {
        state.ws = new WebSocket(wsUrl);

        state.ws.onopen = () => {
            setOnline(true);
            state.reconnectAttempts = 0;
            showToast('Terhubung ke Lab Monitoring Server!', 'success');
            playSound('blip');
            sendPing();
        };

        state.ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                handleServerMessage(msg);
            } catch (err) {
                console.error('Error parsing WS message:', err);
            }
        };

        state.ws.onclose = (event) => {
            setOnline(false);
            if (event && event.code === 4001) {
                // Token tidak valid atau sesi berakhir
                try { localStorage.removeItem('labguard_token'); } catch (e) {}
                window.location.replace(BASE_PATH ? `${BASE_PATH}/login` : '/login');
                return;
            }
            const delay = Math.min(1000 * Math.pow(1.5, state.reconnectAttempts), 8000);
            state.reconnectAttempts++;
            setTimeout(initWebSocket, delay);
        };

        state.ws.onerror = (err) => {
            setOnline(false);
        };

    } catch (e) {
        setOnline(false);
        setTimeout(initWebSocket, 3000);
    }

    // Ping interval to measure latency
    function sendPing() {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
            state.pingStartTime = performance.now();
            state.ws.send(JSON.stringify({ type: 'ping' }));
        }
        setTimeout(sendPing, 10000);
    }
}

// --- Server Message Handler ---
function handleServerMessage(msg) {
    const type = msg.type;

    if (type === 'pong') {
        const latency = Math.round(performance.now() - state.pingStartTime);
        state.latencyMs = latency;
        const latencyEl = document.getElementById('serverLatency');
        if (latencyEl) latencyEl.textContent = `${latency}ms`;
    }
    else if (type === 'init') {
        // Initial state with instant screenshots
        state.clients.clear();
        state.thumbnails.clear();
        if (msg.thumbnails) {
            Object.entries(msg.thumbnails).forEach(([cid, thumb]) => {
                if (thumb) state.thumbnails.set(cid, thumb);
            });
        }
        if (msg.installed_apps) {
            Object.entries(msg.installed_apps).forEach(([cid, apps]) => {
                if (apps && Array.isArray(apps)) state.clientInstalledApps.set(cid, apps);
            });
        }
        const seenIps = new Map();
        const sortedInitClients = (msg.clients || []).slice().sort((a, b) => {
            if (Boolean(b.is_online) !== Boolean(a.is_online)) return b.is_online ? 1 : -1;
            if (Boolean(b.custom_name) !== Boolean(a.custom_name)) return b.custom_name ? 1 : -1;
            return 0;
        });

        sortedInitClients.forEach(c => {
            const ip = c.ip_address || '';
            const isLocal = !ip || ip.startsWith('127.');
            if (!isLocal && seenIps.has(ip)) {
                // Ghost duplicate client detected on same IP, ignore stale record
                return;
            }
            if (!isLocal) seenIps.set(ip, c.client_id);
            state.clients.set(c.client_id, c);
            if (c.thumbnail) state.thumbnails.set(c.client_id, c.thumbnail);
        });
        if (msg.stats) updateStatsOverview(msg.stats);
        renderClients();
    }
    else if (type === 'client_connected') {
        const client = msg.client;
        if (client && client.client_id) {
            // Frontend Deduplication Defense: If an offline ghost card with the same IP exists, prune it immediately
            if (client.ip_address && !client.ip_address.startsWith('127.')) {
                for (const [existingCid, existingC] of state.clients.entries()) {
                    if (existingCid !== client.client_id && existingC.ip_address === client.ip_address && !existingC.is_online) {
                        state.clients.delete(existingCid);
                        state.thumbnails.delete(existingCid);
                        const oldCard = document.getElementById(`pc-card-${existingCid}`);
                        if (oldCard) oldCard.remove();
                    }
                }
            }
            state.clients.set(client.client_id, client);
            if (client.thumbnail) {
                state.thumbnails.set(client.client_id, client.thumbnail);
            }
        }
        if (msg.stats) updateStatsOverview(msg.stats);
        updateOrAddClientCard(client);
        showToast(`Komputer <strong>${getClientDisplayName(client)}</strong> (${client.ip_address}) terhubung.`, 'success');
        playSound('blip');
    }
    else if (type === 'client_updated') {
        const client = msg.client;
        if (client && client.client_id) {
            const existing = state.clients.get(client.client_id) || {};
            const merged = Object.assign(existing, client);
            state.clients.set(client.client_id, merged);
            updateOrAddClientCard(merged);
            if (state.viewMode === 'list') renderTable();
            if (state.viewMode === 'seating') renderSeatingPlan();
            if (state.activeInspectId === client.client_id) {
                setText('inspectPcTitle', getClientDisplayName(merged));
                updateInspectModalContent(merged);
            }
        }
    }
    else if (type === 'client_disconnected') {
        const cid = msg.client_id;
        if (state.clients.has(cid)) {
            const c = state.clients.get(cid);
            c.is_online = false;
            updateOrAddClientCard(c);
        }
        if (msg.stats) updateStatsOverview(msg.stats);
        showToast(`Komputer <strong>${cid}</strong> terputus (offline).`, 'warning');
    }
    else if (type === 'client_deleted') {
        const cid = msg.client_id;
        state.clients.delete(cid);
        state.thumbnails.delete(cid);
        state.selectedClientIds.delete(cid);
        const card = document.getElementById(`pc-card-${cid}`);
        if (card) card.remove();
        if (state.activeInspectId === cid) {
            closeModal('modalInspect');
        }
        if (state.remoteClientId === cid) {
            closeRemoteControl(false);
        }
        if (msg.stats) updateStatsOverview(msg.stats);
        renderTable();
        updateSelectionPill();
        const emptyState = document.getElementById('emptyState');
        if (emptyState && Array.from(state.clients.values()).filter(isClientVisible).length === 0) {
            emptyState.style.display = 'flex';
        }
        showToast(`Komputer <strong>${cid}</strong> dihapus dari dashboard.`, 'info');
    }
    else if (type === 'telemetry') {
        const cid = msg.client_id;
        if (state.clients.has(cid)) {
            const c = state.clients.get(cid);
            Object.assign(c, msg.data);
            updateCardTelemetry(cid, c);
        }
        if (msg.stats) updateStatsOverview(msg.stats);

        // Update inspect modal if active
        if (state.activeInspectId === cid) {
            updateInspectModalContent(state.clients.get(cid));
        }
    }
    else if (type === 'screen_update') {
        const cid = msg.client_id;
        state.thumbnails.set(cid, msg.thumbnail);

        // Efficiency: If tab is hidden or minimized, skip DOM reflow and image decoding
        if (document.hidden) return;

        updateCardThumbnail(cid, msg.thumbnail);

        if (state.activeInspectId === cid) {
            const largeScreen = document.getElementById('inspectLargeScreen');
            if (largeScreen) largeScreen.src = `data:image/jpeg;base64,${msg.thumbnail}`;
        }

        // Live fallback: If teacher is viewing this PC via Remote Control, update screen immediately
        if (state.remoteClientId === cid) {
            const remoteImg = document.getElementById('remoteScreenImg');
            if (remoteImg) remoteImg.src = `data:image/jpeg;base64,${msg.thumbnail}`;
            const loadingOverlay = document.getElementById('remoteLoadingOverlay');
            if (loadingOverlay) loadingOverlay.style.display = 'none';
            const fpsEl = document.getElementById('remoteFpsText');
            if (fpsEl && fpsEl.textContent.includes('Menghubungkan')) {
                fpsEl.textContent = '● Streaming Aktif';
            }
        }
    }
    else if (type === 'command_broadcasted') {
        const count = msg.sent_count || 0;
        let actionLabel = msg.action;
        if (msg.action === 'shutdown') actionLabel = 'Matikan Komputer (Shutdown)';
        else if (msg.action === 'restart') actionLabel = 'Mulai Ulang (Restart)';
        else if (msg.action === 'lock') actionLabel = 'Kunci Layar';
        else if (msg.action === 'unlock') actionLabel = 'Buka Kunci Layar';
        else if (msg.action === 'wake') actionLabel = 'Wake-on-LAN';
        else if (msg.action === 'launch_app') actionLabel = 'Luncurkan Aplikasi';

        if (count > 0) {
            showToast(`Perintah <strong>${actionLabel}</strong> terkirim ke <strong>${count} komputer</strong>.`, 'success');
            playSound('success');
        } else {
            showToast(`Perintah <strong>${actionLabel}</strong>: Tidak ada komputer aktif yang terhubung.`, 'warning');
        }
    }
    else if (type === 'command_result') {
        if (msg.status === 'ERROR') {
            showToast(`Gagal di <strong>${msg.client_id || 'PC'}</strong>: ${msg.message}`, 'danger');
        } else if (msg.action === 'launch_app') {
            const pcName = getClientDisplayName(msg.client_id) || msg.client_id || 'PC Siswa';
            showToast(`🚀 <strong>${escapeHtml(pcName)}</strong>: ${escapeHtml(msg.message)}`, 'success');
        } else if (msg.action === 'shutdown' || msg.action === 'restart') {
            showToast(`✅ <strong>${msg.client_id || 'PC'}</strong>: ${msg.message}`, 'info');
        }
    }
    else if (type === 'installed_apps') {
        const cid = msg.client_id;
        const apps = msg.apps || [];
        state.clientInstalledApps.set(cid, apps);
        if (state.activeInspectId === cid) {
            renderInspectInstalledApps(cid);
        }
    }
    else if (type === 'winget_updates') {
        // Pending software updates received from student PC
        const cid = msg.client_id;
        const updates = msg.updates || [];
        state.clientWingetUpdates = state.clientWingetUpdates || new Map();
        state.clientWingetUpdates.set(cid, updates);
        if (state.activeInspectId === cid && state.activeInspectTab === 'updates') {
            renderWingetUpdates(cid);
        }
        const count = updates.length;
        if (count > 0) {
            showToast(`📦 <strong>${cid}</strong>: Ditemukan <strong>${count}</strong> update software tersedia`, 'info');
        } else {
            showToast(`✅ <strong>${cid}</strong>: Semua software sudah terbaru (winget)`, 'success');
        }
    }
    else if (type === 'windows_updates') {
        // Windows hotfix/patch list received from student PC
        const cid = msg.client_id;
        const patches = msg.patches || [];
        state.clientWindowsUpdates = state.clientWindowsUpdates || new Map();
        state.clientWindowsUpdates.set(cid, patches);
        if (state.activeInspectId === cid && state.activeInspectTab === 'updates') {
            renderWindowsUpdates(cid);
        }
        showToast(`🪟 <strong>${cid}</strong>: ${patches.length} Windows Update patch terpasang`, 'info');
    }
    else if (type === 'violation_alert') {
        const cid = msg.client_id;
        const client = state.clients.get(cid);
        if (client) {
            client.violation = msg;
            updateOrAddClientCard(client);
            updateOrAddSeatingCard(client);
        }
        playSound('danger');
        const autoKillText = msg.auto_killed ? ' (Otomatis Ditutup oleh Server)' : '';
        showToast(`🚨 <strong>PERINGATAN LAB!</strong> PC <strong>${escapeHtml(msg.hostname || cid)}</strong> membuka game/aplikasi terlarang: <strong>${escapeHtml(msg.keyword)}</strong>${autoKillText}`, 'danger');
        updateViolationStatsBadge();
    }
    else if (type === 'violation_cleared') {
        const cid = msg.client_id;
        const client = state.clients.get(cid);
        if (client) {
            delete client.violation;
            updateOrAddClientCard(client);
            updateOrAddSeatingCard(client);
        }
        updateViolationStatsBadge();
    }
    else if (type === 'remote_frame') {
        if (state.remoteClientId === msg.client_id) {
            handleRemoteFrame(msg);
        }
    }
    else if (type === 'remote_started') {
        if (state.remoteClientId === msg.client_id) {
            const loadingOverlay = document.getElementById('remoteLoadingOverlay');
            if (loadingOverlay) loadingOverlay.style.display = 'none';
            const fpsEl = document.getElementById('remoteFpsText');
            if (fpsEl && fpsEl.textContent.includes('Menghubungkan')) {
                fpsEl.textContent = '● Streaming Aktif';
            }
        }
    }
    else if (type === 'remote_stopped') {
        if (state.remoteClientId === msg.client_id) {
            showToast(`Sesi Remote Control di <strong>${msg.client_id}</strong> terputus. Mencoba menghubungkan ulang...`, 'warning');
            const fpsEl = document.getElementById('remoteFpsText');
            if (fpsEl) fpsEl.textContent = 'Menghubungkan ulang...';
            // Automatically attempt reconnect after 1.5s if still active
            setTimeout(() => {
                if (state.remoteClientId === msg.client_id && state.ws && state.ws.readyState === WebSocket.OPEN) {
                    state.ws.send(JSON.stringify({
                        type: 'start_remote',
                        client_id: state.remoteClientId,
                        quality: state.remoteQuality || 55
                    }));
                }
            }, 1500);
        }
    }
}

// --- Send Command Helper ---
function sendCommand(target, action, payload = {}) {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
        showToast('Koneksi server belum siap!', 'danger');
        return;
    }
    const msg = {
        type: 'command',
        target: target,
        action: action,
        payload: payload
    };
    state.ws.send(JSON.stringify(msg));
}

// --- Statistics Overview Display ---
function updateStatsOverview(stats) {
    state.stats = stats;
    setText('statTotalClients', stats.total_clients || 0);
    setText('statOnlineClients', stats.online_clients || 0);
    setText('statOfflineClients', stats.offline_clients || 0);
    setText('statLockedClients', stats.locked_clients || 0);
    setText('statAvgCpu', `${stats.avg_cpu || 0}%`);
    setText('statAvgRam', `${stats.avg_ram || 0}%`);

    const onlinePct = stats.total_clients ? Math.round((stats.online_clients / stats.total_clients) * 100) : 0;
    setText('statOnlinePercent', `${onlinePct}%`);

    const cpuFill = document.getElementById('statCpuFill');
    if (cpuFill) cpuFill.style.width = `${Math.min(stats.avg_cpu || 0, 100)}%`;

    const ramFill = document.getElementById('statRamFill');
    if (ramFill) ramFill.style.width = `${Math.min(stats.avg_ram || 0, 100)}%`;

    // Update toolbar violation badge
    const vBadge = document.getElementById('violationCountBadge');
    if (vBadge) {
        const vCount = stats.violations_count || 0;
        vBadge.textContent = vCount;
        vBadge.style.display = vCount > 0 ? 'inline-flex' : 'none';
    }
}

function setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
}

function setHTML(id, val) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = val;
}

// --- Filter & Search Matcher ---
function isClientVisible(client) {
    // Room Filter
    if (state.roomFilter !== 'all' && client.lab_room !== state.roomFilter) {
        return false;
    }
    // Search Query
    if (state.searchQuery) {
        const q = state.searchQuery.toLowerCase();
        const displayName = getClientDisplayName(client).toLowerCase();
        const hostname = (client.hostname || '').toLowerCase();
        const nameMatch = displayName.includes(q) || hostname.includes(q);
        const ipMatch = (client.ip_address || '').includes(q);
        const appMatch = (client.active_app || '').toLowerCase().includes(q);
        const roomMatch = (client.lab_room || '').toLowerCase().includes(q);
        return nameMatch || ipMatch || appMatch || roomMatch;
    }
    return true;
}

// --- Diagnostic Health Badge & Uptime Formatter ---
function formatUptime(seconds) {
    if (!seconds || seconds <= 0) return 'Baru aktif';
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    if (hrs >= 24) {
        const days = Math.floor(hrs / 24);
        const remHrs = hrs % 24;
        return `${days}h ${remHrs}j`;
    }
    if (hrs > 0) return `${hrs}j ${mins}m`;
    return `${mins}m`;
}

function createHealthBadgeHtml(c) {
    if (!c.is_online) {
        return '<span class="badge-health offline" title="PC Standby / Offline">⚪ Standby</span>';
    }
    const score = (typeof c.health_score === 'number') ? c.health_score : 100;
    const issues = Array.isArray(c.health_issues) ? c.health_issues : [];
    const status = c.health_status || (score < 75 ? 'critical' : (score < 90 ? 'warning' : 'good'));

    if (status === 'critical') {
        const issueText = issues[0] || 'Kondisi Kritis';
        return `<span class="badge-health critical" title="Kritis (${score}%): ${escapeHtml(issues.join(', ') || 'Perlu tindakan')}">🔴 ${escapeHtml(issueText)}</span>`;
    } else if (status === 'warning') {
        const issueText = issues[0] || 'Peringatan';
        return `<span class="badge-health warning" title="Peringatan (${score}%): ${escapeHtml(issues.join(', ') || 'Perhatian')}">⚠️ ${escapeHtml(issueText)}</span>`;
    } else {
        const uptimeStr = formatUptime(c.uptime_seconds);
        const diskFree = c.disk_free_gb ? `${c.disk_free_gb} GB` : 'Bebas';
        return `<span class="badge-health good" title="Kondisi Prima (${score}%) · Uptime: ${uptimeStr} · Disk C: Sisa ${diskFree}">🟢 Sehat ${score}%</span>`;
    }
}

// --- Client Card Element Creator ---
function createClientCardElement(c) {
    const card = document.createElement('div');
    card.id = `pc-card-${c.client_id}`;
    card.className = `pc-card ${c.is_online ? 'online' : 'offline'} ${c.is_locked ? 'locked' : ''} ${state.selectedClientIds.has(c.client_id) ? 'selected' : ''}`;

    const isSelected = state.selectedClientIds.has(c.client_id);
    const cpuPct = Math.round(c.cpu_pct || 0);
    const ramPct = Math.round(c.ram_pct || 0);
    const cpuClass = cpuPct > 80 ? 'danger' : (cpuPct > 60 ? 'warning' : 'normal');
    const ramClass = ramPct > 85 ? 'danger' : (ramPct > 70 ? 'warning' : 'normal');

    const thumbSrc = state.thumbnails.get(c.client_id) ? `data:image/jpeg;base64,${state.thumbnails.get(c.client_id)}` : '';

    // Check if active app contains forbidden keyword / game
    const hasViolation = Boolean(c.violation && c.violation.detected);
    const violationKeyword = hasViolation ? (c.violation.keyword || 'GAME').toUpperCase() : '';
    const isGame = hasViolation || (c.active_app || '').toLowerCase().includes('roblox') || (c.active_app || '').toLowerCase().includes('game');

    const statusPill = c.is_online 
        ? `<span class="badge-status online"><span class="pulse-indicator"></span>ONLINE</span>`
        : `<span class="badge-status offline"><span class="dormant-dot"></span>OFFLINE</span>`;

    const isTeacher = isTeacherClient(c);
    if (isTeacher) card.classList.add('teacher-pc');
    if (hasViolation) card.classList.add('card-violation');

    const displayName = getClientDisplayName(c);
    const hasCustomName = Boolean(c.custom_name && c.custom_name.trim() && c.custom_name.trim() !== c.hostname);

    const ramUsedGb = c.ram_used_mb ? (c.ram_used_mb / 1024).toFixed(1) : ((ramPct / 100) * 16).toFixed(1);
    const ramTotalGb = c.ram_total_mb ? (c.ram_total_mb / 1024).toFixed(0) : '16';

    card.innerHTML = `
        <div class="pc-card-header">
            <div class="header-main">
                <label class="custom-checkbox card-check" title="Pilih Komputer">
                    <input type="checkbox" class="pc-card-checkbox" data-cid="${c.client_id}" ${isSelected ? 'checked' : ''}>
                    <span class="checkbox-mark"></span>
                </label>
                <div class="pc-title-group">
                    <h4 class="pc-hostname" title="${escapeHtml(displayName)}${hasCustomName ? ` (Asli: ${escapeHtml(c.hostname)})` : ''} · Klik ✏️ untuk ubah nama">
                        <span class="pc-name-text" id="pc-name-text-${c.client_id}" title="Klik untuk ubah nama">${escapeHtml(displayName)}</span>
                        ${hasCustomName ? `<span class="badge-custom-tag" title="Nama Kustom (Asli Windows: ${escapeHtml(c.hostname)})">🏷️</span>` : ''}
                        <button type="button" class="btn-rename-quick" data-cid="${c.client_id}" title="Ubah Nama Komputer ini">✏️</button>
                        ${isTeacher ? `<span class="badge-teacher-role" title="Komputer Pengajar / Guru">👑 Guru</span>` : ''}
                    </h4>
                    <div class="pc-meta-row">
                        <span class="pc-room-badge">${escapeHtml(c.lab_room || 'Lab Umum')}</span>
                        <span class="pc-ip-mono">${c.ip_address || '127.0.0.1'}</span>
                        <span class="pc-ver-pill" title="Versi Agen Siswa">${escapeHtml(c.agent_version || 'v2.1.0')}</span>
                    </div>
                </div>
            </div>
            <div class="header-status" id="status-container-${c.client_id}">
                ${statusPill}
                <div id="health-badge-wrap-${c.client_id}">${createHealthBadgeHtml(c)}</div>
            </div>
        </div>

        <div class="pc-thumbnail-wrapper" data-cid="${c.client_id}">
            <img src="${thumbSrc}" alt="Screen" class="pc-thumbnail-img" id="thumb-${c.client_id}" style="${thumbSrc ? '' : 'display:none;'}">
            <div class="pc-thumbnail-offline" id="placeholder-${c.client_id}" style="${thumbSrc ? 'display:none;' : ''}">
                <div class="offline-workstation-graphic">
                    <div class="monitor-icon">🖥️</div>
                    <div class="signal-radar ${c.is_online ? 'active' : ''}"></div>
                </div>
                <div class="offline-caption">
                    <span class="offline-title">${c.is_online ? 'Menunggu Snapshot Layar...' : 'PC Standby / Offline'}</span>
                    <span class="offline-sub">${c.is_online ? 'Sinyal terhubung, memproses frame' : 'Belum ada transmisi dari PC siswa'}</span>
                    ${!c.is_online ? `<button class="btn-card-wake" data-cid="${c.client_id}" title="Kirim sinyal Wake-on-LAN untuk menghidupkan komputer">⚡ Hidupkan PC</button>` : ''}
                </div>
            </div>
            
            <div class="pc-locked-overlay" id="lock-overlay-${c.client_id}" style="${c.is_locked ? '' : 'display:none;'}" title="Klik untuk Buka Kunci Langsung">
                <div class="lock-overlay-content">
                    <span class="lock-emblem">🔒</span>
                    <span class="lock-overlay-text">LAYAR DIKUNCI GURU</span>
                    <span class="lock-overlay-sub">Klik untuk Buka Kunci 🔓</span>
                </div>
            </div>

            <div class="pc-offline-overlay" id="offline-overlay-${c.client_id}" style="${!c.is_online && thumbSrc ? '' : 'display:none;'}">
                <div class="offline-overlay-pill">
                    <span>⚪ PC STANDBY / OFFLINE</span>
                </div>
            </div>

            <div class="thumbnail-hover-hint">
                <span class="hint-icon">🔍</span>
                <span>Perbesar Layar</span>
            </div>
        </div>

        <div class="pc-telemetry-panel">
            <div class="telemetry-bar-row">
                <div class="telemetry-meta">
                    <span class="metric-label">BEBAN CPU</span>
                    <span class="metric-val ${c.is_online ? cpuClass : 'dimmed'}" id="cpu-val-${c.client_id}">${c.is_online ? cpuPct + '%' : '0% (Mati)'}</span>
                </div>
                <div class="progress-track">
                    <div class="progress-fill ${c.is_online ? cpuClass : 'dimmed'}" id="cpu-bar-${c.client_id}" style="width: ${c.is_online ? Math.min(cpuPct, 100) : 0}%;"></div>
                </div>
            </div>

            <div class="telemetry-bar-row">
                <div class="telemetry-meta">
                    <span class="metric-label">MEMORI RAM (${ramUsedGb} / ${ramTotalGb} GB)</span>
                    <span class="metric-val ${c.is_online ? ramClass : 'dimmed'}" id="ram-val-${c.client_id}">${c.is_online ? ramPct + '%' : 'Standby'}</span>
                </div>
                <div class="progress-track">
                    <div class="progress-fill ${c.is_online ? ramClass : 'dimmed'}" id="ram-bar-${c.client_id}" style="width: ${c.is_online ? Math.min(ramPct, 100) : 0}%;"></div>
                </div>
            </div>
        </div>

        <div class="pc-app-strip ${(hasViolation || isGame) && c.is_online ? 'app-game-alert' : ''}">
            <span class="app-icon">${c.is_online ? ((hasViolation || isGame) ? '🚨' : '⚡') : '⚪'}</span>
            <span class="app-title-text" id="app-${c.client_id}" title="${escapeHtml(c.active_app || 'Desktop / Standby')}">
                ${escapeHtml(c.is_online ? (c.active_app || 'Desktop / Standby') : 'Komputer Dimatikan / Standby')}
            </span>
            ${(hasViolation || isGame) && c.is_online ? `<span class="badge-game-tag">🚨 ${escapeHtml(violationKeyword || 'GAME')}</span>` : ''}
        </div>

        ${hasViolation && c.is_online ? `
        <div class="card-violation-strip" id="violation-strip-${c.client_id}">
            <span class="violation-label">🚨 Membuka: <strong>${escapeHtml(c.violation.keyword)}</strong></span>
            <button class="btn-violation-quick-kill" onclick="quickKillViolation('${c.client_id}', '${escapeHtml(c.active_app || '').replace(/'/g, "\\'")}')" title="Hentikan game/aplikasi ini sekarang">⚡ Tutup</button>
        </div>` : ''}

        <div class="pc-card-actions">
            ${c.is_online ? `
            <button class="btn-card-btn btn-action-remote text-cyan" data-cid="${c.client_id}" title="Remote Control Interaktif (Mouse & Keyboard)">
                <span>🎮 Remote</span>
            </button>
            <button class="btn-card-btn btn-action-lock ${c.is_locked ? 'is-locked' : ''}" data-cid="${c.client_id}" title="${c.is_locked ? 'Buka Kunci Layar' : 'Kunci Layar'}">
                <span>${c.is_locked ? '🔓 Buka' : '🔒 Kunci'}</span>
            </button>
            <button class="btn-card-btn btn-action-msg" data-cid="${c.client_id}" title="Kirim Pesan Pop-up">
                <span>💬 Pesan</span>
            </button>
            <button class="btn-card-btn btn-action-sendfile text-amber" data-cid="${c.client_id}" title="Kirim Berkas / Materi ke PC ini">
                <span>📁 File</span>
            </button>
            ` : `
            <button class="btn-card-btn btn-action-wake text-emerald" data-cid="${c.client_id}" title="Kirim Sinyal Wake-on-LAN untuk menghidupkan komputer">
                <span>⚡ Hidupkan</span>
            </button>
            <button class="btn-card-btn btn-action-delete text-rose" data-cid="${c.client_id}" title="Hapus PC dari Dashboard (jika tidak lagi terpasang agen)">
                <span>🗑️ Hapus</span>
            </button>
            `}
            <button class="btn-card-btn btn-action-inspect" data-cid="${c.client_id}" title="Periksa Rinci & Task Manager">
                <span>⚙️ Rincian</span>
            </button>
            <button class="btn-card-btn btn-action-power" data-cid="${c.client_id}" title="Opsi Daya">
                <span>⏻</span>
            </button>
        </div>
    `;

    // Bind card events
    bindCardEvents(card, c.client_id);
    return card;
}

function bindCardEvents(card, cid) {
    // Rename button & Hostname click
    const btnRename = card.querySelector('.btn-rename-quick');
    if (btnRename) {
        btnRename.onclick = (e) => {
            e.stopPropagation();
            openRenameModal(cid);
        };
    }
    const pcNameText = card.querySelector('.pc-name-text');
    if (pcNameText) {
        pcNameText.onclick = (e) => {
            e.stopPropagation();
            openRenameModal(cid);
        };
    }

    // Checkbox change
    const chk = card.querySelector('.pc-card-checkbox');
    if (chk) {
        chk.onchange = (e) => {
            if (e.target.checked) {
                state.selectedClientIds.add(cid);
                card.classList.add('selected');
            } else {
                state.selectedClientIds.delete(cid);
                card.classList.remove('selected');
            }
            updateSelectionPill();
        };
    }

    // Thumbnail click -> inspect modal
    const thumbBox = card.querySelector('.pc-thumbnail-wrapper');
    if (thumbBox) {
        thumbBox.onclick = () => openInspectModal(cid);
    }

    // Direct 1-click unlock on lock overlay
    const lockOverlay = card.querySelector('.pc-locked-overlay');
    if (lockOverlay) {
        lockOverlay.onclick = (e) => {
            e.stopPropagation();
            const client = state.clients.get(cid);
            if (!client) return;
            client.is_locked = 0;
            updateOrAddClientCard(client);
            if (state.activeInspectId === cid) updateInspectModalContent(client);
            sendCommand(cid, 'unlock', {});
            showToast(`Membuka kunci layar ${getClientDisplayName(client)}...`, 'success');
        };
    }

    // Bind action buttons
    bindCardActionButtons(card, cid);
}

function bindCardActionButtons(card, cid) {
    const btnRemote = card.querySelector('.btn-action-remote');
    if (btnRemote) {
        btnRemote.onclick = (e) => {
            e.stopPropagation();
            openRemoteControl(cid);
        };
    }

    const btnLock = card.querySelector('.btn-action-lock');
    if (btnLock) {
        btnLock.onclick = (e) => {
            e.stopPropagation();
            const client = state.clients.get(cid);
            if (!client) return;
            const btnText = (btnLock.textContent || '').toLowerCase();
            const wantsToUnlock = Boolean(client.is_locked) || btnText.includes('buka') || btnLock.classList.contains('is-locked');
            const newAction = wantsToUnlock ? 'unlock' : 'lock';

            // Optimistic update
            client.is_locked = wantsToUnlock ? 0 : 1;
            updateOrAddClientCard(client);
            if (state.activeInspectId === cid) updateInspectModalContent(client);

            sendCommand(cid, newAction, { message: 'Layar dikunci oleh Guru / Administrator' });
            showToast(wantsToUnlock ? `Membuka kunci ${getClientDisplayName(client)}...` : `Mengunci layar ${getClientDisplayName(client)}...`, wantsToUnlock ? 'success' : 'warning');
        };
    }

    const btnMsg = card.querySelector('.btn-action-msg');
    if (btnMsg) {
        btnMsg.onclick = (e) => {
            e.stopPropagation();
            openBroadcastModalForSingle(cid);
        };
    }

    const btnSendFile = card.querySelector('.btn-action-sendfile');
    if (btnSendFile) {
        btnSendFile.onclick = (e) => {
            e.stopPropagation();
            openSendFileModalForSingle(cid);
        };
    }

    const btnInspect = card.querySelector('.btn-action-inspect');
    if (btnInspect) {
        btnInspect.onclick = (e) => {
            e.stopPropagation();
            openInspectModal(cid);
        };
    }

    const btnPower = card.querySelector('.btn-action-power');
    if (btnPower) {
        btnPower.onclick = (e) => {
            e.stopPropagation();
            state.selectedClientIds.clear();
            state.selectedClientIds.add(cid);
            updateSelectionPill();
            openPowerModal();
        };
    }

    const btnWake = card.querySelector('.btn-action-wake');
    if (btnWake) {
        btnWake.onclick = (e) => {
            e.stopPropagation();
            const client = state.clients.get(cid);
            const name = client ? (client.hostname || cid) : cid;
            sendCommand(cid, 'wake');
            showToast(`Sinyal Wake-on-LAN dikirim ke ${name}`, 'success');
        };
    }

    const btnQuickWake = card.querySelector('.btn-card-wake');
    if (btnQuickWake) {
        btnQuickWake.onclick = (e) => {
            e.stopPropagation();
            const client = state.clients.get(cid);
            const name = client ? (client.hostname || cid) : cid;
            sendCommand(cid, 'wake');
            showToast(`Sinyal Wake-on-LAN dikirim ke ${name}`, 'success');
        };
    }

    const btnDelete = card.querySelector('.btn-action-delete');
    if (btnDelete) {
        btnDelete.onclick = async (e) => {
            e.stopPropagation();
            const client = state.clients.get(cid);
            const name = client ? (client.hostname || cid) : cid;
            if (confirm(`Hapus komputer "${name}" dari dashboard?\n\nKomputer hanya akan muncul kembali jika aplikasi agen dijalankan di komputer tersebut.`)) {
                try {
                    const res = await fetch(`/api/client/${encodeURIComponent(cid)}`, { method: 'DELETE' });
                    if (!res.ok) {
                        showToast('Gagal menghapus komputer.', 'danger');
                    }
                } catch (err) {
                    showToast('Gagal menghapus: ' + err.message, 'danger');
                }
            }
        };
    }
}


// --- Incremental DOM Updaters (Prevent layout thrashing & jitter) ---
function updateOrAddClientCard(client) {
    const grid = document.getElementById('clientsGrid');
    const emptyState = document.getElementById('emptyState');
    if (emptyState) emptyState.style.display = 'none';

    let card = document.getElementById(`pc-card-${client.client_id}`);
    const visible = isClientVisible(client);

    if (!card) {
        card = createClientCardElement(client);
        card.style.display = visible ? '' : 'none';
        grid.appendChild(card);
        reorderGridCards();
    } else {
        card.style.display = visible ? '' : 'none';
        card.className = `pc-card ${client.is_online ? 'online' : 'offline'} ${client.is_locked ? 'locked' : ''} ${state.selectedClientIds.has(client.client_id) ? 'selected' : ''}`;
        
        // Update PC Name text if changed
        const nameText = document.getElementById(`pc-name-text-${client.client_id}`);
        const displayName = getClientDisplayName(client);
        if (nameText && nameText.textContent !== displayName) {
            nameText.textContent = displayName;
        }
        
        // Status badge container
        const statusContainer = document.getElementById(`status-container-${client.client_id}`);
        if (statusContainer) {
            statusContainer.innerHTML = client.is_online 
                ? `<span class="badge-status online"><span class="pulse-indicator"></span>ONLINE</span>`
                : `<span class="badge-status offline"><span class="dormant-dot"></span>OFFLINE</span>`;
        }

        // Lock overlay
        const lockOverlay = document.getElementById(`lock-overlay-${client.client_id}`);
        if (lockOverlay) lockOverlay.style.display = client.is_locked ? '' : 'none';

        // Refresh actions row to ensure Remote and Wake buttons are in sync
        const actionsContainer = card.querySelector('.pc-card-actions');
        if (actionsContainer) {
            actionsContainer.innerHTML = `
                ${client.is_online ? `
                <button class="btn-card-btn btn-action-remote text-cyan" data-cid="${client.client_id}" title="Remote Control Interaktif (Mouse & Keyboard)">
                    <span>🎮 Remote</span>
                </button>
                <button class="btn-card-btn btn-action-lock ${client.is_locked ? 'is-locked' : ''}" data-cid="${client.client_id}" title="${client.is_locked ? 'Buka Kunci Layar' : 'Kunci Layar'}">
                    <span>${client.is_locked ? '🔓 Buka' : '🔒 Kunci'}</span>
                </button>
                <button class="btn-card-btn btn-action-msg" data-cid="${client.client_id}" title="Kirim Pesan Pop-up">
                    <span>💬 Pesan</span>
                </button>
                <button class="btn-card-btn btn-action-sendfile text-amber" data-cid="${client.client_id}" title="Kirim Berkas / Materi ke PC ini">
                    <span>📁 File</span>
                </button>
                ` : `
                <button class="btn-card-btn btn-action-wake text-emerald" data-cid="${client.client_id}" title="Kirim Sinyal Wake-on-LAN untuk menghidupkan komputer">
                    <span>⚡ Hidupkan</span>
                </button>
                <button class="btn-card-btn btn-action-delete text-rose" data-cid="${client.client_id}" title="Hapus PC dari Dashboard (jika tidak lagi terpasang agen)">
                    <span>🗑️ Hapus</span>
                </button>
                `}
                <button class="btn-card-btn btn-action-inspect" data-cid="${client.client_id}" title="Periksa Rinci & Task Manager">
                    <span>⚙️ Rincian</span>
                </button>
                <button class="btn-card-btn btn-action-power" data-cid="${client.client_id}" title="Opsi Daya">
                    <span>⏻</span>
                </button>
            `;
            bindCardActionButtons(card, client.client_id);
        }

        // Offline overlay
        const offOverlay = document.getElementById(`offline-overlay-${client.client_id}`);
        if (offOverlay) {
            offOverlay.style.display = (!client.is_online && state.thumbnails.has(client.client_id)) ? '' : 'none';
        }

        if (!client.is_online) {
            const cpuVal = document.getElementById(`cpu-val-${client.client_id}`);
            const cpuBar = document.getElementById(`cpu-bar-${client.client_id}`);
            const ramVal = document.getElementById(`ram-val-${client.client_id}`);
            const ramBar = document.getElementById(`ram-bar-${client.client_id}`);
            const appEl = document.getElementById(`app-${client.client_id}`);
            if (cpuVal) { cpuVal.textContent = '0% (Mati)'; cpuVal.className = 'metric-val dimmed'; }
            if (cpuBar) { cpuBar.style.width = '0%'; cpuBar.className = 'progress-fill dimmed'; }
            if (ramVal) { ramVal.textContent = 'Standby'; ramVal.className = 'metric-val dimmed'; }
            if (ramBar) { ramBar.style.width = '0%'; ramBar.className = 'progress-fill dimmed'; }
            if (appEl) appEl.textContent = 'Komputer Dimatikan / Standby';
        }

        // Keep thumbnail in sync if cached
        if (state.thumbnails.has(client.client_id)) {
            updateCardThumbnail(client.client_id, state.thumbnails.get(client.client_id));
        }
    }
    updateOrAddSeatingCard(client);
}

function updateCardTelemetry(cid, client) {
    updateSeatingCardTelemetry(cid, client);
    const healthWrap = document.getElementById(`health-badge-wrap-${cid}`);
    if (healthWrap) {
        healthWrap.innerHTML = createHealthBadgeHtml(client);
    }
    if (state.activeInspectId === cid) {
        updateInspectModalHealth(client);
    }
    if (!client.is_online) return;
    const cpuPct = Math.round(client.cpu_pct || 0);
    const ramPct = Math.round(client.ram_pct || 0);
    const cpuClass = cpuPct > 80 ? 'danger' : (cpuPct > 60 ? 'warning' : 'normal');
    const ramClass = ramPct > 85 ? 'danger' : (ramPct > 70 ? 'warning' : 'normal');

    const cpuVal = document.getElementById(`cpu-val-${cid}`);
    if (cpuVal) {
        cpuVal.textContent = `${cpuPct}%`;
        cpuVal.className = `metric-val ${cpuClass}`;
    }

    const cpuBar = document.getElementById(`cpu-bar-${cid}`);
    if (cpuBar) {
        cpuBar.style.width = `${Math.min(cpuPct, 100)}%`;
        cpuBar.className = `progress-fill ${cpuClass}`;
    }

    const ramVal = document.getElementById(`ram-val-${cid}`);
    if (ramVal) {
        ramVal.textContent = `${ramPct}%`;
        ramVal.className = `metric-val ${ramClass}`;
    }

    const ramBar = document.getElementById(`ram-bar-${cid}`);
    if (ramBar) {
        ramBar.style.width = `${Math.min(ramPct, 100)}%`;
        ramBar.className = `progress-fill ${ramClass}`;
    }

    const appEl = document.getElementById(`app-${cid}`);
    const hasViolation = Boolean(client.violation && client.violation.detected);
    const isGame = hasViolation || (client.active_app || '').toLowerCase().includes('roblox') || (client.active_app || '').toLowerCase().includes('game');
    if (appEl) {
        appEl.textContent = client.active_app || (client.is_online ? 'Desktop / Standby' : 'PC Offline');
        const parentStrip = appEl.closest('.pc-app-strip');
        if (parentStrip) {
            parentStrip.classList.toggle('app-game-alert', isGame);
        }
    }
    const card = document.getElementById(`pc-card-${cid}`);
    if (card) {
        card.classList.toggle('card-violation', hasViolation);
        card.classList.toggle('locked', Boolean(client.is_locked));
    }
    const lockOverlay = document.getElementById(`lock-overlay-${cid}`);
    if (lockOverlay) lockOverlay.style.display = client.is_locked ? '' : 'none';
    const lockBtn = document.querySelector(`.btn-action-lock[data-cid="${cid}"]`);
    if (lockBtn) {
        lockBtn.classList.toggle('is-locked', Boolean(client.is_locked));
        const span = lockBtn.querySelector('span');
        if (span) span.textContent = client.is_locked ? '🔓 Buka' : '🔒 Kunci';
    }
}

function updateCardThumbnail(cid, base64Thumb) {
    updateSeatingCardThumbnail(cid, base64Thumb);
    const thumbImg = document.getElementById(`thumb-${cid}`);
    const placeholder = document.getElementById(`placeholder-${cid}`);
    if (thumbImg && placeholder) {
        if (base64Thumb) {
            thumbImg.src = `data:image/jpeg;base64,${base64Thumb}`;
            thumbImg.style.display = 'block';
            placeholder.style.display = 'none';
        } else {
            thumbImg.style.display = 'none';
            placeholder.style.display = 'flex';
        }
    }
}

// --- Full Grid Re-render ---
function renderClients() {
    const grid = document.getElementById('clientsGrid');
    const emptyState = document.getElementById('emptyState');
    
    // Clear dynamic cards (keep empty state element)
    const existingCards = grid.querySelectorAll('.pc-card');
    existingCards.forEach(c => c.remove());

    const clientList = Array.from(state.clients.values());
    const visibleClients = sortClients(clientList.filter(isClientVisible));

    if (visibleClients.length === 0) {
        if (emptyState) emptyState.style.display = 'flex';
    } else {
        if (emptyState) emptyState.style.display = 'none';
        visibleClients.forEach(c => {
            const card = createClientCardElement(c);
            grid.appendChild(card);
        });
    }

    renderTable();
    if (state.viewMode === 'seating') {
        renderSeatingPlan();
    }
    updateSelectionPill();
}

// --- Table View Renderer ---
function renderTable() {
    const tbody = document.getElementById('clientsTableBody');
    if (!tbody) return;
    tbody.innerHTML = '';

    const visibleClients = sortClients(Array.from(state.clients.values()).filter(isClientVisible));

    visibleClients.forEach(c => {
        const isSelected = state.selectedClientIds.has(c.client_id);
        const displayName = getClientDisplayName(c);
        const hasCustomName = Boolean(c.custom_name && c.custom_name.trim() && c.custom_name.trim() !== c.hostname);
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>
                <label class="custom-checkbox">
                    <input type="checkbox" class="table-row-chk" data-cid="${c.client_id}" ${isSelected ? 'checked' : ''}>
                    <span class="checkbox-mark"></span>
                </label>
            </td>
            <td>
                <div style="display:inline-flex;align-items:center;gap:6px;">
                    <strong>${escapeHtml(displayName)}</strong>
                    ${hasCustomName ? `<span class="badge-custom-tag" title="Nama Asli Windows: ${escapeHtml(c.hostname)}">🏷️</span>` : ''}
                    <button class="btn-rename-quick" data-cid="${c.client_id}" title="Ubah Nama">✏️</button>
                </div>
            </td>
            <td><span class="pc-room-tag">${escapeHtml(c.lab_room || 'Lab Umum')}</span></td>
            <td><span class="font-mono text-muted">${c.ip_address || '127.0.0.1'}</span></td>
            <td>
                <div style="display:inline-flex;flex-direction:column;gap:3px;align-items:flex-start;">
                    <span class="status-indicator-pill ${c.is_online ? 'text-emerald' : 'text-slate'}">
                        ${c.is_online ? '🟢 Online' : '⚪ Offline'}
                    </span>
                    ${createHealthBadgeHtml(c)}
                </div>
            </td>
            <td><span class="font-mono">${c.cpu_pct || 0}%</span></td>
            <td><span class="font-mono">${c.ram_pct || 0}%</span></td>
            <td><span class="text-secondary">${escapeHtml(c.active_app || '-')}</span></td>
            <td><span>${c.is_locked ? '🔒 Dikunci' : '🔓 Terbuka'}</span></td>
            <td style="text-align: right;">
                ${c.is_online ? `<button class="btn-subtle-action text-cyan" onclick="openRemoteControl('${c.client_id}')">🎮 Remote</button>` : ''}
                <button class="btn-subtle-action" onclick="openInspectModal('${c.client_id}')">⚙️ Detail</button>
            </td>
        `;

        const btnRename = tr.querySelector('.btn-rename-quick');
        if (btnRename) {
            btnRename.addEventListener('click', (e) => {
                e.stopPropagation();
                openRenameModal(c.client_id);
            });
        }

        const chk = tr.querySelector('.table-row-chk');
        if (chk) {
            chk.addEventListener('change', (e) => {
                if (e.target.checked) state.selectedClientIds.add(c.client_id);
                else state.selectedClientIds.delete(c.client_id);
                updateSelectionPill();
                // Sync grid card checkbox
                const gridChk = document.querySelector(`#pc-card-${c.client_id} .pc-card-checkbox`);
                if (gridChk) gridChk.checked = e.target.checked;
            });
        }

        tbody.appendChild(tr);
    });
}

// --- Selection Management ---
function updateSelectionPill() {
    const badge = document.getElementById('selectedCountBadge');
    if (badge) badge.textContent = state.selectedClientIds.size;

    const selectAllCheckbox = document.getElementById('selectAllCheckbox');
    const tableHeaderCheckbox = document.getElementById('tableHeaderCheckbox');
    const totalVisible = Array.from(state.clients.values()).filter(isClientVisible).length;

    const allSelected = totalVisible > 0 && state.selectedClientIds.size >= totalVisible;
    if (selectAllCheckbox) selectAllCheckbox.checked = allSelected;
    if (tableHeaderCheckbox) tableHeaderCheckbox.checked = allSelected;

    // Sync seating cards selection state
    document.querySelectorAll('.seating-pc-card').forEach(card => {
        const cid = card.dataset.cid;
        const isSel = state.selectedClientIds.has(cid);
        card.classList.toggle('selected', isSel);
        const chk = card.querySelector('.seating-card-chk');
        if (chk) chk.checked = isSel;
    });
}

function getTargetForAction() {
    if (state.selectedClientIds.size === 0) {
        if (state.roomFilter && state.roomFilter !== 'all') {
            return `room:${state.roomFilter}`;
        }
        return 'all';
    }
    return Array.from(state.selectedClientIds);
}

function getTargetDescription() {
    if (state.selectedClientIds.size === 0) {
        if (state.roomFilter && state.roomFilter !== 'all') {
            return `Semua Komputer (${state.roomFilter})`;
        }
        return 'Semua Komputer Siswa (Seluruh Lab)';
    }
    return `${state.selectedClientIds.size} Komputer Terpilih`;
}

// --- Modals Management ---
function openModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) modal.style.display = 'flex';
}

function closeModal(modalId) {
    const modal = document.getElementById(modalId);
    if (modal) modal.style.display = 'none';
}

// Modal 1: Broadcast
function openBroadcastModal() {
    setHTML('broadcastTargetSummary', `Target: <strong>${getTargetDescription()}</strong>`);
    openModal('modalBroadcast');
}

function openBroadcastModalForSingle(cid) {
    state.selectedClientIds.clear();
    state.selectedClientIds.add(cid);
    updateSelectionPill();
    setHTML('broadcastTargetSummary', `Target: <strong>Komputer ${cid}</strong>`);
    openModal('modalBroadcast');
}

// --- Modal: Rename PC (Ubah Nama / Alias Komputer) ---
function openRenameModal(cid) {
    const client = state.clients.get(cid);
    if (!client) return;

    state.renameTargetCid = cid;
    const origHost = client.hostname || cid;
    setText('renameOriginalHostname', origHost);
    setText('renameClientIp', client.ip_address || '127.0.0.1');
    setText('renameClientRoom', client.lab_room || 'Lab Umum');

    const input = document.getElementById('inputNewPcName');
    if (input) {
        input.value = client.custom_name || '';
    }

    const chkWin = document.getElementById('chkRenameWindows');
    if (chkWin) {
        chkWin.checked = false;
        chkWin.disabled = !client.is_online;
        const optWrap = document.querySelector('.rename-windows-opt');
        if (optWrap) {
            optWrap.style.opacity = client.is_online ? '1' : '0.55';
        }
    }

    openModal('modalRenamePc');
    setTimeout(() => {
        if (input) {
            input.focus();
            input.select();
        }
    }, 150);
}

async function submitRenamePc() {
    const cid = state.renameTargetCid;
    if (!cid) return;
    const client = state.clients.get(cid);
    if (!client) return;

    const input = document.getElementById('inputNewPcName');
    const chkWin = document.getElementById('chkRenameWindows');
    const newName = input ? input.value.trim() : '';
    const renameWindows = chkWin ? chkWin.checked : false;

    const btnSubmit = document.getElementById('btnSubmitRenamePc');
    if (btnSubmit) {
        btnSubmit.disabled = true;
        btnSubmit.innerHTML = '<span>Menyimpan...</span>';
    }

    try {
        const res = await fetch(`/api/client/${encodeURIComponent(cid)}/rename`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ custom_name: newName, rename_windows: renameWindows })
        });
        const data = await res.json();
        if (res.ok) {
            closeModal('modalRenamePc');
            client.custom_name = data.custom_name || '';
            client.display_name = data.display_name || client.hostname;
            updateOrAddClientCard(client);
            reorderGridCards();
            if (state.viewMode === 'list') renderTable();
            if (state.viewMode === 'seating') renderSeatingPlan();
            if (state.activeInspectId === cid) {
                setText('inspectPcTitle', data.display_name);
            }

            let msg = `Nama komputer berhasil diubah menjadi <strong>"${escapeHtml(data.display_name)}"</strong>`;
            if (data.windows_cmd_sent) {
                msg += '<br><small>Perintah ganti nama Windows telah dikirim ke komputer siswa (akan berubah permanen setelah restart).</small>';
            }
            showToast(msg, 'success');
            playSound('blip');
        } else {
            showToast(data.detail || 'Gagal mengubah nama komputer', 'danger');
        }
    } catch (e) {
        showToast('Terjadi kesalahan jaringan saat mengubah nama komputer', 'danger');
    } finally {
        if (btnSubmit) {
            btnSubmit.disabled = false;
            btnSubmit.innerHTML = '<span>💾 Simpan Perubahan Nama</span>';
        }
    }
}

// Modal: Send File / Distribusi Berkas ke Siswa
let currentSendFileObject = null;
let currentSendFileTarget = 'all';

function openSendFileModal(target = null) {
    if (target) {
        currentSendFileTarget = target;
    } else {
        currentSendFileTarget = getTargetForAction();
    }

    let targetDesc = getTargetDescription();
    if (typeof currentSendFileTarget === 'string' && currentSendFileTarget !== 'all' && !currentSendFileTarget.startsWith('room:')) {
        targetDesc = `Komputer ${currentSendFileTarget}`;
    }
    setHTML('sendFileTargetSummary', `Target Penerima: <strong>${targetDesc}</strong>`);

    resetSendFileForm();
    openModal('modalSendFile');
}

function openSendFileModalForSingle(cid) {
    currentSendFileTarget = [cid];
    setHTML('sendFileTargetSummary', `Target Penerima: <strong>Komputer ${cid}</strong>`);
    resetSendFileForm();
    openModal('modalSendFile');
}

function resetSendFileForm() {
    currentSendFileObject = null;
    const fileInput = document.getElementById('fileUploadInput');
    if (fileInput) fileInput.value = '';

    const promptEl = document.getElementById('dropzonePrompt');
    const selectedEl = document.getElementById('dropzoneSelected');
    const submitBtn = document.getElementById('btnSubmitSendFile');
    const progressBox = document.getElementById('transferProgressBox');

    if (promptEl) promptEl.style.display = 'block';
    if (selectedEl) selectedEl.style.display = 'none';
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<span>🚀 Kirim File ke Siswa</span>';
    }
    if (progressBox) progressBox.style.display = 'none';
}

function handleFileSelection(file) {
    if (!file) return;
    currentSendFileObject = file;

    const promptEl = document.getElementById('dropzonePrompt');
    const selectedEl = document.getElementById('dropzoneSelected');
    const nameEl = document.getElementById('selectedFileName');
    const sizeEl = document.getElementById('selectedFileSize');
    const extEl = document.getElementById('selectedFileExt');
    const submitBtn = document.getElementById('btnSubmitSendFile');

    if (promptEl) promptEl.style.display = 'none';
    if (selectedEl) selectedEl.style.display = 'flex';

    if (nameEl) nameEl.textContent = file.name;
    if (sizeEl) {
        const sizeKb = (file.size / 1024).toFixed(1);
        const sizeMb = (file.size / (1024 * 1024)).toFixed(2);
        sizeEl.textContent = file.size > 1024 * 1024 ? `${sizeMb} MB` : `${sizeKb} KB`;
    }
    if (extEl) {
        const parts = file.name.split('.');
        const ext = parts.length > 1 ? parts.pop().toUpperCase() : 'FILE';
        extEl.textContent = ext.substring(0, 5);
    }
    if (submitBtn) submitBtn.disabled = false;
}

function updateInspectModalHealth(client) {
    if (!client) return;
    const badgeWrap = document.getElementById('inspectHealthBadge');
    if (badgeWrap) {
        badgeWrap.innerHTML = createHealthBadgeHtml(client);
    }

    const diskFree = (client.disk_free_gb !== undefined && client.disk_free_gb !== null && client.disk_free_gb > 0) 
        ? client.disk_free_gb 
        : ((100 - (client.disk_pct || 40)) * 1.2).toFixed(1);
    const diskTotal = client.disk_total_gb || 120;
    const diskPct = Math.round(client.disk_pct || 40);
    setText('inspectDiskVal', `Sisa ${diskFree} GB / ${diskTotal} GB (${diskPct}% Terpakai)`);

    const diskBar = document.getElementById('inspectDiskBar');
    if (diskBar) {
        diskBar.style.width = `${Math.min(100, diskPct)}%`;
        diskBar.className = `diag-bar-fill ${diskPct > 90 ? 'crit' : (diskPct > 80 ? 'warn' : '')}`;
    }

    const uptimeStr = formatUptime(client.uptime_seconds);
    setText('inspectUptimeVal', `Menyala ${uptimeStr}`);

    const uptimeSub = document.getElementById('inspectUptimeSub');
    if (uptimeSub) {
        if (client.uptime_seconds && client.uptime_seconds > 0) {
            const bootDate = new Date(Date.now() - (client.uptime_seconds * 1000));
            const hrsStr = String(bootDate.getHours()).padStart(2, '0');
            const minStr = String(bootDate.getMinutes()).padStart(2, '0');
            uptimeSub.textContent = `Boot: ${bootDate.toLocaleDateString('id-ID')} ${hrsStr}:${minStr} WIB`;
        } else {
            uptimeSub.textContent = 'Boot: Terkini';
        }
    }

    const notesEl = document.getElementById('inspectHealthNotes');
    if (notesEl) {
        if (client.health_issues && client.health_issues.length > 0) {
            notesEl.innerHTML = `<span style="color:#f43f5e;font-weight:700;">⚠️ Terdeteksi: ${escapeHtml(client.health_issues.join(' · '))}</span>`;
        } else {
            notesEl.textContent = 'Semua metrik sistem bekerja prima dalam batas normal.';
        }
    }

    const fpsMode = client.fps_mode || (client.is_online ? '1.0s (Aktif)' : '-');
    const isIdle = fpsMode.includes('Hemat') || fpsMode.includes('4.5s');
    setText('inspectFpsModeVal', client.is_online ? (isIdle ? '🍃 4.5s (Hemat Bandwidth LAN)' : '🟢 1.0s (Aktif Responsif)') : 'Offline / Standby');
    const idleSeconds = Math.round(client.idle_seconds || 0);
    setText('inspectFpsModeSub', client.is_online ? (isIdle ? `Layar statis & keyboard/mouse idle (${idleSeconds}s)` : 'Siswa aktif bergerak / mengetik') : 'Transmisi layar dijeda');
}

// Modal 2: Inspect PC
function openInspectModal(cid) {
    const client = state.clients.get(cid);
    if (!client) return;

    state.activeInspectId = cid;
    setText('inspectPcTitle', getClientDisplayName(client));
    setText('inspectIp', client.ip_address || '127.0.0.1');
    setText('inspectMac', client.mac_address || 'Belum Tercatat');
    setText('inspectRoom', client.lab_room || 'Lab Umum');
    setText('inspectOs', client.os_info || 'Windows');
    setText('inspectRamTotal', client.ram_total_mb ? `${(client.ram_total_mb / 1024).toFixed(1)} GB` : '8 GB');
    const verEl = document.getElementById('inspectAgentVer');
    if (verEl) {
        const ver = client.agent_version || 'v2.1.0';
        verEl.innerHTML = `<span class="badge-agent-ver">${ver}</span>`;
    }

    // Populate Diagnostic Health Card
    updateInspectModalHealth(client);

    const btnCleanTemp = document.getElementById('btnInspectCleanTemp');
    if (btnCleanTemp) {
        btnCleanTemp.onclick = async () => {
            if (!confirm(`Jalankan pembersihan cache & temporary files pada ${getClientDisplayName(client)}?`)) return;
            btnCleanTemp.disabled = true;
            btnCleanTemp.textContent = '⏳ Membersihkan...';
            sendCommand(cid, 'clean_temp');
            showToast(`Perintah pembersihan sampah dikirim ke ${getClientDisplayName(client)}...`, 'info');
            setTimeout(() => {
                if (btnCleanTemp) {
                    btnCleanTemp.disabled = false;
                    btnCleanTemp.textContent = '🧹 Bersihkan Cache & Temp (1-Klik)';
                }
            }, 3000);
        };
    }

    const lockBadge = document.getElementById('inspectLockedBadge');
    if (lockBadge) lockBadge.style.display = client.is_locked ? 'flex' : 'none';

    const btnLockToggle = document.getElementById('btnInspectLockToggle');
    if (btnLockToggle) btnLockToggle.textContent = client.is_locked ? '🔓 Buka Kunci' : '🔒 Kunci Layar';

    const largeScreen = document.getElementById('inspectLargeScreen');
    const thumb = state.thumbnails.get(cid);
    if (largeScreen && thumb) {
        largeScreen.src = `data:image/jpeg;base64,${thumb}`;
    }

    // Reset count badges
    const runningBadge = document.getElementById('inspectRunningCount');
    if (runningBadge) runningBadge.textContent = '...';
    const installedBadge = document.getElementById('inspectInstalledCount');
    if (installedBadge) {
        const cached = state.clientInstalledApps.get(cid);
        installedBadge.textContent = (cached && cached.length) ? cached.length : '...';
    }

    // Load processes and installed applications
    loadClientProcesses(cid);
    loadClientInstalledApps(cid);
    switchInspectTab(state.activeInspectTab || 'running');
    openModal('modalInspect');
}

function updateInspectModalContent(client) {
    if (!client || state.activeInspectId !== client.client_id) return;
    const lockBadge = document.getElementById('inspectLockedBadge');
    if (lockBadge) lockBadge.style.display = client.is_locked ? 'flex' : 'none';

    const btnLockToggle = document.getElementById('btnInspectLockToggle');
    if (btnLockToggle) {
        btnLockToggle.textContent = client.is_locked ? '🔓 Buka Kunci' : '🔒 Kunci Layar';
        btnLockToggle.title = client.is_locked ? 'Klik untuk membuka kunci layar PC siswa ini' : 'Kunci layar PC siswa ini';
        if (client.is_locked) {
            btnLockToggle.style.borderColor = 'var(--emerald, #10b981)';
            btnLockToggle.style.color = 'var(--emerald, #10b981)';
        } else {
            btnLockToggle.style.borderColor = '';
            btnLockToggle.style.color = '';
        }
    }
}

// --- Inspect Dual-Tab Switcher ---
function switchInspectTab(tab) {
    state.activeInspectTab = tab;
    const tabBtnRunning = document.getElementById('tabBtnRunning');
    const tabBtnInstalled = document.getElementById('tabBtnInstalled');
    const tabBtnUpdates = document.getElementById('tabBtnUpdates');
    const contentRunning = document.getElementById('inspectTabRunningContent');
    const contentInstalled = document.getElementById('inspectTabInstalledContent');
    const contentUpdates = document.getElementById('inspectTabUpdatesContent');

    // Reset all tabs
    [tabBtnRunning, tabBtnInstalled, tabBtnUpdates].forEach(btn => { if (btn) btn.classList.remove('active'); });
    [contentRunning, contentInstalled, contentUpdates].forEach(el => { if (el) el.style.display = 'none'; });

    if (tab === 'installed') {
        if (tabBtnInstalled) tabBtnInstalled.classList.add('active');
        if (contentInstalled) contentInstalled.style.display = 'block';
        if (state.activeInspectId) {
            renderInspectInstalledApps(state.activeInspectId);
        }
    } else if (tab === 'updates') {
        if (tabBtnUpdates) tabBtnUpdates.classList.add('active');
        if (contentUpdates) contentUpdates.style.display = 'block';
        if (state.activeInspectId) {
            renderWingetUpdates(state.activeInspectId);
            renderWindowsUpdates(state.activeInspectId);
        }
    } else {
        if (tabBtnRunning) tabBtnRunning.classList.add('active');
        if (contentRunning) contentRunning.style.display = 'block';
    }
}
window.switchInspectTab = switchInspectTab;

async function loadClientProcesses(cid) {
    const tbody = document.getElementById('inspectProcessTableBody');
    const runningBadge = document.getElementById('inspectRunningCount');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;">Memuat proses...</td></tr>';

    try {
        const resp = await fetch(`/api/client/${encodeURIComponent(cid)}`);
        if (!resp.ok) throw new Error('Failed to fetch details');
        const data = await resp.json();

        // Also cache installed apps if included in detail response
        if (data.installed_apps && (!state.clientInstalledApps.has(cid) || state.clientInstalledApps.get(cid).length === 0)) {
            state.clientInstalledApps.set(cid, data.installed_apps);
            const installedBadge = document.getElementById('inspectInstalledCount');
            if (installedBadge) installedBadge.textContent = data.installed_apps.length;
        }

        tbody.innerHTML = '';
        const procs = data.processes || [];
        if (runningBadge) runningBadge.textContent = procs.length;

        if (procs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:#94a3b8;">Tidak ada data proses aktif</td></tr>';
            return;
        }

        procs.forEach(p => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td><strong>${escapeHtml(p.name)}</strong></td>
                <td><span class="font-mono text-muted">${p.pid}</span></td>
                <td><span class="font-mono">${p.cpu}%</span></td>
                <td><span class="font-mono">${p.memory_mb} MB</span></td>
                <td>
                    <button class="btn-kill-proc" onclick="killProcess('${cid}', ${p.pid}, '${escapeHtml(p.name)}')">Hentikan</button>
                </td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:#f43f5e;">Gagal memuat proses</td></tr>';
        if (runningBadge) runningBadge.textContent = '0';
    }
}

function killProcess(cid, pid, name) {
    if (confirm(`Hentikan proses '${name}' (PID: ${pid}) di ${cid}?`)) {
        sendCommand(cid, 'kill_process', { pid: pid, name: name });
        setTimeout(() => loadClientProcesses(cid), 1500);
    }
}

// --- Installed Software Management on Student PC ---
async function loadClientInstalledApps(cid, forceRefresh = false) {
    const summaryText = document.getElementById('installedAppsSummaryText');
    const badgeEl = document.getElementById('inspectInstalledCount');

    if (forceRefresh) {
        if (summaryText) summaryText.textContent = '🔄 Meminta PC siswa memindai registry & daftar aplikasi...';
        try {
            await fetch(`/api/client/${encodeURIComponent(cid)}/apps/refresh`, { method: 'POST' });
            showToast(`Meminta PC <strong>${cid}</strong> memindai ulang daftar aplikasi terpasang...`, 'info');
        } catch (e) {
            console.error('Refresh apps error:', e);
        }
        return;
    }

    // Use cached list if available
    if (state.clientInstalledApps.has(cid) && state.clientInstalledApps.get(cid).length > 0) {
        renderInspectInstalledApps(cid);
        return;
    }

    if (summaryText) summaryText.textContent = 'Memuat data software terpasang...';

    try {
        const resp = await fetch(`/api/client/${encodeURIComponent(cid)}/apps`);
        if (!resp.ok) throw new Error('Gagal memuat aplikasi');
        const data = await resp.json();
        const apps = data.apps || [];
        state.clientInstalledApps.set(cid, apps);
        if (badgeEl) badgeEl.textContent = apps.length;
        renderInspectInstalledApps(cid);
    } catch (err) {
        if (summaryText) summaryText.textContent = 'Gagal memuat data software terpasang.';
        const tbody = document.getElementById('inspectInstalledAppsTableBody');
        if (tbody) {
            tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color:#f43f5e; padding: 20px;">Gagal memuat daftar aplikasi dari PC siswa.</td></tr>';
        }
    }
}

function getAppCategoryEmoji(name = '') {
    const n = (name || '').toLowerCase();
    if (n.includes('chrome') || n.includes('firefox') || n.includes('edge') || n.includes('browser') || n.includes('opera') || n.includes('brave')) return '🌐';
    if (n.includes('word') || n.includes('docs') || n.includes('writer')) return '📘';
    if (n.includes('excel') || n.includes('sheets') || n.includes('spreadsheet')) return '📗';
    if (n.includes('powerpoint') || n.includes('presentation') || n.includes('slides')) return '📙';
    if (n.includes('code') || n.includes('visual studio') || n.includes('pycharm') || n.includes('sublime') || n.includes('intellij')) return '💻';
    if (n.includes('python') || n.includes('idle')) return '🐍';
    if (n.includes('scratch')) return '🐱';
    if (n.includes('packet tracer') || n.includes('cisco')) return '🛰️';
    if (n.includes('geogebra')) return '📐';
    if (n.includes('photoshop') || n.includes('paint') || n.includes('canva') || n.includes('gimp') || n.includes('corel')) return '🎨';
    if (n.includes('vlc') || n.includes('media player') || n.includes('video') || n.includes('audio')) return '🎬';
    if (n.includes('winrar') || n.includes('7-zip') || n.includes('zip')) return '📦';
    if (n.includes('notion') || n.includes('notepad')) return '📝';
    if (n.includes('zoom') || n.includes('teams') || n.includes('discord')) return '💬';
    if (n.includes('game') || n.includes('roblox') || n.includes('steam') || n.includes('minecraft')) return '🎮';
    return '📦';
}

function renderInspectInstalledApps(cid) {
    if (state.activeInspectId !== cid) return;
    const tbody = document.getElementById('inspectInstalledAppsTableBody');
    const summaryText = document.getElementById('installedAppsSummaryText');
    const countBadge = document.getElementById('inspectInstalledCount');
    if (!tbody) return;

    const allApps = state.clientInstalledApps.get(cid) || [];
    if (countBadge) {
        countBadge.textContent = allApps.length > 0 ? allApps.length : '0';
    }

    if (allApps.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; padding: 24px; color:var(--text-muted);">Belum ada software yang terdeteksi di PC ini. Klik tombol <strong>Refresh</strong> di atas untuk memindai langsung.</td></tr>';
        if (summaryText) summaryText.textContent = '0 software terpasang terdeteksi';
        return;
    }

    const query = (state.installedAppSearchQuery || '').trim().toLowerCase();
    const filtered = allApps.filter(app => {
        if (!query) return true;
        const nameMatch = (app.name || '').toLowerCase().includes(query);
        const pubMatch = (app.publisher || '').toLowerCase().includes(query);
        const verMatch = (app.version || '').toLowerCase().includes(query);
        return nameMatch || pubMatch || verMatch;
    });

    if (summaryText) {
        if (query) {
            summaryText.innerHTML = `Menampilkan <strong>${filtered.length}</strong> dari <strong>${allApps.length}</strong> aplikasi terpasang (pencarian: "<em>${escapeHtml(query)}</em>")`;
        } else {
            summaryText.innerHTML = `Total <strong>${allApps.length}</strong> software / aplikasi terpasang di komputer siswa`;
        }
    }

    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="4" style="text-align:center; padding: 20px; color:var(--text-muted);">Tidak ada aplikasi yang cocok dengan kata kunci "<strong>${escapeHtml(query)}</strong>".</td></tr>`;
        return;
    }

    tbody.innerHTML = '';
    filtered.forEach(app => {
        const tr = document.createElement('tr');
        const icon = getAppCategoryEmoji(app.name);
        const safeName = escapeHtml(app.name || 'Aplikasi Tanpa Nama');
        const safeVer = escapeHtml(app.version || '-');
        const safePub = escapeHtml(app.publisher || 'Windows / Unknown');
        const exeHint = app.exe || app.name;

        tr.innerHTML = `
            <td>
                <div class="app-name-cell" title="${escapeHtml(app.install_location || app.exe || app.name)}">
                    <span class="app-icon-avatar">${icon}</span>
                    <div>
                        <div style="font-weight: 600; color: #0f172a; font-size: 0.8rem;">${safeName}</div>
                        ${app.install_location ? `<div style="font-size: 0.68rem; color: #64748b; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(app.install_location)}</div>` : ''}
                    </div>
                </div>
            </td>
            <td><span class="font-mono text-muted" style="font-size: 0.72rem;">${safeVer}</span></td>
            <td><span style="font-size: 0.75rem; color: #475569;">${safePub}</span></td>
            <td style="text-align: right;">
                <button class="btn-launch-installed-app" title="Luncurkan '${safeName}' di layar komputer siswa ini" onclick="launchInstalledApp('${cid}', '${escapeHtml(exeHint).replace(/'/g, "\\'")}', '${safeName.replace(/'/g, "\\'")}')">
                    🚀 Jalankan
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function launchInstalledApp(cid, appTarget, appDisplayName) {
    if (!cid || !appTarget) return;
    const client = state.clients.get(cid);
    const host = client ? (client.hostname || cid) : cid;

    sendCommand(cid, 'launch_app', { app: appTarget });
    showToast(`🚀 Meluncurkan <strong>${appDisplayName}</strong> di PC <strong>${host}</strong>...`, 'success');
    playSound('blip');
}
window.launchInstalledApp = launchInstalledApp;

// ─── Software & Windows Update Functions ────────────────────────────────────

/** Renders winget pending software updates for a specific PC in the updates tab */
function renderWingetUpdates(cid) {
    const tbody = document.getElementById('wingetUpdatesTableBody');
    const badge = document.getElementById('wingetUpdatesBadge');
    const summary = document.getElementById('wingetUpdatesSummary');
    if (!tbody) return;

    state.clientWingetUpdates = state.clientWingetUpdates || new Map();
    const updates = state.clientWingetUpdates.get(cid);

    if (!updates) {
        // No data fetched yet
        if (badge) badge.textContent = '...';
        tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-muted);padding:16px;">Belum ada data. Klik <strong>Cek Update</strong> untuk memeriksa.</td></tr>';
        if (summary) summary.innerHTML = 'Klik <strong>Cek Update</strong> untuk memeriksa pembaruan software di PC ini.';
        return;
    }

    if (badge) badge.textContent = updates.length;
    if (summary) {
        if (updates.length === 0) {
            summary.innerHTML = '✅ <strong>Semua software sudah terbaru</strong> di PC ini.';
        } else {
            summary.innerHTML = `⚠️ Ditemukan <strong>${updates.length}</strong> software yang dapat diperbarui.`;
        }
    }

    if (updates.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#22c55e;padding:16px;">✅ Tidak ada update tersedia — semua software sudah terbaru!</td></tr>';
        return;
    }

    tbody.innerHTML = '';
    updates.forEach(u => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><strong style="font-size:0.82rem;">${escapeHtml(u.name || u.id || '-')}</strong>
                <div style="font-size:0.7rem;color:var(--text-muted);font-family:monospace;">${escapeHtml(u.id || '')}</div></td>
            <td><span class="font-mono" style="font-size:0.75rem;color:#f59e0b;">${escapeHtml(u.version || '-')}</span></td>
            <td><span class="font-mono" style="font-size:0.75rem;color:#22c55e;">${escapeHtml(u.available_version || '-')}</span></td>
            <td style="text-align:right;">
                <button class="btn-launch-installed-app" style="background:rgba(34,197,94,0.15);border-color:rgba(34,197,94,0.4);"
                    onclick="runWingetUpgradePkg('${escapeHtml(cid)}', '${escapeHtml(u.id || '').replace(/'/g,"\\'")}', '${escapeHtml(u.name || u.id).replace(/'/g,"\\'")}')">
                    ⬆️ Update
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

/** Renders Windows hotfix/KB list for a specific PC in the updates tab */
function renderWindowsUpdates(cid) {
    const tbody = document.getElementById('winUpdatesTableBody');
    const badge = document.getElementById('winUpdatesBadge');
    const summary = document.getElementById('winUpdatesSummary');
    if (!tbody) return;

    state.clientWindowsUpdates = state.clientWindowsUpdates || new Map();
    const patches = state.clientWindowsUpdates.get(cid);

    if (!patches) {
        if (badge) badge.textContent = '...';
        tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-muted);padding:16px;">Belum ada data. Klik <strong>Cek Patch</strong>.</td></tr>';
        if (summary) summary.innerHTML = 'Klik <strong>Cek Patch</strong> untuk melihat riwayat Windows Update di PC ini.';
        return;
    }

    if (badge) badge.textContent = patches.length;
    if (summary) summary.innerHTML = `Menampilkan <strong>${patches.length}</strong> patch Windows Update terakhir yang terpasang.`;

    if (patches.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-muted);padding:16px;">Tidak ada data patch Windows Update ditemukan.</td></tr>';
        return;
    }

    tbody.innerHTML = '';
    patches.forEach(p => {
        const tr = document.createElement('tr');
        const kbId = escapeHtml(p.hotfix_id || '-');
        const desc = escapeHtml(p.description || '-');
        const by = escapeHtml(p.installed_by || '-');
        const date = escapeHtml(p.installed_on || '-');
        const isRecent = date && date >= new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10);
        tr.innerHTML = `
            <td><span class="font-mono" style="font-size:0.8rem;font-weight:700;color:${isRecent ? '#22c55e' : '#94a3b8'};">${kbId}</span>
                ${isRecent ? '<span style="font-size:0.65rem;margin-left:4px;background:rgba(34,197,94,0.2);color:#22c55e;padding:1px 5px;border-radius:8px;">Baru</span>' : ''}
            </td>
            <td style="font-size:0.78rem;">${desc}</td>
            <td style="font-size:0.75rem;color:var(--text-muted);">${by}</td>
            <td style="font-size:0.75rem;color:var(--text-muted);">${date}</td>
        `;
        tbody.appendChild(tr);
    });
}

/** Send winget_check command to the currently inspected PC */
async function checkWingetUpdates() {
    const cid = state.activeInspectId;
    if (!cid) return;
    const badge = document.getElementById('wingetUpdatesBadge');
    const summary = document.getElementById('wingetUpdatesSummary');
    if (badge) badge.textContent = '⏳';
    if (summary) summary.innerHTML = '⏳ Memeriksa update software via winget... (Harap tunggu)';
    try {
        const resp = await fetch(`/api/client/${encodeURIComponent(cid)}/updates/winget/check`, { method: 'POST' });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            showToast(`❌ Gagal cek update: ${err.detail || resp.statusText}`, 'danger');
            if (summary) summary.innerHTML = '❌ Gagal mengirim perintah cek update.';
            if (badge) badge.textContent = '!';
            return;
        }
        showToast(`📦 Perintah <strong>Cek Update</strong> dikirim ke <strong>${cid}</strong>. Menunggu hasil...`, 'info');
    } catch (e) {
        showToast(`❌ Error: ${e.message}`, 'danger');
    }
}
window.checkWingetUpdates = checkWingetUpdates;

/** Send winget_upgrade for a specific package to the inspected PC */
async function runWingetUpgradePkg(cid, pkgId, pkgName) {
    if (!cid || !pkgId) return;
    if (!confirm(`Upgrade software "${pkgName}" di PC ${cid}?\n\nProses ini mungkin memerlukan beberapa menit.`)) return;
    showToast(`⬆️ Mengirim perintah upgrade <strong>${pkgName}</strong> ke <strong>${cid}</strong>...`, 'info');
    try {
        await fetch(`/api/client/${encodeURIComponent(cid)}/updates/winget/upgrade`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ package_id: pkgId })
        });
    } catch (e) {
        showToast(`❌ Error: ${e.message}`, 'danger');
    }
}
window.runWingetUpgradePkg = runWingetUpgradePkg;

/** Upgrade ALL outdated software on the inspected PC */
async function runWingetUpgradeAll() {
    const cid = state.activeInspectId;
    if (!cid) return;
    if (!confirm(`Upgrade SEMUA software di PC ${cid} via winget?\n\nProses ini mungkin memerlukan beberapa menit. PC tidak perlu di-restart selama proses.`)) return;
    showToast(`⬆️ Mengirim perintah <strong>Upgrade Semua</strong> ke <strong>${cid}</strong>...`, 'info');
    try {
        await fetch(`/api/client/${encodeURIComponent(cid)}/updates/winget/upgrade`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ package_id: '' })
        });
    } catch (e) {
        showToast(`❌ Error: ${e.message}`, 'danger');
    }
}
window.runWingetUpgradeAll = runWingetUpgradeAll;

/** Send windows_update_check to the inspected PC */
async function checkWindowsUpdates() {
    const cid = state.activeInspectId;
    if (!cid) return;
    const badge = document.getElementById('winUpdatesBadge');
    const summary = document.getElementById('winUpdatesSummary');
    if (badge) badge.textContent = '⏳';
    if (summary) summary.innerHTML = '⏳ Memeriksa riwayat Windows Update... (Harap tunggu)';
    try {
        const resp = await fetch(`/api/client/${encodeURIComponent(cid)}/updates/windows/check`, { method: 'POST' });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            showToast(`❌ Gagal cek Windows Update: ${err.detail || resp.statusText}`, 'danger');
            if (summary) summary.innerHTML = '❌ Gagal mengirim perintah cek Windows Update.';
            if (badge) badge.textContent = '!';
            return;
        }
        showToast(`🪟 Perintah <strong>Cek Windows Update</strong> dikirim ke <strong>${cid}</strong>. Menunggu hasil...`, 'info');
    } catch (e) {
        showToast(`❌ Error: ${e.message}`, 'danger');
    }
}
window.checkWindowsUpdates = checkWindowsUpdates;

/** Bulk: cek winget updates di SEMUA PC online sekaligus */
async function checkAllWingetUpdates() {
    if (!confirm('Kirim perintah Cek Update Software (winget) ke SEMUA PC online?\n\nHasil akan muncul di tab Pembaruan masing-masing PC.')) return;
    try {
        const resp = await fetch('/api/updates/winget/check-all', { method: 'POST' });
        const data = await resp.json();
        showToast(`📦 Perintah Cek Update dikirim ke <strong>${data.sent_count || 0}</strong> PC online.`, 'info');
    } catch (e) {
        showToast(`❌ Gagal broadcast: ${e.message}`, 'danger');
    }
}
window.checkAllWingetUpdates = checkAllWingetUpdates;

/** Bulk: upgrade ALL software di SEMUA PC online */
async function upgradeAllPCs() {
    if (!confirm('⚠️ PERHATIAN!\n\nUpgrade SEMUA software via winget di SEMUA PC online?\n\nProses ini akan memakan waktu beberapa menit di setiap PC.\nPastikan tidak ada ujian yang sedang berlangsung.')) return;
    try {
        const resp = await fetch('/api/updates/winget/upgrade-all', { method: 'POST' });
        const data = await resp.json();
        showToast(`⬆️ Perintah Upgrade Semua dikirim ke <strong>${data.sent_count || 0}</strong> PC online.`, 'success');
    } catch (e) {
        showToast(`❌ Gagal broadcast: ${e.message}`, 'danger');
    }
}
window.upgradeAllPCs = upgradeAllPCs;

// ─── Audio Mute / Unmute Control (Lab Discipline) ───────────────────────────

async function bulkMuteAudio(mute = true) {
    const actionLabel = mute ? 'Membisukan (Mute)' : 'Menyalakan (Unmute)';
    const confirmMsg = mute 
        ? 'Senyapkan (MUTE) audio/speaker pada SEMUA komputer siswa?\n\nBerguna agar suasana ruang lab kembali hening saat guru menerangkan materi.'
        : 'Nyalakan kembali (UNMUTE) audio/speaker pada semua komputer siswa?';
    if (!confirm(confirmMsg)) return;

    try {
        const endpoint = mute ? '/api/audio/mute-all' : '/api/audio/unmute-all';
        const resp = await fetch(endpoint, { method: 'POST' });
        const data = await resp.json();
        showToast(mute ? `🔇 ${data.message}` : `🔊 ${data.message}`, 'success');
        playSound('blip');
    } catch (e) {
        showToast(`❌ Gagal ${actionLabel}: ${e.message}`, 'danger');
    }
}
window.bulkMuteAudio = bulkMuteAudio;

async function toggleInspectMute() {
    const cid = state.activeInspectId;
    if (!cid) return;
    try {
        const resp = await fetch(`/api/client/${encodeURIComponent(cid)}/mute`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mute: true })
        });
        const data = await resp.json();
        showToast(`🔇 ${data.message}`, 'success');
        playSound('blip');
    } catch (e) {
        showToast(`❌ Gagal mute PC: ${e.message}`, 'danger');
    }
}
window.toggleInspectMute = toggleInspectMute;

// ─── Blacklist & Game Protection Management ─────────────────────────────────

async function openBlacklistModal() {
    openModal('modalBlacklist');
    await loadBlacklistData();
}
window.openBlacklistModal = openBlacklistModal;

async function loadBlacklistData() {
    const tbody = document.getElementById('blacklistRulesTableBody');
    const countEl = document.getElementById('blacklistRulesCount');
    const autoKillSwitch = document.getElementById('toggleAutoKillSwitch');
    const violationsSection = document.getElementById('blacklistViolationsSection');
    const violationsContainer = document.getElementById('activeViolationsListContainer');
    const violationCountBadge = document.getElementById('activeViolationHeaderCount');

    try {
        const resp = await fetch('/api/blacklist');
        if (!resp.ok) throw new Error('Gagal memuat aturan');
        const data = await resp.json();

        // Update auto-kill switch
        if (autoKillSwitch) autoKillSwitch.checked = Boolean(data.auto_kill);

        // Update violations
        const violations = data.violations || [];
        if (violationCountBadge) violationCountBadge.textContent = violations.length;
        if (violationsSection) {
            violationsSection.style.display = violations.length > 0 ? 'block' : 'none';
        }
        if (violationsContainer) {
            if (violations.length === 0) {
                violationsContainer.innerHTML = '<div style="color:var(--text-muted);font-size:0.8rem;padding:6px;">Tidak ada pelanggaran saat ini. Seluruh PC siswa tertib.</div>';
            } else {
                violationsContainer.innerHTML = violations.map(v => `
                    <div class="active-violation-row">
                        <div class="violation-info">
                            <strong>🖥️ ${escapeHtml(v.hostname || v.client_id)}</strong>
                            <span class="violation-pill">🚨 ${escapeHtml(v.keyword.toUpperCase())}</span>
                            <span class="violation-detail" title="${escapeHtml(v.app)}">${escapeHtml(v.app || '')}</span>
                        </div>
                        <div class="violation-actions">
                            <button class="btn-kill-violation" onclick="quickKillViolation('${escapeHtml(v.client_id)}', '${escapeHtml(v.app).replace(/'/g, "\\'")}')">⚡ Tutup Program</button>
                            <button class="btn-lock-violation" onclick="sendCommand('${escapeHtml(v.client_id)}', 'lock'); showToast('🔒 PC ${escapeHtml(v.hostname)} dikunci', 'warning');">🔒 Kunci PC</button>
                        </div>
                    </div>
                `).join('');
            }
        }

        // Update rules table
        const rules = data.rules || [];
        if (countEl) countEl.textContent = rules.length;
        if (tbody) {
            if (rules.length === 0) {
                tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;padding:16px;color:var(--text-muted);">Belum ada aturan kata kunci. Tambahkan di atas.</td></tr>';
                return;
            }
            tbody.innerHTML = rules.map(r => {
                const catLabels = {
                    'game': '🎮 Game',
                    'social': '💬 Media Sosial',
                    'media': '🎬 Hiburan/Video',
                    'gambling': '🎰 Judi/Bahaya',
                    'custom': '📌 Khusus'
                };
                const catBadgeClass = r.category === 'game' ? 'badge-cat-game' : (r.category === 'gambling' ? 'badge-cat-danger' : 'badge-cat-normal');
                return `
                    <tr>
                        <td><strong class="font-mono text-rose" style="font-size:0.85rem;">${escapeHtml(r.keyword)}</strong></td>
                        <td><span class="badge-cat ${catBadgeClass}">${catLabels[r.category] || r.category}</span></td>
                        <td style="font-size:0.78rem;color:var(--text-muted);">${escapeHtml(r.description || '-')}</td>
                        <td style="text-align:right;">
                            <button class="btn-del-rule" onclick="deleteBlacklistRule(${r.id}, '${escapeHtml(r.keyword)}')">🗑️ Hapus</button>
                        </td>
                    </tr>
                `;
            }).join('');
        }
    } catch (e) {
        if (tbody) tbody.innerHTML = `<tr><td colspan="4" style="color:#f43f5e;text-align:center;padding:16px;">Error: ${e.message}</td></tr>`;
    }
}

async function submitNewBlacklistRule() {
    const input = document.getElementById('newBlacklistKeywordInput');
    const select = document.getElementById('newBlacklistCategorySelect');
    if (!input || !input.value.trim()) {
        showToast('Ketik kata kunci aplikasi/game terlebih dahulu!', 'warning');
        return;
    }
    const kw = input.value.trim().toLowerCase();
    const cat = select ? select.value : 'game';
    await saveBlacklistRule(kw, cat, `Aplikasi ${kw}`);
    input.value = '';
}
window.submitNewBlacklistRule = submitNewBlacklistRule;

async function quickAddBlacklist(keyword, category, desc) {
    await saveBlacklistRule(keyword, category, desc);
}
window.quickAddBlacklist = quickAddBlacklist;

async function saveBlacklistRule(keyword, category, description) {
    try {
        const resp = await fetch('/api/blacklist/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ keyword, category, description })
        });
        const data = await resp.json();
        showToast(`🚨 ${data.message}`, 'success');
        await loadBlacklistData();
    } catch (e) {
        showToast(`❌ Gagal menambah kata kunci: ${e.message}`, 'danger');
    }
}

async function deleteBlacklistRule(id, keyword) {
    if (!confirm(`Hapus kata kunci terlarang "${keyword}"?`)) return;
    try {
        const resp = await fetch('/api/blacklist/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id, keyword })
        });
        showToast(`Kata kunci "${keyword}" dihapus`, 'info');
        await loadBlacklistData();
    } catch (e) {
        showToast(`❌ Gagal menghapus: ${e.message}`, 'danger');
    }
}
window.deleteBlacklistRule = deleteBlacklistRule;

async function toggleAutoKillSetting() {
    try {
        const resp = await fetch('/api/blacklist/toggle-auto-kill', { method: 'POST' });
        const data = await resp.json();
        showToast(data.message, data.auto_kill ? 'warning' : 'info');
    } catch (e) {
        showToast(`❌ Gagal mengubah auto-kill: ${e.message}`, 'danger');
    }
}
window.toggleAutoKillSetting = toggleAutoKillSetting;

async function quickKillViolation(cid, activeApp = '') {
    let procToKill = '';
    if (activeApp) {
        procToKill = activeApp.split('(')[0].trim();
    }
    const client = state.clients.get(cid);
    const host = client ? (client.hostname || cid) : cid;

    if (procToKill && procToKill.endsWith('.exe')) {
        sendCommand(cid, 'kill_process', { name: procToKill });
        showToast(`⚡ Menghentikan proses <strong>${escapeHtml(procToKill)}</strong> di PC <strong>${escapeHtml(host)}</strong>...`, 'warning');
    } else {
        sendCommand(cid, 'kill_process', { name: 'RobloxPlayerBeta.exe' });
        showToast(`⚡ Mengirim perintah tutup game ke PC <strong>${escapeHtml(host)}</strong>...`, 'warning');
    }
    playSound('blip');
    setTimeout(loadBlacklistData, 1200);
}
window.quickKillViolation = quickKillViolation;

function updateViolationStatsBadge() {
    let count = 0;
    state.clients.forEach(c => {
        if (c.violation && c.violation.detected) count++;
    });
    const badge = document.getElementById('violationCountBadge');
    if (badge) {
        badge.textContent = count;
        badge.style.display = count > 0 ? 'inline-flex' : 'none';
    }
}

// Modal 3: Launch App
// --- Modal 3: Launch App Target Picker System ---
state.launchAppTargetMode = 'all'; // 'all' | 'custom'
state.launchAppSelectedCids = new Set();

function renderLaunchPcCheckboxes() {
    const grid = document.getElementById('launchPcCheckboxGrid');
    if (!grid) return;
    grid.innerHTML = '';

    const allClients = Array.from(state.clients.values());
    const clientsToShow = allClients.filter(c => c.is_online);
    if (clientsToShow.length === 0) {
        grid.innerHTML = '<span style="grid-column: 1/-1; font-size: 0.75rem; color: var(--text-muted); padding: 8px;">Tidak ada PC siswa yang sedang online saat ini.</span>';
        return;
    }

    clientsToShow.forEach(c => {
        const item = document.createElement('label');
        item.className = 'launch-pc-checkbox-item';
        const isChecked = state.launchAppSelectedCids.has(c.client_id);
        const name = getClientDisplayName(c);

        item.innerHTML = `
            <input type="checkbox" value="${escapeHtml(c.client_id)}" ${isChecked ? 'checked' : ''}>
            <span style="font-weight: 600;">${escapeHtml(name)}</span>
            <span style="color: var(--text-muted); font-size: 0.7rem;">(${escapeHtml(c.ip_address)})</span>
        `;

        const chk = item.querySelector('input');
        chk.addEventListener('change', (e) => {
            if (e.target.checked) {
                state.launchAppSelectedCids.add(c.client_id);
            } else {
                state.launchAppSelectedCids.delete(c.client_id);
            }
            updateLaunchTargetSummary();
        });

        grid.appendChild(item);
    });
}

function setLaunchTargetMode(mode) {
    state.launchAppTargetMode = mode;
    const pillAll = document.getElementById('pillLaunchTargetAll');
    const pillSelect = document.getElementById('pillLaunchTargetSelect');
    const pickerBox = document.getElementById('launchPcPickerBox');

    if (pillAll) pillAll.classList.toggle('active', mode === 'all');
    if (pillSelect) pillSelect.classList.toggle('active', mode === 'custom');

    if (pickerBox) {
        pickerBox.style.display = mode === 'custom' ? 'block' : 'none';
        if (mode === 'custom') renderLaunchPcCheckboxes();
    }
    updateLaunchTargetSummary();
}

function selectAllLaunchPcTargets(selectAll) {
    if (selectAll) {
        const onlineClients = Array.from(state.clients.values()).filter(c => c.is_online);
        onlineClients.forEach(c => state.launchAppSelectedCids.add(c.client_id));
    } else {
        state.launchAppSelectedCids.clear();
    }
    renderLaunchPcCheckboxes();
    updateLaunchTargetSummary();
}

function updateLaunchTargetSummary() {
    const textEl = document.getElementById('launchTargetText');
    const badgeEl = document.getElementById('launchTargetCountBadge');
    if (!textEl) return;

    if (state.launchAppTargetMode === 'all') {
        textEl.textContent = 'Semua Komputer Siswa (Online)';
        if (badgeEl) {
            badgeEl.textContent = 'Semua Online';
            badgeEl.className = 'badge-status online';
        }
    } else {
        const count = state.launchAppSelectedCids.size;
        if (count === 0) {
            textEl.innerHTML = '<span style="color: #f43f5e;">Belum ada PC dipilih (centang di bawah)</span>';
            if (badgeEl) {
                badgeEl.textContent = '0 PC';
                badgeEl.className = 'badge-status offline';
            }
        } else if (count === 1) {
            const cid = Array.from(state.launchAppSelectedCids)[0];
            const client = state.clients.get(cid);
            const name = client ? getClientDisplayName(client) : cid;
            textEl.textContent = `1 PC: ${name} (${client ? client.ip_address : ''})`;
            if (badgeEl) {
                badgeEl.textContent = '1 PC Khusus';
                badgeEl.className = 'badge-status online';
            }
        } else {
            textEl.textContent = `${count} Komputer Siswa Terpilih`;
            if (badgeEl) {
                badgeEl.textContent = `${count} PC Terpilih`;
                badgeEl.className = 'badge-status online';
            }
        }
    }
}

function openLaunchAppModal() {
    // If computers were already checked on the main grid, preselect them
    if (state.selectedClientIds.size > 0) {
        state.launchAppSelectedCids = new Set(state.selectedClientIds);
        setLaunchTargetMode('custom');
    } else {
        state.launchAppSelectedCids.clear();
        setLaunchTargetMode('all');
    }
    const customInput = document.getElementById('customAppCommandInput');
    if (customInput && !customInput.value) {
        customInput.value = 'chrome';
    }
    openModal('modalLaunchApp');
}

function openLaunchAppModalForSingle(cid) {
    state.launchAppSelectedCids = new Set([cid]);
    setLaunchTargetMode('custom');
    const customInput = document.getElementById('customAppCommandInput');
    if (customInput && !customInput.value) {
        customInput.value = 'chrome';
    }
    openModal('modalLaunchApp');
}

function resetLaunchTargetToAll() {
    setLaunchTargetMode('all');
    showToast('Target peluncuran aplikasi dialihkan ke: Semua Komputer Siswa', 'info');
}

window.setLaunchTargetMode = setLaunchTargetMode;
window.selectAllLaunchPcTargets = selectAllLaunchPcTargets;
window.openLaunchAppModal = openLaunchAppModal;
window.openLaunchAppModalForSingle = openLaunchAppModalForSingle;
window.resetLaunchTargetToAll = resetLaunchTargetToAll;

// Modal 4: Power Controls
function openPowerModal() {
    setHTML('powerTargetSummary', `Target: <strong>${getTargetDescription()}</strong>`);
    openModal('modalPower');
}

// Modal 5: Audit Logs
async function openLogsModal() {
    openModal('modalLogs');
    loadAuditLogs();
}

async function loadAuditLogs() {
    const tbody = document.getElementById('auditLogTableBody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;">Memuat catatan log...</td></tr>';

    try {
        const resp = await fetch('/api/logs?limit=40');
        const data = await resp.json();
        tbody.innerHTML = '';
        (data.logs || []).forEach(log => {
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td class="font-mono text-muted">${log.timestamp}</td>
                <td><strong>${escapeHtml(log.action)}</strong></td>
                <td><span class="badge-subtle">${escapeHtml(log.target)}</span></td>
                <td>${escapeHtml(log.details)}</td>
                <td><span class="text-emerald">${log.status}</span></td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:#f43f5e;">Gagal memuat log</td></tr>';
    }
}

async function exportLogsToCSV() {
    try {
        window.location.href = '/api/export/logs';
        showToast('Mengunduh catatan log audit dan pelanggaran...', 'success');
        playSound('success');
    } catch (err) {
        showToast('Gagal mengekspor CSV: ' + err.message, 'danger');
    }
}

async function exportClientsToCSV() {
    try {
        const clientList = Array.from(state.clients.values());
        if (clientList.length === 0) {
            showToast('Tidak ada data komputer lab untuk diekspor.', 'info');
            return;
        }

        window.location.href = '/api/export/clients';
        showToast('Mengunduh laporan inventaris komputer lab...', 'success');
        playSound('success');
    } catch (err) {
        showToast('Gagal mengekspor data: ' + err.message, 'danger');
    }
}

// Modal 6: Download & Install Agent (.EXE)
async function openDownloadAgentModal() {
    openModal('modalDownloadAgent');
    const inputUrl = document.getElementById('inputDownloadUrl');
    const linkExe = document.getElementById('linkDownloadExe');
    const badgeVer = document.getElementById('agentVersionBadge');
    const verText = document.getElementById('agentVerText');
    const dateText = document.getElementById('agentDateText');
    const buildTime = document.getElementById('agentBuildTime');
    const changelogVer = document.getElementById('agentChangelogVer');
    const changelogList = document.getElementById('agentChangelogList');
    const exeCardTitle = document.getElementById('agentExeCardTitle');
    const stepExeName = document.getElementById('stepDownloadExeName');

    try {
        const resp = await fetch('/api/server-info');
        if (resp.ok) {
            const info = await resp.json();
            const ver = info.agent_version || 'v2.3.0';
            const cleanVer = ver.replace(/^v/, '');
            const exeFilename = info.agent_filename || `Client_Siswa_Web_v${cleanVer}.exe`;
            const size = info.exe_size_mb ? `${info.exe_size_mb} MB` : '~29.7 MB';
            const dlUrl = info.download_url || `${window.location.origin}${BASE_PATH}/download/${exeFilename}`;
            const batchUrl = info.batch_url || `${window.location.origin}${BASE_PATH}/download/pasang.bat`;

            if (inputUrl) {
                inputUrl.value = dlUrl;
            }
            if (exeCardTitle) {
                exeCardTitle.textContent = exeFilename;
            }
            if (stepExeName) {
                stepExeName.textContent = exeFilename;
            }
            if (linkExe) {
                linkExe.href = `${BASE_PATH}/download/${exeFilename}`;
                linkExe.setAttribute('download', exeFilename);
                linkExe.innerHTML = `<span>⬇️ Download ${escapeHtml(exeFilename)} (${ver} · ${size})</span>`;
            }
            const linkBatch = document.getElementById('linkDownloadBatch');
            if (linkBatch) {
                linkBatch.href = `${BASE_PATH}/download/pasang.bat`;
            }
            if (badgeVer) badgeVer.textContent = `${ver} (Terbaru)`;
            if (verText) verText.textContent = ver;
            if (dateText && info.agent_release_date) dateText.textContent = info.agent_release_date;
            if (buildTime && info.exe_last_modified) buildTime.textContent = info.exe_last_modified;
            if (changelogVer) changelogVer.textContent = ver;
            if (changelogList && Array.isArray(info.changelog) && info.changelog.length > 0) {
                changelogList.innerHTML = info.changelog.map(item => `<li>${escapeHtml(item)}</li>`).join('');
            }
        } else {
            if (inputUrl) inputUrl.value = `${window.location.origin}${BASE_PATH}/download/Client_Siswa_Web_v2.3.0.exe`;
        }
    } catch (e) {
        if (inputUrl) inputUrl.value = `${window.location.origin}${BASE_PATH}/download/Client_Siswa_Web_v2.3.0.exe`;
    }
}

// ==========================================================================
// Remote Control Interactive Studio (Mouse & Keyboard) Logic
// ==========================================================================

function handleRemoteFrame(msg) {
    const img = document.getElementById('remoteScreenImg');
    const loadingOverlay = document.getElementById('remoteLoadingOverlay');
    if (loadingOverlay) loadingOverlay.style.display = 'none';

    if (img && msg.frame) {
        img.src = `data:image/jpeg;base64,${msg.frame}`;
    }

    // Calculate FPS and Latency
    const now = performance.now();
    state.remoteFrameCount++;
    if (!state.remoteLastFpsTime) state.remoteLastFpsTime = now;

    if (now - state.remoteLastFpsTime >= 1000) {
        state.remoteFps = Math.round((state.remoteFrameCount * 1000) / (now - state.remoteLastFpsTime));
        state.remoteFrameCount = 0;
        state.remoteLastFpsTime = now;
        
        let latencyEstimate = 25;
        if (msg.timestamp) {
            const diffMs = Math.max(5, Math.round((Date.now() / 1000 - msg.timestamp) * 1000));
            latencyEstimate = diffMs;
        }

        const fpsEl = document.getElementById('remoteFpsText');
        if (fpsEl) {
            fpsEl.textContent = `FPS: ${state.remoteFps} · Latensi: ~${latencyEstimate}ms`;
        }
    }
}

function openRemoteControl(clientId) {
    const client = state.clients.get(clientId);
    if (!client) {
        showToast('Data komputer siswa tidak ditemukan!', 'danger');
        return;
    }
    if (!client.is_online) {
        showToast(`Komputer <strong>${client.hostname || clientId}</strong> sedang offline. Hidupkan dengan Wake-on-LAN terlebih dahulu.`, 'warning');
        return;
    }

    state.remoteClientId = clientId;
    state.remoteFrameCount = 0;
    state.remoteLastFpsTime = 0;

    // Update UI headers
    setText('remotePcName', getClientDisplayName(client));
    setText('remotePcIp', client.ip_address || '127.0.0.1');
    // Instantaneous screen display: NEVER delay or show black screen if we already hold a thumbnail!
    const existingThumb = state.thumbnails.get(clientId);
    const img = document.getElementById('remoteScreenImg');
    const loadingOverlay = document.getElementById('remoteLoadingOverlay');
    const fpsEl = document.getElementById('remoteFpsText');

    if (existingThumb) {
        if (img) img.src = `data:image/jpeg;base64,${existingThumb}`;
        if (loadingOverlay) loadingOverlay.style.display = 'none';
        if (fpsEl) fpsEl.textContent = '● Streaming Langsung';
    } else {
        if (img) img.src = '';
        if (loadingOverlay) loadingOverlay.style.display = 'flex';
        if (fpsEl) fpsEl.textContent = 'Menghubungkan streaming...';
    }

    // Show modal
    openModal('modalRemoteControl');

    // Make viewport interactive by default
    setRemoteMode(state.remoteMode || 'interactive');

    // Send start_remote over WebSocket
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({
            type: 'start_remote',
            client_id: clientId,
            quality: state.remoteQuality || 55
        }));
    }

    // Focus viewport
    setTimeout(() => {
        const viewport = document.getElementById('remoteViewportWrapper');
        if (viewport) viewport.focus();
    }, 100);

    // Check if user is remoting into the teacher's own PC
    const isSelfHost = (client.ip_address === '127.0.0.1' || client.ip_address === window.location.hostname || (client.hostname && client.hostname.toLowerCase() === window.location.hostname.toLowerCase()));
    const hintBadge = document.getElementById('remoteHintBadge');
    if (isSelfHost) {
        if (hintBadge) {
            hintBadge.innerHTML = '⚠️ <strong>Anda sedang me-remote Komputer Guru ini sendiri!</strong> Tampilan cermin berulang (infinite mirror) adalah hal normal saat me-remote layar sendiri. Tekan <strong>Esc</strong> untuk menutup lalu pilih Komputer Siswa.';
            hintBadge.style.background = 'rgba(234, 88, 12, 0.95)';
            hintBadge.style.color = '#ffffff';
        }
        showToast(`⚠️ <strong>Perhatian:</strong> Anda membuka remote ke komputer Guru ini sendiri (${client.hostname || clientId}). Efek cermin berulang adalah hal normal saat me-remote layar sendiri. Silakan tutup dan pilih komputer siswa di lab.`, 'warning');
    } else {
        if (hintBadge) {
            hintBadge.innerHTML = '💡 Klik pada layar siswa untuk mengontrol kursor & keyboard. Tekan <strong>Esc</strong> untuk keluar.';
            hintBadge.style.background = '';
            hintBadge.style.color = '';
        }
    }

    playSound('success');
    showToast(`Memulai sesi Remote Control ke <strong>${client.hostname || clientId}</strong>`, 'info');
}

function closeRemoteControl(notifyServer = true) {
    if (state.remoteClientId && notifyServer) {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
            state.ws.send(JSON.stringify({
                type: 'stop_remote',
                client_id: state.remoteClientId
            }));
        }
    }
    state.remoteClientId = null;
    closeModal('modalRemoteControl');

    if (document.fullscreenElement) {
        try { document.exitFullscreen(); } catch (e) {}
    }
}

function setRemoteMode(mode) {
    state.remoteMode = mode;
    const btnInteractive = document.getElementById('btnRemoteModeInteractive');
    const btnView = document.getElementById('btnRemoteModeView');
    const viewport = document.getElementById('remoteViewportWrapper');

    if (mode === 'interactive') {
        if (btnInteractive) btnInteractive.classList.add('active');
        if (btnView) btnView.classList.remove('active');
        if (viewport) viewport.classList.add('interactive-mode');
    } else {
        if (btnInteractive) btnInteractive.classList.remove('active');
        if (btnView) btnView.classList.add('active');
        if (viewport) viewport.classList.remove('interactive-mode');
    }
}

function sendRemoteInput(eventObj) {
    if (!state.remoteClientId) return;
    if (state.remoteMode !== 'interactive' && eventObj.type !== 'stop_remote') return;
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;

    state.ws.send(JSON.stringify({
        type: 'remote_input',
        client_id: state.remoteClientId,
        event: eventObj
    }));
}

function getRemoteNormalizedCoords(e, img) {
    if (!img) return null;
    const rect = img.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return null;

    const nw = img.naturalWidth || rect.width;
    const nh = img.naturalHeight || rect.height;
    const imgRatio = nw / nh;
    const elemRatio = rect.width / rect.height;

    let renderW = rect.width;
    let renderH = rect.height;
    let offsetX = 0;
    let offsetY = 0;

    if (elemRatio > imgRatio) {
        renderW = rect.height * imgRatio;
        offsetX = (rect.width - renderW) / 2;
    } else {
        renderH = rect.width / imgRatio;
        offsetY = (rect.height - renderH) / 2;
    }

    const clickX = e.clientX - rect.left - offsetX;
    const clickY = e.clientY - rect.top - offsetY;

    if (clickX < 0 || clickX > renderW || clickY < 0 || clickY > renderH) return null;

    return {
        rx: Math.max(0, Math.min(1, clickX / renderW)),
        ry: Math.max(0, Math.min(1, clickY / renderH))
    };
}

function setupRemoteControlListeners() {
    const viewport = document.getElementById('remoteViewportWrapper');
    const img = document.getElementById('remoteScreenImg');
    const btnClose = document.getElementById('btnRemoteClose');
    const btnInteractive = document.getElementById('btnRemoteModeInteractive');
    const btnView = document.getElementById('btnRemoteModeView');
    const qualitySelect = document.getElementById('remoteQualitySelect');
    const btnFullscreen = document.getElementById('btnRemoteFullscreen');
    const btnSendText = document.getElementById('btnRemoteSendText');
    const textInput = document.getElementById('remoteTextInput');

    if (btnClose) {
        btnClose.addEventListener('click', () => closeRemoteControl(true));
    }

    if (btnInteractive) {
        btnInteractive.addEventListener('click', () => setRemoteMode('interactive'));
    }
    if (btnView) {
        btnView.addEventListener('click', () => setRemoteMode('view'));
    }

    if (qualitySelect) {
        qualitySelect.addEventListener('change', (e) => {
            state.remoteQuality = parseInt(e.target.value, 10);
            if (state.remoteClientId && state.ws && state.ws.readyState === WebSocket.OPEN) {
                state.ws.send(JSON.stringify({
                    type: 'start_remote',
                    client_id: state.remoteClientId,
                    quality: state.remoteQuality
                }));
                showToast(`Kualitas remote diubah ke ${state.remoteQuality}%`, 'info', 2000);
            }
        });
    }

    if (btnFullscreen) {
        btnFullscreen.addEventListener('click', () => {
            const modal = document.getElementById('modalRemoteControl');
            if (!document.fullscreenElement) {
                if (modal.requestFullscreen) modal.requestFullscreen();
            } else {
                if (document.exitFullscreen) document.exitFullscreen();
            }
        });
    }

    // Text sending
    function sendTypedText() {
        if (!textInput) return;
        const txt = textInput.value;
        if (!txt) return;
        sendRemoteInput({ type: 'text', text: txt });
        textInput.value = '';
        showToast('Teks berhasil dikirim ke komputer siswa', 'success', 2000);
        if (viewport) viewport.focus();
    }

    if (btnSendText) {
        btnSendText.addEventListener('click', sendTypedText);
    }
    if (textInput) {
        textInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                sendTypedText();
            }
        });
    }

    // Shortcut buttons
    const shortcutBtns = document.querySelectorAll('.remote-key-btn');
    shortcutBtns.forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.preventDefault();
            const sc = btn.dataset.shortcut;
            if (sc) {
                sendRemoteInput({ type: 'shortcut', name: sc });
                showToast(`Shortcut <strong>${sc}</strong> dikirim`, 'info', 1500);
                if (viewport) viewport.focus();
            }
        });
    });

    // Viewport Mouse & Keyboard listeners
    if (img) {
        // Prevent accidental image dragging
        img.addEventListener('dragstart', (e) => e.preventDefault());

        img.addEventListener('mousemove', (e) => {
            if (state.remoteMode !== 'interactive') return;
            const now = performance.now();
            if (now - state.remoteLastMoveTime < (state.remoteThrottleMs || 25)) return;
            state.remoteLastMoveTime = now;

            const coords = getRemoteNormalizedCoords(e, img);
            if (coords) {
                sendRemoteInput({
                    type: 'mouse_move',
                    rx: coords.rx,
                    ry: coords.ry
                });
            }
        });

        img.addEventListener('mousedown', (e) => {
            if (state.remoteMode !== 'interactive') return;
            e.preventDefault();
            const coords = getRemoteNormalizedCoords(e, img);
            if (!coords) return;

            state.remoteMouseDown = true;
            let btn = 'left';
            if (e.button === 2) btn = 'right';
            else if (e.button === 1) btn = 'middle';

            sendRemoteInput({
                type: 'mouse_down',
                button: btn,
                rx: coords.rx,
                ry: coords.ry
            });
            if (viewport) viewport.focus();
        });

        img.addEventListener('mouseup', (e) => {
            if (state.remoteMode !== 'interactive') return;
            e.preventDefault();
            state.remoteMouseDown = false;
            const coords = getRemoteNormalizedCoords(e, img);
            if (!coords) return;

            let btn = 'left';
            if (e.button === 2) btn = 'right';
            else if (e.button === 1) btn = 'middle';

            sendRemoteInput({
                type: 'mouse_up',
                button: btn,
                rx: coords.rx,
                ry: coords.ry
            });
        });

        img.addEventListener('click', (e) => {
            if (state.remoteMode !== 'interactive') return;
            e.preventDefault();
            const coords = getRemoteNormalizedCoords(e, img);
            if (!coords) return;

            let btn = 'left';
            if (e.button === 2) btn = 'right';
            else if (e.button === 1) btn = 'middle';

            sendRemoteInput({
                type: 'click',
                button: btn,
                rx: coords.rx,
                ry: coords.ry
            });
        });

        img.addEventListener('dblclick', (e) => {
            if (state.remoteMode !== 'interactive') return;
            e.preventDefault();
            const coords = getRemoteNormalizedCoords(e, img);
            if (coords) {
                sendRemoteInput({
                    type: 'dblclick',
                    button: 'left',
                    rx: coords.rx,
                    ry: coords.ry
                });
            }
        });

        img.addEventListener('contextmenu', (e) => {
            e.preventDefault();
        });

        img.addEventListener('wheel', (e) => {
            if (state.remoteMode !== 'interactive') return;
            e.preventDefault();
            const deltaY = e.deltaY < 0 ? 120 : -120;
            sendRemoteInput({
                type: 'mouse_wheel',
                delta_y: deltaY
            });
        }, { passive: false });
    }

    // Safety mouseup on window so releasing outside doesn't leave mouse stuck down
    window.addEventListener('mouseup', () => {
        if (state.remoteMouseDown && state.remoteClientId) {
            state.remoteMouseDown = false;
            sendRemoteInput({
                type: 'mouse_up',
                button: 'left'
            });
        }
    });

    // Global keyboard listener while Remote Control modal is open
    window.addEventListener('keydown', (e) => {
        if (!state.remoteClientId) return;
        if (state.remoteMode !== 'interactive') return;
        
        // Never intercept if typing in input field
        const tag = (e.target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select') return;

        if (e.key === 'Escape') {
            closeRemoteControl(true);
            return;
        }

        // Block browser default for functional keys and shortcuts
        const blockedKeys = ['Tab', 'Alt', 'F1', 'F3', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Backspace', ' '];
        if (blockedKeys.includes(e.key) || (e.ctrlKey && ['a', 'c', 'v', 'x', 'z', 'y', 's', 'w'].includes(e.key.toLowerCase()))) {
            e.preventDefault();
        }

        sendRemoteInput({
            type: 'key_down',
            key: e.key
        });
    });

    window.addEventListener('keyup', (e) => {
        if (!state.remoteClientId) return;
        if (state.remoteMode !== 'interactive') return;
        
        const tag = (e.target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select') return;

        sendRemoteInput({
            type: 'key_up',
            key: e.key
        });
    });

    // Inspect modal Remote button hook
    const btnInspectRemote = document.getElementById('btnInspectRemote');
    if (btnInspectRemote) {
        btnInspectRemote.addEventListener('click', () => {
            if (state.activeInspectId) {
                const targetCid = state.activeInspectId;
                closeModal('modalInspect');
                openRemoteControl(targetCid);
            }
        });
    }
}

// --- Setup DOM Event Listeners ---
function setupEventListeners() {
    // Search input
    const searchInput = document.getElementById('searchInput');
    const clearSearchBtn = document.getElementById('clearSearchBtn');
    if (searchInput) {
        searchInput.addEventListener('input', (e) => {
            state.searchQuery = e.target.value.trim();
            if (clearSearchBtn) clearSearchBtn.style.display = state.searchQuery ? 'block' : 'none';
            renderClients();
        });
    }
    if (clearSearchBtn) {
        clearSearchBtn.addEventListener('click', () => {
            if (searchInput) searchInput.value = '';
            state.searchQuery = '';
            clearSearchBtn.style.display = 'none';
            renderClients();
        });
    }

    // Room Filter
    const roomFilter = document.getElementById('roomFilter');
    if (roomFilter) {
        roomFilter.addEventListener('change', (e) => {
            state.roomFilter = e.target.value;
            if (state.viewMode === 'seating') {
                loadAndRenderSeatingPlan();
            }
            renderClients();
        });
    }

    // Sort Filter
    const sortFilter = document.getElementById('sortFilter');
    if (sortFilter) {
        sortFilter.value = state.sortBy || 'name_asc';
        sortFilter.addEventListener('change', (e) => {
            state.sortBy = e.target.value;
            try {
                localStorage.setItem('labguard_sort_by', state.sortBy);
            } catch (err) {}
            reorderGridCards();
            renderTable();
            if (state.viewMode === 'seating') {
                renderSeatingPlan();
            }
        });
    }

    // Logout Button Trigger (Opens Modern Logout Modal)
    const btnLogout = document.getElementById('btnLogout');
    if (btnLogout) {
        btnLogout.addEventListener('click', () => {
            openModal('modalLogout');
        });
    }

    // Confirm Logout Inside Modal
    const btnConfirmLogout = document.getElementById('btnConfirmLogout');
    if (btnConfirmLogout) {
        btnConfirmLogout.addEventListener('click', async () => {
            btnConfirmLogout.disabled = true;
            btnConfirmLogout.textContent = 'Logging out...';
            try {
                await fetch('/api/auth/logout', { method: 'POST' });
            } catch (e) {}
            try { 
                localStorage.removeItem('labguard_token'); 
                document.cookie = "labguard_token=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;";
            } catch (e) {}
            window.location.replace(BASE_PATH ? `${BASE_PATH}/login` : '/login');
        });
    }

    // Change Password Modal Trigger & Form
    const btnOpenChangePwd = document.getElementById('btnOpenChangePwd');
    if (btnOpenChangePwd) {
        btnOpenChangePwd.addEventListener('click', () => {
            openModal('modalChangePassword');
            const alertEl = document.getElementById('alertChangePwd');
            if (alertEl) alertEl.style.display = 'none';
            const f = document.getElementById('formChangePassword');
            if (f) f.reset();
        });
    }

    const formChangePassword = document.getElementById('formChangePassword');
    if (formChangePassword) {
        formChangePassword.addEventListener('submit', async (e) => {
            e.preventDefault();
            const alertEl = document.getElementById('alertChangePwd');
            const oldPwd = (document.getElementById('inputOldPassword')?.value || '').trim();
            const newPwd = (document.getElementById('inputNewPassword')?.value || '').trim();
            const confirmPwd = (document.getElementById('inputConfirmPassword')?.value || '').trim();

            if (newPwd !== confirmPwd) {
                if (alertEl) {
                    alertEl.style.background = '#fef2f2';
                    alertEl.style.color = '#b91c1c';
                    alertEl.style.border = '1px solid #fecaca';
                    alertEl.textContent = 'Konfirmasi kata sandi baru tidak cocok!';
                    alertEl.style.display = 'block';
                }
                return;
            }

            const btnSubmit = document.getElementById('btnSubmitChangePwd');
            if (btnSubmit) {
                btnSubmit.disabled = true;
                btnSubmit.innerHTML = '<span>Menyimpan...</span>';
            }

            try {
                const res = await fetch('/api/auth/change-password', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ old_password: oldPwd, new_password: newPwd })
                });
                const data = await res.json();
                if (res.ok && data.ok) {
                    closeModal('modalChangePassword');
                    showToast('Kata sandi Guru berhasil diperbarui!', 'success');
                    playSound('blip');
                } else {
                    if (alertEl) {
                        alertEl.style.background = '#fef2f2';
                        alertEl.style.color = '#b91c1c';
                        alertEl.style.border = '1px solid #fecaca';
                        alertEl.textContent = data.detail || 'Gagal mengubah kata sandi.';
                        alertEl.style.display = 'block';
                    }
                }
            } catch (err) {
                if (alertEl) {
                    alertEl.style.background = '#fef2f2';
                    alertEl.style.color = '#b91c1c';
                    alertEl.style.border = '1px solid #fecaca';
                    alertEl.textContent = 'Terjadi kesalahan jaringan saat mengubah kata sandi.';
                    alertEl.style.display = 'block';
                }
            } finally {
                if (btnSubmit) {
                    btnSubmit.disabled = false;
                    btnSubmit.innerHTML = '<span>💾 Simpan Kata Sandi</span>';
                }
            }
        });
    }

    // View Toggles (Grid vs List vs Seating)
    const btnViewGrid = document.getElementById('btnViewGrid');
    const btnViewList = document.getElementById('btnViewList');
    const btnViewSeating = document.getElementById('btnViewSeating');
    const clientsGrid = document.getElementById('clientsGrid');
    const clientsTableWrapper = document.getElementById('clientsTableWrapper');
    const seatingPlanWrapper = document.getElementById('seatingPlanWrapper');

    function switchViewMode(mode) {
        state.viewMode = mode;
        if (btnViewGrid) btnViewGrid.classList.toggle('active', mode === 'grid');
        if (btnViewList) btnViewList.classList.toggle('active', mode === 'list');
        if (btnViewSeating) btnViewSeating.classList.toggle('active', mode === 'seating');

        if (clientsGrid) clientsGrid.style.display = (mode === 'grid') ? 'grid' : 'none';
        if (clientsTableWrapper) clientsTableWrapper.style.display = (mode === 'list') ? 'block' : 'none';
        if (seatingPlanWrapper) seatingPlanWrapper.style.display = (mode === 'seating') ? 'flex' : 'none';

        if (mode === 'list') {
            renderTable();
        } else if (mode === 'seating') {
            loadAndRenderSeatingPlan();
        }
    }

    if (btnViewGrid) btnViewGrid.addEventListener('click', () => switchViewMode('grid'));
    if (btnViewList) btnViewList.addEventListener('click', () => switchViewMode('list'));
    if (btnViewSeating) btnViewSeating.addEventListener('click', () => switchViewMode('seating'));

    initSeatingPlanControls();

    // Sound Toggle
    const btnSound = document.getElementById('btnSoundToggle');
    if (btnSound) {
        btnSound.addEventListener('click', () => {
            state.soundEnabled = !state.soundEnabled;
            btnSound.textContent = state.soundEnabled ? '🔊' : '🔇';
            btnSound.classList.toggle('active', state.soundEnabled);
            showToast(`Suara Peringatan: ${state.soundEnabled ? 'Aktif' : 'Dimatikan'}`, 'info');
        });
    }

    // Log modal trigger
    const btnLogs = document.getElementById('btnOpenLogs');
    if (btnLogs) btnLogs.addEventListener('click', openLogsModal);

    // Simulator Trigger
    const btnToggleSim = document.getElementById('btnToggleSim');
    const btnToggleSimText = document.getElementById('btnToggleSimText');
    if (btnToggleSim) {
        fetch('/api/simulation/status')
            .then(res => res.json())
            .then(data => {
                if (data && data.simulation_enabled) {
                    btnToggleSim.classList.add('active');
                    if (btnToggleSimText) btnToggleSimText.textContent = 'Simulasi Aktif (12 PC)';
                } else {
                    btnToggleSim.classList.remove('active');
                    if (btnToggleSimText) btnToggleSimText.textContent = 'Simulasi Lab';
                }
            })
            .catch(() => {});

        btnToggleSim.addEventListener('click', async () => {
            try {
                btnToggleSim.disabled = true;
                const res = await fetch('/api/simulation/toggle', { method: 'POST' });
                const data = await res.json();
                if (data.simulation_enabled) {
                    btnToggleSim.classList.add('active');
                    if (btnToggleSimText) btnToggleSimText.textContent = 'Simulasi Aktif (12 PC)';
                    showToast('⚡ Mode simulasi aktif! 12 PC siswa virtual sedang berjalan.', 'success');
                } else {
                    btnToggleSim.classList.remove('active');
                    if (btnToggleSimText) btnToggleSimText.textContent = 'Simulasi Lab';
                    showToast('Simulasi dinonaktifkan.', 'info');
                }
            } catch (err) {
                showToast('Gagal mengubah status simulasi: ' + err.message, 'danger');
            } finally {
                btnToggleSim.disabled = false;
            }
        });
    }

    // Download agent modal trigger
    const btnDownloadAgent = document.getElementById('btnDownloadAgent');
    if (btnDownloadAgent) {
        btnDownloadAgent.addEventListener('click', openDownloadAgentModal);
    }

    // Copy Download URL Button
    const btnCopyUrl = document.getElementById('btnCopyDownloadUrl');
    if (btnCopyUrl) {
        btnCopyUrl.addEventListener('click', () => {
            const inputUrl = document.getElementById('inputDownloadUrl');
            if (inputUrl && inputUrl.value) {
                navigator.clipboard.writeText(inputUrl.value).then(() => {
                    const orig = btnCopyUrl.innerHTML;
                    btnCopyUrl.innerHTML = '✅ Tersalin!';
                    showToast('Tautan unduh agen berhasil disalin ke clipboard!', 'success');
                    setTimeout(() => {
                        btnCopyUrl.innerHTML = orig;
                    }, 2000);
                }).catch(() => {
                    inputUrl.select();
                    document.execCommand('copy');
                    showToast('Tautan unduh agen berhasil disalin!', 'success');
                });
            }
        });
    }

    // Refresh grid
    const btnRefresh = document.getElementById('btnRefreshGrid');
    if (btnRefresh) {
        btnRefresh.addEventListener('click', () => {
            renderClients();
            showToast('Tampilan client dimuat ulang.', 'info');
        });
    }

    // Selection Controls
    const selectAllCheckbox = document.getElementById('selectAllCheckbox');
    if (selectAllCheckbox) {
        selectAllCheckbox.addEventListener('change', (e) => {
            const visible = Array.from(state.clients.values()).filter(isClientVisible);
            if (e.target.checked) {
                visible.forEach(c => state.selectedClientIds.add(c.client_id));
            } else {
                state.selectedClientIds.clear();
            }
            renderClients();
        });
    }

    const btnSelectOnline = document.getElementById('btnSelectOnlineOnly');
    if (btnSelectOnline) {
        btnSelectOnline.addEventListener('click', () => {
            state.selectedClientIds.clear();
            Array.from(state.clients.values())
                .filter(c => c.is_online && isClientVisible(c))
                .forEach(c => state.selectedClientIds.add(c.client_id));
            renderClients();
        });
    }

    const btnClearSelection = document.getElementById('btnClearSelection');
    if (btnClearSelection) {
        btnClearSelection.addEventListener('click', () => {
            state.selectedClientIds.clear();
            renderClients();
        });
    }

    // Bulk Buttons
    const btnBulkBroadcast = document.getElementById('btnBulkBroadcast');
    if (btnBulkBroadcast) btnBulkBroadcast.addEventListener('click', openBroadcastModal);

    const btnBulkSendFile = document.getElementById('btnBulkSendFile');
    if (btnBulkSendFile) btnBulkSendFile.addEventListener('click', () => openSendFileModal());

    const btnBulkLock = document.getElementById('btnBulkLock');
    if (btnBulkLock) {
        btnBulkLock.addEventListener('click', () => {
            const target = getTargetForAction();
            sendCommand(target, 'lock', { message: 'Layar Komputer Ini Dikunci oleh Guru / Administrator' });
        });
    }

    const btnBulkUnlock = document.getElementById('btnBulkUnlock');
    if (btnBulkUnlock) {
        btnBulkUnlock.addEventListener('click', () => {
            const target = getTargetForAction();
            sendCommand(target, 'unlock');
        });
    }

    const btnBulkLaunch = document.getElementById('btnBulkLaunchApp');
    if (btnBulkLaunch) btnBulkLaunch.addEventListener('click', openLaunchAppModal);

    const btnBulkPower = document.getElementById('btnBulkPower');
    if (btnBulkPower) btnBulkPower.addEventListener('click', openPowerModal);

    const btnBulkWake = document.getElementById('btnBulkWake');
    if (btnBulkWake) {
        btnBulkWake.addEventListener('click', () => {
            const target = getTargetForAction();
            const desc = getTargetDescription();
            if (confirm(`Kirim sinyal Wake-on-LAN (Magic Packet) untuk menghidupkan ${desc}?`)) {
                sendCommand(target, 'wake');
                showToast(`Sinyal Wake-on-LAN (WOL) sedang dikirim ke ${desc}...`, 'info');
                playSound('blip');
            }
        });
    }

    // Modal Close Buttons
    document.querySelectorAll('.btn-close-modal, .btn-secondary').forEach(btn => {
        btn.addEventListener('click', () => {
            const modalId = btn.getAttribute('data-modal');
            if (modalId) closeModal(modalId);
        });
    });

    // Close on backdrop click
    document.querySelectorAll('.modal-backdrop').forEach(modal => {
        modal.addEventListener('click', (e) => {
            if (e.target === modal) modal.style.display = 'none';
        });
    });

    // Template Chips in Broadcast Modal
    document.querySelectorAll('.chip-btn').forEach(chip => {
        chip.addEventListener('click', () => {
            const text = chip.getAttribute('data-template');
            const txtInput = document.getElementById('broadcastMessageInput');
            if (txtInput) txtInput.value = text;
        });
    });

    // Submit Broadcast
    const btnSubmitBroadcast = document.getElementById('btnSubmitBroadcast');
    if (btnSubmitBroadcast) {
        btnSubmitBroadcast.addEventListener('click', () => {
            const msgInput = document.getElementById('broadcastMessageInput');
            const urgentToggle = document.getElementById('broadcastUrgentToggle');
            const text = (msgInput ? msgInput.value : '').trim();
            if (!text) {
                alert('Silakan ketik isi pesan pengumuman terlebih dahulu.');
                return;
            }
            const target = getTargetForAction();
            const urgent = urgentToggle ? urgentToggle.checked : false;

            sendCommand(target, 'broadcast', { message: text, urgent: urgent });
            closeModal('modalBroadcast');
            if (msgInput) msgInput.value = '';
        });
    }

    // Enhanced Launch App Modal logic
    // 1. Category Tabs Filtering
    document.querySelectorAll('.app-category-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            document.querySelectorAll('.app-category-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            const cat = tab.getAttribute('data-cat');
            document.querySelectorAll('.preset-app-btn').forEach(btn => {
                const btnCat = btn.getAttribute('data-cat');
                if (cat === 'all' || btnCat === cat) {
                    btn.style.display = 'flex';
                } else {
                    btn.style.display = 'none';
                }
            });
        });
    });

    // 2. Preset Apps Selection & Double-Click Launch
    document.querySelectorAll('.preset-app-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.preset-app-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            const appCmd = btn.getAttribute('data-app');
            const customInput = document.getElementById('customAppCommandInput');
            if (customInput) customInput.value = appCmd;
        });

        // Double-click to instantly launch
        btn.addEventListener('dblclick', () => {
            const appCmd = (btn.getAttribute('data-app') || '').trim();
            if (appCmd) {
                const target = state.launchAppTargetSingle || getTargetForAction();
                const desc = state.launchAppTargetSingle 
                    ? (getClientDisplayName(state.launchAppTargetSingle) || state.launchAppTargetSingle)
                    : getTargetDescription();
                sendCommand(target, 'launch_app', { app: appCmd });
                showToast(`Meluncurkan <strong>${escapeHtml(appCmd)}</strong> ke ${desc}...`, 'success');
                playSound('blip');
                closeModal('modalLaunchApp');
            }
        });
    });

    // 3. Quick URL Chips in Chrome Launcher Box
    document.querySelectorAll('.quick-url-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            const url = chip.getAttribute('data-url') || '';
            const urlInput = document.getElementById('quickUrlInput');
            const customInput = document.getElementById('customAppCommandInput');
            if (urlInput) urlInput.value = url;
            if (customInput) customInput.value = url;
            document.querySelectorAll('.preset-app-btn').forEach(b => b.classList.remove('active'));
        });
    });

    // 4. Quick URL Apply Button
    const btnApplyQuickUrl = document.getElementById('btnApplyQuickUrl');
    if (btnApplyQuickUrl) {
        btnApplyQuickUrl.addEventListener('click', () => {
            const urlInput = document.getElementById('quickUrlInput');
            let url = (urlInput ? urlInput.value : '').trim();
            if (!url) {
                alert('Silakan masukkan link website terlebih dahulu.');
                return;
            }
            if (!url.startsWith('http://') && !url.startsWith('https://')) {
                url = 'https://' + url;
                if (urlInput) urlInput.value = url;
            }
            const customInput = document.getElementById('customAppCommandInput');
            if (customInput) customInput.value = url;
            showToast(`Tautan dipilih: ${url}`, 'info');
        });
    }

    // 5. Submit Launch App
    const btnSubmitLaunch = document.getElementById('btnSubmitLaunchApp');
    if (btnSubmitLaunch) {
        btnSubmitLaunch.onclick = () => {
            const customInput = document.getElementById('customAppCommandInput');
            let appCmd = (customInput ? customInput.value : '').trim();
            if (!appCmd) {
                appCmd = 'chrome';
            }

            let target;
            let desc;

            if (state.launchAppTargetMode === 'custom') {
                if (state.launchAppSelectedCids.size === 0) {
                    showToast('Pilih minimal 1 komputer siswa pada daftar di bawah!', 'warning');
                    return;
                }
                if (state.launchAppSelectedCids.size === 1) {
                    const cid = Array.from(state.launchAppSelectedCids)[0];
                    target = cid;
                    const c = state.clients.get(cid);
                    desc = c ? `${getClientDisplayName(c)} (${c.ip_address})` : cid;
                } else {
                    target = Array.from(state.launchAppSelectedCids);
                    desc = `${state.launchAppSelectedCids.size} Komputer Terpilih`;
                }
            } else {
                target = 'all';
                desc = 'Semua Komputer Siswa (Online)';
            }

            sendCommand(target, 'launch_app', { app: appCmd });
            showToast(`Meluncurkan <strong>${escapeHtml(appCmd)}</strong> ke ${desc}...`, 'success');
            playSound('blip');
            closeModal('modalLaunchApp');
        };
    }

// Global Power Action Dispatcher (Callable directly from HTML onclick or JS listeners)
window.executePowerAction = function(actionType) {
    const target = getTargetForAction();
    const desc = getTargetDescription();

    if (actionType === 'shutdown') {
        sendCommand(target, 'shutdown');
        showToast(`⚡ Mengirim perintah <strong>MATIKAN KOMPUTER</strong> ke ${desc}...`, 'warning');
        playSound('blip');
        closeModal('modalPower');
    } else if (actionType === 'restart') {
        sendCommand(target, 'restart');
        showToast(`🔄 Mengirim perintah <strong>RESTART</strong> ke ${desc}...`, 'warning');
        playSound('blip');
        closeModal('modalPower');
    } else if (actionType === 'wake') {
        sendCommand(target, 'wake');
        showToast(`⚡ Sinyal <strong>Wake-on-LAN</strong> berhasil dikirim ke ${desc}!`, 'success');
        playSound('blip');
        closeModal('modalPower');
    }
};

    // Power Options
    const optWake = document.getElementById('optWake');
    if (optWake) {
        optWake.onclick = () => window.executePowerAction('wake');
    }

    const optShutdown = document.getElementById('optShutdown');
    if (optShutdown) {
        optShutdown.onclick = () => window.executePowerAction('shutdown');
    }

    const optRestart = document.getElementById('optRestart');
    if (optRestart) {
        optRestart.onclick = () => window.executePowerAction('restart');
    }

    // Inspect Modal Single Controls
    const btnInspectRemote = document.getElementById('btnInspectRemote');
    if (btnInspectRemote) {
        btnInspectRemote.addEventListener('click', () => {
            const cid = state.activeInspectId;
            if (!cid) return;
            closeModal('modalInspect');
            openRemoteControl(cid);
        });
    }

    const btnInspectWake = document.getElementById('btnInspectWake');
    if (btnInspectWake) {
        btnInspectWake.addEventListener('click', () => {
            const cid = state.activeInspectId;
            if (!cid) return;
            const client = state.clients.get(cid);
            const name = client ? (client.hostname || cid) : cid;
            sendCommand(cid, 'wake');
            showToast(`Sinyal Wake-on-LAN dikirim ke ${name}`, 'success');
            playSound('blip');
        });
    }

    // Rename PC Modal Event Listeners
    const btnSubmitRenamePc = document.getElementById('btnSubmitRenamePc');
    if (btnSubmitRenamePc) {
        btnSubmitRenamePc.addEventListener('click', submitRenamePc);
    }
    const inputNewPcName = document.getElementById('inputNewPcName');
    if (inputNewPcName) {
        inputNewPcName.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                submitRenamePc();
            }
        });
    }
    const btnResetPcName = document.getElementById('btnResetPcName');
    if (btnResetPcName) {
        btnResetPcName.addEventListener('click', () => {
            if (inputNewPcName) inputNewPcName.value = '';
            submitRenamePc();
        });
    }
    const renameChips = document.querySelectorAll('.chip-rename-sug');
    renameChips.forEach(btn => {
        btn.addEventListener('click', () => {
            const sug = btn.getAttribute('data-sug');
            if (inputNewPcName && sug) {
                inputNewPcName.value = sug;
                inputNewPcName.focus();
            }
        });
    });

    // Inspect modal rename buttons
    const btnInspectRename = document.getElementById('btnInspectRename');
    if (btnInspectRename) {
        btnInspectRename.addEventListener('click', () => {
            if (state.activeInspectId) openRenameModal(state.activeInspectId);
        });
    }
    const btnInspectRenameAction = document.getElementById('btnInspectRenameAction');
    if (btnInspectRenameAction) {
        btnInspectRenameAction.addEventListener('click', () => {
            if (state.activeInspectId) openRenameModal(state.activeInspectId);
        });
    }

    const btnInspectLock = document.getElementById('btnInspectLockToggle');
    if (btnInspectLock) {
        btnInspectLock.onclick = () => {
            const cid = state.activeInspectId;
            if (!cid) return;
            const client = state.clients.get(cid);
            if (!client) return;
            const btnText = (btnInspectLock.textContent || '').toLowerCase();
            const wantsToUnlock = Boolean(client.is_locked) || btnText.includes('buka');
            const action = wantsToUnlock ? 'unlock' : 'lock';

            // Optimistic update
            client.is_locked = wantsToUnlock ? 0 : 1;
            updateInspectModalContent(client);
            updateOrAddClientCard(client);

            sendCommand(cid, action, { message: 'Layar dikunci oleh Guru / Administrator' });
            showToast(wantsToUnlock ? `Membuka kunci layar ${getClientDisplayName(client)}...` : `Mengunci layar ${getClientDisplayName(client)}...`, wantsToUnlock ? 'success' : 'warning');
        };
    }

    const btnInspectMsg = document.getElementById('btnInspectMessage');
    if (btnInspectMsg) {
        btnInspectMsg.addEventListener('click', () => {
            const cid = state.activeInspectId;
            if (cid) openBroadcastModalForSingle(cid);
        });
    }

    const btnInspectSendFile = document.getElementById('btnInspectSendFile');
    if (btnInspectSendFile) {
        btnInspectSendFile.addEventListener('click', () => {
            const cid = state.activeInspectId;
            if (cid) openSendFileModalForSingle(cid);
        });
    }

    const btnInspectLaunch = document.getElementById('btnInspectLaunchApp');
    if (btnInspectLaunch) {
        btnInspectLaunch.addEventListener('click', () => {
            const cid = state.activeInspectId;
            if (cid) openLaunchAppModalForSingle(cid);
        });
    }

    const btnInspectRestart = document.getElementById('btnInspectRestart');
    if (btnInspectRestart) {
        btnInspectRestart.onclick = () => {
            const cid = state.activeInspectId;
            if (!cid) return;
            sendCommand(cid, 'restart');
            showToast(`Mengirim perintah RESTART ke <strong>${cid}</strong>...`, 'warning');
            playSound('blip');
        };
    }

    const btnInspectPower = document.getElementById('btnInspectPower');
    if (btnInspectPower) {
        btnInspectPower.onclick = () => {
            const cid = state.activeInspectId;
            if (!cid) return;
            sendCommand(cid, 'shutdown');
            showToast(`Mengirim perintah MATIKAN KOMPUTER ke <strong>${cid}</strong>...`, 'warning');
            playSound('blip');
        };
    }

    const btnInspectDelete = document.getElementById('btnInspectDelete');
    if (btnInspectDelete) {
        btnInspectDelete.addEventListener('click', async () => {
            const cid = state.activeInspectId;
            if (!cid) return;
            const client = state.clients.get(cid);
            const name = client ? (client.hostname || cid) : cid;
            if (confirm(`Hapus komputer "${name}" dari dashboard?\n\nKomputer ini tidak akan ditampilkan lagi kecuali agen diinstal & dijalankan kembali di PC tersebut.`)) {
                try {
                    const res = await fetch(`/api/client/${encodeURIComponent(cid)}`, { method: 'DELETE' });
                    if (res.ok) {
                        showToast(`Komputer "${name}" berhasil dihapus dari dashboard.`, 'success');
                        closeModal('modalInspect');
                    } else {
                        showToast('Gagal menghapus komputer.', 'danger');
                    }
                } catch (err) {
                    showToast('Terjadi kesalahan: ' + err.message, 'danger');
                }
            }
        });
    }

    // Installed Software Search & Refresh Listeners
    const installedSearch = document.getElementById('installedAppSearchInput');
    if (installedSearch) {
        installedSearch.addEventListener('input', (e) => {
            state.installedAppSearchQuery = e.target.value;
            if (state.activeInspectId) {
                renderInspectInstalledApps(state.activeInspectId);
            }
        });
    }

    const btnRefreshInstalledApps = document.getElementById('btnRefreshInstalledApps');
    if (btnRefreshInstalledApps) {
        btnRefreshInstalledApps.addEventListener('click', () => {
            if (state.activeInspectId) {
                loadClientInstalledApps(state.activeInspectId, true);
            }
        });
    }

    const btnRemoteOpenAppPicker = document.getElementById('btnRemoteOpenAppPicker');
    if (btnRemoteOpenAppPicker) {
        btnRemoteOpenAppPicker.addEventListener('click', () => {
            if (!state.remoteClientId) return;
            const cid = state.remoteClientId;
            openInspectModal(cid);
            switchInspectTab('installed');
        });
    }

    // Log modal toolbar buttons
    const btnExportLogsCSV = document.getElementById('btnExportLogsCSV');
    if (btnExportLogsCSV) {
        btnExportLogsCSV.addEventListener('click', exportLogsToCSV);
    }

    const btnExportClientsCSV = document.getElementById('btnExportClientsCSV');
    if (btnExportClientsCSV) {
        btnExportClientsCSV.addEventListener('click', exportClientsToCSV);
    }

    const btnHeaderExportClients = document.getElementById('btnHeaderExportClients');
    if (btnHeaderExportClients) {
        btnHeaderExportClients.addEventListener('click', exportClientsToCSV);
    }

    const btnRefreshLogs = document.getElementById('btnRefreshLogs');
    if (btnRefreshLogs) {
        btnRefreshLogs.addEventListener('click', () => {
            loadAuditLogs();
            showToast('Catatan log diperbarui.', 'info');
        });
    }

    // Clear logs
    const btnClearLogs = document.getElementById('btnClearLogs');
    if (btnClearLogs) {
        btnClearLogs.addEventListener('click', async () => {
            if (confirm('Hapus seluruh riwayat log aktivitas?')) {
                await fetch('/api/logs/clear', { method: 'POST' });
                loadAuditLogs();
            }
        });
    }

    // Keyboard Shortcuts
    document.addEventListener('keydown', (e) => {
        // Ctrl+A -> Select all visible
        if (e.ctrlKey && e.key.toLowerCase() === 'a' && !['input', 'textarea'].includes(document.activeElement.tagName.toLowerCase())) {
            e.preventDefault();
            const visible = Array.from(state.clients.values()).filter(isClientVisible);
            visible.forEach(c => state.selectedClientIds.add(c.client_id));
            renderClients();
        }
        // Escape -> Close modals
        if (e.key === 'Escape') {
            if (state.remoteClientId) {
                closeRemoteControl(true);
            }
            document.querySelectorAll('.modal-backdrop').forEach(m => m.style.display = 'none');
        }
    });

    // Remote Control Studio listeners
    setupRemoteControlListeners();
    initSendFileEvents();
}

// --- Send File Event Listeners ---
function initSendFileEvents() {
    const dropzone = document.getElementById('fileDropzone');
    const fileInput = document.getElementById('fileUploadInput');
    const btnRemove = document.getElementById('btnRemoveSelectedFile');
    const btnSubmit = document.getElementById('btnSubmitSendFile');

    if (dropzone && fileInput) {
        dropzone.addEventListener('click', (e) => {
            if (e.target.closest('#btnRemoveSelectedFile')) return;
            fileInput.click();
        });

        fileInput.addEventListener('change', (e) => {
            if (e.target.files && e.target.files[0]) {
                handleFileSelection(e.target.files[0]);
            }
        });

        // Drag & drop handlers
        ['dragenter', 'dragover'].forEach(eventName => {
            dropzone.addEventListener(eventName, (e) => {
                e.preventDefault();
                e.stopPropagation();
                dropzone.classList.add('dragover');
            });
        });

        ['dragleave', 'drop'].forEach(eventName => {
            dropzone.addEventListener(eventName, (e) => {
                e.preventDefault();
                e.stopPropagation();
                dropzone.classList.remove('dragover');
            });
        });

        dropzone.addEventListener('drop', (e) => {
            const dt = e.dataTransfer;
            if (dt && dt.files && dt.files[0]) {
                handleFileSelection(dt.files[0]);
            }
        });
    }

    if (btnRemove) {
        btnRemove.addEventListener('click', (e) => {
            e.stopPropagation();
            resetSendFileForm();
        });
    }

    // Radio destination selection
    document.querySelectorAll('.dest-radio-card input[type="radio"]').forEach(radio => {
        radio.addEventListener('change', () => {
            document.querySelectorAll('.dest-radio-card').forEach(card => card.classList.remove('active'));
            const parent = radio.closest('.dest-radio-card');
            if (parent) parent.classList.add('active');
        });
    });

    if (btnSubmit) {
        btnSubmit.addEventListener('click', async () => {
            if (!currentSendFileObject) {
                showToast('Pilih berkas yang ingin dikirim terlebih dahulu!', 'warning');
                return;
            }

            const destRadio = document.querySelector('input[name="destFolder"]:checked');
            const destination = destRadio ? destRadio.value : 'desktop';
            const autoOpen = document.getElementById('chkAutoOpenFile')?.checked ?? true;
            const showPopup = document.getElementById('chkShowPopupNotice')?.checked ?? true;

            const progressBox = document.getElementById('transferProgressBox');
            const progressLabel = document.getElementById('transferProgressLabel');
            const progressPct = document.getElementById('transferProgressPct');
            const progressBar = document.getElementById('transferProgressBar');

            if (progressBox) progressBox.style.display = 'block';
            if (progressBar) progressBar.style.width = '0%';
            btnSubmit.disabled = true;
            btnSubmit.innerHTML = '<span>⏳ Mengirim Berkas...</span>';

            const formData = new FormData();
            formData.append('file', currentSendFileObject);

            let targetStr = 'all';
            if (Array.isArray(currentSendFileTarget)) {
                targetStr = currentSendFileTarget.join(',');
            } else if (currentSendFileTarget) {
                targetStr = currentSendFileTarget;
            }
            formData.append('target', targetStr);
            formData.append('destination', destination);
            formData.append('auto_open', autoOpen ? 'true' : 'false');
            formData.append('show_popup', showPopup ? 'true' : 'false');

            try {
                const xhr = new XMLHttpRequest();
                const targetUrl = typeof getApiUrl === 'function' ? getApiUrl('/api/files/distribute') : '/api/files/distribute';

                xhr.open('POST', targetUrl, true);

                xhr.upload.onprogress = (e) => {
                    if (e.lengthComputable) {
                        const pct = Math.round((e.loaded / e.total) * 100);
                        if (progressBar) progressBar.style.width = `${pct}%`;
                        if (progressPct) progressPct.textContent = `${pct}%`;
                        if (progressLabel) progressLabel.textContent = pct < 100 ? 'Mengunggah berkas ke server...' : 'Mendistribusikan ke komputer siswa...';
                    }
                };

                xhr.onload = () => {
                    if (xhr.status >= 200 && xhr.status < 300) {
                        try {
                            const res = JSON.parse(xhr.responseText);
                            const sentCount = res.sent_count || 0;
                            const destLabel = destination === 'desktop' ? 'Desktop' : (destination === 'downloads' ? 'Downloads' : 'Documents');
                            showToast(`Berkas <strong>${res.filename}</strong> berhasil dikirim ke <strong>${sentCount} PC siswa</strong> (${destLabel})!`, 'success');
                            playSound('blip');
                            closeModal('modalSendFile');
                        } catch (e) {
                            showToast('Berkas berhasil dikirim ke PC siswa!', 'success');
                            closeModal('modalSendFile');
                        }
                    } else {
                        showToast(`Gagal mengirim file (HTTP ${xhr.status})`, 'danger');
                    }
                    btnSubmit.disabled = false;
                    btnSubmit.innerHTML = '<span>🚀 Kirim File ke Siswa</span>';
                };

                xhr.onerror = () => {
                    showToast('Gagal terhubung ke server saat mengirim file.', 'danger');
                    btnSubmit.disabled = false;
                    btnSubmit.innerHTML = '<span>🚀 Kirim File ke Siswa</span>';
                };

                xhr.send(formData);

            } catch (err) {
                showToast(`Error: ${err.message}`, 'danger');
                btnSubmit.disabled = false;
                btnSubmit.innerHTML = '<span>🚀 Kirim File ke Siswa</span>';
            }
        });
    }
}

// ==========================================================================
// VISUAL SEATING PLAN (DENAH MEJA LABORATORIUM DRAG & DROP)
// ==========================================================================

function getActiveRoomLayoutKey() {
    return state.roomFilter || 'all';
}

async function loadAndRenderSeatingPlan() {
    const roomKey = getActiveRoomLayoutKey();
    const subtitleEl = document.getElementById('seatingRoomSubtitle');
    if (subtitleEl) {
        subtitleEl.textContent = roomKey === 'all' ? 'Semua Ruang Lab' : roomKey;
    }

    if (!state.seatingLayouts.has(roomKey)) {
        try {
            const res = await fetch(`/api/seating-plan?room=${encodeURIComponent(roomKey)}`);
            if (res.ok) {
                const data = await res.json();
                if (data && data.layout && typeof data.layout === 'object' && Object.keys(data.layout).length > 0) {
                    state.seatingLayouts.set(roomKey, data.layout);
                }
            }
        } catch (e) {
            console.warn('[Seating Plan] Gagal mengambil tata letak dari server:', e);
        }

        // Fallback to localStorage
        if (!state.seatingLayouts.has(roomKey)) {
            const local = localStorage.getItem(`labguard_seating_${roomKey}`);
            if (local) {
                try {
                    state.seatingLayouts.set(roomKey, JSON.parse(local));
                } catch (e) {}
            }
        }

        if (!state.seatingLayouts.has(roomKey)) {
            state.seatingLayouts.set(roomKey, {});
        }
    }

    renderSeatingPlan();
}

function saveSeatingPlanToServer(showToastNotice = false) {
    const roomKey = getActiveRoomLayoutKey();
    const layout = state.seatingLayouts.get(roomKey) || {};

    try {
        localStorage.setItem(`labguard_seating_${roomKey}`, JSON.stringify(layout));
    } catch (e) {}

    fetch('/api/seating-plan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ room: roomKey, layout: layout })
    })
    .then(res => res.json())
    .then(data => {
        if (showToastNotice) {
            showToast(data.message || 'Denah tata letak meja berhasil disimpan!', 'success');
        }
    })
    .catch(err => {
        console.error('[Seating Plan] Save error:', err);
        if (showToastNotice) {
            showToast('Tersimpan di penyimpanan lokal peramban.', 'warning');
        }
    });
}

function debounceSaveSeatingPlan() {
    if (state.seatingSaveTimer) clearTimeout(state.seatingSaveTimer);
    state.seatingSaveTimer = setTimeout(() => {
        saveSeatingPlanToServer(false);
    }, 1200);
}

function renderSeatingPlan() {
    const canvas = document.getElementById('seatingCanvas');
    if (!canvas) return;

    const visibleClients = sortClients(Array.from(state.clients.values()).filter(isClientVisible));
    const roomKey = getActiveRoomLayoutKey();
    const layout = state.seatingLayouts.get(roomKey) || {};

    // Separate Teacher PC(s) from student PCs
    const teacherClients = visibleClients.filter(c => isTeacherClient(c));
    const studentClients = visibleClients.filter(c => !isTeacherClient(c));

    // Position Teacher PC(s) front and center at y = 15
    teacherClients.forEach((t, tIdx) => {
        if (!layout[t.client_id] || typeof layout[t.client_id].x !== 'number') {
            layout[t.client_id] = { x: 450 + (tIdx * 200), y: 15 };
        }
    });

    // Auto-calculate coordinates for student PCs (starts below teacher station at y = 215)
    let unpositionedIndex = 0;
    studentClients.forEach(c => {
        if (!layout[c.client_id] || typeof layout[c.client_id].x !== 'number') {
            const col = unpositionedIndex % 3;
            const row = Math.floor(unpositionedIndex / 3);
            const defaultX = 30 + (col * 390);
            const defaultY = 215 + (row * 195);
            layout[c.client_id] = { x: defaultX, y: defaultY };
            unpositionedIndex++;
        }
    });
    state.seatingLayouts.set(roomKey, layout);

    // Remove cards no longer visible
    const existingCards = canvas.querySelectorAll('.seating-pc-card');
    const visibleIds = new Set(visibleClients.map(c => c.client_id));
    existingCards.forEach(card => {
        if (!visibleIds.has(card.dataset.cid)) {
            card.remove();
        }
    });

    // Render or update visible client cards
    visibleClients.forEach(c => {
        let card = document.getElementById(`seating-card-${c.client_id}`);
        const isTeacher = isTeacherClient(c);
        const pos = layout[c.client_id] || { x: isTeacher ? 450 : 30, y: isTeacher ? 15 : 215 };

        if (!card) {
            card = createSeatingCardElement(c, pos.x, pos.y);
            canvas.appendChild(card);
            bindSeatingCardDrag(card, c.client_id);
        } else {
            if (!card.classList.contains('dragging')) {
                card.style.left = `${pos.x}px`;
                card.style.top = `${pos.y}px`;
            }
            updateSeatingCardElement(card, c);
        }
    });

    // Expand canvas height if needed
    let maxY = 640;
    Object.values(layout).forEach(pos => {
        if (pos && typeof pos.y === 'number' && (pos.y + 240 > maxY)) {
            maxY = pos.y + 240;
        }
    });
    canvas.style.minHeight = `${maxY}px`;
}

function createSeatingCardElement(c, x, y) {
    const card = document.createElement('div');
    card.id = `seating-card-${c.client_id}`;
    const isTeacher = isTeacherClient(c);
    card.className = `seating-pc-card ${isTeacher ? 'teacher-pc' : ''} ${c.is_online ? 'online' : 'offline'} ${c.is_locked ? 'locked' : ''} ${state.selectedClientIds.has(c.client_id) ? 'selected' : ''} ${state.seatingEditMode ? 'mode-drag' : ''}`;
    card.dataset.cid = c.client_id;
    card.style.left = `${x}px`;
    card.style.top = `${y}px`;

    const isSelected = state.selectedClientIds.has(c.client_id);
    const cpuPct = Math.round(c.cpu_pct || 0);
    const ramPct = Math.round(c.ram_pct || 0);
    const cpuClass = cpuPct > 80 ? 'danger' : (cpuPct > 60 ? 'warning' : 'normal');
    const ramClass = ramPct > 85 ? 'danger' : (ramPct > 70 ? 'warning' : 'normal');
    const isGame = (c.active_app || '').toLowerCase().includes('roblox') || (c.active_app || '').toLowerCase().includes('game');
    if (cpuPct > 85 || isGame) card.classList.add('high-alert');

    const thumbSrc = state.thumbnails.get(c.client_id);
    const displayName = getClientDisplayName(c);
    const hasCustomName = Boolean(c.custom_name && c.custom_name.trim() && c.custom_name.trim() !== c.hostname);

    card.innerHTML = `
        ${isTeacher ? `
        <div class="teacher-pc-top-banner" title="Komputer Meja Pengajar / Guru">
            <span>👑 KOMPUTER PENGAJAR</span>
        </div>
        ` : ''}
        <div class="seating-card-header">
            <div class="seating-card-title">
                <input type="checkbox" class="seating-card-chk" data-cid="${c.client_id}" ${isSelected ? 'checked' : ''} title="Pilih PC ini">
                <span title="${escapeHtml(displayName)}${hasCustomName ? ` (Asli: ${escapeHtml(c.hostname)})` : ''}">${escapeHtml(displayName)}</span>
                <button type="button" class="btn-rename-quick btn-rename-seating" data-cid="${c.client_id}" title="Ubah Nama">✏️</button>
                ${isTeacher ? `<span class="badge-teacher-role" title="Komputer Pengajar / Guru">Guru</span>` : ''}
            </div>
            <span class="seating-card-status-dot ${c.is_online ? 'online' : 'offline'}" id="seating-dot-${c.client_id}" title="${c.is_online ? 'Online' : 'Offline'}"></span>
        </div>

        <div class="seating-card-thumb-wrap" title="Klik untuk Remote Control">
            <img id="seating-thumb-${c.client_id}" src="${thumbSrc ? `data:image/jpeg;base64,${thumbSrc}` : ''}" style="${thumbSrc ? '' : 'display:none;'}" alt="Layar">
            <div class="seating-card-thumb-placeholder" id="seating-placeholder-${c.client_id}" style="${thumbSrc ? 'display:none;' : ''}">
                <span>${c.is_online ? '🖥️ Menunggu Layar' : '⚪ Komputer Mati'}</span>
            </div>
            <div class="seating-card-lock-badge" id="seating-lock-badge-${c.client_id}" style="${c.is_locked ? '' : 'display:none;'}">
                🔒 TERKUNCI
            </div>
        </div>

        <div class="seating-card-metrics">
            <div class="seating-metric-row">
                <span>CPU</span>
                <div class="seating-bar-track">
                    <div class="seating-bar-fill ${cpuClass}" id="seating-cpu-bar-${c.client_id}" style="width: ${cpuPct}%"></div>
                </div>
                <span id="seating-cpu-val-${c.client_id}">${cpuPct}%</span>
            </div>
            <div class="seating-metric-row">
                <span>RAM</span>
                <div class="seating-bar-track">
                    <div class="seating-bar-fill ${ramClass}" id="seating-ram-bar-${c.client_id}" style="width: ${ramPct}%"></div>
                </div>
                <span id="seating-ram-val-${c.client_id}">${ramPct}%</span>
            </div>
            <div class="seating-app-pill ${isGame ? 'app-game-alert' : ''}" id="seating-app-${c.client_id}" title="${escapeHtml(c.active_app || '-')}">
                ${escapeHtml(c.active_app || (c.is_online ? 'Desktop / Standby' : 'PC Offline'))}
            </div>
        </div>

        <div class="seating-card-actions">
            ${c.is_online ? `
            <button class="btn-seating-mini-action btn-seating-remote text-cyan" data-cid="${c.client_id}" title="Remote Control Interaktif">🎮</button>
            <button class="btn-seating-mini-action btn-seating-lock" data-cid="${c.client_id}" title="${c.is_locked ? 'Buka Kunci' : 'Kunci Layar'}">${c.is_locked ? '🔓' : '🔒'}</button>
            <button class="btn-seating-mini-action btn-seating-msg" data-cid="${c.client_id}" title="Kirim Pesan">💬</button>
            ` : `
            <button class="btn-seating-mini-action btn-seating-wake text-emerald" data-cid="${c.client_id}" title="Hidupkan PC (Wake-on-LAN)">⚡</button>
            `}
            ${isTeacher ? `
            <button class="btn-seating-mini-action btn-seating-front text-amber" data-cid="${c.client_id}" title="Posisikan ke Meja Depan Guru">📍 Depan</button>
            ` : ''}
            <button class="btn-seating-mini-action btn-seating-teacher-toggle ${isTeacher ? 'text-amber' : ''}" data-cid="${c.client_id}" title="${isTeacher ? 'Ubah menjadi Komputer Siswa' : 'Jadikan Komputer Pengajar / Guru'}">${isTeacher ? '👑' : '🎓'}</button>
            <button class="btn-seating-mini-action btn-seating-inspect" data-cid="${c.client_id}" title="Periksa Rincian & Task Manager">⚙️</button>
        </div>
    `;

    bindSeatingCardEvents(card, c.client_id);
    return card;
}

function bindSeatingCardEvents(card, cid) {
    const btnRename = card.querySelector('.btn-rename-seating');
    if (btnRename) {
        btnRename.addEventListener('click', (e) => {
            e.stopPropagation();
            openRenameModal(cid);
        });
    }

    const chk = card.querySelector('.seating-card-chk');
    if (chk) {
        chk.addEventListener('change', (e) => {
            if (e.target.checked) state.selectedClientIds.add(cid);
            else state.selectedClientIds.delete(cid);
            updateSelectionPill();
            const gridChk = document.querySelector(`#pc-card-${cid} .pc-card-checkbox`);
            if (gridChk) gridChk.checked = e.target.checked;
        });
    }

    const thumbWrap = card.querySelector('.seating-card-thumb-wrap');
    if (thumbWrap) {
        thumbWrap.addEventListener('click', () => {
            const client = state.clients.get(cid);
            if (client && client.is_online) {
                openRemoteControl(cid);
            } else {
                openInspectModal(cid);
            }
        });
    }

    const btnRemote = card.querySelector('.btn-seating-remote');
    if (btnRemote) {
        btnRemote.addEventListener('click', (e) => {
            e.stopPropagation();
            openRemoteControl(cid);
        });
    }

    const btnLock = card.querySelector('.btn-seating-lock');
    if (btnLock) {
        btnLock.addEventListener('click', (e) => {
            e.stopPropagation();
            const client = state.clients.get(cid);
            if (!client) return;
            const newAction = client.is_locked ? 'unlock' : 'lock';
            sendCommand(cid, newAction, { message: 'Layar dikunci oleh Guru / Administrator' });
        });
    }

    const btnMsg = card.querySelector('.btn-seating-msg');
    if (btnMsg) {
        btnMsg.addEventListener('click', (e) => {
            e.stopPropagation();
            openBroadcastModalForSingle(cid);
        });
    }

    const btnWake = card.querySelector('.btn-seating-wake');
    if (btnWake) {
        btnWake.addEventListener('click', (e) => {
            e.stopPropagation();
            const client = state.clients.get(cid);
            const name = client ? (client.hostname || cid) : cid;
            sendCommand(cid, 'wake');
            showToast(`Sinyal Wake-on-LAN dikirim ke ${name}`, 'success');
        });
    }

    const btnInspect = card.querySelector('.btn-seating-inspect');
    if (btnInspect) {
        btnInspect.addEventListener('click', (e) => {
            e.stopPropagation();
            openInspectModal(cid);
        });
    }

    const btnFront = card.querySelector('.btn-seating-front');
    if (btnFront) {
        btnFront.addEventListener('click', (e) => {
            e.stopPropagation();
            card.style.transition = 'left 0.4s cubic-bezier(0.16, 1, 0.3, 1), top 0.4s cubic-bezier(0.16, 1, 0.3, 1)';
            card.style.left = '450px';
            card.style.top = '15px';
            setTimeout(() => { card.style.transition = ''; }, 450);

            const roomKey = getActiveRoomLayoutKey();
            const layout = state.seatingLayouts.get(roomKey) || {};
            layout[cid] = { x: 450, y: 15 };
            state.seatingLayouts.set(roomKey, layout);
            saveSeatingPlanToServer(true);
            showToast('Komputer Pengajar dipindahkan ke Meja Depan Guru', 'success');
        });
    }

    const btnTeacherToggle = card.querySelector('.btn-seating-teacher-toggle');
    if (btnTeacherToggle) {
        btnTeacherToggle.addEventListener('click', async (e) => {
            e.stopPropagation();
            try {
                const res = await fetch(`/api/client/${encodeURIComponent(cid)}/toggle-teacher`, { method: 'POST' });
                if (res.ok) {
                    const data = await res.json();
                    const client = state.clients.get(cid);
                    if (client) client.is_teacher = data.is_teacher;
                    showToast(`${client ? client.hostname : cid} diset sebagai: ${data.is_teacher ? '👑 Komputer Guru / Pengajar' : '👨‍🎓 Komputer Siswa'}`, 'info');
                    renderClients();
                }
            } catch (err) {
                showToast(`Gagal mengubah peran: ${err.message}`, 'danger');
            }
        });
    }
}

function bindSeatingCardDrag(card, clientId) {
    let isDragging = false;
    let startX = 0;
    let startY = 0;
    let initialLeft = 0;
    let initialTop = 0;

    const onPointerDown = (e) => {
        if (!state.seatingEditMode) return;
        if (e.target.closest('button') || e.target.closest('input') || e.target.closest('.seating-card-actions') || e.target.closest('.seating-card-thumb-wrap')) {
            return;
        }

        isDragging = true;
        startX = e.clientX;
        startY = e.clientY;
        initialLeft = parseFloat(card.style.left) || card.offsetLeft;
        initialTop = parseFloat(card.style.top) || card.offsetTop;

        card.classList.add('dragging');
        try {
            card.setPointerCapture(e.pointerId);
        } catch (err) {}
        e.preventDefault();
    };

    const onPointerMove = (e) => {
        if (!isDragging) return;

        const zoom = state.seatingZoom || 1.0;
        const deltaX = (e.clientX - startX) / zoom;
        const deltaY = (e.clientY - startY) / zoom;

        let newX = initialLeft + deltaX;
        let newY = initialTop + deltaY;

        if (state.seatingSnap) {
            newX = Math.round(newX / 20) * 20;
            newY = Math.round(newY / 20) * 20;
        }

        if (newX < 10) newX = 10;
        if (newY < 10) newY = 10;

        card.style.left = `${newX}px`;
        card.style.top = `${newY}px`;
    };

    const onPointerUp = (e) => {
        if (!isDragging) return;
        isDragging = false;
        card.classList.remove('dragging');
        try {
            card.releasePointerCapture(e.pointerId);
        } catch (err) {}

        const finalX = parseFloat(card.style.left) || 10;
        const finalY = parseFloat(card.style.top) || 10;

        const roomKey = getActiveRoomLayoutKey();
        const layout = state.seatingLayouts.get(roomKey) || {};
        layout[clientId] = { x: finalX, y: finalY };
        state.seatingLayouts.set(roomKey, layout);

        debounceSaveSeatingPlan();
    };

    card.addEventListener('pointerdown', onPointerDown);
    card.addEventListener('pointermove', onPointerMove);
    card.addEventListener('pointerup', onPointerUp);
    card.addEventListener('pointercancel', onPointerUp);
}

function updateOrAddSeatingCard(client) {
    if (state.viewMode !== 'seating') return;
    const canvas = document.getElementById('seatingCanvas');
    if (!canvas) return;

    let card = document.getElementById(`seating-card-${client.client_id}`);
    const visible = isClientVisible(client);

    if (!card) {
        if (visible) {
            const roomKey = getActiveRoomLayoutKey();
            const layout = state.seatingLayouts.get(roomKey) || {};
            const pos = layout[client.client_id] || { x: 30, y: 20 };
            card = createSeatingCardElement(client, pos.x, pos.y);
            canvas.appendChild(card);
            bindSeatingCardDrag(card, client.client_id);
        }
    } else {
        card.style.display = visible ? '' : 'none';
        updateSeatingCardElement(card, client);
    }
}

function updateSeatingCardElement(card, client) {
    const isSelected = state.selectedClientIds.has(client.client_id);
    const cpuPct = Math.round(client.cpu_pct || 0);
    const isTeacher = isTeacherClient(client);
    const hasViolation = Boolean(client.violation && client.violation.detected);
    const isGame = hasViolation || (client.active_app || '').toLowerCase().includes('roblox') || (client.active_app || '').toLowerCase().includes('game');
    const isAlert = cpuPct > 85 || isGame || hasViolation;

    card.className = `seating-pc-card ${isTeacher ? 'teacher-pc' : ''} ${client.is_online ? 'online' : 'offline'} ${client.is_locked ? 'locked' : ''} ${isSelected ? 'selected' : ''} ${state.seatingEditMode ? 'mode-drag' : ''} ${isAlert ? 'high-alert' : ''} ${hasViolation ? 'card-violation' : ''}`;

    const dot = document.getElementById(`seating-dot-${client.client_id}`);
    if (dot) {
        dot.className = `seating-card-status-dot ${client.is_online ? 'online' : 'offline'}`;
        dot.title = client.is_online ? 'Online' : 'Offline';
    }

    const lockBadge = document.getElementById(`seating-lock-badge-${client.client_id}`);
    if (lockBadge) {
        lockBadge.style.display = client.is_locked ? '' : 'none';
    }

    updateSeatingCardTelemetry(client.client_id, client);

    if (state.thumbnails.has(client.client_id)) {
        updateSeatingCardThumbnail(client.client_id, state.thumbnails.get(client.client_id));
    }
}

function updateSeatingCardTelemetry(cid, client) {
    const cpuBar = document.getElementById(`seating-cpu-bar-${cid}`);
    const cpuVal = document.getElementById(`seating-cpu-val-${cid}`);
    const ramBar = document.getElementById(`seating-ram-bar-${cid}`);
    const ramVal = document.getElementById(`seating-ram-val-${cid}`);
    const appEl = document.getElementById(`seating-app-${cid}`);

    const cpuPct = Math.round(client.cpu_pct || 0);
    const ramPct = Math.round(client.ram_pct || 0);
    const cpuClass = cpuPct > 80 ? 'danger' : (cpuPct > 60 ? 'warning' : 'normal');
    const ramClass = ramPct > 85 ? 'danger' : (ramPct > 70 ? 'warning' : 'normal');
    const isGame = (client.active_app || '').toLowerCase().includes('roblox') || (client.active_app || '').toLowerCase().includes('game');

    if (cpuBar) {
        cpuBar.style.width = `${Math.min(cpuPct, 100)}%`;
        cpuBar.className = `seating-bar-fill ${cpuClass}`;
    }
    if (cpuVal) cpuVal.textContent = client.is_online ? `${cpuPct}%` : '0%';

    if (ramBar) {
        ramBar.style.width = `${Math.min(ramPct, 100)}%`;
        ramBar.className = `seating-bar-fill ${ramClass}`;
    }
    if (ramVal) ramVal.textContent = client.is_online ? `${ramPct}%` : '0%';

    if (appEl) {
        appEl.textContent = client.active_app || (client.is_online ? 'Desktop / Standby' : 'PC Offline');
        appEl.className = `seating-app-pill ${isGame ? 'app-game-alert' : ''}`;
    }
}

function updateSeatingCardThumbnail(cid, base64Thumb) {
    const thumbImg = document.getElementById(`seating-thumb-${cid}`);
    const placeholder = document.getElementById(`seating-placeholder-${cid}`);
    if (thumbImg && placeholder) {
        if (base64Thumb) {
            thumbImg.src = `data:image/jpeg;base64,${base64Thumb}`;
            thumbImg.style.display = 'block';
            placeholder.style.display = 'none';
        } else {
            thumbImg.style.display = 'none';
            placeholder.style.display = 'flex';
        }
    }
}

function toggleSeatingEditMode() {
    state.seatingEditMode = !state.seatingEditMode;
    const btn = document.getElementById('btnToggleEditSeating');
    const icon = document.getElementById('seatingLockIcon');
    const text = document.getElementById('seatingLockText');

    if (btn) {
        btn.classList.toggle('active', state.seatingEditMode);
        btn.classList.toggle('locked', !state.seatingEditMode);
    }
    if (icon) icon.textContent = state.seatingEditMode ? '🔓' : '🔒';
    if (text) text.textContent = state.seatingEditMode ? 'Geser: Bebas' : 'Posisi: Terkunci';

    const cards = document.querySelectorAll('.seating-pc-card');
    cards.forEach(c => {
        c.classList.toggle('mode-drag', state.seatingEditMode);
    });

    showToast(state.seatingEditMode ? 'Mode Geser Meja aktif. Anda dapat menarik meja.' : 'Posisi meja dikunci.', 'info');
}

function applySeatingPreset(presetType) {
    const visibleClients = sortClients(Array.from(state.clients.values()).filter(isClientVisible));
    if (visibleClients.length === 0) {
        showToast('Tidak ada komputer yang tampil untuk disusun.', 'warning');
        return;
    }

    const roomKey = getActiveRoomLayoutKey();
    const layout = {};

    // Separate Teacher PC from student PCs
    const teacherClients = visibleClients.filter(c => isTeacherClient(c));
    const studentClients = visibleClients.filter(c => !isTeacherClient(c));

    // Position Teacher PC(s) front and center at y = 15
    teacherClients.forEach((t, idx) => {
        layout[t.client_id] = { x: (presetType === 'reallab' ? 840 : (450 + (idx * 200))), y: 15 };
    });

    if (presetType === 'reallab') {
        // Tata Letak Presisi Sesuai Foto Laboratorium Nyata:
        // Meja Guru di depan kanan, Proyektor di depan tengah, 3 lajur meja siswa
        const specificMap = {
            'D18_2026': { x: 40, y: 140 },
            'D1_03': { x: 40, y: 350 },
            'D101_2026': { x: 340, y: 140 },
            'D102_2026': { x: 550, y: 140 },
            'DESKTOP-QPE2QIM': { x: 340, y: 350 },
            'DESKTOP-D7P7ANT': { x: 550, y: 350 },
            'DESKTOP-M81HAJ3': { x: 340, y: 560 },
            'DESKTOP-7SI3B8F': { x: 550, y: 560 },
            'DESKTOP-2PTRR2S': { x: 840, y: 250 },
            'DESKTOP-K7C6ALJ': { x: 840, y: 460 },
            'DESKTOP-JAPMDOG': { x: 840, y: 670 },
            'DESKTOP-6VJO58D': { x: 140, y: 560 },
            'LAB1-PC01': { x: 40, y: 560 },
            'LAB1-PC02': { x: 40, y: 770 }
        };

        const remainingStudents = [];
        studentClients.forEach(c => {
            if (specificMap[c.client_id]) {
                layout[c.client_id] = specificMap[c.client_id];
            } else {
                remainingStudents.push(c);
            }
        });

        remainingStudents.forEach((c, idx) => {
            const col = idx % 3;
            const row = Math.floor(idx / 3);
            layout[c.client_id] = { x: 30 + (col * 390), y: 770 + (row * 195) };
        });
    } else if (presetType === '3cols') {
        studentClients.forEach((c, idx) => {
            const col = idx % 3;
            const row = Math.floor(idx / 3);
            const x = 30 + (col * 390);
            const y = 215 + (row * 195);
            layout[c.client_id] = { x, y };
        });
    } else if (presetType === 'ushape') {
        const total = studentClients.length;
        const perSide = Math.max(2, Math.floor(total / 3));
        const bottomCount = Math.max(1, total - (perSide * 2));

        let idx = 0;
        for (let i = 0; i < perSide && idx < total; i++, idx++) {
            layout[studentClients[idx].client_id] = { x: 30, y: 215 + (i * 190) };
        }
        const bottomY = 215 + (perSide * 190);
        for (let i = 0; i < bottomCount && idx < total; i++, idx++) {
            layout[studentClients[idx].client_id] = { x: 230 + (i * 200), y: bottomY };
        }
        for (let i = 0; idx < total; i++, idx++) {
            layout[studentClients[idx].client_id] = { x: 880, y: bottomY - ((i + 1) * 190) };
        }
    } else if (presetType === 'grid') {
        studentClients.forEach((c, idx) => {
            const col = idx % 4;
            const row = Math.floor(idx / 4);
            const x = 30 + (col * 240);
            const y = 215 + (row * 195);
            layout[c.client_id] = { x, y };
        });
    }

    state.seatingLayouts.set(roomKey, layout);

    visibleClients.forEach(c => {
        const card = document.getElementById(`seating-card-${c.client_id}`);
        const pos = layout[c.client_id];
        if (card && pos) {
            card.style.transition = 'left 0.4s cubic-bezier(0.16, 1, 0.3, 1), top 0.4s cubic-bezier(0.16, 1, 0.3, 1)';
            card.style.left = `${pos.x}px`;
            card.style.top = `${pos.y}px`;
            setTimeout(() => {
                card.style.transition = '';
            }, 450);
        }
    });

    saveSeatingPlanToServer(true);
}

function setSeatingZoom(newZoom) {
    newZoom = Math.max(0.5, Math.min(1.5, Math.round(newZoom * 10) / 10));
    state.seatingZoom = newZoom;
    const zoomDisplay = document.getElementById('zoomLevelDisplay');
    if (zoomDisplay) zoomDisplay.textContent = `${Math.round(newZoom * 100)}%`;

    const viewport = document.getElementById('seatingZoomViewport');
    if (viewport) {
        viewport.style.transform = `scale(${newZoom})`;
    }
}

function initSeatingPlanControls() {
    const btnToggleEdit = document.getElementById('btnToggleEditSeating');
    if (btnToggleEdit) btnToggleEdit.addEventListener('click', toggleSeatingEditMode);

    const btnPresetToggle = document.getElementById('btnPresetDropdownToggle');
    const menuPresets = document.getElementById('seatingPresetsMenu');
    if (btnPresetToggle && menuPresets) {
        btnPresetToggle.addEventListener('click', (e) => {
            e.stopPropagation();
            menuPresets.style.display = menuPresets.style.display === 'none' ? 'flex' : 'none';
        });
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.seating-presets-dropdown-wrapper')) {
                menuPresets.style.display = 'none';
            }
        });
    }

    const btnPresetRealLab = document.getElementById('btnPresetRealLab');
    if (btnPresetRealLab) {
        btnPresetRealLab.addEventListener('click', () => {
            applySeatingPreset('reallab');
            if (menuPresets) menuPresets.style.display = 'none';
        });
    }

    const btnPreset3Cols = document.getElementById('btnPreset3Cols');
    if (btnPreset3Cols) {
        btnPreset3Cols.addEventListener('click', () => {
            applySeatingPreset('3cols');
            if (menuPresets) menuPresets.style.display = 'none';
        });
    }

    const btnPresetUShape = document.getElementById('btnPresetUShape');
    if (btnPresetUShape) {
        btnPresetUShape.addEventListener('click', () => {
            applySeatingPreset('ushape');
            if (menuPresets) menuPresets.style.display = 'none';
        });
    }

    const btnPresetGrid = document.getElementById('btnPresetGrid');
    if (btnPresetGrid) {
        btnPresetGrid.addEventListener('click', () => {
            applySeatingPreset('grid');
            if (menuPresets) menuPresets.style.display = 'none';
        });
    }

    const chkSnap = document.getElementById('chkSeatingSnap');
    if (chkSnap) {
        chkSnap.addEventListener('change', () => {
            state.seatingSnap = chkSnap.checked;
        });
    }

    const btnZoomIn = document.getElementById('btnZoomIn');
    if (btnZoomIn) btnZoomIn.addEventListener('click', () => setSeatingZoom(state.seatingZoom + 0.1));

    const btnZoomOut = document.getElementById('btnZoomOut');
    if (btnZoomOut) btnZoomOut.addEventListener('click', () => setSeatingZoom(state.seatingZoom - 0.1));

    const btnZoomReset = document.getElementById('btnZoomReset');
    if (btnZoomReset) btnZoomReset.addEventListener('click', () => setSeatingZoom(1.0));

    const btnSave = document.getElementById('btnSaveSeating');
    if (btnSave) btnSave.addEventListener('click', () => saveSeatingPlanToServer(true));
}

// Utility: Escape HTML
function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// When tab is restored/visible, refresh visible thumbnails once
document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
        state.thumbnails.forEach((thumb, cid) => {
            updateCardThumbnail(cid, thumb);
        });
        if (state.activeInspectId && state.thumbnails.has(state.activeInspectId)) {
            const largeScreen = document.getElementById('inspectLargeScreen');
            if (largeScreen) largeScreen.src = `data:image/jpeg;base64,${state.thumbnails.get(state.activeInspectId)}`;
        }
    }
});

// --- Theme Manager (Dark / Light Mode) ---
function initTheme() {
    let savedTheme = '';
    try { savedTheme = localStorage.getItem('labguard_theme') || ''; } catch (e) {}
    // If not set by user, check if body has dark-theme by default
    const isDark = (savedTheme === 'dark' || (savedTheme === '' && document.body.classList.contains('dark-theme')));
    setTheme(isDark ? 'dark' : 'light', false);

    const btnThemeToggle = document.getElementById('btnThemeToggle');
    if (btnThemeToggle) {
        btnThemeToggle.onclick = (e) => {
            if (e) e.preventDefault();
            const current = document.body.classList.contains('dark-theme') ? 'dark' : 'light';
            const next = (current === 'dark') ? 'light' : 'dark';
            setTheme(next, true);
        };
    }

    const btnMobileThemeToggle = document.getElementById('btnMobileThemeToggle');
    if (btnMobileThemeToggle) {
        btnMobileThemeToggle.onclick = (e) => {
            if (e) e.preventDefault();
            const current = document.body.classList.contains('dark-theme') ? 'dark' : 'light';
            const next = (current === 'dark') ? 'light' : 'dark';
            setTheme(next, true);
        };
    }
}

function setTheme(theme, showFeedback = false) {
    const btn = document.getElementById('btnThemeToggle');
    const mBtn = document.getElementById('btnMobileThemeToggle');
    const isDark = (theme === 'dark');

    if (isDark) {
        document.body.classList.add('dark-theme');
        try { localStorage.setItem('labguard_theme', 'dark'); } catch (e) {}
        if (btn) {
            btn.innerHTML = '☀️ <span class="hide-mobile">Terang</span>';
            btn.title = 'Beralih ke Mode Terang (Light Mode)';
            btn.classList.add('active');
            btn.style.color = '#f59e0b';
        }
        if (mBtn) {
            const icon = mBtn.querySelector('.dock-icon');
            const label = mBtn.querySelector('.dock-label');
            if (icon) icon.textContent = '☀️';
            if (label) label.textContent = 'Terang';
        }
        if (showFeedback) showToast('Mode Gelap diaktifkan 🌙 (Nyaman untuk lab redup)', 'info');
    } else {
        document.body.classList.remove('dark-theme');
        try { localStorage.setItem('labguard_theme', 'light'); } catch (e) {}
        if (btn) {
            btn.innerHTML = '🌙 <span class="hide-mobile">Gelap</span>';
            btn.title = 'Beralih ke Mode Gelap (Dark Mode)';
            btn.classList.remove('active');
            btn.style.color = '#0284c7';
        }
        if (mBtn) {
            const icon = mBtn.querySelector('.dock-icon');
            const label = mBtn.querySelector('.dock-label');
            if (icon) icon.textContent = '🌙';
            if (label) label.textContent = 'Gelap';
        }
        if (showFeedback) showToast('Mode Terang diaktifkan ☀️', 'info');
    }
}

// --- Mobile Action Dock Listeners (Untuk Guru Berkeliling Membawa Tablet/HP) ---
function setupMobileDock() {
    const btnLock = document.getElementById('dockBtnLockAll');
    if (btnLock) {
        btnLock.addEventListener('click', () => {
            if (confirm('Kunci seluruh layar komputer siswa di lab sekarang?')) {
                sendCommand('all', 'lock_screen', { message: 'Layar komputer dikunci oleh Guru / Pengajar' });
                showToast('Perintah mengunci seluruh PC siswa telah dikirim 🔒', 'warning');
            }
        });
    }

    const btnUnlock = document.getElementById('dockBtnUnlockAll');
    if (btnUnlock) {
        btnUnlock.addEventListener('click', () => {
            sendCommand('all', 'unlock_screen');
            showToast('Layar seluruh PC siswa berhasil dibuka kuncinya 🔓', 'success');
        });
    }

    const btnMute = document.getElementById('dockBtnMuteAll');
    if (btnMute) {
        btnMute.addEventListener('click', () => {
            bulkMuteAudio(true);
            showToast('Seluruh speaker komputer siswa disenyapkan (Muted) 🔇', 'info');
        });
    }

    const btnBcast = document.getElementById('dockBtnBroadcast');
    if (btnBcast) {
        btnBcast.addEventListener('click', () => {
            openModal('modalBroadcast');
        });
    }
}

// --- Initialize App ---
document.addEventListener('DOMContentLoaded', async () => {
    initTheme();
    setupMobileDock();
    try {
        const res = await fetch('/api/auth/status');
        const authData = await res.json();
        if (!authData.authenticated) {
            window.location.replace(BASE_PATH ? `${BASE_PATH}/login` : '/login');
            return;
        }
        if (authData.token) {
            try { localStorage.setItem('labguard_token', authData.token); } catch (e) {}
        }
    } catch (e) {
        // network issue, proceed
    }
    startClock();
    setupEventListeners();
    initWebSocket();
});
