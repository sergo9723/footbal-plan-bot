# ПРОГОН №3 (v342): двойной счёт побед (ai_explain_win) + стирание bad_hours (apply_learning)
import importlib.util, os, glob, re, sys, tempfile, threading, time

_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_cands = glob.glob(os.path.join(_repo, 'spot_bot_v*_fixed.py'))
_latest = max(_cands, key=lambda p: int(re.search(r'v(\d+)', os.path.basename(p)).group(1)))
print('Проверяемая версия spot_bot:', os.path.basename(_latest))
spec = importlib.util.spec_from_file_location('spotbot', _latest)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

sys.path.insert(0, _repo)
import ai_brain_advisor_v4 as adv

fails = []
tmpdir = tempfile.mkdtemp()

# ---------- ЧАСТЬ А: ai_explain_win больше НЕ дублирует счёт побед ----------
kn = m.BotKnowledge(tmpdir)
class FakeTG:
    enabled = False
    def send(self, *a, **k): pass
    def send_throttled(self, *a, **k): pass
class FakeCfg:
    AI_BRAIN_ENABLE = True
    ANTHROPIC_KEY = 'sk-ant-test'
brain = adv.AIBrainAdvisor(kn, FakeCfg(), FakeTG(), tmpdir)

class _FakeEng:
    _current_relax_step = "строгое"
kn._engine_ref = _FakeEng()

# ровно сценарий XANUSDT: 1 сделка, TP_fill (профит) -> log_trade_exit + ai_explain_win, как в run()
kn.log_trade_entry("XANUSDT", 1.0, adx=40, rsi=55)
kn.log_trade_exit(1.02, 0.05, "TP_fill")
brain.ai_explain_win({'symbol': 'XANUSDT', 'pnl_usdt': 0.05, 'adx': 40, 'rsi': 55, 'hour': 10, 'mode': 'SMART'})

ss = kn._data.get('symbol_stats', {}).get('XANUSDT', {})
print(f"А: XANUSDT после 1 победы (TP_fill) — symbol_stats.wins = {ss.get('wins')} (правильно: 1)")
if ss.get('wins') != 1:
    fails.append(f"А: двойной счёт всё ещё есть, wins={ss.get('wins')}")

# ---------- ЧАСТЬ Б: apply_learning() сохраняет часы от ИИ вместо перезаписи ----------
kn2 = m.BotKnowledge(tmpdir + "_b")
brain2 = adv.AIBrainAdvisor(kn2, FakeCfg(), FakeTG(), tmpdir + "_b")
kn2._engine_ref = _FakeEng()

# ИИ добавляет час 12 после убытка (как в сценарии SPXUSDT из реального лога)
brain2.knowledge._data['bad_hours'] = [0]
_ai_ts = brain2.knowledge._data.setdefault('_ai_bad_hours_ts', {})
_ai_ts['12'] = time.time()  # только что добавлено ИИ, должно пережить 24ч

# Симулируем apply_learning: заполняем hour_stats так, чтобы час 12 НЕ набрал порог 5 сделок
# (ровно ситуация из реального лога — короткая выборка), а час 0 набрал и подтверждён плохим
for i in range(6):
    kn2._data.setdefault('hour_stats', {}).setdefault('0', {"trades":0,"wins":0,"losses":0,"pnl":0.0})
    s = kn2._data['hour_stats']['0']
    s['trades'] += 1
    if i < 1: s['wins'] += 1
    else: s['losses'] += 1
kn2._data['total_scans'] = 100  # триггер apply_learning раз в 100 сканов

kn2._data.setdefault('trades', []).append({'symbol':'X','outcome':'loss'})

kn2.apply_learning(min_trades=1)
bad_after = kn2._data.get('bad_hours', [])
print(f"Б: bad_hours после apply_learning() = {sorted(bad_after)} (правильно: содержит и 0, и 12)")
if 0 not in bad_after:
    fails.append("Б1: час 0 (подтверждённый деткой статистикой) пропал")
if 12 not in bad_after:
    fails.append("Б2: час 12 от ИИ стёрт apply_learning() — старый баг воспроизведён")

# ---------- ЧАСТЬ В: просроченная метка ИИ (>24ч) больше не защищает час ----------
kn3 = m.BotKnowledge(tmpdir + "_c")
kn3._engine_ref = _FakeEng()
kn3._data['bad_hours'] = [0]
kn3._data.setdefault('_ai_bad_hours_ts', {})['5'] = time.time() - 25*3600  # 25ч назад — истекло
for i in range(6):
    kn3._data.setdefault('hour_stats', {}).setdefault('0', {"trades":0,"wins":0,"losses":0,"pnl":0.0})
    s = kn3._data['hour_stats']['0']
    s['trades'] += 1
    if i < 1: s['wins'] += 1
    else: s['losses'] += 1
kn3._data['total_scans'] = 100
kn3._data.setdefault('trades', []).append({'symbol':'X','outcome':'loss'})

kn3.apply_learning(min_trades=1)
bad_after3 = kn3._data.get('bad_hours', [])
print(f"В: истёкшая метка (25ч) для часа 5 — bad_hours = {sorted(bad_after3)} (правильно: 5 отсутствует)")
if 5 in bad_after3:
    fails.append("В: истёкшая (>24ч) метка ИИ всё ещё защищает час — должна была истечь")

print()
print("ИТОГ ПРОГОНА №3:", "ВСЕ ПРОВЕРКИ ПРОШЛИ (А,Б1,Б2,В)" if not fails else "ПРОВАЛЫ: " + "; ".join(fails))
sys.exit(1 if fails else 0)
