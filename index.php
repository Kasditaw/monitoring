<?php
/**
 * LabGuard PRO v2.0 - Web & WebSocket Monitoring Launcher
 * Provides seamless bridge between Laragon/XAMPP (Apache/Nginx/PHP) and Python FastAPI (Port 8000)
 */

$PORT = 8000;
$HOST = '127.0.0.1';

// Function to check if Python FastAPI server is running on port 8000
function isServerRunning($host, $port, $timeout = 0.5) {
    $fp = @fsockopen($host, $port, $errno, $errstr, $timeout);
    if ($fp) {
        fclose($fp);
        return true;
    }
    return false;
}

// Function to locate Python executable on Windows (system PATH or Laragon fallback)
function findPythonPath() {
    // 1. Try system PATH
    $output = [];
    $ret = -1;
    @exec('where python 2>nul', $output, $ret);
    if ($ret === 0 && !empty($output[0]) && file_exists(trim($output[0]))) {
        return trim($output[0]);
    }

    // 2. Check Laragon Python directory
    $laragonPyPaths = [
        'C:\\laragon\\bin\\python\\python-3.13\\python.exe',
        'C:\\laragon\\bin\\python\\python-3.12\\python.exe',
        'C:\\laragon\\bin\\python\\python-3.11\\python.exe',
        'C:\\laragon\\bin\\python\\python.exe'
    ];
    foreach ($laragonPyPaths as $path) {
        if (file_exists($path)) {
            return $path;
        }
    }

    // 3. Check AppData Python
    $appData = getenv('LOCALAPPDATA');
    if ($appData) {
        $globs = glob($appData . '\\Programs\\Python\\Python*\\python.exe');
        if (!empty($globs) && file_exists($globs[0])) {
            return $globs[0];
        }
    }

    return 'python'; // Fallback
}

$isRunning = isServerRunning($HOST, $PORT);

// Handle AJAX API requests
if (isset($_GET['action'])) {
    header('Content-Type: application/json');

    if ($_GET['action'] === 'check') {
        echo json_encode([
            'online' => $isRunning,
            'port' => $PORT,
            'python' => findPythonPath(),
            'timestamp' => time()
        ]);
        exit;
    }

    if ($_GET['action'] === 'start') {
        if ($isRunning) {
            echo json_encode(['status' => 'already_running', 'message' => 'Server sudah berjalan']);
            exit;
        }

        $python = findPythonPath();
        $scriptPath = __DIR__ . DIRECTORY_SEPARATOR . 'server_web.py';

        if (!file_exists($scriptPath)) {
            echo json_encode(['status' => 'error', 'message' => 'File server_web.py tidak ditemukan']);
            exit;
        }

        // Start server in background without blocking PHP
        $cmd = 'start "" /B "' . $python . '" "' . $scriptPath . '"';
        if (strtoupper(substr(PHP_OS, 0, 3)) === 'WIN') {
            pclose(popen($cmd, 'r'));
        } else {
            exec($python . ' "' . $scriptPath . '" > /dev/null 2>&1 &');
        }

        // Brief delay and check
        usleep(400000); // 400ms
        $nowRunning = isServerRunning($HOST, $PORT);

        echo json_encode([
            'status' => 'started',
            'command' => $cmd,
            'online' => $nowRunning,
            'message' => 'Perintah menjalankan server telah dikirim'
        ]);
        exit;
    }
}

// Target URL when server is active - prioritize HTTP_HOST so LAN IP or domain is preserved
$serverHost = !empty($_SERVER['HTTP_HOST']) ? explode(':', $_SERVER['HTTP_HOST'])[0] : ($_SERVER['SERVER_NAME'] ?? 'localhost');
$targetUrl = 'http://' . $serverHost . ':' . $PORT;

// If server is ALREADY running and user didn't ask for launcher panel, redirect directly
if ($isRunning && !isset($_GET['launcher'])) {
    header('Location: ' . $targetUrl);
    exit;
}
?>
<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>LabGuard PRO v2.0 - Web Monitoring Launcher</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-base: #090d16;
            --bg-card: rgba(18, 26, 45, 0.75);
            --border-card: rgba(255, 255, 255, 0.08);
            --border-glow: rgba(56, 189, 248, 0.25);
            --primary: #38bdf8;
            --primary-glow: rgba(56, 189, 248, 0.4);
            --emerald: #10b981;
            --rose: #f43f5e;
            --amber: #f59e0b;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --font-main: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif;
            --font-mono: 'JetBrains Mono', monospace;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: var(--font-main);
            background: var(--bg-base);
            color: var(--text-main);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px;
            background-image: 
                radial-gradient(circle at 15% 20%, rgba(56, 189, 248, 0.08) 0%, transparent 45%),
                radial-gradient(circle at 85% 80%, rgba(139, 92, 246, 0.07) 0%, transparent 45%);
        }

        .launcher-card {
            background: var(--bg-card);
            border: 1px solid var(--border-card);
            border-radius: 24px;
            padding: 40px;
            max-width: 680px;
            width: 100%;
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            box-shadow: 0 25px 60px -15px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(255, 255, 255, 0.05);
            animation: fadeIn 0.4s ease-out;
        }

        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(12px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .header-brand {
            display: flex;
            align-items: center;
            gap: 16px;
            margin-bottom: 28px;
            padding-bottom: 24px;
            border-bottom: 1px solid var(--border-card);
        }

        .brand-logo {
            width: 56px;
            height: 56px;
            background: linear-gradient(135deg, #0284c7, #6366f1);
            border-radius: 16px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 28px;
            box-shadow: 0 8px 24px rgba(2, 132, 199, 0.35);
        }

        .brand-text h1 {
            font-size: 22px;
            font-weight: 800;
            letter-spacing: -0.02em;
            background: linear-gradient(120deg, #ffffff, #cbd5e1);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .brand-text p {
            font-size: 13px;
            color: var(--text-muted);
            margin-top: 3px;
        }

        .status-hero {
            background: rgba(15, 23, 42, 0.65);
            border: 1px solid var(--border-card);
            border-radius: 18px;
            padding: 22px 24px;
            margin-bottom: 28px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 20px;
        }

        .status-info {
            display: flex;
            align-items: center;
            gap: 14px;
        }

        .status-dot {
            width: 14px;
            height: 14px;
            border-radius: 50%;
            background: var(--rose);
            box-shadow: 0 0 16px var(--rose);
            transition: all 0.3s ease;
        }

        .status-dot.online {
            background: var(--emerald);
            box-shadow: 0 0 16px var(--emerald);
            animation: pulseDot 2s infinite;
        }

        @keyframes pulseDot {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.65; transform: scale(0.9); }
        }

        .status-title {
            font-size: 15px;
            font-weight: 700;
            color: #fff;
        }

        .status-sub {
            font-size: 12px;
            color: var(--text-muted);
            margin-top: 2px;
        }

        .badge-port {
            background: rgba(56, 189, 248, 0.12);
            color: var(--primary);
            border: 1px solid rgba(56, 189, 248, 0.25);
            padding: 6px 14px;
            border-radius: 999px;
            font-size: 12px;
            font-family: var(--font-mono);
            font-weight: 600;
        }

        .action-group {
            display: flex;
            flex-direction: column;
            gap: 14px;
            margin-bottom: 28px;
        }

        .btn-start {
            background: linear-gradient(135deg, #0284c7, #2563eb);
            color: #fff;
            border: none;
            padding: 16px 24px;
            border-radius: 14px;
            font-size: 15px;
            font-weight: 700;
            font-family: var(--font-main);
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 10px;
            box-shadow: 0 10px 25px -5px rgba(2, 132, 199, 0.5);
            transition: all 0.2s ease;
        }

        .btn-start:hover {
            transform: translateY(-2px);
            box-shadow: 0 14px 30px -5px rgba(2, 132, 199, 0.65);
        }

        .btn-start:active {
            transform: translateY(0);
        }

        .btn-start:disabled {
            opacity: 0.65;
            cursor: not-allowed;
            transform: none;
        }

        .btn-open {
            background: linear-gradient(135deg, #10b981, #059669);
            box-shadow: 0 10px 25px -5px rgba(16, 185, 129, 0.5);
        }

        .btn-open:hover {
            box-shadow: 0 14px 30px -5px rgba(16, 185, 129, 0.65);
        }

        .meta-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 12px;
            margin-bottom: 24px;
        }

        .meta-box {
            background: rgba(15, 23, 42, 0.45);
            border: 1px solid var(--border-card);
            border-radius: 12px;
            padding: 14px 16px;
        }

        .meta-box-label {
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--text-muted);
            margin-bottom: 4px;
        }

        .meta-box-value {
            font-size: 13px;
            font-weight: 600;
            color: #e2e8f0;
            font-family: var(--font-mono);
            word-break: break-all;
        }

        .notice-box {
            background: rgba(56, 189, 248, 0.06);
            border: 1px solid rgba(56, 189, 248, 0.18);
            border-radius: 12px;
            padding: 14px 16px;
            font-size: 13px;
            line-height: 1.5;
            color: #cbd5e1;
        }

        .notice-box strong {
            color: var(--primary);
        }

        .spinner {
            width: 18px;
            height: 18px;
            border: 2px solid rgba(255, 255, 255, 0.3);
            border-top-color: #fff;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            display: none;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }
    </style>
</head>
<body>

    <div class="launcher-card">
        <div class="header-brand">
            <div class="brand-logo">🛡️</div>
            <div class="brand-text">
                <h1>LabGuard PRO v2.0</h1>
                <p>Sistem Pemantauan Lab Komputer Terintegrasi (Laragon & XAMPP Bridge)</p>
            </div>
        </div>

        <div class="status-hero">
            <div class="status-info">
                <div class="status-dot <?= $isRunning ? 'online' : '' ?>" id="statusDot"></div>
                <div>
                    <div class="status-title" id="statusTitle">
                        <?= $isRunning ? 'Server Monitoring Aktif' : 'Server Python Belum Berjalan' ?>
                    </div>
                    <div class="status-sub" id="statusSub">
                        <?= $isRunning ? 'FastAPI & WebSocket siap melayani komputer siswa' : 'Port 8000 belum menerima koneksi telemetri' ?>
                    </div>
                </div>
            </div>
            <div class="badge-port">PORT: <?= $PORT ?></div>
        </div>

        <div class="action-group">
            <?php if ($isRunning): ?>
                <a href="<?= htmlspecialchars($targetUrl) ?>" class="btn-start btn-open" style="text-decoration:none;">
                    <span>🌐 Buka Dashboard Monitoring (Port 8000)</span>
                </a>
            <?php else: ?>
                <button id="btnStartServer" class="btn-start" onclick="startServer()">
                    <span class="spinner" id="spinner"></span>
                    <span id="btnText">🚀 Jalankan Server Sekarang (1-Klik)</span>
                </button>
            <?php endif; ?>
        </div>

        <div class="meta-grid">
            <div class="meta-box">
                <div class="meta-box-label">Laragon Virtual Host</div>
                <div class="meta-box-value">http://monitoring.test</div>
            </div>
            <div class="meta-box">
                <div class="meta-box-label">Direct App Access</div>
                <div class="meta-box-value"><?= htmlspecialchars($targetUrl) ?></div>
            </div>
            <div class="meta-box">
                <div class="meta-box-label">Python Environment</div>
                <div class="meta-box-value"><?= htmlspecialchars(basename(findPythonPath())) ?></div>
            </div>
            <div class="meta-box">
                <div class="meta-box-label">Database Engine</div>
                <div class="meta-box-value">SQLite (Built-in)</div>
            </div>
        </div>

        <div class="notice-box">
            💡 <strong>Dukungan Penuh Laragon:</strong> Proyek ini terhubung langsung di <code>C:\laragon\www\Monitoring</code>. Anda juga dapat menjalankan server kapan saja dengan klik dua kali <strong>JALANKAN_WEB_SERVER.bat</strong>.
        </div>
    </div>

    <script>
        const targetUrl = <?= json_encode($targetUrl) ?>;
        let isPolling = false;

        async function startServer() {
            const btn = document.getElementById('btnStartServer');
            const spinner = document.getElementById('spinner');
            const btnText = document.getElementById('btnText');
            const statusTitle = document.getElementById('statusTitle');
            const statusSub = document.getElementById('statusSub');

            if (!btn) return;
            btn.disabled = true;
            if (spinner) spinner.style.display = 'inline-block';
            if (btnText) btnText.textContent = 'Memulai Server Python FastAPI...';
            if (statusSub) statusSub.textContent = 'Mengaktifkan uvicorn server pada port 8000...';

            try {
                const res = await fetch('?action=start');
                const data = await res.json();
                pollServer();
            } catch (err) {
                if (btnText) btnText.textContent = 'Mengecek Server...';
                pollServer();
            }
        }

        function pollServer() {
            if (isPolling) return;
            isPolling = true;

            let attempts = 0;
            const maxAttempts = 20;

            const timer = setInterval(async () => {
                attempts++;
                try {
                    const res = await fetch('?action=check&t=' + Date.now());
                    const data = await res.json();
                    if (data.online) {
                        clearInterval(timer);
                        const statusDot = document.getElementById('statusDot');
                        const statusTitle = document.getElementById('statusTitle');
                        const statusSub = document.getElementById('statusSub');
                        const btnText = document.getElementById('btnText');

                        if (statusDot) statusDot.classList.add('online');
                        if (statusTitle) statusTitle.textContent = 'Server Berhasil Aktif!';
                        if (statusSub) statusSub.textContent = 'Mengarahkan ke Dashboard Monitoring...';
                        if (btnText) btnText.textContent = 'Membuka Dashboard...';

                        setTimeout(() => {
                            window.location.href = targetUrl;
                        }, 700);
                        return;
                    }
                } catch (e) {}

                if (attempts >= maxAttempts) {
                    clearInterval(timer);
                    isPolling = false;
                    const btn = document.getElementById('btnStartServer');
                    const spinner = document.getElementById('spinner');
                    const btnText = document.getElementById('btnText');
                    const statusSub = document.getElementById('statusSub');

                    if (btn) btn.disabled = false;
                    if (spinner) spinner.style.display = 'none';
                    if (btnText) btnText.textContent = 'Coba Jalankan Lagi';
                    if (statusSub) statusSub.textContent = 'Silakan coba lagi atau jalankan JALANKAN_WEB_SERVER.bat secara manual.';
                }
            }, 1000);
        }

        // Auto poll if currently waiting
        setInterval(async () => {
            if (isPolling) return;
            try {
                const res = await fetch('?action=check&t=' + Date.now());
                const data = await res.json();
                if (data.online) {
                    window.location.href = targetUrl;
                }
            } catch (e) {}
        }, 3000);
    </script>
</body>
</html>
