#!/usr/bin/env python3
"""
volume_bot.py — v1.0
Анализ аномального объёма на Bybit Spot.
Монеты с объёмом >3× среднего за 24 свечи = сильный сигнал входа.
FVG (от smc_bot) + аномальный объём = двойное подтверждение.

Пишет: volume_signals.json
Читают: spot_bot_v188, smc_bot
"""
import os, json, time, datetime, requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNALS_FILE = os.path.join(BASE_DIR, "volume_signals.json")
# [FIX-aux-SEC] Убран зашитый в код токен-фолбэк — см. тот же фикс в correlation_bot.py.
TG_TOKEN  = os.environ.get("TG_TOKEN","")
TG_CHAT   = os.environ.get("TG_CHAT_ID","")

SCAN_INTERVAL = 180        # каждые 3 минуты
VOLUME_MULT   = 3.0        # объём > 3× среднего = аномалия
TOP_N         = 60         # анализируем топ-60 монет по объёму
CANDLES_BACK  = 24         # среднее за 24 свечи (1H)

_client_url = "https://api.bybit.com"

def log(m): print(f"{datetime.datetime.now().strftime('%H:%M:%S')} | {m}")

def tg(msg):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":msg,"parse_mode":"HTML"}, timeout=10)
    except Exception as _tg_e: pass  # TG ошибки не критичны

_tg_ts: dict = {}
def tg_throttled(key, msg, cooldown=1800):
    now=time.time()
    if now-_tg_ts.get(key,0)<cooldown: return
    _tg_ts[key]=now; tg(msg)

def bybit_get(endpoint, **params):
    try:
        r = requests.get(f"{_client_url}{endpoint}", params=params, timeout=15)
        if r.status_code==200: return r.json().get("result",{})
    except Exception as e: log(f"⚠️ API: {e}")
    return {}

def get_top_symbols():
    r = bybit_get("/v5/market/tickers", category="spot")
    tickers = r.get("list",[]) or []
    # Фильтруем: только USDT, оборот>5M, не stable
    STABLE={"USDCUSDT","BUSDUSDT","DAIUSDT","TUSDUSDT","USDTUSDT"}
    result=[]
    for t in tickers:
        sym=t.get("symbol","")
        if not sym.endswith("USDT"): continue
        if sym in STABLE: continue
        try:
            turn=float(t.get("turnover24h",0) or 0)
            if turn<5_000_000: continue
            result.append((sym,turn))
        except Exception: continue
    result.sort(key=lambda x:-x[1])
    return [s for s,_ in result[:TOP_N]]

def analyze_volume(symbol):
    """Возвращает (is_anomaly, ratio, avg_vol, cur_vol)"""
    try:
        r = bybit_get("/v5/market/kline", category="spot",
                      symbol=symbol, interval="60", limit=CANDLES_BACK+1)
        candles = r.get("list",[]) or []
        if len(candles)<CANDLES_BACK+1: return False,0,0,0
        # Bybit: [ts,open,high,low,close,volume,turnover]
        vols=[float(c[5]) for c in candles[1:]]  # исключаем текущую свечу
        cur_vol=float(candles[0][5])
        avg_vol=sum(vols)/len(vols)
        ratio=cur_vol/avg_vol if avg_vol>0 else 0
        return ratio>=VOLUME_MULT, round(ratio,2), round(avg_vol,0), round(cur_vol,0)
    except Exception as e:
        log(f"⚠️ [{symbol}] volume: {e}")
        return False,0,0,0

def main():
    log("="*55)
    log("  📊 VOLUME BOT v1.0 ЗАПУЩЕН")
    log(f"  Порог аномалии: >{VOLUME_MULT}× среднего объёма")
    log(f"  Сканирую топ {TOP_N} монет каждые {SCAN_INTERVAL}с")
    log("="*55)
    tg("📊 <b>Volume Bot запущен</b>\nИщу аномальный объём (>3× среднего)\nРаботаю с spot_bot_v188 ✅")

    scan_num=0
    while True:
        scan_num+=1
        log(f"\n{'─'*50}")
        log(f"📡 СКАН #{scan_num} | {datetime.datetime.now().strftime('%H:%M:%S')}")
        anomalies=[]; buy_ctx_map={}
        try:
            symbols=get_top_symbols()
            log(f"  Монет для анализа: {len(symbols)}")
            for sym in symbols:
                is_anom,ratio,avg,cur=analyze_volume(sym)
                if is_anom:
                    # [v219-FIX] Направление объёма через taker
                    _buy_pct=50.0; _vol_ctx="neutral"
                    try:
                        _tr=requests.get(f"{_client_url}/v5/market/recent-trade",
                            params={"category":"spot","symbol":sym,"limit":"50"},timeout=5)
                        if _tr.status_code==200:
                            _trd=_tr.json().get("result",{}).get("list",[])
                            bv=sum(float(t.get("size",0)) for t in _trd if str(t.get("side","")).lower()=="buy")
                            sv=sum(float(t.get("size",0)) for t in _trd if str(t.get("side","")).lower()!="buy")
                            tot=bv+sv
                            if tot>0: _buy_pct=round(bv/tot*100,1)
                        _vol_ctx=("bullish" if _buy_pct>60 else "bearish" if _buy_pct<40 else "neutral")
                    except Exception: pass
                    anomalies.append({
                        "symbol":sym,"ratio":ratio,
                        "avg_vol":avg,"cur_vol":cur,
                        "ts":time.time(),
                        "valid_until":time.time()+3600,
                        "buy_pct":_buy_pct,     # [v219] % покупателей
                        "volume_ctx":_vol_ctx,  # bullish/bearish/neutral
                    })
                    log(f"  🔥 {sym}: объём {ratio:.1f}× среднего ctx={_vol_ctx}({_buy_pct:.0f}%buy)")
                time.sleep(0.3)

            data={
                "updated":datetime.datetime.now().isoformat(),
                "updated_ts":time.time(),
                "anomalies":anomalies,
                "scan_num":scan_num,
            }
            # [FIX-aux-ATOMIC] См. тот же фикс в correlation_bot.py.
            _tmp = SIGNALS_FILE + ".tmp"
            with open(_tmp,"w",encoding="utf-8") as f:
                json.dump(data,f,ensure_ascii=False,indent=2)
            os.replace(_tmp, SIGNALS_FILE)
            log(f"  💾 volume_signals.json: {len(anomalies)} аномалий")

            if anomalies:
                top3=sorted(anomalies,key=lambda x:-x["ratio"])[:3]
                msg=("📊 <b>АНОМАЛЬНЫЙ ОБЪЁМ</b>\n"
                     +"\n".join(f"🔥 {a['symbol']}: {a['ratio']:.1f}× среднего" for a in top3))
                tg_throttled("vol_anomaly",msg,cooldown=900)
        except Exception as e:
            log(f"  ❌ Ошибка: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__=="__main__": main()
