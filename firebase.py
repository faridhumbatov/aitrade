import json
import os
import threading
import time
import ccxt
import firebase_admin
from firebase_admin import credentials, db
from flask import Flask, jsonify, render_template_string
from google import genai
from google.genai import types
import os
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 1. API AÇARLARI VƏ AYARLAR
# ==========================================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

app = Flask(__name__)

USE_TESTNET = False
LEVERAGE = 5

client = genai.Client(api_key=GEMINI_API_KEY)

exchange = ccxt.binance({
    "apiKey": BINANCE_API_KEY,
    "secret": BINANCE_SECRET_KEY,
    "options": {"defaultType": "future"},
    "enableRateLimit": True,
})

if USE_TESTNET:
    exchange.set_sandbox_mode(True)

# Firebase Quraşdırması
cred = credentials.Certificate("firebase-adminsdk.json")
firebase_admin.initialize_app(cred, {
    'databaseURL': 'https://aitrade-theferid-default-rtdb.firebaseio.com/'
})
db_ref = db.reference('/bot_data')

coins = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "AVAX/USDT",
    "LINK/USDT", "NEAR/USDT", "DOT/USDT", "BNB/USDT", "NVDA/USDT"
]

# ==========================================
# 2. FIREBASE YADDAŞ FUNKSİYALARI
# ==========================================
def init_db():
    data = db_ref.get()
    if not data:
        db_ref.set({
            "balance": 0.0,
            "trade_logs": [],
            "system_logs": [],
            "audit_logs": [],
            "prices": {},
            "price_dirs": {},
            "auto_trading_enabled": True,
        })

init_db()

def push_log(log_type, message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    
    logs = db_ref.child(log_type).get() or []
    logs.insert(0, log_entry)
    if len(logs) > 300:
        logs.pop()
    db_ref.child(log_type).set(logs)

def log_audit(title, content):
    push_log("audit_logs", f"[{title}] -> {content}")

# ==========================================
# 3. BİNANCE REAL-TIME DATA (MOVQELƏR)
# ==========================================
def get_active_positions():
    active_positions = []
    try:
        positions = exchange.fetch_positions()
        for p in positions:
            pos_amt = float(p['info'].get('positionAmt', 0))
            if pos_amt != 0:
                active_positions.append({
                    'symbol': p['symbol'],
                    'action': 'BUY' if pos_amt > 0 else 'SELL',
                    'entry_price': float(p['info'].get('entryPrice', 0)),
                    'pnl': float(p['info'].get('unRealizedProfit', 0)),
                    'amount_qty': abs(pos_amt),
                    'leverage': int(p['info'].get('leverage', LEVERAGE))
                })
    except Exception as e:
        push_log("system_logs", f"Mövqeləri çəkmək xətası: {str(e)}")
    return active_positions

def price_updater():
    while True:
        try:
            # Balansı yenilə
            bal_info = exchange.fetch_balance()
            if "USDT" in bal_info:
                db_ref.child("balance").set(float(bal_info["USDT"].get("free", 0.0)))

            # Qiymətləri yenilə
            old_prices = db_ref.child("prices").get() or {}
            new_prices = {}
            price_dirs = db_ref.child("price_dirs").get() or {}

            for symbol in coins:
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    new_price = ticker["last"]
                    
                    # Firebase üçün təhlükəsiz key yaradırıq ( / simvolunu _ ilə əvəz edirik )
                    safe_symbol = symbol.replace("/", "_")
                    
                    old_price = old_prices.get(safe_symbol, new_price)
                    if new_price > old_price:
                        price_dirs[safe_symbol] = "up"
                    elif new_price < old_price:
                        price_dirs[safe_symbol] = "down"
                    else:
                        if safe_symbol not in price_dirs:
                            price_dirs[safe_symbol] = "same"
                    
                    new_prices[safe_symbol] = new_price
                except Exception:
                    continue
            
            db_ref.child("prices").set(new_prices)
            db_ref.child("price_dirs").set(price_dirs)
            
            # Açıq mövqeləri yenilə
            active_pos = get_active_positions()
            db_ref.child("positions").set(active_pos)

        except Exception as e:
            push_log("system_logs", f"Qiymət yeniləmə xətası: {str(e)}")
            
        time.sleep(3)
        
def execute_market_close(symbol, side, amount):
    try:
        log_audit("BINANCE_REQUEST", f"Mövqe bağlama əmri: {symbol} | Side: {side} | Qty: {amount}")
        order = exchange.create_order(
            symbol=symbol,
            type="market",
            side=side,
            amount=amount,
            params={"reduceOnly": True},
        )
        exchange.cancel_all_orders(symbol)
        log_audit("BINANCE_RESPONSE", f"Mövqe bağlandı: {json.dumps(order)}")
        return True
    except Exception as e:
        push_log("system_logs", f"Order bağlama xətası ({symbol}): {str(e)}")
        return False

# ==========================================
# 4. BOTUN ƏSAS DÖVRÜ (ANALİZ VƏ TİCARƏT)
# ==========================================
def bot_loop():
    while True:
        try:
            is_enabled = db_ref.child("auto_trading_enabled").get()
            if not is_enabled:
                time.sleep(5)
                continue

            push_log("system_logs", "Bazar analizi başladı (15m + 4h)...")
            all_market_data = []
            
            active_positions = get_active_positions()
            active_symbols = [p['symbol'] for p in active_positions]
            prices = db_ref.child("prices").get() or {}

            for symbol in coins:
                # Əgər bu koin üzrə açıq mövqe varsa, analizə daxil etmə
                if symbol in active_symbols:
                    continue

                try:
                    ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe="15m", limit=30)
                    closes_15m = [c[4] for c in ohlcv_15m[-10:]]
                    high_15m = max(c[2] for c in ohlcv_15m[-30:])
                    low_15m = min(c[3] for c in ohlcv_15m[-30:])

                    ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe="4h", limit=20)
                    closes_4h = [c[4] for c in ohlcv_4h[-10:]]
                    high_4h = max(c[2] for c in ohlcv_4h[-20:])
                    low_4h = min(c[3] for c in ohlcv_4h[-20:])

                    all_market_data.append({
                        "symbol": symbol,
                        "current_price": prices.get(symbol, closes_15m[-1]),
                        "timeframe_15m": {
                            "recent_closes": closes_15m,
                            "period_high": high_15m,
                            "period_low": low_15m,
                        },
                        "timeframe_4h": {
                            "recent_closes": closes_4h,
                            "period_high": high_4h,
                            "period_low": low_4h,
                        }
                    })
                except Exception as ex:
                    push_log("system_logs", f"{symbol} datası alınmadı: {str(ex)}")

            if not all_market_data:
                time.sleep(60)
                continue

            prompt = f"""
                Aşağıdakı kriptovalyuta bazar məlumatlarını həm qısamüddətli (15 dəqiqəlik), həm də uzunmüddətli (4 saatlıq) zaman intervalında texniki olaraq təhlil et. 
                Qərar verərkən 4 saatlıq trendin istiqamətini nəzərə al ki, yalandan əks istiqamətə giriş edilməsin.
                
                {json.dumps(all_market_data)}
                
                Leverage {LEVERAGE}x

                Cavabı YALNIZ aşağıdakı dəqiq strukturda JSON massivi (List) formatında qaytar, heç bir əlavə mətn yazma:
                [
                  {{
                    "symbol": "COIN_NAME",
                    "action": "buy" və ya "sell" və ya "float",
                    "tp": 0.0,
                    "sl": 0.0,
                    "assurance": "XX%"
                  }}
                ]
            """

            log_audit("AI_PROMPT_SENT", f"Analiz edilən koinlər: {[d['symbol'] for d in all_market_data]}")

            response = client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )

            log_audit("AI_RESPONSE_RECEIVED", response.text)
            results = json.loads(response.text)

            best_opportunity = None
            highest_assurance = 0

            for result in results:
                action = result.get("action", "float").lower()
                assurance_str = str(result.get("assurance", "0%")).replace("%", "").strip()
                assurance_val = float(assurance_str) if assurance_str.isdigit() else 0

                if action in ["buy", "sell"] and assurance_val > highest_assurance:
                    highest_assurance = assurance_val
                    best_opportunity = result

            if best_opportunity and highest_assurance >= 75 and is_enabled:
                sym = best_opportunity["symbol"]
                
                # İkinci dəfə təhlükəsizlik yoxlanışı
                current_active = [p['symbol'] for p in get_active_positions()]
                if sym in current_active:
                    push_log("system_logs", f"{sym} üzrə artıq mövqe var (İkinci yoxlanış blokladı).")
                    continue
                
                act = best_opportunity["action"].lower()
                
                # Firebase-dən qiyməti oxumaq üçün alt xətt ilə axtarırıq:
                safe_sym = sym.replace("/", "_")
                price = prices.get(safe_sym, 0)
                
                margin_amount = 5.0
                balance = db_ref.child("balance").get() or 0.0

                if balance >= margin_amount and price > 0:
                    try:
                        exchange.set_leverage(LEVERAGE, sym)
                        notional_value = margin_amount * LEVERAGE
                        raw_qty = notional_value / price
                        qty = float(exchange.amount_to_precision(sym, raw_qty))

                        side = "buy" if act == "buy" else "sell"
                        order = exchange.create_order(
                            symbol=sym, type="market", side=side, amount=qty
                        )

                        close_side = "sell" if side == "buy" else "buy"
                        tp_val = float(best_opportunity.get("tp", 0.0))
                        sl_val = float(best_opportunity.get("sl", 0.0))

                        # TP order
                        if tp_val > 0:
                            if (act == "buy" and tp_val > price) or (act == "sell" and tp_val < price):
                                formatted_tp = float(exchange.price_to_precision(sym, tp_val))
                                exchange.create_order(
                                    symbol=sym, type="TAKE_PROFIT_MARKET", side=close_side, amount=qty,
                                    params={"stopPrice": formatted_tp, "reduceOnly": True},
                                )
                        
                        # SL order
                        if sl_val > 0:
                            if (act == "buy" and sl_val < price) or (act == "sell" and sl_val > price):
                                formatted_sl = float(exchange.price_to_precision(sym, sl_val))
                                exchange.create_order(
                                    symbol=sym, type="STOP_MARKET", side=close_side, amount=qty,
                                    params={"stopPrice": formatted_sl, "reduceOnly": True},
                                )

                        trade_msg = f"REAL MÖVQEYƏ GİRİLDİ: {sym} {act.upper()} ({LEVERAGE}x) | TP: {tp_val} | SL: {sl_val}"
                        push_log("trade_logs", trade_msg)
                        log_audit("TRADE_OPENED", trade_msg)

                    except Exception as order_ex:
                        push_log("system_logs", f"Binance Order xətası ({sym}): {str(order_ex)}")

        except Exception as e:
            push_log("system_logs", f"Dövr xətası: {str(e)}")

        time.sleep(300)

# ==========================================
# 5. VEB PANEL / HTML ARAYÜZ
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="az">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Binance Futures AI Trading Bot</title>
    <style>
        body { background-color: #181a20; color: #eaecef; font-family: Arial, sans-serif; margin: 0; padding: 15px; }
        h1, h2 { color: #f0b90b; }
        .card { background-color: #1e2329; border: 1px solid #2b313a; padding: 15px; margin-bottom: 15px; border-radius: 8px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 8px; text-align: left; border-bottom: 1px solid #2b313a; font-size: 14px; }
        th { color: #848e9c; }
        .buy { color: #0ecb81; font-weight: bold; }
        .sell { color: #f6465d; font-weight: bold; }
        .profit { color: #0ecb81; font-weight: bold; }
        .loss { color: #f6465d; font-weight: bold; }
        .price-up { color: #0ecb81; font-weight: bold; }
        .price-down { color: #f6465d; font-weight: bold; }
        .logs-container { display: flex; flex-direction: column; gap: 15px; }
        .log-box { background-color: #0b0e11; padding: 10px; height: 150px; overflow-y: scroll; font-family: monospace; font-size: 12px; border-radius: 4px; border: 1px solid #2b313a; }
        .trade-logs { color: #0ecb81; }
        .sys-logs { color: #848e9c; }
        .btn-action { border: none; font-weight: bold; cursor: pointer; transition: 0.3s; }
        .nav-link { display: inline-block; background-color: #f0b90b; color: #181a20; padding: 10px 15px; text-decoration: none; font-weight: bold; border-radius: 5px; margin-bottom: 15px; }
    </style>
</head>
<body>
    <h1 style="font-size: 20px; text-align: center;">Binance AI Bot Control (Firebase)</h1>
    
    <div style="text-align: center;">
        <a href="/audit" class="nav-link" target="_blank">🔍 Detallı Audit Log Səhifəsi</a>
    </div>

    <div class="card" style="text-align: center;">
        <h2 style="font-size: 16px;">Bot Statusu</h2>
        <button id="auto-toggle-btn" class="btn-action" onclick="toggleAutoTrading()" style="padding: 12px 20px; font-size: 16px; width: 100%; max-width: 300px; border-radius: 6px;">
            Yoxlanılır...
        </button>
    </div>

    <div class="card">
        <h2 style="font-size: 16px;">Binance Hesab Məlumatı</h2>
        <p>Cari USDT Balansı: <strong id="balance" style="color: #f0b90b; font-size: 18px;">0</strong> USDT</p>
    </div>

    <div class="card">
        <h2 style="font-size: 16px;">Canlı Qiymətlər</h2>
        <table>
            <thead>
                <tr><th>Simvol</th><th>Qiymət</th><th>Trend</th></tr>
            </thead>
            <tbody id="market-prices"></tbody>
        </table>
    </div>

    <div class="card">
        <h2 style="font-size: 16px;">Açıq Mövqelər (Birbaşa Binance)</h2>
        <div style="overflow-x: auto;">
            <table>
                <thead>
                    <tr><th>Simvol</th><th>Tip</th><th>Giriş</th><th>Unrealized PnL</th><th>Əməl</th></tr>
                </thead>
                <tbody id="positions"></tbody>
            </table>
        </div>
    </div>

    <div class="card">
        <h2 style="font-size: 16px;">Log Mərkəzi</h2>
        <div class="logs-container">
            <div>
                <h3 style="color: #0ecb81; font-size: 12px; margin: 5px 0;">Ticarət Logları</h3>
                <div class="log-box trade-logs" id="trade-logs"></div>
            </div>
            <div>
                <h3 style="color: #848e9c; font-size: 12px; margin: 5px 0;">Sistem Logları</h3>
                <div class="log-box sys-logs" id="system-logs"></div>
            </div>
        </div>
    </div>

    <script>
        function toggleAutoTrading() {
            fetch('/toggle_auto', {method: 'POST'})
            .then(res => res.json())
            .then(data => { updateAutoButton(data.auto_trading_enabled); });
        }

        function updateAutoButton(isEnabled) {
            let btn = document.getElementById('auto-toggle-btn');
            if (isEnabled) {
                btn.innerText = '🟢 BOT AKTİVDİR (DAYANDIR)';
                btn.style.backgroundColor = '#f6465d';
                btn.style.color = '#fff';
            } else {
                btn.innerText = '🔴 BOT PAUZADADIR (BAŞLAT)';
                btn.style.backgroundColor = '#0ecb81';
                btn.style.color = '#000';
            }
        }

        function closePosition(symbol, action, qty) {
            let side = action === 'BUY' ? 'sell' : 'buy';
            fetch(`/close_position?symbol=${symbol}&side=${side}&qty=${qty}`, {method: 'POST'})
            .then(res => res.json())
            .then(data => { updateData(); });
        }

        function updateData() {
            fetch('/data')
            .then(res => res.json())
            .then(data => {
                if(!data) return;
                document.getElementById('balance').innerText = (data.balance || 0).toFixed(2);
                updateAutoButton(data.auto_trading_enabled);
                
                let priceHtml = '';
                if(data.prices) {
                    for (let [sym, prc] of Object.entries(data.prices)) {
                        let dir = data.price_dirs ? data.price_dirs[sym] : 'same';
                        let cls = dir === 'up' ? 'price-up' : (dir === 'down' ? 'price-down' : '');
                        let arrow = dir === 'up' ? '▲' : (dir === 'down' ? '▼' : '▬');
                        
                        // _ simvolunu ekranda göstərmək üçün / ilə əvəz edirik
                        let displaySym = sym.replace('_', '/'); 
                        
                        priceHtml += `<tr><td>${displaySym}</td><td class="${cls}">${prc.toFixed(2)}</td><td class="${cls}">${arrow}</td></tr>`;
                    }
                }
                document.getElementById('market-prices').innerHTML = priceHtml;

                let posHtml = '';
                if(data.positions) {
                    data.positions.forEach((p) => {
                        let cls = p.action === 'BUY' ? 'buy' : 'sell';
                        let pnlClass = p.pnl >= 0 ? 'profit' : 'loss';
                        let pnlSign = p.pnl >= 0 ? '+' : '';
                        posHtml += `<tr>
                            <td>${p.symbol}</td>
                            <td class="${cls}">${p.action}</td>
                            <td>${p.entry_price.toFixed(2)}</td>
                            <td class="${pnlClass}">${pnlSign}${p.pnl.toFixed(2)}</td>
                            <td><button class="btn-action" onclick="closePosition('${p.symbol}', '${p.action}', ${p.amount_qty})" style="background-color: #f6465d; color: white; padding: 4px 8px; font-size: 11px; border-radius: 4px;">X</button></td>
                        </tr>`;
                    });
                }
                document.getElementById('positions').innerHTML = posHtml;

                let tradeLogHtml = '';
                if(data.trade_logs) data.trade_logs.forEach(l => { tradeLogHtml += l + '<br>'; });
                document.getElementById('trade-logs').innerHTML = tradeLogHtml;

                let sysLogHtml = '';
                if(data.system_logs) data.system_logs.forEach(l => { sysLogHtml += l + '<br>'; });
                document.getElementById('system-logs').innerHTML = sysLogHtml;
            });
        }
        
        setInterval(updateData, 2000);
        updateData();
    </script>
</body>
</html>
"""

AUDIT_HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="az">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Detallı Audit Loglar - AI & Binance Trafiki</title>
    <style>
        body { background-color: #0b0e11; color: #eaecef; font-family: monospace; margin: 0; padding: 20px; }
        h1 { color: #f0b90b; font-family: Arial, sans-serif; }
        .log-container { background-color: #1e2329; border: 1px solid #2b313a; padding: 15px; border-radius: 8px; height: 85vh; overflow-y: scroll; white-space: pre-wrap; word-wrap: break-word; font-size: 13px; line-height: 1.5; }
        .refresh-info { color: #848e9c; margin-bottom: 10px; font-family: Arial, sans-serif; font-size: 14px; }
    </style>
</head>
<body>
    <h1>📋 Bot Detallı Trafik və Audit Logları</h1>
    <div class="refresh-info">Hər 3 saniyədən bir avtomatik yenilənir. Bütün Binance məlumatları Firebase-dən çəkilir.</div>
    <div class="log-container" id="audit-logs">Yüklənir...</div>

    <script>
        function fetchAuditLogs() {
            fetch('/data')
            .then(res => res.json())
            .then(data => {
                let html = '';
                if (data && data.audit_logs && data.audit_logs.length > 0) {
                    data.audit_logs.forEach(l => {
                        html += l + '\\n------------------------------------------------------------\\n';
                    });
                } else {
                    html = 'Hələ ki heç bir log qeydə alınmayıb...';
                }
                document.getElementById('audit-logs').innerText = html;
            });
        }
        setInterval(fetchAuditLogs, 3000);
        fetchAuditLogs();
    </script>
</body>
</html>
"""

# ==========================================
# 6. FLASK ROUTE-LAR
# ==========================================
from flask import request

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/audit")
def audit_page():
    return render_template_string(AUDIT_HTML_TEMPLATE)

@app.route("/data")
def get_data():
    return jsonify(db_ref.get() or {})

@app.route("/toggle_auto", methods=["POST"])
def toggle_auto():
    current_status = db_ref.child("auto_trading_enabled").get()
    new_status = not current_status
    db_ref.child("auto_trading_enabled").set(new_status)
    push_log("system_logs", f"Bot statusu dəyişdirildi. Yeni status: {'Aktiv' if new_status else 'Pauza'}")
    return jsonify({"auto_trading_enabled": new_status})

@app.route("/close_position", methods=["POST"])
def close_position():
    symbol = request.args.get('symbol')
    side = request.args.get('side')
    qty = float(request.args.get('qty'))
    
    if execute_market_close(symbol, side, qty):
        push_log("trade_logs", f"ƏLLƏ BAĞLANDI: {symbol}")
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 400

# ==========================================
# 7. TƏTBİQİ BAŞLATMAQ
# ==========================================
if __name__ == "__main__":
    t_prices = threading.Thread(target=price_updater, daemon=True)
    t_prices.start()

    t_bot = threading.Thread(target=bot_loop, daemon=True)
    t_bot.start()

    app.run(debug=True, host="0.0.0.0", port=5000, use_reloader=False)