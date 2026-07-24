# ПРОГОН №4 (v343): вес доверия к сигнальным ботам — явно убыточный бот должен реально
# получать и УДЕРЖИВАТЬ вес 0.3 (раньше эта ветка была математически недостижима).
import importlib.util, os, glob, re, sys, tempfile

_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_cands = glob.glob(os.path.join(_repo, 'spot_bot_v*_fixed.py'))
_latest = max(_cands, key=lambda p: int(re.search(r'v(\d+)', os.path.basename(p)).group(1)))
print('Проверяемая версия spot_bot:', os.path.basename(_latest))
spec = importlib.util.spec_from_file_location('spotbot', _latest)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []
tmpdir = tempfile.mkdtemp()
kn = m.BotKnowledge(tmpdir)

# ---------- А: реальный сценарий smc из вашего 4-дневного прогона (76 сделок, WR=38.2%, PF=0.52) ----------
for _ in range(29):
    kn.update_bot_trust("smc", "profit", pnl_pct=24.4368)
for _ in range(47):
    kn.update_bot_trust("smc", "loss", pnl_pct=-47.348)
b = kn._data["bot_trust"]["smc"]
print(f"А: smc после 76 сделок (WR={b['wr']:.1%}, PF={b['profit_factor']:.2f}) → weight={b['weight']}")
if b['weight'] != 0.3:
    fails.append(f"А: явно убыточный бот (WR=38%, PF=0.52) должен получить weight=0.3, получил {b['weight']}")

# ---------- Б: вес держится и на следующей сделке (не соскальзывает обратно на 0.7) ----------
kn.update_bot_trust("smc", "loss", pnl_pct=-40.0)
b2 = kn._data["bot_trust"]["smc"]
print(f"Б: после ещё одного убытка → weight={b2['weight']} (правильно: всё ещё 0.3, не откатился на 0.7)")
if b2['weight'] != 0.3:
    fails.append(f"Б: вес соскользнул обратно, получили {b2['weight']}")

# ---------- В: этот вес реально приводит к игнорированию сигнала (порог <0.5 в коде run()) ----------
_smc_weight = b2['weight']
_ignored = _smc_weight < 0.5
print(f"В: при weight={_smc_weight} сигнал будет проигнорирован (порог <0.5 в scan): {_ignored}")
if not _ignored:
    fails.append("В: вес всё ещё выше порога игнорирования — фикс не даёт реального эффекта")

# ---------- Г: сильно прибыльный бот по-прежнему получает высокий вес (нет регресса) ----------
for _ in range(8):
    kn.update_bot_trust("good_bot", "profit", pnl_pct=2.0)
for _ in range(2):
    kn.update_bot_trust("good_bot", "loss", pnl_pct=-1.0)
bg = kn._data["bot_trust"]["good_bot"]
print(f"Г: сильный бот (WR={bg['wr']:.0%}, PF={bg['profit_factor']:.2f}) → weight={bg['weight']} (правильно: 1.5)")
if bg['weight'] != 1.5:
    fails.append(f"Г: регресс — сильный бот должен получить 1.5, получил {bg['weight']}")

# ---------- Д: посредственный (не явно плохой, не явно хороший) бот держит средний вес 0.7 ----------
for _ in range(5):
    kn.update_bot_trust("mediocre_bot", "profit", pnl_pct=2.0)
for _ in range(6):
    kn.update_bot_trust("mediocre_bot", "loss", pnl_pct=-2.1)
bm = kn._data["bot_trust"]["mediocre_bot"]
print(f"Д: посредственный бот (WR={bm['wr']:.0%}, PF={bm['profit_factor']:.2f}) → weight={bm['weight']} (правильно: 0.7)")
if bm['weight'] != 0.7:
    fails.append(f"Д: посредственный бот должен остаться на 0.7 (не 0.3 и не выше), получил {bm['weight']}")

print()
print("ИТОГ ПРОГОНА №4:", "ВСЕ ПРОВЕРКИ ПРОШЛИ (А,Б,В,Г,Д)" if not fails else "ПРОВАЛЫ: " + "; ".join(fails))
sys.exit(1 if fails else 0)
