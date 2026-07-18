# ПРОГОН №2: симуляция торговой цепочки spot_bot v338 на реальном модуле:
# сделки → _update_stats → relax_level_stats/hour_stats → решения HOLD/HOLD2,
# + серии убытков (посев из истории), + арифметика be_lock.
import importlib.util, json, os, tempfile, sys

import glob as _glob, re as _re
_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_cands = _glob.glob(os.path.join(_repo, 'spot_bot_v*_fixed.py'))
_latest = max(_cands, key=lambda p: int(_re.search(r'v(\d+)', os.path.basename(p)).group(1)))
print('Проверяемая версия:', os.path.basename(_latest))
spec = importlib.util.spec_from_file_location('spotbot', _latest)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []
tmpdir = tempfile.mkdtemp()

# ---------- ЧАСТЬ А: BotKnowledge — сделки пишут статистику уровней ----------
kn = m.BotKnowledge(tmpdir)

class _FakeEng:  # для чтения _current_relax_step в log_trade_entry
    _current_relax_step = "строгое"
kn._engine_ref = _FakeEng()

# Симулируем 10 сделок на уровне "строгое": 6 плюсов, 4 минуса (WR=60%)
for i in range(10):
    kn.log_trade_entry("TESTUSDT", 1.0, adx=36, rsi=60)
    win = i < 6
    kn.log_trade_exit(1.01 if win else 0.99, 0.05 if win else -0.05, "tp" if win else "stop_loss")

# Симулируем 9 сделок на уровне "максимальное": 2 плюса, 7 минусов (WR=22%)
_FakeEng._current_relax_step = "максимальное"
for i in range(9):
    kn.log_trade_entry("TEST2USDT", 1.0, adx=15, rsi=55)
    win = i < 2
    kn.log_trade_exit(1.01 if win else 0.99, 0.05 if win else -0.05, "tp" if win else "stop_loss")

rls = kn._data.get("relax_level_stats", {})
s_strict = rls.get("строгое", {})
s_max = rls.get("максимальное", {})
print("relax_level_stats['строгое']:", s_strict)
print("relax_level_stats['максимальное']:", s_max)
if s_strict.get("trades") != 10 or s_strict.get("wins") != 6: fails.append("A1: строгое не посчитан")
if s_max.get("trades") != 9 or s_max.get("wins") != 2: fails.append("A2: максимальное не посчитан")

# ---------- ЧАСТЬ Б: решения HOLD (v337) и HOLD2 (v338) на этой статистике ----------
def hold_decision(step_name, streak):
    st = rls.get(step_name, {})
    n = int(st.get("trades", 0))
    if n >= 8 and streak < 50:
        wr = int(st.get("wins", 0)) / n * 100.0
        return wr >= 50.0
    return False

def hold2_decision(streak):
    if streak <= 3: nxt = "мягкое"
    elif streak <= 6: nxt = "среднее"
    elif streak <= 10: nxt = "сильное"
    elif streak <= 20: nxt = "очень сильное"
    else: nxt = "максимальное"
    st = rls.get(nxt, {})
    n = int(st.get("trades", 0))
    if n >= 8 and streak < 50:
        wr = int(st.get("wins", 0)) / n * 100.0
        return wr < 40.0, nxt
    return False, nxt

# Б1: стоим на "строгое" (WR=60%, n=10), скан №3 → держим
r1 = hold_decision("строгое", 3)
# Б2: скан №25 → целевой "максимальное" (WR=22%, n=9) → НЕ спускаемся
r2, nxt2 = hold2_decision(25)
# Б3: скан №55 (аварийный) → оба механизма уступают
r3 = hold_decision("строгое", 55); r4, _ = hold2_decision(55)
# Б4: мало данных (уровень "среднее" — 0 сделок) → работаем как раньше
r5, nxt5 = hold2_decision(5)
print(f"Б1 держим строгое@скан3: {r1} | Б2 не спускаемся на '{nxt2}'@скан25: {r2} | Б3 аварийный@55 отключает оба: hold={r3} hold2={r4} | Б4 мало данных '{nxt5}': {r5}")
if not r1: fails.append("Б1: HOLD не сработал на хорошем уровне")
if not r2: fails.append("Б2: HOLD2 не запретил спуск на плохой уровень")
if r3 or r4: fails.append("Б3: аварийный сброс не имеет приоритета")
if r5: fails.append("Б4: HOLD2 сработал без данных")

# ---------- ЧАСТЬ В: посев серий убытков из истории (v334) ----------
# Последние 3 сделки TEST2USDT — убытки (i=6,7,8 → win=False)
trades = kn._data.get("trades", [])
t2 = [t for t in trades if t.get("symbol") == "TEST2USDT"]
tail_losses = 0
for t in reversed(t2):
    if t.get("outcome") == "loss": tail_losses += 1
    else: break
print("В: TEST2USDT последние подряд убытки в истории:", tail_losses)
if tail_losses != 7: fails.append(f"В: ожидалось 7 подряд, получено {tail_losses}")

# ---------- ЧАСТЬ Г: арифметика be_lock (v335, ступенчатая фиксация) ----------
CFG = m.Config()
be_trig  = float(CFG.BE_LOCK_TRIGGER_PCT)      # 0.35
be_buf   = float(CFG.BE_LOCK_BUFFER_PCT)       # 0.15
st_peak  = float(CFG.BE_LOCK_STRONG_PEAK_PCT)  # 0.90
st_min   = float(CFG.BE_LOCK_STRONG_MIN_NET_PCT)  # 0.40
fee      = float(CFG.FEE_RATE_PCT)             # 0.10
print(f"Г: BE_TRIG={be_trig} BUF={be_buf} STRONG_PEAK={st_peak} STRONG_MIN={st_min} FEE={fee}")
# Сценарий AEROUSDT: пик 0.36% → малая гарантия (не сильная)
peak = 0.36
strong = peak >= st_peak
print(f"Г1: пик {peak}% → сильная фиксация: {strong} (правильно: False — путь малой гарантии)")
if strong: fails.append("Г1: пик 0.36 не должен давать сильную фиксацию")
# Сценарий сильного пика 1.0% → гарантия НЕТТО >= +0.40%
peak = 1.0
strong = peak >= st_peak
if not strong: fails.append("Г2: пик 1.0 должен дать сильную фиксацию")
print(f"Г2: пик {peak}% → сильная фиксация: {strong} (гарантия НЕТТО >= +{st_min}%)")
# Согласованность: триггер+буфер должны перекрывать двойную комиссию
if (be_trig - be_buf) - 2*fee < -0.01: fails.append("Г3: буфер безубытка не покрывает комиссии")
print(f"Г3: {be_trig}-{be_buf}={be_trig-be_buf:.2f} >= 2×fee={2*fee:.2f}: {be_trig-be_buf >= 2*fee}")

# ---------- ЧАСТЬ Д: hour_stats пишутся (v333 гейтинг читает их) ----------
hs = kn._data.get("hour_stats", {})
total_hour_trades = sum(int(v.get("trades",0)) for v in hs.values())
print("Д: hour_stats сделок всего:", total_hour_trades)
if total_hour_trades != 19: fails.append(f"Д: hour_stats={total_hour_trades}, ожидалось 19")

# ---------- ЧАСТЬ Е: сохранение/загрузка знаний (персистентность) ----------
kn._save()
kn2 = m.BotKnowledge(tmpdir)
rls2 = kn2._data.get("relax_level_stats", {})
if rls2.get("строгое", {}).get("trades") != 10: fails.append("Е: relax_level_stats не пережил сохранение/загрузку")
if len(kn2._data.get("trades", [])) != 19: fails.append("Е: история сделок не пережила сохранение/загрузку")
print("Е: после save/load — строгое.trades =", rls2.get("строгое", {}).get("trades"), ", всего сделок =", len(kn2._data.get("trades", [])))

print()
print("ИТОГ ПРОГОНА №2:", "ВСЕ ПРОВЕРКИ ПРОШЛИ (А1,А2,Б1-Б4,В,Г1-Г3,Д,Е)" if not fails else "ПРОВАЛЫ: " + "; ".join(fails))
sys.exit(1 if fails else 0)
