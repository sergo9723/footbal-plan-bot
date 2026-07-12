#!/usr/bin/env python3
"""
funding_bot.py — v1.0
Фандинг с Bybit фьючерсов (бесплатно через API).
Высокий фандинг (>0.05%) → рынок перегрет лонгами → не входим.
Отрицательный фандинг (<-0.02%) → шорты перегреты → хорошая точка входа для лонга.

Пишет: funding_signals.json
"""
import os, json, time, datetime, requests

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
SIGNALS_FILE = os.path.join(BASE_DIR, "funding_signals.json")
# [FIX-aux-SEC] Убран зашитый в код токен-фолбэк — см. тот же фикс в correlation_bot.py.
TG_TOKEN   = os.environ.get("TG_TOKEN","")
TG_CHAT    = os.environ.get("TG_CHAT_ID","")

SCAN_INTERVAL   = 300   # каждые 5 минут
HIGH_FUNDING    = 0.05  # % → перегрет лонгами → не входим
EXTREME_FUNDING = 0.10  # % → очень перегрет
NEG_FUNDING     = -0.02 # % → шорты перегреты → хорошая точка входа
TOP_SYMBOLS = [
    "BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","BNBUSDT",
    "ADAUSDT","DOGEUSDT","AVAXUSDT","DOTUSDT","LINKUSDT",
    "TONUSDT","RENDERUSDT","FETUSDT","NEARUSDT","XLMUSDT",
    "POLUSDT","FILUSDT","HBARUSDT","INJUSDT","ARBUSDT",
]
_url = "https://api.bybit.com"
def log(m): print(f"{datetime.datetime.now().strftime('%H:%M:%S')} | {m}")

def tg(msg):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":msg,"parse_mode":"HTML"}, timeout=10)
    except Exception as _tg_e: pass  # TG ошибки не критичны

_tg_ts:dict={}
def tg_th(key,msg,c=3600):
    n=time.time()
    if n-_tg_ts.get(key,0)<c: return
    _tg_ts[key]=n; tg(msg)

def get_funding_rate(symbol):
    """[v219-FIX] Bybit: funding rate через public API с nextFundingTime фильтром."""
    try:
        import time as _t_fr
        sym_fut = symbol
        r = requests.get(f"{_url}/v5/market/tickers",
            params={"category":"linear","symbol":sym_fut}, timeout=10)
        if r.status_code == 200:
            lst = r.json().get("result",{}).get("list",[]) or []
            if lst:
                item = lst[0]
                fr   = float(item.get("fundingRate",0) or 0) * 100
                nft  = int(item.get("nextFundingTime",0) or 0)  # ms
                # [v219-FIX] Фильтр устаревших данных (старше 2 часов)
                if nft > 0 and nft < int(_t_fr.time() * 1000) - 7_200_000:
                    log(f"⚠️ [funding] {symbol}: nextFundingTime устарел ({nft})")
                    return None
                return round(fr, 4)
    except Exception as _fund_ex:
        log(f"⚠️ [get_funding_rate] {symbol}: {_fund_ex}")  # [v219-FIX] pass  # TG ошибки не критичны
    return None

def main():
    log("="*55)
    log("  💰 FUNDING BOT v1.0 ЗАПУЩЕН")
    log(f"  Высокий фандинг >{HIGH_FUNDING}% → не входим")
    log(f"  Отриц. фандинг <{NEG_FUNDING}% → хорошая точка входа")
    log("="*55)
    tg("💰 <b>Funding Bot запущен</b>\nМониторю фандинг Bybit фьючерсов\nРаботаю с spot_bot_v188 ✅")

    while True:
        try:
            overheated=[]   # перегретые лонги
            good_entry=[]   # хорошие точки входа
            normal=[]

            for sym in TOP_SYMBOLS:
                fr=get_funding_rate(sym)
                if fr is None: continue
                if fr>=HIGH_FUNDING:
                    overheated.append({"symbol":sym,"funding":fr})
                    log(f"  🔥 {sym}: фандинг {fr:.4f}% (перегрет!)")
                elif fr<=NEG_FUNDING:
                    good_entry.append({"symbol":sym,"funding":fr})
                    log(f"  ✅ {sym}: фандинг {fr:.4f}% (шорты перегреты → хорошая точка)")
                else:
                    normal.append({"symbol":sym,"funding":fr})
                time.sleep(0.2)

            # Глобальный сигнал
            overheated_syms=[x["symbol"] for x in overheated]
            good_syms=[x["symbol"] for x in good_entry]

            # BTC фандинг определяет общее настроение
            btc_fr=next((x["funding"] for x in overheated+normal+good_entry if x["symbol"]=="BTCUSDT"),0)
            if btc_fr>=EXTREME_FUNDING:
                global_signal="avoid"
            elif btc_fr>=HIGH_FUNDING:
                global_signal="caution"
            elif btc_fr<=NEG_FUNDING:
                global_signal="buy_opportunity"
            else:
                global_signal="normal"

            data={
                "updated": datetime.datetime.now().isoformat(),
                "updated_ts": time.time(),
                "btc_funding": btc_fr,
                "global_signal": global_signal,
                "overheated_symbols": overheated_syms,  # не входить
                "good_entry_symbols": good_syms,         # хорошая точка
                "overheated_details": overheated[:10],
                "good_entry_details": good_entry[:10],
            }
            # [FIX-aux-ATOMIC] См. тот же фикс в correlation_bot.py — пишем атомарно, чтобы
            # spot_bot.py не поймал недописанный файл на середине записи.
            _tmp = SIGNALS_FILE + ".tmp"
            with open(_tmp,"w",encoding="utf-8") as f:
                json.dump(data,f,ensure_ascii=False,indent=2)
            os.replace(_tmp, SIGNALS_FILE)
            log(f"  💾 funding_signals.json: BTC={btc_fr:.4f}% signal={global_signal}")

            if overheated:
                tg_th("fund_overheat",
                    f"🔥 <b>FUNDING BOT: Рынок перегрет!</b>\n"
                    f"BTC фандинг: {btc_fr:.4f}%\n"
                    f"Перегретые: {', '.join(overheated_syms[:5])}\n"
                    f"spot_bot: осторожнее с входом",3600)
            if good_entry:
                tg_th("fund_good",
                    f"✅ <b>FUNDING BOT: Хорошая точка входа</b>\n"
                    f"Отрицательный фандинг: {', '.join(good_syms[:5])}\n"
                    f"Шорты перегреты → вероятен отскок",3600)
        except Exception as e:
            log(f"  ❌ {e}")
        time.sleep(SCAN_INTERVAL)

if __name__=="__main__": main()
