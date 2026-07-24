# ПРОГОН №5 (v344): идея владельца — маленький плюс на откате + поездка на крупном движении.
# Берёт РЕАЛЬНЫЕ значения Config новейшего spot_bot и проверяет их на be_lock-сделках
# из истории bot_knowledge (peak записан в exit_reason). Данные ищет в нескольких местах.
import importlib.util, os, glob, re, sys, json

_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_cands = glob.glob(os.path.join(_repo, 'spot_bot_v*_fixed.py'))
_latest = max(_cands, key=lambda p: int(re.search(r'v(\d+)', os.path.basename(p)).group(1)))
print('Проверяемая версия spot_bot:', os.path.basename(_latest))
spec = importlib.util.spec_from_file_location('spotbot', _latest)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
CFG = m.Config()

fails = []
TRIG=float(CFG.BE_LOCK_TRIGGER_PCT); BUF=float(CFG.BE_LOCK_BUFFER_PCT)
SPK=float(CFG.BE_LOCK_STRONG_PEAK_PCT); SMIN=float(CFG.BE_LOCK_STRONG_MIN_NET_PCT)
TRAIL=float(CFG.TRAIL_DISTANCE_PCT); FEE=float(CFG.FEE_RATE_PCT)
print(f"Config: TRIGGER={TRIG} BUFFER={BUF} STRONG_PEAK={SPK} STRONG_MIN_NET={SMIN} TRAIL={TRAIL}")

# Инварианты (эти правила НЕ должны нарушаться при любой будущей настройке)
if not (SPK > TRIG): fails.append("STRONG_PEAK должен быть > TRIGGER")
if not (SMIN <= SPK): fails.append("STRONG_MIN_NET не выше STRONG_PEAK")
if not (BUF > 0): fails.append("BUFFER должен давать реальный плюс (>0)")

# Реальные be_lock-сделки: ищем bot_knowledge_4.json рядом или в scratchpad
_bk = None
for cand in [os.path.join(_repo,'bot_knowledge.json'),
             '/tmp/claude-0/-home-user-footbal-plan-bot/0f91ebcd-5f1c-598b-95f0-79cf48373d06/scratchpad/bk4.json']:
    if os.path.exists(cand):
        _bk = cand; break
if _bk:
    d = json.load(open(_bk))
    rows=[]
    for t in d.get('trades',[]):
        er=t.get('exit_reason','')
        mt=re.search(r'be_lock.*peak=([\d.]+)% now=(-?[\d.]+)%', er)
        if mt:
            pk=float(mt.group(1)); now=float(mt.group(2))
            if pk>=100: continue
            pnl=t.get('pnl_usdt',0); pct=t.get('pnl_pct',0) or 0.001
            vpp=abs(pnl/pct) if pct else 0.073
            rows.append((pk,now,pnl,vpp))
    SLIP=0.05
    def floor_pct(peak): return max(BUF, peak-TRAIL, (SMIN if peak>=SPK else -99))
    tot_now=tot_new=0.0; win_now=win_new=0
    for pk,now,usdt,vpp in rows:
        new=now if pk<TRIG else max(floor_pct(pk)-SLIP, now)
        nu=new*vpp; tot_now+=usdt; tot_new+=nu
        win_now+= usdt>0; win_new+= nu>0
    print(f"На {len(rows)} реальных be_lock-сделках: сумма {tot_now:+.3f}$ → {tot_new:+.3f}$ | плюсовых {win_now}→{win_new}")
    if not (tot_new > tot_now): fails.append(f"v344 не улучшает сумму ({tot_now:.3f}→{tot_new:.3f})")
    if not (win_new >= win_now): fails.append("v344 не увеличивает долю плюсовых")
else:
    print("(bot_knowledge не найден — проверяю только инварианты)")

print()
print("ИТОГ ПРОГОНА №5:", "ВСЕ ПРОВЕРКИ ПРОШЛИ" if not fails else "ПРОВАЛЫ: " + "; ".join(fails))
sys.exit(1 if fails else 0)
