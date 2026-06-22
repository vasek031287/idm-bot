import os
import subprocess
import json
import asyncio
import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode
from config import TELEGRAM_TOKEN, CHAT_ID

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
log = logging.getLogger("idm")

TIMEFRAME_TREND = "4h"
TIMEFRAME_ENTRY = "15m"
SCAN_INTERVAL_MIN = 15
TOP_N_COINS = 25
STATS_FILE = "stats.json"
TRADES_FILE = "active_trades.json"
TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "BNB-USDT", "SOL-USDT", "XRP-USDT",
    "ADA-USDT", "DOGE-USDT", "TON-USDT", "TRX-USDT", "AVAX-USDT",
    "LINK-USDT", "DOT-USDT", "SHIB-USDT", "LTC-USDT", "BCH-USDT",
]

def load_stats():
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r") as f:
            return json.load(f)
    return {"signals": 0, "tp": 0, "sl": 0, "open": 0}

def save_stats(s):
    with open(STATS_FILE, "w") as f:
        json.dump(s, f, indent=2)

def load_active_trades():
    global active_trades
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r") as f:
            data = json.load(f)
        active_trades = data or {}

def save_active_trades():
    with open(TRADES_FILE, "w") as f:
        json.dump(active_trades, f, indent=2)

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for price in values[period:]:
        e = price * k + e * (1 - k)
    return e

def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][2], candles[i][3], candles[i-1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a

def find_swings(candles, lookback=3):
    highs, lows = [], []
    for i in range(lookback, len(candles) - lookback):
        if all(candles[i][2] > candles[i-j][2] for j in range(1, lookback+1)) and \
           all(candles[i][2] > candles[i+j][2] for j in range(1, lookback+1)):
            highs.append(i)
        if all(candles[i][3] < candles[i-j][3] for j in range(1, lookback+1)) and \
           all(candles[i][3] < candles[i+j][3] for j in range(1, lookback+1)):
            lows.append(i)
    return highs, lows

def get_trend_4h(candles_4h):
    closes = [c[4] for c in candles_4h]
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    if e50 is None or e200 is None:
        return "neutral"
    last = closes[-1]
    if last > e200 and e50 > e200:
        return "bullish"
    if last < e200 and e50 < e200:
        return "bearish"
    return "neutral"

def get_trend_1h(candles_1h):
    """Тренд на 1H (средний ТФ для подтверждения)"""
    closes = [c[4] for c in candles_1h]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    if e20 is None or e50 is None:
        return "neutral"
    last = closes[-1]
    if last > e50 and e20 > e50:
        return "bullish"
    if last < e50 and e20 < e50:
        return "bearish"
    return "neutral"

def market_structure(candles):
    highs, lows = find_swings(candles, lookback=3)
    if len(highs) < 2 or len(lows) < 2:
        return "neutral"
    h1, h2 = candles[highs[-2]][2], candles[highs[-1]][2]
    l1, l2 = candles[lows[-2]][3], candles[lows[-1]][3]
    if h2 > h1 and l2 > l1:
        return "bullish"
    if h2 < h1 and l2 < l1:
        return "bearish"
    return "neutral"

def detect_choch(candles, trend):
    if len(candles) < 30:
        return None
    _, lows = find_swings(candles, lookback=2)
    highs, _ = find_swings(candles, lookback=2)
    closes = [c[4] for c in candles]
    last = closes[-1]
    if trend == "bullish" and lows and last > candles[lows[-1]][3]:
        return "long"
    if trend == "bearish" and highs and last < candles[highs[-1]][2]:
        return "short"
    return None

def detect_sweep(candles, trend):
    """Забор ликвидности: цена пробивает свинг, но возвращается.
    Возвращает 'bullish' / 'bearish' / None."""
    if len(candles) < 20:
        return None
    highs, lows = find_swings(candles, lookback=3)
    if not highs or not lows:
        return None
    last = candles[-1]
    last_high = last[2]
    last_low = last[3]
    last_close = last[4]

    # Bullish sweep: пробили минимум, закрылись выше
    if lows:
        swing_low = candles[lows[-1]][3]
        if last_low < swing_low and last_close > swing_low:
            return "bullish"

    # Bearish sweep: пробили максимум, закрылись ниже
    if highs:
        swing_high = candles[highs[-1]][2]
        if last_high > swing_high and last_close < swing_high:
            return "bearish"

    return None

def find_order_block(candles, direction):
    """Ищем OB: последняя противоположная свеча перед BOS (слом структуры)
    с проверкой что OB не был отработан (mitigated)"""
    if len(candles) < 30:
        return None

    # Шаг 1: найти последний BOS (пробой предыдущего свинга)
    bos_idx = None
    for i in range(len(candles) - 1, 15, -1):
        # Ищем ближайший предыдущий swing
        swing_high = max(c[2] for c in candles[max(0, i-10):i])
        swing_low = min(c[3] for c in candles[max(0, i-10):i])

        # Long BOS: пробой swing high
        if direction == "long" and candles[i][4] > swing_high:
            bos_idx = i
            break
        # Short BOS: пробой swing low
        if direction == "short" and candles[i][4] < swing_low:
            bos_idx = i
            break

    if bos_idx is None:
        return None

    # Шаг 2: ищем последнюю противоположную свечу перед BOS
    for i in range(bos_idx - 1, max(0, bos_idx - 20), -1):
        c = candles[i]
        rng = c[2] - c[3]
        if rng == 0:
            continue
        body = abs(c[4] - c[1])

        # Тело должно быть узким или средним (не огромная бычья/медвежья)
        if body / rng > 0.6:
            continue

        # Long OB: медвежья свеча перед BOS вверх
        if direction == "long" and c[4] < c[1]:
            ob = {"low": c[3], "high": c[2], "idx": i}
            # Проверка: не был ли OB уже отработан
            if not is_mitigated(candles, ob, "long"):
                return ob

        # Short OB: бычья свеча перед BOS вниз
        if direction == "short" and c[4] > c[1]:
            ob = {"low": c[3], "high": c[2], "idx": i}
            if not is_mitigated(candles, ob, "short"):
                return ob

    return None


def is_mitigated(candles, ob, direction):
    """Проверяет был ли OB уже отработан (цена заходила внутрь после BOS)"""
    ob_idx = ob["idx"]
    for i in range(ob_idx + 1, len(candles)):
        if direction == "long" and candles[i][3] <= ob["high"]:
            return True  # цена зашла в OB снизу
        if direction == "short" and candles[i][2] >= ob["low"]:
            return True  # цена зашла в OB сверху
    return False

def has_fvg(candles, direction):
    if len(candles) < 3:
        return False
    c0, c1, c2 = candles[-3], candles[-2], candles[-1]
    if direction == "long" and c2[1] > c0[2] > c1[3]:
        return True
    if direction == "short" and c2[1] < c0[3] < c1[2]:
        return True
    return False

def volume_ok(candles):
    if len(candles) < 21:
        return False
    vols = [c[5] for c in candles[-21:-1]]
    avg = sum(vols) / len(vols)
    return candles[-1][5] > avg * 1.2

def in_kill_zone():
    h = datetime.now(timezone.utc).hour
    return 8 <= h < 12 or 13 <= h < 17

def fetch_candles(symbol, tf, limit=250):
    """Получить свечи OKX в хронологическом порядке (старые → новые)"""
    url = (
        f"https://www.okx.com/api/v5/market/history-candles"
        f"?instId={symbol}&bar={tf}&limit={limit}"
    )
    for attempt in range(3):
        try:
            out = subprocess.check_output(
                ["curl", "-s", "--max-time", "20", url],
                stderr=subprocess.DEVNULL,
            )
            data = json.loads(out.decode("utf-8"))
            if data.get("code") == "0":
                candles = data.get("data", []) or []
                return candles[::-1]
            elif data.get("code") in ["50011", "50012", "50013", "50014"]:
                log.warning(f"{symbol} rate limit, sleep {5*(attempt+1)}s")
                import time as _t
                _t.sleep(5 * (attempt + 1))
                continue
            else:
                log.warning(f"{symbol} API: {data.get('code')} {data.get('msg')}")
                return []
        except Exception as e:
            log.error(f"{symbol} {tf} FAIL: {type(e).__name__}: {e}")
            import time as _t
            _t.sleep(2)
        return []

def analyze_symbol(symbol):
    # Загружаем 3 таймфрейма
    c4 = fetch_candles(symbol, "4h", 250)   # Старший ТФ
    c1 = fetch_candles(symbol, "1h", 200)   # Средний ТФ
    c15 = fetch_candles(symbol, "15m", 200) # Младший ТФ
    
    if not c4 or not c1 or not c15:
        return None
    
    # Тренды на старших ТФ
    trend_4h = get_trend_4h(c4)
    trend_1h = get_trend_1h(c1)
    
    # ФИЛЬТР 1: тренд 4H должен быть определённым
    if trend_4h == "neutral":
        return None
    
    # ФИЛЬТР 2: 1H должен подтверждать 4H
    if trend_1h != trend_4h:
        return None
    
    trend = trend_4h
    direction = "long" if trend == "bullish" else "short"
    
    # Структура рынка на 4H
    struct = market_structure(c4[-100:])
    if struct != trend:
        return None
    
    # CHoCH на 15m
    if detect_choch(c15, trend) != direction:
        return None
    
    # Забор ликвидности
    sweep = detect_sweep(c15, trend)
    if sweep != direction:
        return None
    
    # Order Block
    ob = find_order_block(c15, direction)
    if not ob:
        return None
    
    # FVG + объём
    if not has_fvg(c15, direction) or not volume_ok(c15):
        return None
    
    # ATR
    a = atr(c15, 14)
    if not a:
        return None
    
    entry = c15[-1][4]
    if direction == "long":
        sl = ob["low"] - a * 0.5
        risk = entry - sl
        tp = entry + risk * 3
    else:
        sl = ob["high"] + a * 0.5
        risk = sl - entry
        tp = entry - risk * 3
    
    if risk <= 0:
        return None
    
    # Сила сигнала
    strength = 50  # базовая
    if trend_4h == trend_1h:
        strength += 10  # оба ТФ совпадают
    if detect_choch(c15, trend) == direction:
        strength += 10
    if detect_sweep(c15, trend) == direction:
        strength += 5
    if ob:
        strength += 10
    if has_fvg(c15, direction):
        strength += 5
    if volume_ok(c15):
        strength += 5
    if in_kill_zone():
        strength += 5
    strength = min(strength, 100)
    
    # МИНИМАЛЬНАЯ СИЛА — отсекаем слабые сигналы
    if strength < 70:
        return None
    
    return {
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": 3.0,
        "atr": a,
        "strength": strength,
        "kill_zone": in_kill_zone(),
        "trend_4h": trend_4h,
        "trend_1h": trend_1h,
        "time": datetime.now(timezone.utc).strftime("%H:%M UTC"),
    }

def format_signal(sig):
    emoji = "🟢" if sig["direction"] == "long" else "🔴"
    d = "LONG" if sig["direction"] == "long" else "SHORT"
    kz = "✅ Kill Zone" if sig["kill_zone"] else "⚪️ вне Kill Zone"
    
    # Тренды для отображения
    t4 = sig.get("trend_4h", "neutral")
    t1 = sig.get("trend_1h", "neutral")
    t4_e = "🟢" if t4 == "bullish" else "🔴" if t4 == "bearish" else "⚪️"
    t1_e = "🟢" if t1 == "bullish" else "🔴" if t1 == "bearish" else "⚪️"
    
    return (
        f"{emoji} *{sig['symbol']} — {d}*\n\n"
        f"💪 Сила: *{sig['strength']}%*\n"
        f"🎯 Вход: `{sig['entry']:.5f}`\n"
        f"🛑 Стоп: `{sig['sl']:.5f}`\n"
        f"🏁 Тейк: `{sig['tp']:.5f}`\n"
        f"📊 R/R: 1:{sig['rr']:.1f}\n"
        f"📈 ATR: `{sig['atr']:.5f}`\n\n"
        f"📊 *Мультитаймфрейм:*\n"
        f"   4H: {t4_e} {t4}\n"
        f"   1H: {t1_e} {t1}\n"
        f"   15m: {emoji} {d}\n\n"
        f"🕐 {sig['time']}  {kz}\n"
        f"📐 4H+1H+CHoCH+Sweep+OB+FVG+Vol"
    )

def signal_keyboard(sid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟢 LONG открыл", callback_data=f"open:{sid}:long"),
         InlineKeyboardButton("🔴 SHORT открыл", callback_data=f"open:{sid}:short")],
        [InlineKeyboardButton("❌ Пропустить", callback_data=f"skip:{sid}"),
         InlineKeyboardButton("⏰ Через час", callback_data=f"snooze:{sid}")],
        [InlineKeyboardButton("✅ Закрыл в TP", callback_data=f"close:{sid}:tp"),
         InlineKeyboardButton("⛔️ Закрыл в SL", callback_data=f"close:{sid}:sl")],
    ])

signals_log = {}
active_trades = {}  # {sid: {symbol, direction, entry, sl, tp}}
TRADES_FILE = "active_trades.json"

def sig_id(sig):
    return f"{sig['symbol'].replace('/','')}_{sig['time'].replace(':','').replace(' ','')}"

async def scan_market(app):
    log.info("Сканирование рынка...")
    found = 0
    for symbol in SYMBOLS:
        try:
            sig = await asyncio.to_thread(analyze_symbol, symbol)
        except Exception as e:
            log.warning(f"{symbol}: {e}")
            continue
        if not sig:
            continue
        sid = sig_id(sig)
        if sid in signals_log:
            continue
        signals_log[sid] = sig
        s = load_stats()
        s["signals"] += 1
        s["open"] += 1
        save_stats(s)
        found += 1
        try:
            await app.bot.send_message(
                chat_id=CHAT_ID, text=format_signal(sig),
                parse_mode=ParseMode.MARKDOWN, reply_markup=signal_keyboard(sid),
            )
        except Exception as e:
            log.warning(f"send: {e}")
        await asyncio.sleep(0.3)
    log.info(f"Новых сигналов: {found}")

async def track_trades(app):
    """Каждые 30 сек проверяет цены и закрывает сделки по TP/SL"""
    while True:
        await asyncio.sleep(30)
        if not active_trades:
            continue
        for sid, trade in list(active_trades.items()):
            try:
                url = f"https://www.okx.com/api/v5/market/ticker?instId={trade['symbol']}"
                out = subprocess.check_output(
                    ["curl", "-s", "--max-time", "5", url],
                    stderr=subprocess.DEVNULL,
                )
                data = json.loads(out.decode("utf-8"))
                if data.get("code") != "0" or not data.get("data"):
                    continue
                last = float(data["data"][0]["last"])

                triggered = None
                if trade["direction"] == "long":
                    if last >= trade["tp"]:
                        triggered = "tp"
                    elif last <= trade["sl"]:
                        triggered = "sl"
                else:  # short
                    if last <= trade["tp"]:
                        triggered = "tp"
                    elif last >= trade["sl"]:
                        triggered = "sl"

                if triggered:
                    s = load_stats()
                    s["open"] = max(0, s["open"] - 1)
                    if triggered == "tp":
                        s["tp"] += 1
                        pnl = "+"
                        emoji = "✅"
                    else:
                        s["sl"] += 1
                        pnl = "-"
                        emoji = "⛔️"
                    save_stats(s)

                    await app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=f"{emoji} *{trade['symbol']} {'LONG' if trade['direction']=='long' else 'SHORT'} закрыт по {triggered.upper()}*\n"
                             f"💰 Цена: `{last}` ({pnl})\n"
                             f"🎯 TP: `{trade['tp']:.5f}`\n"
                             f"🛑 SL: `{trade['sl']:.5f}`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    active_trades.pop(sid, None)
                    save_active_trades()
            except Exception as e:
                log.error(f"track_trades {sid}: {e}")

async def scan_loop_aligned(app):
    """Цикл сканирования, привязанный к :00/:15/:30/:45 — открытие 15m свечей"""
    while True:
        try:
            await sync_sleep_to_next_15m()
            log.info("Запуск скана по таймеру")
            await scan_market(app)
        except Exception as e:
            log.error(f"Ошибка в scan_loop_aligned: {e}")
            await asyncio.sleep(60)


def sync_sleep_to_next_15m():
    """Спать до ближайшей :00/:15/:30/:45 минуты"""
    now = datetime.now()
    minute = now.minute
    second = now.second
    minutes_to_next = 15 - (minute % 15)
    if minutes_to_next == 15 and second > 0:
        minutes_to_next = 0
    seconds_to_next = minutes_to_next * 60 - second
    if seconds_to_next < 30:
        seconds_to_next += 900  # минимум 30 сек + ждём следующую свечу
    log.info(f"Следующий скан через {seconds_to_next // 60} мин {seconds_to_next % 60} сек")
    return asyncio.sleep(seconds_to_next)

async def start_cmd(update, context):
    await update.message.reply_text(
        "🤖 *IDM Bot v4*\n\n"
        "Топ-25 OKX, тренд 4H + вход 15m.\n"
        "R/R 1:3, скан каждые 15 мин.\n\n"
        "/signals /scan /stats /top",
        parse_mode=ParseMode.MARKDOWN,
    )

async def signals_cmd(update, context):
    if not signals_log:
        await update.message.reply_text("Нет активных сигналов.")
        return
    for sid, sig in list(signals_log.items())[-10:]:
        await update.message.reply_text(
            format_signal(sig), parse_mode=ParseMode.MARKDOWN,
            reply_markup=signal_keyboard(sid),
        )

async def scan_cmd(update, context):
    await update.message.reply_text("🔍 Сканирую...")
    await scan_market(context.application)
    await update.message.reply_text("✅ Готово")

async def stats_cmd(update, context):
    s = load_stats()
    total = s["tp"] + s["sl"]
    wr = (s["tp"] / total * 100) if total else 0
    await update.message.reply_text(
        f"📊 *Статистика*\n\n"
        f"📡 Сигналов: *{s['signals']}*\n"
        f"🟢 TP: *{s['tp']}*\n"
        f"🔴 SL: *{s['sl']}*\n"
        f"📈 Winrate: *{wr:.1f}%*\n"
        f"⏳ Открытых: *{s['open']}*",
        parse_mode=ParseMode.MARKDOWN,
    )

async def top_cmd(update, context):
    rows = []
    for symbol in SYMBOLS:
        url = f"https://www.okx.com/api/v5/market/ticker?instId={symbol}"
        try:
            out = subprocess.check_output(
                ["curl", "-s", "--max-time", "10", url], stderr=subprocess.DEVNULL,
            )
            data = json.loads(out.decode("utf-8"))
        except Exception:
            continue
        if data.get("code") != "0" or not data.get("data"):
            continue
        try:
            ticker = data["data"][0]
            last = float(ticker.get("last", 0))
            open24 = float(ticker.get("open24h", 0))
            ch = ((last - open24) / open24 * 100) if open24 else 0.0
        except Exception:
            ch = 0.0
        rows.append((symbol.replace("-", "/"), ch))
    rows.sort(key=lambda x: x[1], reverse=True)
    msg = "🔥 *Топ-25 OKX (24h %)*\n\n"
    for i, (s, ch) in enumerate(rows, 1):
        e = "🟢" if ch >= 0 else "🔴"
        msg += f"{i:>2}. {e} {s}: *{ch:+.2f}%*\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def button_handler(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    s = load_stats()
    if data.startswith("open:"):
        _, sid, direction = data.split(":", 2)
        if sid not in signals_log:
            await q.edit_message_text("Сигнал устарел.")
            return
        sig = signals_log[sid]
        d = "LONG" if direction == "long" else "SHORT"
        active_trades[sid] = {
            "symbol": sig["symbol"],
            "direction": direction,
            "entry": sig["entry"],
            "sl": sig["sl"],
            "tp": sig["tp"],
        }
        save_active_trades()
        await q.edit_message_text(
            f"{q.message.text}\n\n✅ *Открыл {d}*\n\n"
            f"🤖 Бот следит за сделкой автоматически.\n"
            f"Закроет сам по TP/SL.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif data.startswith("close:"):
        _, sid, result = data.split(":", 2)
        s["open"] = max(0, s["open"] - 1)
        if result == "tp":
            s["tp"] += 1
            tag = "✅ TP"
        else:
            s["sl"] += 1
            tag = "⛔️ SL"
        save_stats(s)
        active_trades.pop(sid, None)
        save_active_trades()
        await q.edit_message_text(f"{q.message.text}\n\n{tag} закрыто")
    elif data.startswith("skip:"):
        _, sid = data.split(":", 1)
        if signals_log.pop(sid, None):
            s["open"] = max(0, s["open"] - 1)
            save_stats(s)
        active_trades.pop(sid, None)
        save_active_trades()
        await q.edit_message_text(f"{q.message.text}\n\n❌ Пропущено")
    elif data.startswith("snooze:"):
        _, sid = data.split(":", 1)
        sig = signals_log.get(sid)
        if sig:
            await q.edit_message_text(f"{q.message.text}\n\n⏰ Напомню через час")
            context.job_queue.run_once(
                resend_signal, when=3600,
                data={"chat_id": q.message.chat_id, "sig": sig, "sid": sid},
            )

async def resend_signal(context):
    p = context.job.data
    await context.application.bot.send_message(
        chat_id=p["chat_id"], text="⏰ *Напоминание*\n\n" + format_signal(p["sig"]),
        parse_mode=ParseMode.MARKDOWN, reply_markup=signal_keyboard(p["sid"]),
    )

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Запустить бота"),
        BotCommand("signals", "Активные сигналы"),
        BotCommand("scan", "Сканировать сейчас"),
        BotCommand("stats", "Статистика TP/SL"),
        BotCommand("top", "Топ монет OKX"),
    ])

    load_active_trades()
    asyncio.get_event_loop().create_task(track_trades(app))
    log.info("Tracker сделок запущен (проверка каждые 30 сек)")

    # Запуск сканера синхронизированного с :00/:15/:30/:45
    asyncio.get_event_loop().create_task(scan_loop_aligned(app))
    log.info("Сканер запущен, синхронизирован с :00/:15/:30/:45")

def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("signals", signals_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CallbackQueryHandler(button_handler))
    log.info("Старт polling...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        poll_interval=3,
    )

if __name__ == "__main__":
    main()


