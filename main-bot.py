import json
import os
import threading
import time
import ccxt
from flask import Flask, jsonify, render_template_string
from google import genai
from google.genai import types
import os
from dotenv import load_dotenv

app = Flask(__name__)

load_dotenv()

# ==========================================
# 1. API AÇARLARI VƏ AYARLAR
# ==========================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

USE_TESTNET = False
LEVERAGE = 5

client = genai.Client(api_key=GEMINI_API_KEY)

exchange = ccxt.binance(
    {
        "apiKey": BINANCE_API_KEY,
        "secret": BINANCE_SECRET_KEY,
        "options": {"defaultType": "future"},
        "enableRateLimit": True,
    }
)

if USE_TESTNET:
    exchange.set_sandbox_mode(True)

DATA_FILE = "bot_data.json"
TRADE_TXT = "trade_log.txt"
SYSTEM_TXT = "system_log.txt"
DETAILED_LOG_TXT = "detailed_audit.txt"

coins = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "ADA/USDT",
    "AVAX/USDT",
    "LINK/USDT",
    "NEAR/USDT",
    "DOT/USDT",
    "BNB/USDT",
    "NVDA/USDT",
]

# ==========================================
# 2. YADDAŞ (DATA) FUNKSİYALARI
# ==========================================
def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "balance": 0.0,
        "positions": [],
        "trade_logs": [],
        "system_logs": [],
        "audit_logs": [],
        "prices": {},
        "price_dirs": {},
        "auto_trading_enabled": True,
    }

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(sim_data, f, ensure_ascii=False, indent=4)
    except Exception:
        pass

def append_to_txt(filename, text):
    try:
        with open(filename, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass

def log_audit(title, content):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] [{title}] -> {content}"
    sim_data["audit_logs"].insert(0, log_entry)
    if len(sim_data["audit_logs"]) > 300:
        sim_data["audit_logs"].pop()
    append_to_txt(DETAILED_LOG_TXT, log_entry)

sim_data = load_data()

# Data strukturunu tamamlamaq
if "trade_logs" not in sim_data: sim_data["trade_logs"] = []
if "system_logs" not in sim_data: sim_data["system_logs"] = []
if "audit_logs" not in sim_data: sim_data["audit_logs"] = []
if "prices" not in sim_data: sim_data["prices"] = {}
if "price_dirs" not in sim_data: sim_data["price_dirs"] = {}
if "auto_trading_enabled" not in sim_data: sim_data["auto_trading_enabled"] = True

# ==========================================
# 3. BİNANCE VƏ QİYMƏT YENİLƏMƏ
# ==========================================
def execute_market_close(position):
    try:
        sym = position["symbol"]
        side = "sell" if position["action"] == "BUY" else "buy"
        amount = position["amount_qty"]

        log_audit("BINANCE_REQUEST", f"Mövqe bağlama əmri göndərilir: {sym} | Side: {side} | Qty: {amount}")
        order = exchange.create_order(
            symbol=sym,
            type="market",
            side=side,
            amount=amount,
            params={"reduceOnly": True},
        )
        
        # Mövqe bağlandığı üçün həmin simvoldakı qalıq TP/SL orderlərini ləğv edirik
        try:
            exchange.cancel_all_orders(sym)
            log_audit("BINANCE_INFO", f"{sym} üzrə bütün qalıq orderlər ləğv edildi.")
        except Exception as cancel_err:
            log_audit("BINANCE_ERROR", f"Qalıq orderləri ləğv etmə xətası ({sym}): {str(cancel_err)}")

        log_audit("BINANCE_RESPONSE", f"Mövqe uğurla bağlandı: {json.dumps(order)}")
        return order
    except Exception as e:
        # Əgər mövqe onsuz da bağlanıbsa (ReduceOnly xətası), sadəcə qalıq orderləri silib keçirik
        if "-2022" in str(e) or "ReduceOnly" in str(e):
            log_audit("BINANCE_INFO", f"ReduceOnly xətası (mövqe onsuz da bağlıdır): {str(e)}")
            try:
                exchange.cancel_all_orders(position["symbol"])
                log_audit("BINANCE_INFO", f"{position['symbol']} üzrə qalıq orderlər təmizləndi.")
            except:
                pass
            return None

        err_msg = f"Binance order bağlama xətası ({position['symbol']}): {str(e)}"
        sim_data["system_logs"].insert(0, err_msg)
        append_to_txt(SYSTEM_TXT, err_msg)
        log_audit("BINANCE_ERROR", err_msg)
        return None

def price_updater():
    while True:
        try:
            try:
                bal_info = exchange.fetch_balance()
                if "USDT" in bal_info:
                    sim_data["balance"] = float(bal_info["USDT"].get("free", 0.0))
            except Exception:
                pass

            for symbol in coins:
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    new_price = ticker["last"]

                    old_price = sim_data["prices"].get(symbol, new_price)
                    if new_price > old_price:
                        sim_data["price_dirs"][symbol] = "up"
                    elif new_price < old_price:
                        sim_data["price_dirs"][symbol] = "down"
                    else:
                        if symbol not in sim_data["price_dirs"]:
                            sim_data["price_dirs"][symbol] = "same"

                    sim_data["prices"][symbol] = new_price
                except Exception:
                    pass

            for p in sim_data["positions"][:]:
                curr_price = sim_data["prices"].get(p["symbol"], p["entry_price"])
                margin = p.get("margin", 7.0)
                lev = p.get("leverage", LEVERAGE)
                quantity = p.get("amount_qty", (margin * lev) / p["entry_price"])

                if p["action"] == "BUY":
                    p["pnl"] = quantity * (curr_price - p["entry_price"])
                    if p["tp"] > 0 and curr_price >= p["tp"]:
                        execute_market_close(p)
                        log_msg = f"TP HƏDƏFİNƏ ÇATDI: {p['symbol']} ({lev}x) | Qiymət: {curr_price} | PnL: +{p['pnl']:.2f} USDT"
                        sim_data["trade_logs"].insert(0, log_msg)
                        append_to_txt(TRADE_TXT, log_msg)
                        log_audit("TRADE_EVENT", log_msg)
                        sim_data["positions"].remove(p)
                        save_data()
                        continue
                    elif p["sl"] > 0 and curr_price <= p["sl"]:
                        execute_market_close(p)
                        log_msg = f"SL HƏDƏFİNƏ ÇATDI: {p['symbol']} ({lev}x) | Qiymət: {curr_price} | PnL: {p['pnl']:.2f} USDT"
                        sim_data["trade_logs"].insert(0, log_msg)
                        append_to_txt(TRADE_TXT, log_msg)
                        log_audit("TRADE_EVENT", log_msg)
                        sim_data["positions"].remove(p)
                        save_data()
                        continue
                else:
                    p["pnl"] = quantity * (p["entry_price"] - curr_price)
                    if p["tp"] > 0 and curr_price <= p["tp"]:
                        execute_market_close(p)
                        log_msg = f"TP HƏDƏFİNƏ ÇATDI: {p['symbol']} ({lev}x) | Qiymət: {curr_price} | PnL: +{p['pnl']:.2f} USDT"
                        sim_data["trade_logs"].insert(0, log_msg)
                        append_to_txt(TRADE_TXT, log_msg)
                        log_audit("TRADE_EVENT", log_msg)
                        sim_data["positions"].remove(p)
                        save_data()
                        continue
                    elif p["sl"] > 0 and curr_price >= p["sl"]:
                        execute_market_close(p)
                        log_msg = f"SL HƏDƏFİNƏ ÇATDI: {p['symbol']} ({lev}x) | Qiymət: {curr_price} | PnL: {p['pnl']:.2f} USDT"
                        sim_data["trade_logs"].insert(0, log_msg)
                        append_to_txt(TRADE_TXT, log_msg)
                        log_audit("TRADE_EVENT", log_msg)
                        sim_data["positions"].remove(p)
                        save_data()
                        continue

                p["current_price"] = curr_price
            save_data()
        except Exception:
            pass
        time.sleep(2)

# ==========================================
# 4. BOTUN ƏSAS DÖVRÜ (ANALİZ VƏ TİCARƏT)
# ==========================================
def bot_loop():
    while True:
        try:
            if not sim_data.get("auto_trading_enabled", True):
                time.sleep(5)
                continue

            sys_msg = "Bazar analizi başladı (15m + 4h)..."
            sim_data["system_logs"].insert(0, sys_msg)
            append_to_txt(SYSTEM_TXT, sys_msg)
            log_audit("SYSTEM", sys_msg)
            save_data()

            all_market_data = []

            for symbol in coins:
                try:
                    ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe="15m", limit=30)
                    closes_15m = [c[4] for c in ohlcv_15m[-10:]]
                    high_15m = max(c[2] for c in ohlcv_15m[-30:])
                    low_15m = min(c[3] for c in ohlcv_15m[-30:])

                    ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe="4h", limit=20)
                    closes_4h = [c[4] for c in ohlcv_4h[-10:]]
                    high_4h = max(c[2] for c in ohlcv_4h[-20:])
                    low_4h = min(c[3] for c in ohlcv_4h[-20:])

                    current_price = sim_data["prices"].get(symbol, closes_15m[-1])

                    all_market_data.append(
                        {
                            "symbol": symbol,
                            "current_price": current_price,
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
                        }
                    )
                except Exception as ex:
                    err_msg = f"{symbol} datası alınmadı: {str(ex)}"
                    sim_data["system_logs"].insert(0, err_msg)
                    append_to_txt(SYSTEM_TXT, err_msg)
                    log_audit("ERROR", err_msg)

            if not all_market_data:
                time.sleep(60)
                continue

            log_audit("BINANCE_TO_AI_DATA", json.dumps(all_market_data))

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

            log_audit("AI_PROMPT_SENT", prompt)

            try:
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
                highest_assurance_value = 0

                for result in results:
                    action = result.get("action", "float").lower()
                    assurance_str = result.get("assurance", "0%").replace("%", "").strip()
                    assurance_val = float(assurance_str) if assurance_str.isdigit() else 0

                    if action in ["buy", "sell"] and assurance_val > highest_assurance_value:
                        highest_assurance_value = assurance_val
                        best_opportunity = result

                if (
                    best_opportunity
                    and highest_assurance_value >= 75
                    and sim_data.get("auto_trading_enabled", True)
                ):
                    sym = best_opportunity["symbol"]
                    
                    # Yoxlayaq ki, bu simvolda onsuz da açıq mövqeyimiz varmı?
                    already_has_position = any(p["symbol"] == sym for p in sim_data["positions"])
                    
                    if already_has_position:
                        log_audit("SYSTEM", f"{sym} üzrə artıq açıq mövqe var. Yeni əmr ötürüldü.")
                    else:
                        act = best_opportunity["action"].lower()
                        price = sim_data["prices"].get(sym, 0)
                        margin_amount = 5.0

                        if sim_data["balance"] >= margin_amount and price > 0:
                            try:
                                log_audit("BINANCE_REQUEST", f"Leverage təyin edilir: {LEVERAGE}x for {sym}")
                                exchange.set_leverage(LEVERAGE, sym)

                                notional_value = margin_amount * LEVERAGE
                                raw_qty = notional_value / price
                                qty = float(exchange.amount_to_precision(sym, raw_qty))

                                side = "buy" if act == "buy" else "sell"
                                log_audit("BINANCE_REQUEST", f"Market Order açılır: {sym} | Side: {side} | Qty: {qty}")
                                order = exchange.create_order(
                                    symbol=sym,
                                    type="market",
                                    side=side,
                                    amount=qty,
                                )
                                log_audit("BINANCE_RESPONSE", f"Market Order Cavabı: {json.dumps(order)}")

                                close_side = "sell" if side == "buy" else "buy"
                                tp_val = float(best_opportunity.get("tp", 0.0))
                                sl_val = float(best_opportunity.get("sl", 0.0))

                                if tp_val > 0:
                                    is_valid_tp = (act == "buy" and tp_val > price) or (act == "sell" and tp_val < price)
                                    if is_valid_tp:
                                        try:
                                            formatted_tp = float(exchange.price_to_precision(sym, tp_val))
                                            log_audit("BINANCE_REQUEST", f"TP Order açılır: {sym} | Price: {formatted_tp}")
                                            tp_order = exchange.create_order(
                                                symbol=sym,
                                                type="TAKE_PROFIT_MARKET",
                                                side=close_side,
                                                amount=qty,
                                                params={"stopPrice": formatted_tp, "reduceOnly": True},
                                            )
                                            log_audit("BINANCE_RESPONSE", f"TP Order Cavabı: {json.dumps(tp_order)}")
                                        except Exception as tp_err:
                                            err_t = f"Binance TP Order xətası ({sym}): {str(tp_err)}"
                                            sim_data["system_logs"].insert(0, err_t)
                                            log_audit("BINANCE_ERROR", err_t)

                                if sl_val > 0:
                                    is_valid_sl = (act == "buy" and sl_val < price) or (act == "sell" and sl_val > price)
                                    if is_valid_sl:
                                        try:
                                            formatted_sl = float(exchange.price_to_precision(sym, sl_val))
                                            log_audit("BINANCE_REQUEST", f"SL Order açılır: {sym} | Price: {formatted_sl}")
                                            sl_order = exchange.create_order(
                                                symbol=sym,
                                                type="STOP_MARKET",
                                                side=close_side,
                                                amount=qty,
                                                params={"stopPrice": formatted_sl, "reduceOnly": True},
                                            )
                                            log_audit("BINANCE_RESPONSE", f"SL Order Cavabı: {json.dumps(sl_order)}")
                                        except Exception as sl_err:
                                            err_s = f"Binance SL Order xətası ({sym}): {str(sl_err)}"
                                            sim_data["system_logs"].insert(0, err_s)
                                            log_audit("BINANCE_ERROR", err_s)

                                sim_data["positions"].append(
                                    {
                                        "symbol": sym,
                                        "action": act.upper(),
                                        "entry_price": price,
                                        "current_price": price,
                                        "margin": margin_amount,
                                        "leverage": LEVERAGE,
                                        "amount_qty": qty,
                                        "pnl": 0.0,
                                        "tp": tp_val,
                                        "sl": sl_val,
                                        "assurance": best_opportunity["assurance"],
                                    }
                                )

                                trade_msg = f"REAL MÖVQEYƏ GİRİLDİ (15m+4h): {sym} {act.upper()} ({LEVERAGE}x) | Qiymət: {price} | TP: {tp_val} | SL: {sl_val}"
                                sim_data["trade_logs"].insert(0, trade_msg)
                                append_to_txt(TRADE_TXT, trade_msg)
                                log_audit("TRADE_OPENED", trade_msg)
                                save_data()

                            except Exception as order_ex:
                                err_msg = f"Binance Order xətası ({sym}): {str(order_ex)}"
                                sim_data["system_logs"].insert(0, err_msg)
                                append_to_txt(SYSTEM_TXT, err_msg)
                                log_audit("BINANCE_ERROR", err_msg)
                                save_data()

            except Exception as api_ex:
                err_msg = f"Gemini API xətası: {str(api_ex)}"
                sim_data["system_logs"].insert(0, err_msg)
                append_to_txt(SYSTEM_TXT, err_msg)
                log_audit("AI_ERROR", err_msg)
                save_data()

        except Exception as e:
            err_msg = f"Dövr xətası: {str(e)}"
            sim_data["system_logs"].insert(0, err_msg)
            append_to_txt(SYSTEM_TXT, err_msg)
            log_audit("LOOP_ERROR", err_msg)
            save_data()

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
    <h1 style="font-size: 20px; text-align: center;">Binance AI Bot Control</h1>
    
    <div style="text-align: center;">
        <a href="/audit" class="nav-link" target="_blank">🔍 Detallı Audit Log Səhifəsi (AI & Binance Trafiki)</a>
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
        <h2 style="font-size: 16px;">Açıq Mövqelər</h2>
        <div style="overflow-x: auto;">
            <table>
                <thead>
                    <tr><th>Simvol</th><th>Tip</th><th>Giriş</th><th>TP</th><th>SL</th><th>PnL</th><th>Əməl</th></tr>
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
            .then(data => {
                updateAutoButton(data.auto_trading_enabled);
            });
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

        function closePosition(index) {
            fetch('/close_position/' + index, {method: 'POST'})
            .then(res => res.json())
            .then(data => {
                updateData();
            });
        }

        function updateData() {
            fetch('/data')
            .then(res => res.json())
            .then(data => {
                document.getElementById('balance').innerText = data.balance.toFixed(2);
                updateAutoButton(data.auto_trading_enabled);
                
                let priceHtml = '';
                for (let [sym, prc] of Object.entries(data.prices)) {
                    let dir = data.price_dirs[sym] || 'same';
                    let cls = dir === 'up' ? 'price-up' : (dir === 'down' ? 'price-down' : '');
                    let arrow = dir === 'up' ? '▲' : (dir === 'down' ? '▼' : '▬');
                    priceHtml += `<tr><td>${sym}</td><td class="${cls}">${prc.toFixed(2)}</td><td class="${cls}">${arrow}</td></tr>`;
                }
                document.getElementById('market-prices').innerHTML = priceHtml;

                let posHtml = '';
                data.positions.forEach((p, index) => {
                    let cls = p.action === 'BUY' ? 'buy' : 'sell';
                    let pnlClass = p.pnl >= 0 ? 'profit' : 'loss';
                    let pnlSign = p.pnl >= 0 ? '+' : '';
                    posHtml += `<tr>
                        <td>${p.symbol}</td>
                        <td class="${cls}">${p.action}</td>
                        <td>${p.entry_price.toFixed(2)}</td>
                        <td style="color: #0ecb81;">${p.tp}</td>
                        <td style="color: #f6465d;">${p.sl}</td>
                        <td class="${pnlClass}">${pnlSign}${p.pnl.toFixed(2)}</td>
                        <td><button class="btn-action" onclick="closePosition(${index})" style="background-color: #f6465d; color: white; padding: 4px 8px; font-size: 11px; border-radius: 4px;">X</button></td>
                    </tr>`;
                });
                document.getElementById('positions').innerHTML = posHtml;

                let tradeLogHtml = '';
                data.trade_logs.forEach(l => { tradeLogHtml += l + '<br>'; });
                document.getElementById('trade-logs').innerHTML = tradeLogHtml;

                let sysLogHtml = '';
                data.system_logs.forEach(l => { sysLogHtml += l + '<br>'; });
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
    <div class="refresh-info">Hər 3 saniyədən bir avtomatik yenilənir. Bütün Binance məlumatları, AI sorğuları və cavabları burada əks olunur.</div>
    <div class="log-container" id="audit-logs">Yüklənir...</div>

    <script>
        function fetchAuditLogs() {
            fetch('/data')
            .then(res => res.json())
            .then(data => {
                let html = '';
                if (data.audit_logs && data.audit_logs.length > 0) {
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
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/audit")
def audit_page():
    return render_template_string(AUDIT_HTML_TEMPLATE)

@app.route("/data")
def get_data():
    return jsonify(sim_data)

@app.route("/toggle_auto", methods=["POST"])
def toggle_auto():
    sim_data["auto_trading_enabled"] = not sim_data.get("auto_trading_enabled", True)
    save_data()
    log_audit("SYSTEM", f"Bot statusu dəyişdirildi. Yeni status: {'Aktiv' if sim_data['auto_trading_enabled'] else 'Pauza'}")
    return jsonify({"auto_trading_enabled": sim_data["auto_trading_enabled"]})

@app.route("/close_position/<int:index>", methods=["POST"])
def close_position(index):
    if 0 <= index < len(sim_data["positions"]):
        p = sim_data["positions"].pop(index)
        execute_market_close(p)

        curr_price = sim_data["prices"].get(p["symbol"], p["entry_price"])
        pnl = p.get("pnl", 0.0)

        log_msg = f"ƏLLƏ BAĞLANDI (BINANCE): {p['symbol']} ({p.get('leverage', LEVERAGE)}x) | Qiymət: {curr_price} | PnL: {'+' if pnl >= 0 else ''}{pnl:.2f} USDT"
        sim_data["trade_logs"].insert(0, log_msg)
        append_to_txt(TRADE_TXT, log_msg)
        log_audit("MANUAL_CLOSE", log_msg)
        save_data()
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