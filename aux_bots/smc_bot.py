"""
╔══════════════════════════════════════════════════════════════════╗
║  SMC ANALYZER BOT — Smart Money Concepts Signal Generator       ║
║  Работает РЯДОМ с spot_bot_v170.py в одной папке               ║
║                                                                  ║
║  5 анализов:                                                     ║
║  1. Имбаланс (FVG — Fair Value Gap)                             ║
║  2. Снятие ликвидности (Liquidity Sweep)                        ║
║  3. Структура рынка (Market Structure HH/HL)                    ║
║  4. Слом структуры (BOS / CHoCH)                                ║
║  5. Ордер блок (Order Block)                                     ║
║                                                                  ║
║  Запуск: python3 smc_bot.py                                      ║
║  Общение с v170: через smc_signals.json                         ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os, json, time, logging, datetime, requests
from typing import Optional, List, Dict, Tuple
from pybit.unified_trading import HTTP

# ══════════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ — заполни те же ключи что в v170
# ══════════════════════════════════════════════════════════════════
# [FIX-aux-SEC] КРИТИЧНО: комментарий ниже уже говорил "не хардкодить!", но именно это и
# было сделано — реальные Bybit API-ключ и секрет были зашиты как fallback-значения
# environ.get(), то есть попадали в код (и во все места, куда этот файл когда-либо
# копировался/отправлялся) в открытом виде. Убраны — теперь без переменных окружения бот
# просто не подключится к бирже (безопасное поведение), вместо тихого использования чужого
# зашитого ключа. РЕКОМЕНДУЮ: перевыпустить эти Bybit API-ключ/секрет (Settings → API) —
# раз они были в открытом виде, их нужно считать скомпрометированными.
import os as _os_smc
API_KEY    = _os_smc.environ.get("BYBIT_API_KEY", "")
API_SECRET = _os_smc.environ.get("BYBIT_API_SECRET", "")
if not API_KEY or not API_SECRET:
    # Fallback: читаем из файла рядом с ботом
    try:
        _cfg_path = _os_smc.path.join(_os_smc.path.dirname(__file__), "api_config.py")
        if _os_smc.path.exists(_cfg_path):
            exec(open(_cfg_path).read(), globals())
    except Exception: pass
TESTNET    = False

# [FIX-aux-SEC] Тот же токен, что зашит был во всех остальных 5 файлах в открытом виде —
# рекомендую перевыпустить через @BotFather, дальше держать только в переменных окружения.
TG_TOKEN   = _os_smc.environ.get("TG_TOKEN", "")
TG_CHAT_ID = _os_smc.environ.get("TG_CHAT_ID", "")

# Параметры сканера SMC
SCAN_INTERVAL_SEC  = 300     # сканировать каждые 5 минут
TOP_COINS_COUNT    = 50      # анализировать топ-50 монет по объёму
CANDLES_LIMIT      = 100     # свечей для анализа
SMC_TIMEFRAME      = "15"    # таймфрейм анализа (минуты)
SMC_CONFIRM_TF     = "60"    # подтверждение на 1H
MIN_SIGNAL_SCORE   = 4       # минимум совпавших из 5 условий
SIGNAL_TTL_MIN     = 60      # сигнал актуален 60 минут
MAX_24H_CHANGE_NEG = -2.0    # [FIX] не торгуем монеты с падением хуже -2% за 24h

# Файл связи с v170 (в той же папке)
BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
SIGNALS_FILE = os.path.join(BASE_DIR, "smc_signals.json")
LOG_FILE     = os.path.join(BASE_DIR, "smc_bot.log")

# ══════════════════════════════════════════════════════════════════
# ЛОГИРОВАНИЕ
# ══════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ]
)
log = logging.getLogger("smc_bot").info

# ══════════════════════════════════════════════════════════════════
# BYBIT КЛИЕНТ
# ══════════════════════════════════════════════════════════════════
client = HTTP(api_key=API_KEY, api_secret=API_SECRET,
              testnet=TESTNET, recv_window=20000)

# ══════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════
def tg_send_throttled(key: str, msg: str, cooldown_sec: int = 1800):
    """Отправляет TG сообщение не чаще cooldown_sec."""
    _cache_file = os.path.join(BASE_DIR, f".tg_throttle_{key}.tmp")
    try:
        now = time.time()
        if os.path.exists(_cache_file):
            if now - os.path.getmtime(_cache_file) < cooldown_sec:
                return
        with open(_cache_file, 'w') as _f: _f.write(str(now))
        tg_send(msg)
    except Exception:
        tg_send(msg)

def tg_send(msg: str):
    """Отправляем сигнал в Telegram."""
    if not TG_TOKEN or "ТВОЙ" in TG_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": msg,
                                  "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log(f"⚠️ TG error: {e}")

# ══════════════════════════════════════════════════════════════════
# ПОЛУЧЕНИЕ ДАННЫХ
# ══════════════════════════════════════════════════════════════════
def get_candles(symbol: str, interval: str, limit: int = 100) -> List[Dict]:
    """Получаем OHLCV свечи с Bybit."""
    try:
        r = client.get_kline(category="spot", symbol=symbol,
                              interval=interval, limit=limit)
        raw = r.get("result", {}).get("list", []) or []
        # Bybit возвращает в обратном порядке — переворачиваем
        raw.reverse()
        candles = []
        for c in raw:
            try:
                candles.append({
                    "ts":     int(c[0]),
                    "open":   float(c[1]),
                    "high":   float(c[2]),
                    "low":    float(c[3]),
                    "close":  float(c[4]),
                    "volume": float(c[5]),
                })
            except Exception:
                pass  # bad candle row — skip
        return candles
    except Exception as e:
        log(f"⚠️ candles {symbol}: {e}")
        return []

def _load_shared_blacklist() -> set:
    """[FIX-smc-185-1] Читаем blacklist из ДВУХ источников:
    1. shared_blacklist.json — постоянный (от v185 при старте)
    2. banned_coins.json — динамический (монеты забаненные во время торгов)
    Это гарантирует что smc_bot не шлёт сигналы по забаненным монетам.
    """
    result = set()
    try:
        # Источник 1: shared_blacklist.json (постоянный от v185)
        bl_file = os.path.join(BASE_DIR, "shared_blacklist.json")
        if os.path.exists(bl_file):
            with open(bl_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            bl = set(data.get("blacklist", []))
            result |= bl
    except Exception as e:
        log(f"⚠️ [smc] shared_blacklist: {e}")
    try:
        # Источник 2: banned_coins.json (динамический — монеты забаненные в торгах)
        ban_file = os.path.join(BASE_DIR, "banned_coins.json")
        if os.path.exists(ban_file):
            with open(ban_file, "r", encoding="utf-8") as f:
                ban_data = json.load(f)
            # [FIX-aux2-BUG] КРИТИЧНО: реальный banned_coins.json (проверил файл, который
            # пишет spot_bot.py) хранит список под ключом "banned" — а здесь искали "coins"
            # или "blacklist", которых там никогда не было. Из-за этого весь Источник 2
            # был мёртв с момента написания: smc_bot ни разу не подтягивал монеты, забаненные
            # ВО ВРЕМЯ торгов (только статичный список из shared_blacklist.json при старте) —
            # мог продолжать слать SMC-сигналы и TG-уведомления по монете, которую spot_bot
            # только что забанил навсегда за stop_loss/dump_exit.
            # Поддерживаем оба формата: список и dict с reasons
            if isinstance(ban_data, list):
                result |= set(ban_data)
            elif isinstance(ban_data, dict):
                coins = ban_data.get("banned", ban_data.get("coins", ban_data.get("blacklist", [])))
                result |= set(coins)
    except Exception as e:
        log(f"⚠️ [smc] banned_coins: {e}")
    if result:
        log(f"  📋 [smc] Общий blacklist: {len(result)} монет")
    return result


def get_top_symbols(count: int = 50) -> List[str]:
    """Топ монет по объёму за 24ч."""
    try:
        r = client.get_tickers(category="spot")
        tickers = r.get("result", {}).get("list", []) or []
        # Только USDT пары, фильтруем стейблы и левередж
        STABLE = {"USDT","USDC","BUSD","DAI","TUSD","FDUSD","USDP"}
        LEVERAGE_TOKENS = ["3L","3S","5L","5S","10L","10S","2L","2S"]
        # [FIX-smc-1] Фильтр нестандартных пар: металлы, commodities, индексы
        COMMODITY_TOKENS = ["XAU","XAG","XPT","XPD","OIL","GAS","CORN","WHEAT"]
        SYNTHETIC_PREFIXES = ["W","ST","S","CB","WT"]
        BLACKLIST_EXACT = {"XAUUSDT","XAGUSDT","XPTUSDT","XPDUSDT",
                           "USDCUSDT","EURUSDT","GBPUSDT","JPYUSDT"}
        # [FIX-smc-3] Добавляем blacklist от v180
        SHARED_BL = _load_shared_blacklist()
        filtered = []
        for t in tickers:
            sym = t.get("symbol","")
            if not sym.endswith("USDT"):
                continue
            base = sym.replace("USDT","")
            if base in STABLE:
                continue
            if any(lv in sym for lv in LEVERAGE_TOKENS):
                continue
            # [FIX-smc-4] Мем монеты — не торгуем (высокий риск, мало анализа)
            MEME_TOKENS={"PEPEUSDT","SHIBUSDT","DOGEUSDT","BONKUSDT","WIFUSDT",
                         "FLOKIUSDT","MEMEUSDT","BOMEUSDT","POPCATUSDT"}
            if sym in MEME_TOKENS:
                continue
            # [FIX-v186-3] Читаем news_signals.json — пропускаем новостные блоки
            try:
                _nf = os.path.join(BASE_DIR, "news_signals.json")
                if os.path.exists(_nf) and time.time()-os.path.getmtime(_nf)<1800:
                    with open(_nf,'r') as _fn:
                        _nd = json.load(_fn)
                    if not isinstance(_nd, dict): _nd = {}
                    if sym in set(_nd.get("blocked_coins",[])):
                        continue  # монета под новостным блоком
                    if _nd.get("global_sentiment")=="panic":
                        break     # глобальная паника → не сканируем
            except Exception:
                pass
            # [FIX-smc-1] Фильтруем commodities и нестандартные пары
            if sym in BLACKLIST_EXACT:
                continue
            # [FIX-smc-3b] Фильтруем монеты из blacklist v180
            if sym in SHARED_BL:
                continue
            base_coin = sym.replace("USDT","")
            if any(base_coin.startswith(c) for c in COMMODITY_TOKENS):
                continue
            # [FIX-v183-5b] Не включаем монеты с 24h change < -2% (медвежий тренд)
            try:
                _ch24_pct = float(t.get("price24hPcnt","0") or "0") * 100
                if _ch24_pct < MAX_24H_CHANGE_NEG:
                    continue
            except Exception:
                pass
            try:
                turnover = float(t.get("turnover24h","0") or 0)
                price    = float(t.get("lastPrice","0") or 0)
                if turnover < 1_000_000 or price <= 0:
                    continue
                filtered.append((sym, turnover))
            except Exception:
                pass  # [safe] bad ticker — skip
        # Сортируем по объёму
        filtered.sort(key=lambda x: -x[1])
        return [sym for sym,_ in filtered[:count]]
    except Exception as e:
        log(f"⚠️ get_top_symbols: {e}")
        return []

# ══════════════════════════════════════════════════════════════════
# 5 SMC АНАЛИЗОВ
# ══════════════════════════════════════════════════════════════════

def find_swing_points(candles: List[Dict], lookback: int = 3) -> List[Dict]:
    """Находим swing highs и swing lows."""
    swings = []
    for i in range(lookback, len(candles) - lookback):
        h = candles[i]["high"]
        l = candles[i]["low"]
        # Swing high
        if all(candles[j]["high"] <= h for j in range(i-lookback, i+lookback+1) if j != i):
            swings.append({"type": "high", "price": h, "idx": i,
                           "ts": candles[i]["ts"]})
        # Swing low
        if all(candles[j]["low"] >= l for j in range(i-lookback, i+lookback+1) if j != i):
            swings.append({"type": "low", "price": l, "idx": i,
                           "ts": candles[i]["ts"]})
    return swings


def check_fvg(candles: List[Dict]) -> Tuple[bool, Optional[Dict]]:
    """
    1. ИМБАЛАНС (Fair Value Gap).
    Бычий FVG: low свечи[i] > high свечи[i-2] → незаполненный разрыв вверх.
    Цена стремится вернуться заполнить его → зона поддержки.
    """
    if len(candles) < 10:
        return False, None
    # Ищем бычий FVG в последних 20 свечах
    for i in range(len(candles)-1, max(2, len(candles)-20), -1):
        gap_bottom = candles[i-2]["high"]
        gap_top    = candles[i]["low"]
        if gap_top > gap_bottom:  # Бычий FVG
            gap_size_pct = (gap_top - gap_bottom) / gap_bottom * 100
            # [FIX-smc-185-3] Минимальный FVG 0.6%:
            # Комиссия BUY 0.2% + SELL 0.2% = 0.4% итого
            # FVG 0.5% → прибыль = 0.5-0.4 = 0.1% (слишком мало!)
            # FVG 0.6% → прибыль = 0.6-0.4 = 0.2% (минимально приемлемо)
            # FVG 0.9%+ → прибыль = 0.5%+ (целевой диапазон +0.5% до +1.8%)
            if gap_size_pct >= 0.6:  # [FIX-smc-185-3] мин 0.6%
                current_price = candles[-1]["close"]
                # Цена должна быть вблизи или внутри FVG
                near_fvg = gap_bottom <= current_price <= gap_top * 1.02
                return True, {
                    "top":    gap_top,
                    "bottom": gap_bottom,
                    "size_pct": round(gap_size_pct, 3),
                    "near":   near_fvg,
                }
    return False, None


def check_liquidity_sweep(candles: List[Dict],
                          swings: List[Dict]) -> Tuple[bool, Optional[Dict]]:
    """
    2. СНЯТИЕ ЛИКВИДНОСТИ.
    Цена пробила предыдущий swing low (взяла стопы) и закрылась выше → разворот.
    Это признак того что умные деньги набрали позицию.
    """
    lows = [s for s in swings if s["type"] == "low"]
    if len(lows) < 2:
        return False, None
    # Берём предпоследний swing low
    prev_low = lows[-2]["price"]
    # Проверяем последние 5 свечей
    for i in range(max(0, len(candles)-5), len(candles)):
        c = candles[i]
        swept = c["low"] < prev_low        # Пробили ниже swing low
        recovered = c["close"] > prev_low   # Но закрылись выше
        if swept and recovered:
            sweep_depth = (prev_low - c["low"]) / prev_low * 100
            return True, {
                "swept_level": round(prev_low, 8),
                "sweep_low":   round(c["low"], 8),
                "depth_pct":   round(sweep_depth, 3),
            }
    return False, None


def check_market_structure(swings: List[Dict]) -> Tuple[bool, str]:
    """
    3. СТРУКТУРА РЫНКА.
    Бычья: Higher Highs (HH) + Higher Lows (HL) — тренд вверх.
    Медвежья: Lower Lows (LL) + Lower Highs (LH) — тренд вниз.
    """
    highs = [s for s in swings if s["type"] == "high"]
    lows  = [s for s in swings if s["type"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return False, "unknown"
    hh = highs[-1]["price"] > highs[-2]["price"]   # Higher High
    hl = lows[-1]["price"]  > lows[-2]["price"]    # Higher Low
    if hh and hl:
        return True, "bullish"   # Бычья структура ✅
    lh = highs[-1]["price"] < highs[-2]["price"]   # Lower High
    ll = lows[-1]["price"]  < lows[-2]["price"]    # Lower Low
    if lh and ll:
        return False, "bearish"  # Медвежья структура ❌
    return False, "ranging"


def check_bos(candles: List[Dict],
              swings: List[Dict]) -> Tuple[bool, Optional[Dict]]:
    """
    4. СЛОМ СТРУКТУРЫ (Break of Structure / Change of Character).
    BOS: в бычьей структуре цена пробивает последний HH → продолжение тренда.
    CHoCH: в медвежьей структуре цена пробивает последний LH → смена тренда.
    Нам интересен CHoCH вверх — разворот из медвежьего в бычий.
    """
    highs = [s for s in swings if s["type"] == "high"]
    lows  = [s for s in swings if s["type"] == "low"]
    if not highs or not lows:
        return False, None

    current_price = candles[-1]["close"]
    last_swing_high = highs[-1]["price"]
    last_swing_low  = lows[-1]["price"]

    # BOS вверх: цена пробила последний swing high
    if current_price > last_swing_high:
        break_pct = (current_price - last_swing_high) / last_swing_high * 100
        return True, {
            "type":      "BOS_UP",
            "level":     round(last_swing_high, 8),
            "break_pct": round(break_pct, 3),
        }
    # CHoCH: в серии нисходящих максимумов цена пробила последний LH
    if len(highs) >= 3:
        lh1 = highs[-2]["price"]
        lh2 = highs[-3]["price"]
        if lh1 < lh2 and current_price > lh1:  # Было LH, теперь пробили его
            return True, {
                "type":  "CHoCH",
                "level": round(lh1, 8),
            }
    return False, None


def find_order_block(candles: List[Dict],
                     swings: List[Dict]) -> Tuple[bool, Optional[Dict]]:
    """
    5. ОРДЕР БЛОК.
    Последняя медвежья свеча перед сильным бычьим импульсом.
    Умные деньги открыли позиции здесь — цена вернётся за ними.
    Условие: цена сейчас вернулась в зону ордер блока.
    """
    if len(candles) < 10:
        return False, None
    current_price = candles[-1]["close"]
    # Ищем ордер блок в последних 30 свечах
    for i in range(len(candles)-3, max(2, len(candles)-30), -1):
        c = candles[i]
        c_next = candles[i+1]
        # Медвежья свеча
        is_bearish = c["close"] < c["open"]
        if not is_bearish:
            continue
        # После неё сильный бычий импульс
        body_next = c_next["close"] - c_next["open"]
        body_curr = c["open"] - c["close"]
        is_impulse = body_next > body_curr * 1.5 and c_next["close"] > c_next["open"]
        if not is_impulse:
            continue
        # Ордер блок: зона от close до open медвежьей свечи
        ob_bottom = c["close"]
        ob_top    = c["open"]
        # Цена сейчас внутри или чуть выше ордер блока
        price_in_ob = ob_bottom * 0.995 <= current_price <= ob_top * 1.005
        if price_in_ob:
            ob_size_pct = (ob_top - ob_bottom) / ob_bottom * 100
            return True, {
                "top":      round(ob_top, 8),
                "bottom":   round(ob_bottom, 8),
                "size_pct": round(ob_size_pct, 3),
            }
    return False, None


# ══════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ АНАЛИЗ
# ══════════════════════════════════════════════════════════════════

def analyze_symbol(symbol: str) -> Optional[Dict]:
    """
    Запускаем все 5 SMC анализов на монете.
    Возвращает сигнал если минимум MIN_SIGNAL_SCORE из 5 совпали.
    """
    # Получаем свечи основного и подтверждающего ТФ
    candles_15m = get_candles(symbol, SMC_TIMEFRAME, CANDLES_LIMIT)
    candles_1h  = get_candles(symbol, SMC_CONFIRM_TF, 50)
    if len(candles_15m) < 30 or len(candles_1h) < 15:
        return None

    swings_15m = find_swing_points(candles_15m, lookback=3)
    swings_1h  = find_swing_points(candles_1h,  lookback=3)
    current_price = candles_15m[-1]["close"]

    # ── Запускаем 5 анализов ────────────────────────────
    fvg_ok,   fvg_data   = check_fvg(candles_15m)
    liq_ok,   liq_data   = check_liquidity_sweep(candles_15m, swings_15m)
    struct_ok, struct_dir = check_market_structure(swings_1h)   # На 1H
    bos_ok,   bos_data   = check_bos(candles_15m, swings_15m)
    ob_ok,    ob_data    = find_order_block(candles_15m, swings_15m)

    # Считаем score
    score = sum([fvg_ok, liq_ok, struct_ok, bos_ok, ob_ok])

    # [FIX-smc-185-4] Проверка ожидаемой прибыли
    if fvg_ok and isinstance(fvg_data, dict):
        _fvg_pct = float(fvg_data.get("size_pct", 0) or 0)  # [FIX] было: "gap_size_pct" — ключ не существует в словаре FVG!
        _net = _fvg_pct - 0.4  # минус комиссии 0.2%*2
        if _net < 0.1:
            log(f"  ⚠️ {symbol}: FVG={_fvg_pct:.2f}% → чистая прибыль {_net:.2f}% мала → нет сигнала")
            return None  # [FIX] было: return False, None — tuple вместо Optional[Dict]!
    checks = {
        "1_fvg":       {"ok": fvg_ok,    "data": fvg_data},
        "2_liquidity": {"ok": liq_ok,    "data": liq_data},
        "3_structure": {"ok": struct_ok,  "data": struct_dir},
        "4_bos":       {"ok": bos_ok,    "data": bos_data},
        "5_ob":        {"ok": ob_ok,     "data": ob_data},
    }

    # [FIX-smc-187] Проверяем что структура подходящая для ВХОДА (бычья)
    # [FIX-SMC-CRITICAL] structure_dir → struct_dir (переменная называется struct_dir с L435)
    if struct_dir == "bearish":
        return None  # нисходящий тренд на 1H → нет сигнала (None = нет, как и все другие return в этой функции)

    # [FIX-v188-SMC] Буст от volume_bot и news_bot (trending_coins)
    try:
        _vf = os.path.join(BASE_DIR, "volume_signals.json")
        if os.path.exists(_vf) and time.time()-os.path.getmtime(_vf)<600:
            with open(_vf,'r') as _fv: _vd=json.load(_fv)
            if symbol in set(a["symbol"] for a in _vd.get("anomalies",[])):
                score += 1  # аномальный объём = +1 балл (max 6/5)
                log(f"  🔥 {symbol}: аномальный объём → score={score}")
        _nf = os.path.join(BASE_DIR, "news_signals.json")
        if os.path.exists(_nf) and time.time()-os.path.getmtime(_nf)<1800:
            with open(_nf,'r') as _fn: _nd=json.load(_fn)
            if symbol in set(_nd.get("trending_coins",[])):
                score += 0.5  # trending = +0.5
                log(f"  📈 {symbol}: trending → score={score}")
    except Exception:
        pass

    if score < MIN_SIGNAL_SCORE:
        return None  # Не достаточно условий

    # Дополнительные данные для мозга v170
    # Последние значения для записи в BotKnowledge
    change_24h = 0.0
    try:
        t = client.get_tickers(category="spot", symbol=symbol)
        tl = t.get("result",{}).get("list",[]) or []
        if tl: change_24h = float(tl[0].get("price24hPcnt","0") or 0) * 100
    except Exception as _ce:
        log(f"⚠️ [change_24h] {_ce}")

    signal = {
        "symbol":        symbol,
        "timestamp":     time.time(),
        "datetime":      datetime.datetime.now().isoformat(),
        "price":         current_price,
        "change_24h":    round(change_24h, 2),
        "score":         score,          # из 5
        "score_pct":     score * 20,     # в процентах
        "direction":     "long",         # бот ищет только long на споте
        "structure_dir": struct_dir,
        "checks":        checks,
        "valid_until":   time.time() + SIGNAL_TTL_MIN * 60,
        "consumed_by_v170": False,       # v170 поставит True когда прочитает
        "v170_trade_result": None,       # v170 запишет результат сделки
    }
    return signal


# ══════════════════════════════════════════════════════════════════
# РАБОТА С ФАЙЛОМ СИГНАЛОВ (общение с v170)
# ══════════════════════════════════════════════════════════════════

def load_signals() -> Dict:
    """Читаем текущие сигналы из файла."""
    _default = {"signals": [], "last_scan": 0, "total_sent": 0}
    try:
        if os.path.exists(SIGNALS_FILE):
            with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            # [FIX-ROOT] Защита: если файл повреждён и содержит [] вместо {}
            # json.load вернёт list → data.get() → 'list' object has no attribute 'get'
            if not isinstance(loaded, dict):
                log(f"⚠️ load_signals: файл повреждён (тип {type(loaded).__name__}), сброс")
                return _default
            # Гарантируем наличие ключа signals как список
            if not isinstance(loaded.get("signals"), list):
                loaded["signals"] = []
            return loaded
    except Exception as e:
        log(f"⚠️ load_signals: {e}")
    return _default


def save_signals(data: Dict):
    """Сохраняем сигналы в файл.
    [FIX-aux-ATOMIC] Пишем во временный файл и атомарно подменяем — см. тот же фикс в
    correlation_bot.py. spot_bot.py читает smc_signals.json очень часто (каждый вход
    сверяется с ним), риск поймать недописанный файл здесь выше, чем у остальных ботов.
    """
    try:
        _tmp = SIGNALS_FILE + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(_tmp, SIGNALS_FILE)
    except Exception as e:
        log(f"⚠️ save_signals: {e}")


def clean_old_signals(data: Dict) -> Dict:
    """Удаляем устаревшие сигналы (старше TTL)."""
    now = time.time()
    data["signals"] = [s for s in data["signals"]
                       if s.get("valid_until", 0) > now]
    return data


def add_signal(data: Dict, signal: Dict) -> Dict:
    """Добавляем новый сигнал, избегаем дублей."""
    # Удаляем старый сигнал для этой монеты если есть
    data["signals"] = [s for s in data["signals"]
                       if s.get("symbol") != signal["symbol"]]
    data["signals"].append(signal)
    data["total_sent"] = data.get("total_sent", 0) + 1
    return data


# ══════════════════════════════════════════════════════════════════
# TELEGRAM УВЕДОМЛЕНИЕ О СИГНАЛЕ
# ══════════════════════════════════════════════════════════════════

def format_signal_msg(sig: Dict) -> str:
    """Форматируем красивое сообщение о сигнале."""
    checks = sig.get("checks", {})
    score  = sig.get("score", 0)
    stars  = "⭐" * score + "☆" * (5 - score)

    def ck(key): return "✅" if checks.get(key,{}).get("ok") else "❌"

    msg = (
        f"🎯 <b>SMC СИГНАЛ: {sig['symbol']}</b>\n"
        f"{'═'*28}\n"
        f"Цена: <b>${sig['price']:.6g}</b> | 24h: {sig['change_24h']:+.1f}%\n"
        f"Оценка: {stars} ({score}/5)\n\n"
        f"<b>5 АНАЛИЗОВ:</b>\n"
        f"{ck('1_fvg')} 1. Имбаланс (FVG)\n"
        f"{ck('2_liquidity')} 2. Снятие ликвидности\n"
        f"{ck('3_structure')} 3. Структура рынка ({sig.get('structure_dir','?')})\n"
        f"{ck('4_bos')} 4. Слом структуры (BOS)\n"
        f"{ck('5_ob')} 5. Ордер блок\n\n"
    )
    # Детали
    fvg = checks.get("1_fvg",{}).get("data")
    if fvg:
        msg += f"📊 FVG: {fvg.get('bottom'):.6g} — {fvg.get('top'):.6g} ({fvg.get('size_pct'):.2f}%)\n"
    ob = checks.get("5_ob",{}).get("data")
    if ob:
        msg += f"📦 OB:  {ob.get('bottom'):.6g} — {ob.get('top'):.6g}\n"
    bos = checks.get("4_bos",{}).get("data")
    if bos:
        msg += f"💥 {bos.get('type')}: пробой {bos.get('level'):.6g}\n"
    msg += f"\n⏰ Актуален: {SIGNAL_TTL_MIN} мин\n"
    msg += f"🤖 v187 получит сигнал автоматически"
    return msg


# ══════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ ЦИКЛ
# ══════════════════════════════════════════════════════════════════

def check_signal_feedback(data: dict):
    """[FIX-smc-2] Проверяем обратную связь — открыл ли v177 позицию по сигналу.
    v177 пишет в smc_signals.json поля:
    - consumed_by_v170: True/False
    - v170_trade_result: "opened_by_smc" / "opened_independent" / None
    - v170_skip_reason: почему не открыл если не открыл
    """
    try:
        now = time.time()
        for sig in data.get("signals", []):
            sym    = sig.get("symbol","?")
            score  = sig.get("score", 0)
            result = sig.get("v170_trade_result")
            reason = sig.get("v170_skip_reason","")
            ts     = sig.get("timestamp", 0)
            # Сигнал старше 5 мин и ещё без ответа
            if result is None and (now - ts) > 300:
                age_min = (now - ts) / 60
                msg = (
                    f"⏳ <b>SMC СИГНАЛ БЕЗ ОТВЕТА</b>\n"
                    f"Монета: {sym} | Score: {score}/5\n"
                    f"Прошло: {age_min:.0f} мин\n"
                    f"v177 ещё не открыл позицию\n"
                    f"Причина: фильтры сканера или нет подходящей монеты"
                )
                tg_send(msg)
                # Помечаем чтобы не спамить
                sig["v170_skip_reason"] = "timeout_no_response"
            elif result == "opened_by_smc":
                msg = (
                    f"✅ <b>ПОЗИЦИЯ ОТКРЫТА ПО SMC СИГНАЛУ</b>\n"
                    f"Монета: {sym} | Score: {score}/5\n"
                    f"v177 открыл позицию благодаря SMC боту 🎯"
                )
                tg_send(msg)
                sig["v170_trade_result"] = "reported"
            elif result == "opened_independent":
                msg = (
                    f"🔵 <b>ПОЗИЦИЯ ОТКРЫТА САМОСТОЯТЕЛЬНО</b>\n"
                    f"Монета: {sym}\n"
                    f"v177 открыл по своим фильтрам (без SMC сигнала)"
                )
                tg_send(msg)
                sig["v170_trade_result"] = "reported"
    except Exception as e:
        log(f"⚠️ [check_feedback] {e}")


def main():
    log("="*60)
    log("  SMC ANALYZER BOT ЗАПУЩЕН")
    log(f"  Сканирую топ {TOP_COINS_COUNT} монет каждые {SCAN_INTERVAL_SEC}с")
    log(f"  Мин условий для сигнала: {MIN_SIGNAL_SCORE}/5")
    log(f"  Сигналы → {SIGNALS_FILE}")
    log("="*60)

    tg_send(
        f"🚀 <b>SMC Bot запущен</b>\n"
        f"Сканирую топ {TOP_COINS_COUNT} монет\n"
        f"Мин score: {MIN_SIGNAL_SCORE}/5\n"
        f"Работаю вместе с v170 ✅"
    )

    scan_num = 0
    while True:
        scan_num += 1
        scan_start = time.time()
        log(f"\n{'─'*50}")
        log(f"📡 СКАН #{scan_num} | {datetime.datetime.now().strftime('%H:%M:%S')}")

        try:
            # Загружаем текущие данные
            data = load_signals()

            # [FIX-smc-185-2] Проверяем: v185 уже торгует по нашему сигналу?
            # Если да — пауза сканирования (не нагружаем API пока бот в сделке)
            _v185_trading_our_signal = False
            _current_trading_sym = None
            try:
                _sf = os.path.join(BASE_DIR, "smc_signals.json")
                if os.path.exists(_sf):
                    with open(_sf, "r", encoding="utf-8") as _f:
                        _sd = json.load(_f)
                    if not isinstance(_sd, dict):
                        _sd = {}
                    for _s in (_sd.get("signals") or []):
                        if (_s.get("consumed_by_v170") and
                                _s.get("v170_trade_result") == "opened_by_smc" and
                                float(_s.get("valid_until", 0) or 0) > time.time()):
                            _v185_trading_our_signal = True
                            _current_trading_sym = _s.get("symbol", "?")
                            break
            except Exception:
                pass

            if _v185_trading_our_signal:
                log(f"⏸️ [smc] v185 торгует {_current_trading_sym} по нашему сигналу — пауза сканирования")
                log(f"   Следующая проверка через {SCAN_INTERVAL_SEC//2}с (не нагружаем API)")
                # TG только раз в 30 минут чтобы не спамить
                if not getattr(_pause_tg_sent := None, '_dummy', False):
                    _pause_key = f"smc_pause_{_current_trading_sym}"
                    tg_send_throttled(_pause_key,
                        f"⏸️ <b>SMC Bot в режиме ожидания</b>\n"
                        f"v185 торгует {_current_trading_sym} по нашему сигналу\n"
                        f"Сканирование приостановлено — жду завершения сделки",
                        cooldown_sec=1800)
                time.sleep(SCAN_INTERVAL_SEC // 2)
                continue
            data = clean_old_signals(data)
            data["last_scan"] = time.time()

            # Получаем список монет
            symbols = get_top_symbols(TOP_COINS_COUNT)
            log(f"  Монет для анализа: {len(symbols)}")

            found_count = 0
            for i, symbol in enumerate(symbols):
                try:
                    signal = analyze_symbol(symbol)
                    if signal:
                        found_count += 1
                        data = add_signal(data, signal)
                        log(f"  🎯 СИГНАЛ: {symbol} | score={signal['score']}/5 | {signal['structure_dir']}")
                        # TG уведомление
                        tg_send(format_signal_msg(signal))
                    # Небольшая пауза чтобы не попасть на rate limit
                    time.sleep(0.3)
                except Exception as e:
                    log(f"  ⚠️ {symbol}: {e}")
                    continue

            # Сохраняем
            save_signals(data)
            elapsed = time.time() - scan_start
            active  = len([s for s in data["signals"] if s.get("valid_until",0) > time.time()])
            log(f"  ✅ Скан завершён за {elapsed:.1f}с | сигналов: {found_count} новых | активных: {active}")

            # [FIX-smc-2] Проверяем: открыл ли v177 позицию по нашим сигналам?
            check_signal_feedback(data)
            save_signals(data)  # [FIX] сохраняем после feedback — иначе v170_skip_reason теряется и TG спамит

            if found_count == 0:
                log(f"  ℹ️ Условий нет — рынок не готов. Жду {SCAN_INTERVAL_SEC}с...")

        except KeyboardInterrupt:
            log("  ⛔ SMC Bot остановлен пользователем")
            tg_send("⛔ SMC Bot остановлен")
            break
        except Exception as e:
            log(f"  ❌ Ошибка основного цикла: {e}")

        # Пауза до следующего скана
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
