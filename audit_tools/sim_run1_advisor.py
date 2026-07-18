# ПРОГОН №1: симуляция ИИ-модуля — все пути _call_api с имитацией ответов API
import sys, json, types
import os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_brain_advisor_v4 as adv

class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)
    def json(self):
        return self._payload

captured_bodies = []
def make_post(payload, status=200):
    def _post(url, headers=None, json=None, timeout=None):
        captured_bodies.append(json)
        return FakeResp(status, payload)
    return _post

class FakeCfg:
    AI_BRAIN_ENABLE = True
    ANTHROPIC_KEY = 'sk-ant-test-key-123'
    AI_LOSS_ANALYSIS_RESERVED_CALLS = 15

class FakeTG:
    enabled = False
    def send(self, *a, **k): pass
    def send_throttled(self, *a, **k): pass

import threading as _th
class FakeKnowledge:
    _data = {}
    _lock = _th.RLock()
    def save(self): pass
    def _save(self): pass

brain = adv.AIBrainAdvisor(FakeKnowledge(), FakeCfg(), FakeTG(), '/tmp')
print("init: полный __init__ OK")

results = {}

# Сценарий 1: СТАРЫЙ БАГ — ответ содержит ТОЛЬКО thinking-блок (то, что было с AEROUSDT)
adv.requests.post = make_post({'content': [{'type': 'thinking', 'thinking': 'думаю...'}]})
r = brain._call_api("тест", max_tokens=600, purpose="loss_analysis")
results['only_thinking_block'] = (r, brain._last_error)

# Сценарий 2: нормальный ответ — один text-блок
adv.requests.post = make_post({'content': [{'type': 'text', 'text': '{"ok": 1}'}]})
r = brain._call_api("тест", max_tokens=600, purpose="loss_analysis")
results['normal_text'] = (r, None)

# Сценарий 3: thinking + text вперемешку (адаптивное мышление вернуло оба)
adv.requests.post = make_post({'content': [{'type': 'thinking', 'thinking': 'x'}, {'type': 'text', 'text': 'часть1 '}, {'type': 'text', 'text': 'часть2'}]})
r = brain._call_api("тест", max_tokens=600, purpose="loss_analysis")
results['mixed_blocks'] = (r, None)

# Сценарий 4: HTTP 429 (rate limit)
adv.requests.post = make_post({'error': {'type': 'rate_limit_error'}}, status=429)
r = brain._call_api("тест", max_tokens=600, purpose="loss_analysis")
results['http_429'] = (r, brain._last_error)

# Сценарий 5: HTTP 200, но body с error
adv.requests.post = make_post({'error': {'type': 'invalid_request_error', 'message': 'bad'}})
r = brain._call_api("тест", max_tokens=600, purpose="loss_analysis")
results['body_error'] = (r, brain._last_error)

# Сценарий 6: РЕЗЕРВ — сканер (без purpose) при исчерпанном бюджете до резерва
brain._api_calls_today = brain.MAX_CALLS_PER_DAY - 15  # осталось ровно резерв
adv.requests.post = make_post({'content': [{'type': 'text', 'text': 'ok'}]})
r_scan = brain._call_api("тест", max_tokens=200)  # purpose по умолчанию → сканер
r_loss = brain._call_api("тест", max_tokens=600, purpose="loss_analysis")  # анализ убытка — должен пройти
results['reserve_scan_blocked'] = (r_scan, brain._last_error if r_scan is None else None)
results['reserve_loss_allowed'] = (r_loss, None)

# Сценарий 7 (FIX-v339-RSI-GUARD): полный путь _apply_loss_analysis —
# ИИ ужесточает RSI → должны выставиться пол RSI_MIN и потолок RSI_MAX на 24ч
brain._api_calls_today = 0
_ai_json = '{"diagnosis": "тест", "confidence": 80, "changes": {"SCANNER_ADX_MIN": 33, "SCANNER_RSI_MIN": 44, "SCANNER_RSI_MAX": 65}}'
adv.requests.post = make_post({'content': [{'type': 'text', 'text': _ai_json}]})
brain.cfg.SCANNER_RSI_MIN = 40.0
brain.cfg.SCANNER_RSI_MAX = 72.0
brain.cfg.SCANNER_ADX_MIN = 30.0
brain._apply_loss_analysis("тестовый промпт", {"symbol": "MEGAUSDT", "pnl_usdt": -0.12, "exit_reason": "full_grid_early_stop"})
_guard_ok = (getattr(brain.cfg, '_ai_rsi_min_floor', 0) == 44.0
             and getattr(brain.cfg, '_ai_rsi_max_ceil', 0) == 65.0
             and getattr(brain.cfg, '_ai_adx_floor', 0) == 33.0
             and getattr(brain.cfg, '_ai_rsi_min_floor_ts', 0) > 0
             and getattr(brain.cfg, '_ai_rsi_max_ceil_ts', 0) > 0)
print("Сценарий 7 (RSI-защита после ИИ-ужесточения):",
      f"floor={getattr(brain.cfg,'_ai_rsi_min_floor',None)} ceil={getattr(brain.cfg,'_ai_rsi_max_ceil',None)} adx_floor={getattr(brain.cfg,'_ai_adx_floor',None)} → {'OK' if _guard_ok else 'ПРОВАЛ'}")

# ПРОВЕРКА ТЕЛА ЗАПРОСА: thinking выключен во всех отправленных запросах
thinking_ok = all(b.get('thinking') == {'type': 'disabled'} for b in captured_bodies)
model_ok = all(b.get('model') == 'claude-sonnet-5' for b in captured_bodies)

print()
print("Сценарий 1 (только thinking-блок):", "None +", repr(results['only_thinking_block'][1])[:80] if results['only_thinking_block'][0] is None else "ОШИБКА: вернул " + repr(results['only_thinking_block'][0]))
print("Сценарий 2 (нормальный text):     ", repr(results['normal_text'][0]))
print("Сценарий 3 (thinking+2×text):     ", repr(results['mixed_blocks'][0]))
print("Сценарий 4 (HTTP 429):            ", results['http_429'][0], "| ошибка:", str(results['http_429'][1])[:60])
print("Сценарий 5 (error в body):        ", results['body_error'][0], "| ошибка:", str(results['body_error'][1])[:60])
print("Сценарий 6 (резерв: сканер):      ", results['reserve_scan_blocked'][0], "| причина:", str(results['reserve_scan_blocked'][1])[:70])
print("Сценарий 6 (резерв: анализ убытка):", repr(results['reserve_loss_allowed'][0]))
print()
print("thinking={'type':'disabled'} во ВСЕХ запросах:", thinking_ok)
print("model=claude-sonnet-5 во всех запросах:", model_ok)
print("Всего отправлено запросов:", len(captured_bodies))

fails = []
if results['only_thinking_block'][0] is not None: fails.append("sc1")
if results['normal_text'][0] != '{"ok": 1}': fails.append("sc2")
if results['mixed_blocks'][0] != 'часть1 \nчасть2': fails.append("sc3: " + repr(results['mixed_blocks'][0]))
if results['http_429'][0] is not None: fails.append("sc4")
if results['body_error'][0] is not None: fails.append("sc5")
if results['reserve_scan_blocked'][0] is not None: fails.append("sc6-scan")
if results['reserve_loss_allowed'][0] != 'ok': fails.append("sc6-loss")
if not thinking_ok: fails.append("thinking")
if not model_ok: fails.append("model")
if not _guard_ok: fails.append("sc7-rsi-guard")
print()
print("ИТОГ ПРОГОНА №1:", "ВСЕ 10 ПРОВЕРОК ПРОШЛИ" if not fails else "ПРОВАЛЫ: " + ", ".join(fails))
