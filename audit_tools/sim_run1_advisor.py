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

class FakeKnowledge:
    _data = {}
    def save(self): pass

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
print()
print("ИТОГ ПРОГОНА №1:", "ВСЕ 9 ПРОВЕРОК ПРОШЛИ" if not fails else "ПРОВАЛЫ: " + ", ".join(fails))
