#!/usr/bin/env python3
"""
correlation_bot.py — v1.0
Мониторинг BTC/ETH. Если BTC падает >1.5% за 15 мин →
все альты пойдут за ним. Пишет флаг в market_state.json.
spot_bot_v188 читает и приостанавливает новые BUY.

Также включает: regime_bot логику (ADX BTC 4H → trending/ranging/volatile)
Пишет: market_state.json
"""
import os, json, time, datetime, requests

BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
STATE_FILE  = os.path.join(BASE_DIR, "market_state.json")
# [FIX-aux-SEC] Раньше здесь был зашит реальный TG-токен как fallback-значение — если
# переменная окружения TG_TOKEN не задана, использовался бы этот открытый токен. Токен и
# так уже был виден во всех 6 файлах в открытом виде — рекомендую его перевыпустить через
# @BotFather и держать только в переменных окружения, без запасных значений в коде.
TG_TOKEN    = os.environ.get("TG_TOKEN","")
TG_CHAT     = os.environ.get("TG_CHAT_ID","")

SCAN_INTERVAL   = 60      # каждую минуту
BTC_DROP_THRESH = -1.5    # % за 15 мин → блок
ETH_DROP_THRESH = -2.0
BTC_PUMP_THRESH = 2.0     # % памп → тоже осторожно
ADX_RANGING     = 20.0    # BTC ADX 4H < 20 → боковик

_url = "https://api.bybit.com"
def log(m): print(f"{datetime.datetime.now().strftime('%H:%M:%S')} | {m}")

def tg(msg):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":msg,"parse_mode":"HTML"}, timeout=10)
    except Exception as _tg_e: pass  # TG ошибки не критичны

_tg_ts:dict={}
def tg_th(key,msg,c=1800):
    n=time.time()
    if n-_tg_ts.get(key,0)<c: return
    _tg_ts[key]=n; tg(msg)

def get_kline(symbol,interval,limit):
    try:
        r=requests.get(f"{_url}/v5/market/kline",
            params={"category":"spot","symbol":symbol,
                    "interval":interval,"limit":limit},timeout=10)
        return (r.json().get("result",{}).get("list",[]) or []) if r.status_code==200 else []
    except Exception: return []

def calc_change_pct(candles, bars=3):
    """Изменение цены за последние bars свечей (1H = 3ч, 15m = 45мин)"""
    if len(candles)<bars+1: return 0.0
    old_close=float(candles[bars][4])
    new_close=float(candles[0][4])
    return (new_close-old_close)/old_close*100 if old_close>0 else 0.0

def calc_real_adx(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """[v219-FIX] Настоящий ADX по Wilder (не упрощённый).
    Копировано из spot_bot — единый расчёт во всех ботах.
    """
    try:
        n = len(closes)
        if n < period + 5 or len(highs) < n or len(lows) < n:
            return 0.0
        tr_list, pdm_list, ndm_list = [], [], []
        for i in range(1, n):
            h, l, pc = highs[i], lows[i], closes[i-1]
            tr  = max(h-l, abs(h-pc), abs(l-pc))
            # [FIX-aux2-BUG] Было max(h-highs[i-1],0.0)/max(lows[i-1]-l,0.0) + if/elif —
            # на ТОЧНОМ равенстве up_move==dn_move (>0) "if pdm<=ndm: pdm=0.0" срабатывает
            # (равенство попадает в "<="), но elif после этого уже не проверяется — ndm
            # остаётся НЕобнулённым. У spot_bot.py (единый расчёт, на который ссылается
            # комментарий выше) в этом случае обнуляются ОБА (строгое ">" с обеих сторон
            # само по себе исключает и pdm, и ndm при равенстве) — то есть при тай-брейке
            # эта копия и оригинал расходились. Переписано на ту же логику, что в spot_bot.
            up_move = h - highs[i-1]
            dn_move = lows[i-1] - l
            pdm = up_move if (up_move > dn_move and up_move > 0) else 0.0
            ndm = dn_move if (dn_move > up_move and dn_move > 0) else 0.0
            tr_list.append(tr); pdm_list.append(pdm); ndm_list.append(ndm)
        def wilder_smooth(data, p):
            s = sum(data[:p])
            res = [s]
            for v in data[p:]:
                s = s - s/p + v
                res.append(s)
            return res
        atr  = wilder_smooth(tr_list,  period)
        pdi  = wilder_smooth(pdm_list, period)
        ndi  = wilder_smooth(ndm_list, period)
        dx_list = []
        for a, p, nd in zip(atr, pdi, ndi):
            if a == 0: dx_list.append(0.0)
            else:
                di_plus  = 100.0 * p / a
                di_minus = 100.0 * nd / a
                denom = di_plus + di_minus
                dx_list.append(100.0 * abs(di_plus - di_minus) / denom if denom > 0 else 0.0)
        if not dx_list: return 0.0
        adx = sum(dx_list[-period:]) / period
        return round(float(adx), 2)
    except Exception:
        return 0.0

def calc_adx_simple(highs: list, lows: list, closes: list, period: int = 14) -> float:
    """Обёртка для обратной совместимости — вызывает реальный ADX."""
    return calc_real_adx(highs, lows, closes, period)

def main():
    log("="*55)
    log("  📡 CORRELATION + REGIME BOT v1.0 ЗАПУЩЕН")
    log(f"  BTC падение >1.5% за 15 мин → блок BUY")
    log(f"  ADX BTC 4H < 20 → режим RANGING")
    log("="*55)
    tg("📡 <b>Correlation Bot запущен</b>\nМониторю BTC/ETH + режим рынка\nРаботаю с spot_bot_v188 ✅")

    prev_state="normal"
    while True:
        try:
            # BTC изменение за 15 мин (3 свечи 5m)
            btc_5m=get_kline("BTCUSDT","5",20)
            btc_1h=get_kline("BTCUSDT","60",20)
            eth_5m=get_kline("ETHUSDT","5",10)

            btc_15m_change=calc_change_pct(btc_5m, bars=3)
            btc_1h_change =calc_change_pct(btc_1h, bars=1)
            eth_15m_change=calc_change_pct(eth_5m, bars=3)

            # ADX BTC 4H для определения режима
            btc_4h=get_kline("BTCUSDT","240",20)
            # [FIX] Извлекаем highs/lows/closes из свечей перед расчётом ADX
            # Bybit kline формат: [time, open, high, low, close, volume, turnover]
            if btc_4h and len(btc_4h) >= 15:
                _highs=[float(c[2]) for c in btc_4h]
                _lows=[float(c[3]) for c in btc_4h]
                _closes=[float(c[4]) for c in btc_4h]
                adx_btc_4h=calc_adx_simple(_highs, _lows, _closes)
            else:
                adx_btc_4h=0.0

            # Определяем состояние рынка
            btc_price=float(btc_5m[0][4]) if btc_5m else 0

            # Режим
            if adx_btc_4h < ADX_RANGING:
                regime="ranging"       # боковик → SCALP
            elif adx_btc_4h > 40:
                regime="trending"      # сильный тренд
            else:
                regime="normal"

            # BUY блок
            buy_blocked=False
            block_reason=""
            if btc_15m_change <= BTC_DROP_THRESH:
                buy_blocked=True
                block_reason=f"BTC -15m: {btc_15m_change:.2f}%"
            elif eth_15m_change <= ETH_DROP_THRESH:
                buy_blocked=True
                block_reason=f"ETH -15m: {eth_15m_change:.2f}%"

            # Общий сентимент рынка
            if btc_1h_change > 2.0:
                market_sentiment="bullish"
            elif btc_1h_change < -2.0:
                market_sentiment="bearish"
            else:
                market_sentiment="neutral"

            state={
                "updated": datetime.datetime.now().isoformat(),
                "updated_ts": time.time(),
                "btc_price": btc_price,
                "btc_15m_change": round(btc_15m_change,3),
                "btc_1h_change":  round(btc_1h_change,3),
                "eth_15m_change": round(eth_15m_change,3),
                "adx_btc_4h":    adx_btc_4h,
                "regime":         regime,
                "market_sentiment": market_sentiment,
                "buy_blocked":    buy_blocked,
                "block_reason":   block_reason,
            }
            # [FIX-aux-ATOMIC] Раньше писали прямо в market_state.json — spot_bot.py читает
            # этот же файл каждые несколько секунд и мог поймать пустую/недописанную середину
            # записи (json.dump не атомарен) → JSONDecodeError на стороне читателя (это и
            # чинили defensively в spot_bot v323/v325 — "Expecting value" в логах). Пишем во
            # временный файл и атомарно подменяем — читатель либо видит старую полную версию,
            # либо новую полную, никогда середину записи.
            _tmp = STATE_FILE + ".tmp"
            with open(_tmp,"w",encoding="utf-8") as f:
                json.dump(state,f,ensure_ascii=False,indent=2)
            os.replace(_tmp, STATE_FILE)

            cur_state="blocked" if buy_blocked else regime
            if cur_state!=prev_state:
                log(f"  ⚡ Состояние рынка: {prev_state} → {cur_state}")
                if buy_blocked:
                    tg_th("corr_block",
                        f"⚠️ <b>CORRELATION BOT: BUY ЗАБЛОКИРОВАН</b>\n"
                        f"Причина: {block_reason}\n"
                        f"BTC 1H: {btc_1h_change:+.2f}% | ETH 15m: {eth_15m_change:+.2f}%\n"
                        f"spot_bot автоматически приостановит новые BUY",600)
                elif prev_state=="blocked":
                    tg_th("corr_unblock",
                        f"✅ <b>CORRELATION BOT: BUY разблокирован</b>\n"
                        f"BTC восстановился. Торговля возобновлена.",600)
                if regime=="ranging" and prev_state!="ranging":
                    tg_th("regime_ranging",
                        f"📊 <b>РЕЖИМ РЫНКА: БОКОВИК</b>\n"
                        f"ADX BTC 4H={adx_btc_4h:.1f} (<20)\n"
                        f"Рекомендую режим SCALP",1800)
                prev_state=cur_state

            log(f"  BTC 15m:{btc_15m_change:+.2f}% 1H:{btc_1h_change:+.2f}% | "
                f"ETH:{eth_15m_change:+.2f}% | ADX={adx_btc_4h:.0f} "
                f"regime={regime} block={buy_blocked}")
        except Exception as e:
            log(f"  ❌ {e}")
        time.sleep(SCAN_INTERVAL)

if __name__=="__main__": main()
