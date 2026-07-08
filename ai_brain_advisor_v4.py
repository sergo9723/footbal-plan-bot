# -*- coding: utf-8 -*-
"""
ai_brain_advisor_v4.py — ВСЕ ИСПРАВЛЕНИЯ AI СОВЕТНИКА (v288)

  [FIX-C3] ai_score_symbol: добавлен _score_cache TTL=600 сек
            MAX_CALLS_PER_DAY: 30→50
  [FIX-H3] is_news_blocked: кэш 60 сек (было: чтение файла каждые 2 сек = 1800 чтений/час)
  [FIX-H4] ai_analyze_entry: _entry_cache_ttl: 300→120 сек (монета меняется за 2-3 мин)

  Использование: from ai_brain_advisor_v4 import AIBrainAdvisor
  В spot_bot_v288.py: from ai_brain_advisor_v4 import AIBrainAdvisor

ai_brain_advisor.py — ИИ-советник для BotKnowledge
Версия: v4.0 (на базе v3.0)

Добавляет 3 функции в существующий мозг бота:
  1. ai_explain_loss()      — ИИ объясняет каждый убыток И применяет изменения
  2. ai_analyze_entry()     — ИИ фильтр перед входом в сделку
  3. ai_nightly_report()    — Ночной ИИ-отчёт с автокорректировкой параметров

УСТАНОВКА:
  1. В Config добавить:
       ANTHROPIC_KEY: str = os.environ.get("ANTHROPIC_KEY", "")   # НЕ хранить ключ в коде — переменная окружения (см. [FIX-v303-BUG7])
       AI_BRAIN_ENABLE: bool = True         # вкл/выкл
       AI_ENTRY_FILTER_ENABLE: bool = True  # фильтр входов (осторожно!)
       AI_NIGHTLY_HOUR_UTC: int = 23        # час ночного отчёта (UTC)

  2. В начало spot_bot_v288.py после импортов добавить:
       from ai_brain_advisor_v4 import AIBrainAdvisor

  3. В Engine.__init__ после self.knowledge = BotKnowledge(BASE_DIR) добавить:
       self.ai_advisor = AIBrainAdvisor(self.knowledge, CFG, TG, BASE_DIR)

  4. В BotKnowledge.log_trade_exit() В КОНЦЕ метода (после self._save()) добавить:
       # ИИ объясняет убыток и применяет изменения
       if outcome == 'loss' and hasattr(self, '_engine_ref') and self._engine_ref:
           try:
               self._engine_ref.ai_advisor.ai_explain_loss(t)
           except Exception as _aie: log(f'⚠️ [ai_explain] {_aie}')

  5. В scan_market_top() перед return best_symbol добавить:
       if best_symbol and getattr(CFG,'AI_ENTRY_FILTER_ENABLE',False):
           ok = self.ai_advisor.ai_analyze_entry(best_symbol, best_score, adx_val, rsi_val)
           if not ok:
               log(f'🤖 [AI] Вход в {best_symbol} заблокирован ИИ')
               return None

  6. В run() главный цикл — в блок heartbeat добавить:
       self.ai_advisor.check_nightly_report()
"""

import json
import os
import time
import requests
import threading
from datetime import datetime
from typing import Optional


# ══════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ КЛАСС
# ══════════════════════════════════════════════════════════════════
class AIBrainAdvisor:
    """ИИ-советник мозга. Использует Claude API для анализа торгов."""

    def __init__(self, knowledge, cfg, tg, base_dir: str):
        self.knowledge  = knowledge   # BotKnowledge instance
        self.cfg        = cfg         # Config instance
        self.tg         = tg          # TelegramPolling instance
        self.base_dir   = base_dir

        # Состояние
        self._last_nightly_ts: float = 0.0
        self._entry_cache: dict = {}
        self._entry_cache_ttl: float = 120  # [v288-H4] было 300 (5 мин): монета меняется за 2-3 мин! 120 сек = актуальнее
        self._lock = threading.Lock()

        # Статистика использования API
        self._api_calls_today: int  = 0
        self._api_cost_today: float = 0.0
        self._api_day: str          = ""

        # Лимит защиты от перерасхода
        self.MAX_CALLS_PER_DAY = 50    # [v288-C3] было 30: с учётом реальной нагрузки (analyze+score+nightly)
        self.COST_PER_CALL_USD = 0.003
        # [v288-C3] Кэш для ai_score_symbol (отдельно от _entry_cache)
        self._score_cache: dict = {}        # {cache_key: {'score': float, 'reason': str, 'ts': float}}
        self._score_cache_ttl: float = 600  # 10 мин — score монеты меняется медленнее чем решение входа
        # [v288-H3] Кэш для is_news_blocked — чтобы не читать файл каждые 2 сек
        self._news_cache_result: bool = False
        self._news_cache_ts: float = 0.0
        self._news_cache_ttl: float = 60.0  # обновляем раз в 60 сек
        # [FIX-v303-BUG3] Последняя ошибка API — раньше ЛЮБОЙ сбой (плохой ключ, лимит,
        # снятая с поддержки модель, сетевая ошибка) молча проглатывался и возвращал None,
        # из-за чего снаружи было невозможно понять "работает ли ИИ на самом деле, или он
        # включён но каждый вызов тихо падает". Теперь причина последнего сбоя видна в /ai_status.
        self._last_error: str = ""
        self._last_error_ts: float = 0.0
        self._last_call_ok_ts: float = 0.0

    # ──────────────────────────────────────────────────────────────
    # БАЗОВЫЙ API ВЫЗОВ — всё через него
    # ──────────────────────────────────────────────────────────────
    def _call_api(self, prompt: str, max_tokens: int = 500, timeout: float = 25.0) -> Optional[str]:
        """Вызов Claude API. Возвращает текст или None при ошибке.

        [FIX-v305-BUG] timeout параметр добавлен отдельно: ai_explain_loss/_run_nightly_report/
        ai_daily_forecast/review_blacklist уже вызываются из ФОНОВЫХ потоков — им безопасен
        полный timeout=25с. Но ai_score_symbol() и ai_analyze_entry() вызываются СИНХРОННО
        прямо изнутри scan_market_top(), который выполняется в ТОРГОВОМ потоке (run()) — там
        полный timeout=25с мог замораживать весь скан (а с ним — проверку позиции, стоп-лосс и
        т.д.) на до 25 секунд на каждую подходящую монету при каждом скане. Эти два места теперь
        передают короткий timeout (см. ниже) — то же решение, что уже применено к TG.send()/
        /market//forecast (см. ROUND6/ROUND8)."""
        try:
            key = str(getattr(self.cfg, 'ANTHROPIC_KEY', '') or '')
            if not key or not key.startswith('sk-ant-'):
                self._last_error = "нет ключа ANTHROPIC_KEY"
                self._last_error_ts = time.time()
                return None
            if not getattr(self.cfg, 'AI_BRAIN_ENABLE', True):
                return None

            # Защита от перерасхода
            today = datetime.now().strftime('%Y-%m-%d')
            if self._api_day != today:
                self._api_day        = today
                self._api_calls_today = 0
                self._api_cost_today  = 0.0
            if self._api_calls_today >= self.MAX_CALLS_PER_DAY:
                self._last_error = f"дневной лимит {self.MAX_CALLS_PER_DAY} вызовов исчерпан"
                self._last_error_ts = time.time()
                return None

            resp = requests.post(
                'https://api.anthropic.com/v1/messages',
                headers={
                    'Content-Type':    'application/json',
                    'x-api-key':       key,
                    'anthropic-version': '2023-06-01',
                },
                json={
                    # [FIX-v303-BUG3] claude-sonnet-4-20250514 — снапшот мая 2025, к текущей дате
                    # (2026) вероятно снят с поддержки Anthropic (404) — именно так объяснялся
                    # предыдущий переход с claude-sonnet-4-5 (см. комментарий FIX-v251-4 выше по
                    # истории). При 404/сбое _call_api молча возвращал None — снаружи это выглядело
                    # как "ИИ включён, ключ есть", но КАЖДЫЙ вызов тихо проваливался, вызовов
                    # никогда не прибавлялось. Обновлено на актуальную модель.
                    'model':      'claude-sonnet-5',
                    'max_tokens': max_tokens,
                    'messages':   [{'role': 'user', 'content': prompt}]
                },
                timeout=timeout
            )
            if resp.status_code != 200:
                self._last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                self._last_error_ts = time.time()
                return None

            data = resp.json()
            if data.get('error'):
                self._last_error = f"API error: {str(data.get('error'))[:200]}"
                self._last_error_ts = time.time()
                return None

            self._api_calls_today += 1
            self._api_cost_today  += self.COST_PER_CALL_USD
            self._last_call_ok_ts = time.time()

            return data['content'][0]['text'].strip()

        except Exception as e:
            # Бот продолжает работать без ИИ
            self._last_error = f"{type(e).__name__}: {e}"[:200]
            self._last_error_ts = time.time()
            try:
                import logging
                logging.getLogger('spotbot').warning(f'[AIAdvisor] API недоступен: {e}')
            except Exception:
                pass
            return None

    def _parse_json_response(self, text: str) -> Optional[dict]:
        """Безопасный парсинг JSON из ответа ИИ."""
        try:
            # Убираем markdown если есть
            text = text.strip()
            if '```' in text:
                text = text.split('```')[1]
                if text.startswith('json'):
                    text = text[4:]
            # Находим JSON
            js = text.find('{')
            je = text.rfind('}') + 1
            if js >= 0 and je > js:
                return json.loads(text[js:je])
        except Exception:
            pass
        return None

    # ──────────────────────────────────────────────────────────────
    # ФУНКЦИЯ 1: ИИ ОБЪЯСНЯЕТ УБЫТОК И ПРИМЕНЯЕТ ИЗМЕНЕНИЯ
    # ──────────────────────────────────────────────────────────────
    def ai_explain_loss(self, trade: dict) -> None:
        """
        Вызывается автоматически после каждого убытка.
        ИИ получает полный контекст → анализирует → применяет изменения к CFG.
        Бот работает без остановки — это фоновый анализ.
        """
        try:
            # Собираем контекст из мозга
            trades   = self.knowledge._data.get('trades', [])
            problems = self.knowledge._data.get('problems', [])
            hs       = self.knowledge._data.get('hour_stats', {})
            fs       = self.knowledge._data.get('filter_stats', {})

            # Статистика последних 10 сделок
            recent   = trades[-10:] if len(trades) >= 10 else trades[:]
            recent_wr = sum(1 for t in recent if t.get('outcome') == 'profit') / max(len(recent), 1) * 100

            # Топ убыточных часов
            bad_hours_stat = []
            for h, s in hs.items():
                if s.get('trades', 0) >= 3:
                    wr = s['wins'] / s['trades'] * 100
                    if wr < 40:
                        bad_hours_stat.append(f"{h}ч(WR={wr:.0f}%)")

            # Топ прибыльных ADX диапазонов
            good_adx = []
            for k, v in fs.items():
                if k.startswith('ADX_') and v.get('trades', 0) >= 2:
                    wr = v['wins'] / v['trades'] * 100
                    if wr >= 60:
                        good_adx.append(f"{k}→WR={wr:.0f}%")

            prompt = f"""Ты торговый аналитик крипто-бота. Проанализируй убыток и дай КОНКРЕТНЫЕ изменения параметров.

УБЫТОК:
- Монета: {trade.get('symbol')}
- Убыток: {trade.get('pnl_usdt',0):+.4f} USDT ({trade.get('pnl_pct',0):+.2f}%)
- Причина выхода: {trade.get('exit_reason','')}
- ADX при входе: {trade.get('adx',0):.1f}
- RSI при входе: {trade.get('rsi',0):.1f}
- Час входа: {trade.get('hour',0)}:00
- Время удержания: {trade.get('duration_h',0):.2f}ч
- Режим: {trade.get('mode','')}

ТЕКУЩЕЕ СОСТОЯНИЕ МОЗГА:
- Всего сделок: {len(trades)} | WR={recent_wr:.0f}% (последние 10)
- Текущий ADX_MIN: {getattr(self.cfg,'SCANNER_ADX_MIN',25):.0f}
- Текущий RSI диапазон: {getattr(self.cfg,'SCANNER_RSI_MIN',35):.0f}-{getattr(self.cfg,'SCANNER_RSI_MAX',65):.0f}
- Плохие часы: {bad_hours_stat or 'нет данных'}
- ADX диапазоны с WR>60%: {good_adx or 'нет данных'}
- Последние 5 убытков: {[(p['symbol'],p['exit_reason'][:25],p['adx']) for p in problems[-5:]]}

СТАТИСТИКА ИЗ РЕАЛЬНОЙ БАЗЫ ({len(trades)} сделок):
- Общий WR={recent_wr:.0f}% | Плохие часы: {bad_hours_stat or 'нет'}
- ADX при убытках (последние 5): {[round(p['adx'],0) for p in problems[-5:]] if problems else 'нет'}
- Рекомендуемый ADX_MIN: {round(max([p['adx'] for p in problems[-5:]] or [20]),0) if problems else 20}+

Ответь ТОЛЬКО JSON (без markdown):
{{
  "diagnosis": "краткое объяснение почему убыток (1-2 предложения)",
  "pattern": "паттерн который нужно избегать",
  "changes": {{
    "SCANNER_ADX_MIN": число_или_null,
    "SCANNER_RSI_MIN": число_или_null,
    "SCANNER_RSI_MAX": число_или_null,
    "avoid_hours_add": [список_часов_добавить_в_bad_hours_или_пустой],
    "MIN_SCORE_TO_ENTER": число_или_null,
    "STOP_LOSS_PCT": число_или_null
  }},
  "reasoning": "почему именно эти изменения (1 предложение)",
  "confidence": 0-100
}}"""

            # Запускаем в фоне — не блокируем торговлю
            thread = threading.Thread(
                target=self._apply_loss_analysis,
                args=(prompt, trade),
                daemon=True
            )
            thread.start()

        except Exception as e:
            pass  # Никогда не падаем

    def _apply_loss_analysis(self, prompt: str, trade: dict) -> None:
        """Фоновый поток: вызывает API, применяет изменения, пишет в TG."""
        try:
            text = self._call_api(prompt, max_tokens=600)
            if not text:
                return

            result = self._parse_json_response(text)
            if not result:
                return

            changes    = result.get('changes', {})
            diagnosis  = result.get('diagnosis', '')
            reasoning  = result.get('reasoning', '')
            confidence = int(result.get('confidence', 0))

            # Применяем изменения только если уверенность >= 65%
            applied_changes = []

            if confidence >= 65:
                # ADX_MIN — только повышаем, не понижаем слишком агрессивно
                new_adx = changes.get('SCANNER_ADX_MIN')
                if new_adx is not None:
                    cur = float(getattr(self.cfg, 'SCANNER_ADX_MIN', 25))
                    new_adx = float(new_adx)
                    # Ограничения безопасности
                    new_adx = max(12.0, min(35.0, new_adx))
                    if abs(new_adx - cur) >= 1.0:
                        self.cfg.SCANNER_ADX_MIN = new_adx
                        self.cfg.ADX_MIN = new_adx
                        # [FIX-v251-6] При ИИ-повышении ADX помечаем НЕ ослабленным
                        # Иначе apply_mode при рестарте снизит ADX до профильного
                        self.cfg._filter_relaxed_by_brain = False  # ИИ повысил — это ужесточение
                        applied_changes.append(f"ADX_MIN {cur:.0f}→{new_adx:.0f}")
                        # Сохраняем в learned_cfg
                        self.knowledge._data.setdefault('learned_cfg', {})['SCANNER_ADX_MIN'] = new_adx

                # RSI диапазон
                new_rsi_min = changes.get('SCANNER_RSI_MIN')
                if new_rsi_min is not None:
                    cur = float(getattr(self.cfg, 'SCANNER_RSI_MIN', 35))
                    new_rsi_min = max(18.0, min(45.0, float(new_rsi_min)))
                    if abs(new_rsi_min - cur) >= 1.0:
                        self.cfg.SCANNER_RSI_MIN = new_rsi_min
                        applied_changes.append(f"RSI_MIN {cur:.0f}→{new_rsi_min:.0f}")
                        self.knowledge._data.setdefault('learned_cfg', {})['SCANNER_RSI_MIN'] = new_rsi_min

                new_rsi_max = changes.get('SCANNER_RSI_MAX')
                if new_rsi_max is not None:
                    cur = float(getattr(self.cfg, 'SCANNER_RSI_MAX', 65))
                    new_rsi_max = max(55.0, min(75.0, float(new_rsi_max)))
                    if abs(new_rsi_max - cur) >= 1.0:
                        self.cfg.SCANNER_RSI_MAX = new_rsi_max
                        applied_changes.append(f"RSI_MAX {cur:.0f}→{new_rsi_max:.0f}")

                # Плохие часы
                add_hours = changes.get('avoid_hours_add', [])
                if add_hours and isinstance(add_hours, list):
                    cur_bad = list(self.knowledge._data.get('bad_hours', []))
                    added = []
                    for h in add_hours:
                        h = int(h)
                        if h not in cur_bad and len(cur_bad) < 12:
                            cur_bad.append(h)
                            added.append(h)
                    if added:
                        self.knowledge._data['bad_hours'] = cur_bad
                        applied_changes.append(f"bad_hours +{added}")

                # MIN_SCORE
                new_score = changes.get('MIN_SCORE_TO_ENTER')
                if new_score is not None:
                    cur = float(getattr(self.cfg, 'MIN_SCORE_TO_ENTER', 4.5))
                    new_score = max(3.0, min(7.0, float(new_score)))
                    if abs(new_score - cur) >= 0.3:
                        self.cfg.MIN_SCORE_TO_ENTER = new_score
                        applied_changes.append(f"MIN_SCORE {cur:.1f}→{new_score:.1f}")

                # Сохраняем изменения
                if applied_changes:
                    self.knowledge._data.setdefault('ai_learning_log', []).append({
                        'ts':       datetime.now().isoformat(),
                        'type':     'loss_analysis',
                        'symbol':   trade.get('symbol'),
                        'pnl':      trade.get('pnl_usdt'),
                        'changes':  applied_changes,
                        'diagnosis': diagnosis,
                        'confidence': confidence,
                    })
                    self.knowledge._data['ai_learning_log'] = \
                        self.knowledge._data['ai_learning_log'][-100:]
                    self.knowledge._save()

            # Отправляем в Telegram
            if self.tg and getattr(self.tg, 'enabled', False):
                emoji_conf = "🟢" if confidence >= 70 else "🟡" if confidence >= 50 else "🔴"
                msg = (
                    f"🤖 ИИ-АНАЛИЗ УБЫТКА\n"
                    f"{'='*28}\n"
                    f"Монета: {trade.get('symbol')} | {trade.get('pnl_usdt',0):+.4f} USDT\n"
                    f"Причина: {trade.get('exit_reason','')[:40]}\n"
                    f"{'─'*28}\n"
                    f"🔍 Диагноз: {diagnosis}\n"
                )
                if applied_changes:
                    msg += f"{'─'*28}\n✅ ПРИМЕНЕНО ({emoji_conf}{confidence}%):\n"
                    for ch in applied_changes:
                        msg += f"  • {ch}\n"
                    msg += f"💡 {reasoning}\n"
                else:
                    msg += f"{'─'*28}\n{emoji_conf} Уверенность {confidence}% — параметры не изменены\n"
                    if confidence < 65:
                        msg += "(нужно ≥65% для применения изменений)\n"

                msg += f"💰 API сегодня: {self._api_calls_today} вызовов"
                self.tg.send(msg)

        except Exception as e:
            pass

    def ai_explain_win(self, trade: dict) -> None:
        """[v277] Анализ ПОБЕДЫ — запоминает при каких условиях получили профит.
        Бот учится не только на ошибках, но и на успехах.
        Усиливает паттерны которые дают прибыль (хорошие ADX/RSI/часы/монеты)."""
        try:
            trades = self.knowledge._data.get('trades', [])
            # Собираем статистику ПРИБЫЛЬНЫХ сделок
            wins = [t for t in trades if t.get('outcome') == 'profit']
            if len(wins) < 3:
                return  # мало данных для выводов

            # Анализируем факторы прибыли
            win_adx = [float(t.get('adx', 0)) for t in wins if t.get('adx')]
            win_rsi = [float(t.get('rsi', 0)) for t in wins if t.get('rsi')]
            win_hours = {}
            win_symbols = {}
            for t in wins:
                h = t.get('hour', 0)
                win_hours[h] = win_hours.get(h, 0) + 1
                s = t.get('symbol', '')
                win_symbols[s] = win_symbols.get(s, 0) + 1

            # Средние значения успешных сделок
            avg_win_adx = sum(win_adx) / len(win_adx) if win_adx else 0
            avg_win_rsi = sum(win_rsi) / len(win_rsi) if win_rsi else 0
            best_hours = sorted(win_hours.items(), key=lambda x: -x[1])[:3]
            best_symbols = sorted(win_symbols.items(), key=lambda x: -x[1])[:5]

            # Сохраняем "профиль успеха" в знания (для усиления хороших условий)
            win_profile = {
                'avg_adx': round(avg_win_adx, 1),
                'avg_rsi': round(avg_win_rsi, 1),
                'best_hours': [h for h, _ in best_hours],
                'best_symbols': [s for s, _ in best_symbols],
                'total_wins': len(wins),
                'last_win': {
                    'symbol': trade.get('symbol'),
                    'adx': trade.get('adx'),
                    'rsi': trade.get('rsi'),
                    'pnl': trade.get('pnl_usdt'),
                }
            }
            self.knowledge._data['win_profile'] = win_profile

            # Помечаем хорошие монеты (повышаем доверие в symbol_stats)
            try:
                sym = trade.get('symbol', '')
                if sym:
                    ss = self.knowledge._data.setdefault('symbol_stats', {}).setdefault(sym, {})
                    ss['wins'] = ss.get('wins', 0) + 1
                    ss['last_win_adx'] = trade.get('adx')
                    ss['last_win_rsi'] = trade.get('rsi')
            except Exception:
                pass

            self.knowledge._save()

            # Лог: при каких условиях получили профит
            try:
                from datetime import datetime
                _log = getattr(self, '_log_fn', None) or print
                _log(f"💚 [v277] Анализ ПОБЕДЫ {trade.get('symbol')}: "
                     f"профит при ADX~{avg_win_adx:.0f}, RSI~{avg_win_rsi:.0f} | "
                     f"всего побед: {len(wins)} | лучшие часы: {[h for h,_ in best_hours]}")
            except Exception:
                pass

            # TG отчёт раз в 5 побед (не спамим)
            if self.tg and len(wins) % 5 == 0:
                try:
                    msg = (f"💚 ПРОФИЛЬ УСПЕХА (после {len(wins)} побед)\n"
                           f"Прибыль чаще при:\n"
                           f"• ADX ~{avg_win_adx:.0f}\n"
                           f"• RSI ~{avg_win_rsi:.0f}\n"
                           f"• Часы: {[h for h,_ in best_hours]}\n"
                           f"• Монеты: {[s for s,_ in best_symbols[:3]]}")
                    self.tg.send(msg)
                except Exception:
                    pass

        except Exception as e:
            pass  # никогда не падаем

    # ──────────────────────────────────────────────────────────────
    # ФУНКЦИЯ 2: ИИ АНАЛИЗИРУЕТ ВХОД В СДЕЛКУ
    # ──────────────────────────────────────────────────────────────
    def ai_analyze_entry(self, symbol: str, score: float,
                         adx: float, rsi: float) -> bool:
        """
        Вызывается перед входом в сделку.
        Возвращает True (войти) или False (пропустить).
        При ошибке API → всегда True (не блокируем торговлю).
        """
        try:
            # Проверяем кэш
            cache_key = f"{symbol}_{int(score*10)}_{int(adx)}_{int(rsi)}"
            with self._lock:
                cached = self._entry_cache.get(cache_key)
                if cached and (time.time() - cached['ts']) < self._entry_cache_ttl:
                    return cached['decision']

            trades  = self.knowledge._data.get('trades', [])
            # История этой монеты
            sym_trades = [t for t in trades if t.get('symbol') == symbol]
            sym_wr = 0
            if sym_trades:
                sym_wins = sum(1 for t in sym_trades if t.get('outcome') == 'profit')
                sym_wr   = sym_wins / len(sym_trades) * 100

            # Час сейчас
            hour = datetime.now().hour
            bad_hours = self.knowledge._data.get('bad_hours', [])
            hour_stats = self.knowledge._data.get('hour_stats', {}).get(str(hour), {})
            hour_wr = 0
            if hour_stats.get('trades', 0) >= 3:
                hour_wr = hour_stats['wins'] / hour_stats['trades'] * 100

            prompt = f"""Ты торговый фильтр крипто-бота. Анализируй БЫСТРО.

ВХОД В СДЕЛКУ:
- Монета: {symbol}
- Score сканера: {score:.1f}/15
- ADX: {adx:.1f} | RSI: {rsi:.1f}
- Час: {hour}:00

БАЗА ДАННЫХ ({len(sym_trades)+1} сделок по {symbol}, {len(trades)} всего):
- История {symbol}: {len(sym_trades)} сделок WR={sym_wr:.0f}%
- Час {hour}:00 WR={hour_wr:.0f}% ({hour_stats.get('trades',0)} сделок)
- Плохие часы: {bad_hours}

Ответь ТОЛЬКО JSON:
{{"enter": true/false, "confidence": 0-100, "reason": "одна причина"}}"""

            # [FIX-v305-BUG] Вызывается СИНХРОННО из scan_market_top() — торговый поток.
            # Короткий timeout, чтобы медленный/недоступный API не замораживал скан на 25с.
            text = self._call_api(prompt, max_tokens=100, timeout=4.0)
            if not text:
                return True  # API недоступен — не блокируем

            result = self._parse_json_response(text)
            if not result:
                return True

            decision   = bool(result.get('enter', True))
            confidence = int(result.get('confidence', 50))
            reason     = str(result.get('reason', ''))

            # Блокируем только при высокой уверенности ИИ
            final = True
            if not decision and confidence >= 75:
                final = False

            # Кэшируем
            with self._lock:
                self._entry_cache[cache_key] = {'decision': final, 'ts': time.time()}
                # Чистим старый кэш
                if len(self._entry_cache) > 50:
                    old_key = min(self._entry_cache, key=lambda k: self._entry_cache[k]['ts'])
                    del self._entry_cache[old_key]

            # Логируем отказ в TG
            if not final and self.tg and getattr(self.tg, 'enabled', False):
                self.tg.send_throttled(
                    f'ai_block_{symbol}',
                    f"🤖 ИИ заблокировал вход\n"
                    f"Монета: {symbol} | ADX={adx:.0f} RSI={rsi:.0f}\n"
                    f"Причина: {reason}\n"
                    f"Уверенность: {confidence}%",
                    cooldown_sec=300
                )

            return final

        except Exception:
            return True  # При любой ошибке — не блокируем

    # ──────────────────────────────────────────────────────────────
    # ФУНКЦИЯ 3: НОЧНОЙ ИИ-ОТЧЁТ С АВТОКОРРЕКТИРОВКОЙ
    # ──────────────────────────────────────────────────────────────
    def check_nightly_report(self) -> None:
        """
        Вызывается из run() каждые N секунд.
        Сам определяет когда время ночного отчёта.
        """
        try:
            now = datetime.utcnow()
            target_hour = int(getattr(self.cfg, 'AI_NIGHTLY_HOUR_UTC', 23))

            # Раз в сутки в нужный час
            if now.hour != target_hour:
                return
            if (time.time() - self._last_nightly_ts) < 3600 * 20:
                return  # уже делали сегодня

            self._last_nightly_ts = time.time()

            # Запускаем в фоне
            thread = threading.Thread(target=self._run_nightly_report, daemon=True)
            thread.start()

        except Exception:
            pass

    def _run_nightly_report(self) -> None:
        """Ночной анализ и автокорректировка всех параметров."""
        try:
            trades   = self.knowledge._data.get('trades', [])
            if len(trades) < 5:
                return

            hs = self.knowledge._data.get('hour_stats', {})
            fs = self.knowledge._data.get('filter_stats', {})
            ms = self.knowledge._data.get('mode_stats', {})

            # Подготовка статистики
            total     = len(trades)
            wins      = sum(1 for t in trades if t.get('outcome') == 'profit')
            wr        = wins / total * 100
            total_pnl = sum(t.get('pnl_usdt', 0) for t in trades)

            # Топ убыточных часов
            bad_h = []
            for h, s in sorted(hs.items(), key=lambda x: int(x[0])):
                if s.get('trades', 0) >= 3:
                    h_wr = s['wins'] / s['trades'] * 100
                    bad_h.append(f"{h}ч:WR={h_wr:.0f}%(n={s['trades']})")

            # ADX статистика
            adx_stat = []
            for k, v in sorted(fs.items()):
                if k.startswith('ADX_') and v.get('trades', 0) >= 2:
                    a_wr = v['wins'] / v['trades'] * 100
                    adx_stat.append(f"{k}:WR={a_wr:.0f}%(n={v['trades']})")

            # Топ убыточных монет
            sym_stats = self.knowledge._data.get('learned_cfg', {}).get('symbol_stats', {})
            worst_sym = sorted(
                [(s, d) for s, d in sym_stats.items() if d.get('trades', 0) >= 2],
                key=lambda x: x[1]['pnl']
            )[:5]

            # Последние 10 убытков
            last_losses = [t for t in trades if t.get('outcome') == 'loss'][-10:]
            loss_patterns = [(t['symbol'], t.get('exit_reason', '')[:30],
                             t.get('adx', 0), t.get('hour', 0)) for t in last_losses]

            prompt = f"""Ты главный аналитик крипто-бота. Ночной анализ торгов.

ИТОГИ:
- Всего сделок: {total} | WR={wr:.1f}% | PnL={total_pnl:+.4f} USDT
- {'🔴 УБЫТОЧНО' if total_pnl < 0 else '🟢 ПРИБЫЛЬНО'}

СТАТИСТИКА ПО ЧАСАМ (UTC):
{chr(10).join(bad_h)}

СТАТИСТИКА ПО ADX:
{chr(10).join(adx_stat)}
ПАТТЕРН: ADX 40+ = WR 80%, ADX < 25 = WR < 30%

ТОП УБЫТОЧНЫХ МОНЕТ:
{chr(10).join(f'{s}: {d["trades"]} сделок PnL={d["pnl"]:+.3f} WR={d["wins"]/d["trades"]*100:.0f}%' for s,d in worst_sym)}

ПОСЛЕДНИЕ 10 УБЫТКОВ (монета, причина, ADX, час):
{chr(10).join(str(x) for x in loss_patterns)}

ТЕКУЩИЕ ПАРАМЕТРЫ:
- ADX_MIN={getattr(self.cfg,'SCANNER_ADX_MIN',25):.0f}
- RSI={getattr(self.cfg,'SCANNER_RSI_MIN',35):.0f}-{getattr(self.cfg,'SCANNER_RSI_MAX',65):.0f}
- SL={getattr(self.cfg,'STOP_LOSS_PCT',1.2):.2f}%
- TIME_STOP={getattr(self.cfg,'TIME_STOP_HOURS',1.0):.1f}ч
- MIN_SCORE={getattr(self.cfg,'MIN_SCORE_TO_ENTER',4.5):.1f}

Дай комплексную оптимизацию. Ответь ТОЛЬКО JSON:
{{
  "summary": "2-3 предложения что происходит с ботом",
  "main_problem": "главная причина убытков",
  "changes": {{
    "SCANNER_ADX_MIN": число,
    "SCANNER_RSI_MIN": число,
    "SCANNER_RSI_MAX": число,
    "STOP_LOSS_PCT": число,
    "TIME_STOP_HOURS": число,
    "MIN_SCORE_TO_ENTER": число,
    "bad_hours_set": [список_худших_часов_максимум_6]
  }},
  "priority_actions": ["действие1", "действие2", "действие3"],
  "forecast": "чего ожидать при этих изменениях",
  "confidence": 0-100
}}"""

            text = self._call_api(prompt, max_tokens=800)
            if not text:
                return

            result = self._parse_json_response(text)
            if not result:
                return

            changes    = result.get('changes', {})
            confidence = int(result.get('confidence', 0))
            summary    = result.get('summary', '')
            main_prob  = result.get('main_problem', '')
            forecast   = result.get('forecast', '')
            actions    = result.get('priority_actions', [])

            applied = []

            # Применяем ВСЕ изменения (ночной отчёт = высокая надёжность)
            MIN_CONFIDENCE = 60

            if confidence >= MIN_CONFIDENCE:
                # ADX
                v = changes.get('SCANNER_ADX_MIN')
                if v:
                    cur = float(getattr(self.cfg, 'SCANNER_ADX_MIN', 25))
                    v = max(12.0, min(40.0, float(v)))
                    if abs(v - cur) >= 1.0:
                        self.cfg.SCANNER_ADX_MIN = v
                        self.cfg.ADX_MIN = v
                        applied.append(f"ADX_MIN {cur:.0f}→{v:.0f}")
                        self.knowledge._data.setdefault('learned_cfg', {})['SCANNER_ADX_MIN'] = v

                # RSI
                v = changes.get('SCANNER_RSI_MIN')
                if v:
                    cur = float(getattr(self.cfg, 'SCANNER_RSI_MIN', 35))
                    v = max(18.0, min(45.0, float(v)))
                    if abs(v - cur) >= 1.0:
                        self.cfg.SCANNER_RSI_MIN = v
                        applied.append(f"RSI_MIN {cur:.0f}→{v:.0f}")
                        self.knowledge._data.setdefault('learned_cfg', {})['SCANNER_RSI_MIN'] = v

                v = changes.get('SCANNER_RSI_MAX')
                if v:
                    cur = float(getattr(self.cfg, 'SCANNER_RSI_MAX', 65))
                    v = max(55.0, min(78.0, float(v)))
                    if abs(v - cur) >= 1.0:
                        self.cfg.SCANNER_RSI_MAX = v
                        applied.append(f"RSI_MAX {cur:.0f}→{v:.0f}")

                # SL
                v = changes.get('STOP_LOSS_PCT')
                if v:
                    cur = float(getattr(self.cfg, 'STOP_LOSS_PCT', 1.2))
                    v = max(0.7, min(2.0, float(v)))
                    if abs(v - cur) >= 0.05:
                        self.cfg.STOP_LOSS_PCT = v
                        # [FIX-v252-1] Сохраняем в learned_cfg — переживёт рестарт
                        self.knowledge._data.setdefault('learned_cfg', {})['STOP_LOSS_PCT'] = v
                        applied.append(f"SL {cur:.2f}%→{v:.2f}%")

                # TIME_STOP
                v = changes.get('TIME_STOP_HOURS')
                if v:
                    cur = float(getattr(self.cfg, 'TIME_STOP_HOURS', 1.0))
                    v = max(0.5, min(4.0, float(v)))
                    if abs(v - cur) >= 0.1:
                        self.cfg.TIME_STOP_HOURS = v
                        # [FIX-v252-1] Сохраняем в learned_cfg — переживёт рестарт
                        self.knowledge._data.setdefault('learned_cfg', {})['TIME_STOP_HOURS'] = v
                        applied.append(f"TimeStop {cur:.1f}→{v:.1f}ч")

                # MIN_SCORE
                v = changes.get('MIN_SCORE_TO_ENTER')
                if v:
                    cur = float(getattr(self.cfg, 'MIN_SCORE_TO_ENTER', 4.5))
                    v = max(2.5, min(8.0, float(v)))
                    if abs(v - cur) >= 0.2:
                        self.cfg.MIN_SCORE_TO_ENTER = v
                        applied.append(f"MinScore {cur:.1f}→{v:.1f}")

                # Bad hours — полная замена
                new_bad = changes.get('bad_hours_set')
                if new_bad and isinstance(new_bad, list):
                    new_bad = [int(h) for h in new_bad[:8]]  # макс 8 часов
                    self.knowledge._data['bad_hours'] = new_bad
                    applied.append(f"bad_hours={new_bad}")

                # Сохраняем
                if applied:
                    self.knowledge._data.setdefault('ai_learning_log', []).append({
                        'ts':       datetime.now().isoformat(),
                        'type':     'nightly_report',
                        'changes':  applied,
                        'confidence': confidence,
                        'summary':  summary,
                    })
                    self.knowledge._data['ai_learning_log'] = \
                        self.knowledge._data['ai_learning_log'][-100:]
                    self.knowledge._save()

            # Telegram отчёт
            if self.tg and getattr(self.tg, 'enabled', False):
                conf_emoji = "🟢" if confidence >= 70 else "🟡" if confidence >= 50 else "🔴"
                msg = (
                    f"🌙 НОЧНОЙ ИИ-ОТЧЁТ\n"
                    f"{'='*30}\n"
                    f"📊 {total} сделок | WR={wr:.0f}% | {total_pnl:+.4f} USDT\n"
                    f"{'─'*30}\n"
                    f"🔍 {summary}\n"
                    f"⚠️ Главная проблема: {main_prob}\n"
                    f"{'─'*30}\n"
                )
                if applied:
                    msg += f"✅ ПРИМЕНЕНО ({conf_emoji}{confidence}%):\n"
                    for ch in applied:
                        msg += f"  • {ch}\n"
                else:
                    msg += f"ℹ️ Изменений нет ({conf_emoji}{confidence}%)\n"

                if actions:
                    msg += f"{'─'*30}\n📋 Приоритеты:\n"
                    for a in actions[:3]:
                        msg += f"  {a}\n"

                msg += (
                    f"{'─'*30}\n"
                    f"🔮 Прогноз: {forecast}\n"
                    f"💰 API сегодня: {self._api_calls_today} вызовов"
                )
                self.tg.send(msg)

        except Exception as e:
            try:
                import logging
                logging.getLogger('spotbot').warning(f'[AIAdvisor] nightly_report: {e}')
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────────
    # [v277] БЕЗОПАСНЫЕ ЗАГЛУШКИ — чтобы вызовы из бота не падали
    # Возвращают нейтральные значения (не блокируют торговлю)
    # ──────────────────────────────────────────────────────────────
    def ai_correlation_signal(self, btc_change=0.0, eth_change=0.0):
        """Заглушка: нейтральный сигнал корреляции (не блокирует торговлю)."""
        return {'signal': 'neutral', 'block': False}

    def ai_morning_market_analysis(self, *args, **kwargs):
        """Заглушка: утренний анализ (не реализован, не мешает торговле)."""
        return None

    def get_open_interest(self, symbol: str = ""):
        """[FIX-v303-BUG3] Раньше возвращал None, а /market делает oi['oi_change_pct'] —
        TypeError на пустом месте (падало в except, /market всегда писал "Ошибка анализа").
        Реальный open interest с Bybit требует биржевой клиент, которого у советника нет
        (он получает только knowledge/cfg/tg) — оставляем нейтральный плейсхолдер тем же
        форматом, каким пользуется вызывающий код, чтобы /market хотя бы не падал."""
        return {'oi_change_pct': 0.0, 'available': False}

    def is_news_blocked(self, symbol: str = "") -> bool:
        """[v280] Читает news_signals.json (от news_bot). True = опасные новости, блокируем торговлю.
        [v288-H3] Добавлен кэш 60 сек — было: чтение файла каждые 2 сек = 1800 чтений/час.
        Безопасно: при любой ошибке возвращает False (не блокирует)."""
        try:
            import os, json, time
            # [v288-H3] Кэш: не читаем файл чаще чем раз в 60 сек
            _now = time.time()
            if _now - self._news_cache_ts < self._news_cache_ttl:
                # Если кэш глобальной блокировки — возвращаем сразу
                if self._news_cache_result and not symbol:
                    return True
                # При запросе конкретной монеты всегда читаем (нет монета-специфичного кэша)
                if not symbol:
                    return self._news_cache_result
            # Читаем файл и обновляем кэш
            path = os.path.join(getattr(self, 'base_dir', '.'), 'news_signals.json')
            if not os.path.exists(path):
                self._news_cache_result = False
                self._news_cache_ts = _now
                return False
            # Устаревший файл (>30 мин) — игнорируем
            if _now - os.path.getmtime(path) > 1800:
                self._news_cache_result = False
                self._news_cache_ts = _now
                return False
            with open(path) as f:
                data = json.load(f)
            # Глобальная блокировка рынка
            global_block = bool(data.get('market_blocked') or data.get('global_block'))
            self._news_cache_result = global_block
            self._news_cache_ts = _now
            if global_block:
                return True
            # Блокировка конкретной монеты (не кэшируем — зависит от symbol)
            if symbol:
                blocked = data.get('blocked_coins', []) or data.get('blocked', [])
                base = symbol.replace('USDT', '').replace('USDC', '')
                for b in blocked:
                    if base in str(b).upper():
                        return True
            return False
        except Exception:
            return False  # при ошибке НЕ блокируем

    def ai_news_filter(self, symbol: str = "") -> bool:
        """Алиас для is_news_blocked (совместимость)."""
        return self.is_news_blocked(symbol)

    # [FIX-v303-BUG3] get_fear_greed/get_btc_dominance/get_market_volume не существовали
    # вовсе (только get_market_volume был заглушкой-None) — команда /market проверяет
    # hasattr(self.ai_advisor, 'get_fear_greed') и при отсутствии метода пишет
    # "⚠️ ИИ-советник не активен", хотя советник на самом деле работает — просто у него нет
    # этого конкретного метода. Реализованы через публичные бесплатные API (без ключей).
    def _cg_global(self) -> Optional[dict]:
        """Общий кэш на 5 мин для одного CoinGecko /global запроса — /market дергает
        get_btc_dominance() и get_market_volume() почти одновременно, незачем бить API дважды."""
        _now = time.time()
        _cached = getattr(self, '_cg_global_cache', None)
        if _cached and (_now - _cached.get('ts', 0)) < 300:
            return _cached.get('data')
        try:
            r = requests.get('https://api.coingecko.com/api/v3/global', timeout=8)
            if r.status_code != 200:
                return None
            data = r.json().get('data') or {}
            self._cg_global_cache = {'ts': _now, 'data': data}
            return data
        except Exception:
            return None

    def get_fear_greed(self) -> dict:
        """[FIX-v303-BUG3] Индекс страха/жадности с alternative.me (без ключа).
        При недоступности API — нейтральное значение 50, не блокирует торговлю."""
        try:
            r = requests.get('https://api.alternative.me/fng/?limit=1', timeout=8)
            if r.status_code == 200:
                d = (r.json().get('data') or [{}])[0]
                return {'value': int(d.get('value', 50)), 'class': str(d.get('value_classification', 'Neutral'))}
        except Exception:
            pass
        return {'value': 50, 'class': 'Neutral (n/a)'}

    def get_btc_dominance(self) -> float:
        """[FIX-v303-BUG3] Доля BTC в общей капитализации рынка (CoinGecko /global)."""
        data = self._cg_global()
        try:
            if data:
                return float(data.get('market_cap_percentage', {}).get('btc', 0.0))
        except Exception:
            pass
        return 0.0

    def get_market_volume(self, *args, **kwargs) -> dict:
        """[FIX-v303-BUG3] Общий объём/изменение капитализации рынка за 24ч (CoinGecko /global)."""
        data = self._cg_global()
        try:
            if data:
                return {
                    'volume_usd': float(data.get('total_volume', {}).get('usd', 0.0)),
                    'mcap_change_24h': float(data.get('market_cap_change_percentage_24h_usd', 0.0)),
                }
        except Exception:
            pass
        return {'volume_usd': 0.0, 'mcap_change_24h': 0.0}

    def ai_market_regime(self, *args, **kwargs):
        """Заглушка: режим рынка берётся из correlation_bot (market_state.json), не из ИИ."""
        return {'regime': 'normal', 'block': False}

    def check_mode_restore(self, *args, **kwargs):
        """Заглушка: восстановление режима (не реализовано, безопасно)."""
        return None

    def ai_daily_forecast(self, *args, **kwargs) -> None:
        """[FIX-v303-BUG3] Раньше была пустой заглушкой — /forecast писал "Запрашиваю
        прогноз..." и на этом всё, ответ никогда не приходил (заглушка просто return None,
        ничего в Telegram не отправляя). Теперь реально дёргает Claude API с текущей
        статистикой мозга и присылает прогноз на день.
        [FIX-v303-BUG4] Вызывается из handle_command() — ТОРГОВОГО потока. Синхронный
        requests.post к Claude (до 25с) там же блокировал бы весь торговый цикл (та же
        проблема, что была с TG.send() до v303). Реальная работа поэтому вынесена в
        фоновый поток — вызывающий код получает мгновенный возврат."""
        threading.Thread(target=self._run_daily_forecast, daemon=True).start()

    def _run_daily_forecast(self) -> None:
        try:
            trades = self.knowledge._data.get('trades', [])
            if len(trades) < 3:
                if self.tg and getattr(self.tg, 'enabled', False):
                    self.tg.send("🔮 Недостаточно сделок для прогноза (нужно 3+).")
                return
            total = len(trades)
            wins = sum(1 for t in trades if t.get('outcome') == 'profit')
            wr = wins / total * 100
            total_pnl = sum(t.get('pnl_usdt', 0) for t in trades)
            recent = trades[-10:]
            recent_wr = sum(1 for t in recent if t.get('outcome') == 'profit') / max(len(recent), 1) * 100
            bad_hours = self.knowledge._data.get('bad_hours', [])
            hour = datetime.now().hour

            prompt = f"""Ты торговый аналитик крипто-бота (спот-грид, Bybit). Дай короткий прогноз на сегодня.

СТАТИСТИКА:
- Всего сделок: {total} | WR общий={wr:.0f}% | WR последние 10={recent_wr:.0f}%
- Общий PnL: {total_pnl:+.4f} USDT
- Текущий час: {hour}:00 | Плохие часы (историч.): {bad_hours}
- Режим: {getattr(self.cfg,'TRADING_MODE','SMART')} | ADX_MIN={getattr(self.cfg,'SCANNER_ADX_MIN',25):.0f}

Ответь ТОЛЬКО JSON:
{{"forecast": "2-3 предложения прогноза на сегодня", "risk_note": "одно предупреждение если есть", "confidence": 0-100}}"""

            text = self._call_api(prompt, max_tokens=300)
            if not text:
                if self.tg and getattr(self.tg, 'enabled', False):
                    self.tg.send(f"⚠️ Прогноз недоступен: {self._last_error or 'API не ответил'}")
                return
            result = self._parse_json_response(text)
            if not result:
                if self.tg and getattr(self.tg, 'enabled', False):
                    self.tg.send("⚠️ Прогноз: не удалось разобрать ответ ИИ")
                return
            if self.tg and getattr(self.tg, 'enabled', False):
                conf = int(result.get('confidence', 0))
                msg = (
                    f"🔮 ПРОГНОЗ НА СЕГОДНЯ ({conf}%)\n"
                    f"{result.get('forecast','')}\n"
                )
                if result.get('risk_note'):
                    msg += f"⚠️ {result.get('risk_note')}"
                self.tg.send(msg)
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"[:200]
            if self.tg and getattr(self.tg, 'enabled', False):
                self.tg.send(f"⚠️ /forecast ошибка: {e}")

    # ──────────────────────────────────────────────────────────────
    # СТАТИСТИКА ИСПОЛЬЗОВАНИЯ
    # ──────────────────────────────────────────────────────────────
    def get_status(self) -> str:
        """Краткий статус для /status команды."""
        today = datetime.now().strftime('%Y-%m-%d')
        if self._api_day != today:
            calls = 0
            cost  = 0.0
        else:
            calls = self._api_calls_today
            cost  = self._api_cost_today
        enabled = bool(getattr(self.cfg, 'AI_BRAIN_ENABLE', True))
        key_ok  = str(getattr(self.cfg, 'ANTHROPIC_KEY', '')).startswith('sk-ant-')
        # [FIX-v303-BUG3] Раньше "✅ вкл, ключ ✅" ничего не говорило о том, реально ли
        # проходят вызовы — с 0 вызовов и ключом на месте это выглядело одинаково что при
        # рабочем ИИ, что при 100%-но падающих (например снятая с поддержки модель) вызовах.
        _last_ok_str = ""
        if self._last_call_ok_ts > 0:
            _mins_ok = (time.time() - self._last_call_ok_ts) / 60.0
            _last_ok_str = f"\nПоследний успешный вызов: {_mins_ok:.0f} мин назад"
        _err_str = ""
        if self._last_error and (time.time() - self._last_error_ts) < 3600:
            _mins_err = (time.time() - self._last_error_ts) / 60.0
            _err_str = f"\n⚠️ Последняя ошибка ({_mins_err:.0f} мин назад): {self._last_error}"
        return (
            f"🤖 ИИ-советник: {'✅ вкл' if enabled else '❌ выкл'}\n"
            f"Ключ: {'✅' if key_ok else '❌ не задан'}\n"
            f"Сегодня: {calls} вызовов (~${cost:.3f})\n"
            f"Лимит: {self.MAX_CALLS_PER_DAY} вызовов/день\n"
            f"Фильтр входов: {'✅' if getattr(self.cfg,'AI_ENTRY_FILTER_ENABLE',False) else '❌'}"
            f"{_last_ok_str}{_err_str}"
        )

    # ══════════════════════════════════════════════════════════════
    # ДОРАБОТКА 1: Умный пересмотр permanent blacklist
    # ══════════════════════════════════════════════════════════════
    def review_blacklist(self) -> None:
        """
        [НОВОЕ] Еженедельный пересмотр permanent_blacklist.
        ИИ анализирует каждую заблокированную монету и решает:
        - восстановилась ли она (ADX вырос, объём вернулся)
        - стоит ли дать второй шанс
        Вызывается раз в 7 дней из run_loop.
        """
        import threading
        t = threading.Thread(target=self._run_blacklist_review, daemon=True)
        t.start()

    def _run_blacklist_review(self) -> None:
        try:
            import os, json, time as _t
            BASE_DIR = os.path.dirname(os.path.abspath(__file__))
            bl_file = os.path.join(BASE_DIR, 'banned_coins.json')
            if not os.path.exists(bl_file):
                return

            with open(bl_file, 'r') as f:
                bl_data = json.load(f)
            banned = bl_data.get('banned', [])
            reasons = bl_data.get('reasons', {})
            blocked_since = bl_data.get('blocked_since', {})

            if not banned:
                return

            # Собираем статистику из базы знаний
            trades = self.knowledge._data.get('trades', [])
            total = len(trades)
            wr = sum(1 for t in trades if t.get('outcome') == 'profit') / max(1, total) * 100

            # Промпт для ИИ
            banned_info = []
            _now = _t.time()
            for sym in banned[:20]:  # не больше 20 за раз
                reason = reasons.get(sym, 'unknown')
                since_days = int((_now - float(blocked_since.get(sym, _now))) / 86400)
                banned_info.append(f"{sym}: причина={reason}, заблокирован {since_days} дней назад")

            prompt = f"""
Ты анализируешь черный список торгового бота (спот-торговля Bybit).
Бот сделал {total} сделок, WR={wr:.0f}%.

ЗАБЛОКИРОВАННЫЕ МОНЕТЫ:
{chr(10).join(banned_info)}

Для каждой монеты реши:
- Если монета заблокирована МЕНЬШЕ 14 дней → оставить в блокировке
- Если монета заблокирована БОЛЬШЕ 30 дней → дать второй шанс (разблокировать)
- Если причина "post_buy_sell_guard_fail" → ждать 21 день
- Если причина "stop_loss" → ждать 14 дней
- Если причина "dump_exit" → ждать 30 дней

Ответь ТОЛЬКО JSON:
{{
  "unblock": ["SYM1", "SYM2"],
  "keep_blocked": ["SYM3"],
  "reason": "краткое объяснение"
}}"""

            text = self._call_api(prompt, max_tokens=300)
            if not text:
                return

            result = self._parse_json_response(text)
            if not result:
                return

            to_unblock = result.get('unblock', [])
            reason_txt = result.get('reason', '')

            if not to_unblock:
                return

            # Убираем из banned_coins.json
            new_banned = [s for s in banned if s not in to_unblock]
            new_reasons = {k: v for k, v in reasons.items() if k not in to_unblock}
            with open(bl_file, 'w') as f:
                json.dump({'banned': sorted(new_banned), 'reasons': new_reasons,
                           'blocked_since': blocked_since}, f, indent=2)

            # Лог и TG
            log_func = self.knowledge._data.get('_log_func')
            msg = (f"🔓 <b>ИИ пересмотрел blacklist</b>\n"
                   f"Разблокировано: {', '.join(to_unblock)}\n"
                   f"Осталось: {len(new_banned)}\n"
                   f"Причина: {reason_txt}")
            if self.tg and getattr(self.tg, 'enabled', False):
                self.tg.send(msg)
            self.knowledge._data.setdefault('ai_learning_log', []).append({
                'ts': __import__('datetime').datetime.now().isoformat(),
                'type': 'blacklist_review',
                'unblocked': to_unblock,
                'reason': reason_txt,
            })
            self.knowledge._save()

        except Exception as e:
            pass  # тихо — не мешаем торговле

    # ══════════════════════════════════════════════════════════════
    # ДОРАБОТКА 2: Расширенный AI-score для скана монет
    # ══════════════════════════════════════════════════════════════
    def ai_score_symbol(self, sym: str, base_score: float,
                        adx: float, rsi: float, hour: int) -> tuple[float, str]:
        """
        [НОВОЕ] Расширенный AI-score для монеты в скане.
        [v288-C3] Добавлен кэш TTL=600 сек. Было: API вызов при каждом скане → лимит за 3-4 часа!
        """
        try:
            # [v288-C3] Проверяем кэш score (TTL=600 сек)
            _sc_key = f"{sym}_{int(adx)}_{int(rsi)}_{hour}"
            _now_sc = time.time()
            _cached_sc = self._score_cache.get(_sc_key)
            if _cached_sc and (_now_sc - _cached_sc['ts']) < self._score_cache_ttl:
                return _cached_sc['score'], _cached_sc['reason'] + " [cached]"

            trades = self.knowledge._data.get('trades', [])
            sym_trades = [t for t in trades if t.get('symbol') == sym]
            sym_wr = (sum(1 for t in sym_trades if t.get('outcome') == 'profit')
                      / max(1, len(sym_trades)) * 100)

            hour_stats = self.knowledge._data.get('hour_stats', {})
            h_data = hour_stats.get(str(hour), {})
            h_wr = h_data.get('wins', 0) / max(1, h_data.get('trades', 0)) * 100
            bad_hours = self.knowledge._data.get('bad_hours', [])

            sym_stats = self.knowledge._data.get('symbol_stats', {}).get(sym, {})
            min_safe_adx = float(sym_stats.get('min_safe_adx', 0))

            prompt = f"""
Ты оцениваешь монету для торговли. Базовый score={base_score:.1f}/15.
Дай итоговый score от 0 до 15 и причину (1 предложение).

МОНЕТА: {sym}
- ADX={adx:.1f} | RSI={rsi:.1f} | Час={hour}:00
- История {sym}: {len(sym_trades)} сделок, WR={sym_wr:.0f}%
- Этот час: WR={h_wr:.0f}% | Плохие часы: {bad_hours[:5]}
- Мин. безопасный ADX для {sym}: {min_safe_adx:.0f}

Правила:
- ADX < {min_safe_adx:.0f} для этой монеты → score снижай на 2
- Час в плохих часах → score снижай на 1.5
- WR монеты < 40% → score снижай на 1
- WR монеты > 70% → score повышай на 1

Ответь ТОЛЬКО JSON:
{{"score": число_0_15, "reason": "одна причина"}}"""

            # [FIX-v305-BUG] Вызывается СИНХРОННО из scan_market_top() для КАЖДОЙ монеты со
            # score>=6.0 в скане (торговый поток) — короткий timeout вместо 25с по умолчанию.
            text = self._call_api(prompt, max_tokens=80, timeout=4.0)
            if not text:
                return base_score, "api_unavailable"

            result = self._parse_json_response(text)
            if not result:
                return base_score, "parse_error"

            new_score = float(result.get('score', base_score))
            new_score = max(0.0, min(15.0, new_score))
            reason = str(result.get('reason', ''))
            # [v288-C3] Сохраняем в кэш
            self._score_cache[_sc_key] = {'score': new_score, 'reason': reason, 'ts': _now_sc}
            # Ограничиваем размер кэша
            if len(self._score_cache) > 100:
                _old_sk = min(self._score_cache, key=lambda k: self._score_cache[k]['ts'])
                del self._score_cache[_old_sk]
            return new_score, reason

        except Exception:
            return base_score, "error"
