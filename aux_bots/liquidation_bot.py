#!/usr/bin/env python3
"""
liquidation_bot.py — v1.0
Мониторинг резких изменений открытого интереса (OI) как косвенного признака
массовых ликвидаций — Bybit не даёт публичного API реальных ликвидаций.
Резкое падение OI (>5% за 5 мин) = вероятны ликвидации ЛОНГОВ = дно рядом.
Резкий рост OI (>5% за 5 мин) = вероятны ликвидации ШОРТОВ = перегрев.

[FIX-aux] Раньше в файле были ещё get_liquidations()/get_insurance_fund() —
не вызывались из main() нигде, реального эффекта не давали, удалены.

Пишет: liquidation_signals.json
"""
import os, json, time, datetime, requests

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
SIGNALS_FILE = os.path.join(BASE_DIR, "liquidation_signals.json")
# [FIX-aux-SEC] Убран зашитый в код токен-фолбэк — см. тот же фикс в correlation_bot.py.
TG_TOKEN   = os.environ.get("TG_TOKEN","")
TG_CHAT    = os.environ.get("TG_CHAT_ID","")

SCAN_INTERVAL = 120        # каждые 2 минуты
LIQ_THRESHOLD_USD = 500_000  # ликвидация >500K USD = значимая
_url = "https://api.bybit.com"
def log(m): print(f"{datetime.datetime.now().strftime('%H:%M:%S')} | {m}")

def tg(msg):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":msg,"parse_mode":"HTML"}, timeout=10)
    except Exception as _tg_e:
        log(f"⚠️ [tg] {_tg_e}")  # [FIX-aux] тег лога был скопирован из другой функции (get_open_interest)

_tg_ts:dict={}
def tg_th(key,msg,c=1800):
    n=time.time()
    if n-_tg_ts.get(key,0)<c: return
    _tg_ts[key]=n; tg(msg)

def get_open_interest(symbol):
    """Открытый интерес — снижение = ликвидации проходили"""
    try:
        r=requests.get(f"{_url}/v5/market/open-interest",
            params={"category":"linear","symbol":symbol,
                    "intervalTime":"5min","limit":3},timeout=10)
        if r.status_code==200:
            lst=r.json().get("result",{}).get("list",[]) or []
            if len(lst)>=2:
                oi_now=float(lst[0].get("openInterest",0) or 0)
                oi_prev=float(lst[1].get("openInterest",0) or 0)
                change_pct=(oi_now-oi_prev)/oi_prev*100 if oi_prev>0 else 0
                return oi_now, round(change_pct,3)
    except Exception as _tg_e: pass  # TG ошибки не критичны
    return 0,0

WATCH_SYMBOLS=["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT"]

def main():
    log("="*55)
    log("  💥 LIQUIDATION BOT v1.0 ЗАПУЩЕН")
    log("  Мониторю OI и ликвидации Bybit")
    log("="*55)
    tg("💥 <b>Liquidation Bot запущен</b>\nМониторю ликвидации Bybit\nРаботаю с spot_bot_v188 ✅")

    prev_oi={}
    while True:
        try:
            signals=[]
            market_signal="neutral"

            for sym in WATCH_SYMBOLS:
                oi,oi_change=get_open_interest(sym)
                if oi_change<-5.0:
                    # OI упал >5% за 5 мин = массовые ликвидации
                    signals.append({
                        "symbol":sym,"oi":oi,"oi_change":oi_change,
                        "type":"long_liquidations",
                        "signal":"buy_opportunity",  # лонги выдавлены = дно
                    })
                    log(f"  💥 {sym}: OI {oi_change:.1f}% (ликвидации лонгов = дно?)")
                    market_signal="long_liquidation_opportunity"
                elif oi_change>5.0:
                    signals.append({
                        "symbol":sym,"oi":oi,"oi_change":oi_change,
                        "type":"short_liquidations",
                        "signal":"caution",
                    })
                    log(f"  ⚠️ {sym}: OI +{oi_change:.1f}% (ликвидации шортов = перегрев?)")
                    market_signal="short_liquidation_caution"
                time.sleep(0.3)

            data={
                "updated": datetime.datetime.now().isoformat(),
                "updated_ts": time.time(),
                "market_signal": market_signal,
                "signals": signals,
                "watch_symbols": WATCH_SYMBOLS,
            }
            # [FIX-aux-ATOMIC] См. тот же фикс в correlation_bot.py.
            _tmp = SIGNALS_FILE + ".tmp"
            with open(_tmp,"w",encoding="utf-8") as f:
                json.dump(data,f,ensure_ascii=False,indent=2)
            os.replace(_tmp, SIGNALS_FILE)
            log(f"  💾 liquidation_signals.json: {len(signals)} сигналов | {market_signal}")

            if market_signal=="long_liquidation_opportunity":
                syms=[s["symbol"] for s in signals if s["type"]=="long_liquidations"]
                tg_th("liq_long",
                    f"💥 <b>LIQUIDATION BOT: Массовые ликвидации лонгов!</b>\n"
                    f"Монеты: {', '.join(syms)}\n"
                    f"Дно рядом → возможна хорошая точка входа",1800)
        except Exception as e:
            log(f"  ❌ {e}")
        time.sleep(SCAN_INTERVAL)

if __name__=="__main__": main()
