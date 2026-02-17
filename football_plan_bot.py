# football_plan_bot.py
# BASE: Daily Plan (Top-5 + UEFA) + Smart Sleeping + Live Signals + Post-match Result
# Official API-Football (api-sports.io) + Telegram
#
# Render/GitHub-ready: secrets are read from environment variables:
#   APISPORTS_KEY
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_CHAT_ID

import time
import json
import os
import csv
import requests
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

# =========================
# ENV (Render variables)
# =========================
APISPORTS_KEY = os.getenv("APISPORTS_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# =========================
# API SETTINGS
# =========================
BASE_URL = "https://v3.football.api-sports.io"
TIMEZONE = "Europe/Chisinau"

def api_headers() -> Dict[str, str]:
    return {"x-apisports-key": APISPORTS_KEY}

# =========================
# STRATEGY SETTINGS
# =========================
MIN_MINUTE = 78
MAX_MINUTE = 86
MAX_SIGNALS_PER_MATCH = 1
MAX_SIGNALS_PER_DAY = 25

# Smart sleeping:
ACTIVE_FROM_MIN = 65
ACTIVE_TO_MIN = 95

POLL_SECONDS_ACTIVE = 90
SLEEP_CHUNK_SECONDS = 600  # 10 minutes

# =========================
# COMPETITION FILTER (Top-5 + UEFA)
# =========================
TOP5 = {
    ("Premier League", "England"),
    ("La Liga", "Spain"),
    ("Serie A", "Italy"),
    ("Bundesliga", "Germany"),
    ("Ligue 1", "France"),
}

UEFA = {
    ("UEFA Champions League", "World"),
    ("UEFA Europa League", "World"),
    ("UEFA Europa Conference League", "World"),
}

UEFA_NAMES = {name for (name, _) in UEFA}

# =========================
# FILES
# =========================
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

STATE_FILE = os.path.join(DATA_DIR, "state.json")
CSV_FILE = os.path.join(DATA_DIR, "signals.csv")

# =========================
# TIME (timezone-aware)
# =========================
def now_dt() -> datetime:
    return datetime.now().astimezone()

def now_str() -> str:
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")

def log(msg: str) -> None:
    print(f"[{now_str()}] {msg}", flush=True)

def safe_int(x, default=0) -> int:
    try:
        return int(x)
    except Exception:
        return default

def http_get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{BASE_URL}{path}"
    r = requests.get(url, headers=api_headers(), params=params, timeout=25)
    r.raise_for_status()
    return r.json()

def tg_send(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    requests.post(url, json=payload, timeout=25).raise_for_status()

def ensure_csv() -> None:
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "bet_id", "time", "fixture_id", "league", "country",
                "home", "away", "minute", "score",
                "bet_type", "line", "notes", "result"
            ])

def append_csv(row: List[Any]) -> None:
    ensure_csv()
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)

def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "sent_per_match": {},
        "open_bets": {},
        "signals_today": 0,
        "signals_today_date": now_dt().strftime("%Y-%m-%d"),
        "plan_date": "",
        "plan": [],
    }

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def reset_daily_if_needed(state: Dict[str, Any]) -> None:
    today = now_dt().strftime("%Y-%m-%d")
    if state.get("signals_today_date") != today:
        state["signals_today_date"] = today
        state["signals_today"] = 0
        save_state(state)
        log("Daily counters reset.")

# =========================
# API FUNCTIONS
# =========================
def get_fixtures_by_date(date_yyyy_mm_dd: str) -> List[Dict[str, Any]]:
    data = http_get("/fixtures", {"date": date_yyyy_mm_dd, "timezone": TIMEZONE})
    return data.get("response", [])

def get_live_fixtures() -> List[Dict[str, Any]]:
    data = http_get("/fixtures", {"live": "all"})
    return data.get("response", [])

def get_fixture_by_id(fixture_id: int) -> Optional[Dict[str, Any]]:
    data = http_get("/fixtures", {"id": fixture_id})
    resp = data.get("response", [])
    return resp[0] if resp else None

# =========================
# FILTERS / LOGIC
# =========================
def is_target_competition(fx: Dict[str, Any]) -> bool:
    league = fx.get("league", {}) or {}
    name = (league.get("name") or "").strip()
    country = (league.get("country") or "").strip()

    if (name, country) in TOP5:
        return True

    if name in UEFA_NAMES:
        return True

    return False

def parse_fixture_start_local(fx: Dict[str, Any]) -> Optional[datetime]:
    date_s = (fx.get("fixture", {}) or {}).get("date")
    if not date_s:
        return None
    try:
        return datetime.fromisoformat(date_s)
    except Exception:
        return None

def current_score(fx: Dict[str, Any]) -> Tuple[int, int, int]:
    goals = fx.get("goals", {}) or {}
    h = safe_int(goals.get("home"), 0)
    a = safe_int(goals.get("away"), 0)
    return h, a, h + a

def choose_line(total_goals: int) -> float:
    return total_goals + 0.5

def pick_signal_basic(fx: Dict[str, Any], minute: int) -> Optional[Tuple[str, float, str]]:
    h, a, total = current_score(fx)
    diff = abs(h - a)
    line = choose_line(total)

    if total >= 6:
        return None

    if diff <= 1:
        return ("OVER", line, "близкий счёт (ничья/1 гол разницы) → чаще идёт давление на гол")
    return ("UNDER", line, "разрыв 2+ гола → часто команды доигрывают, шанс без гола выше")

def build_signal_message(fx: Dict[str, Any], minute: int, bet_type: str, line: float, notes: str) -> str:
    league = fx.get("league", {}).get("name", "Unknown League")
    country = fx.get("league", {}).get("country", "")
    home = fx.get("teams", {}).get("home", {}).get("name", "Home")
    away = fx.get("teams", {}).get("away", {}).get("name", "Away")
    h, a, _ = current_score(fx)
    score = f"{h}-{a}"

    return (
        f"⚽ LIVE СИГНАЛ (Top-5 + UEFA)\n"
        f"Лига: {league} ({country})\n"
        f"Матч: {home} vs {away}\n"
        f"Минута: {minute}' | Счёт: {score}\n\n"
        f"✅ Куда ставить: Тотал матча (Full Time)\n"
        f"✅ Что ставить: {bet_type} {line}\n"
        f"🎯 Коэф: проверь у букмекера (цель на тесте: 1.20–1.45)\n"
        f"📝 Причина: {notes}\n\n"
        f"⚠️ Фикс ставка 2–3% банка. Без догонов."
    )

def eval_result(final_total: int, bet_type: str, line: float) -> str:
    if bet_type == "OVER":
        return "WIN" if final_total > line else "LOSE"
    if bet_type == "UNDER":
        return "WIN" if final_total < line else "LOSE"
    return "UNKNOWN"

# =========================
# PLANNING
# =========================
def build_24h_plan() -> List[Dict[str, Any]]:
    now = now_dt()
    today = now.strftime("%Y-%m-%d")
    tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    fixtures: List[Dict[str, Any]] = []
    fixtures.extend(get_fixtures_by_date(today))
    fixtures.extend(get_fixtures_by_date(tomorrow))

    plan = []
    for fx in fixtures:
        if not is_target_competition(fx):
            continue

        start = parse_fixture_start_local(fx)
        if not start:
            continue

        if start < now:
            continue
        if start > now + timedelta(hours=24):
            continue

        fixture_id = safe_int((fx.get("fixture", {}) or {}).get("id"), 0)
        if fixture_id <= 0:
            continue

        league = fx.get("league", {}) or {}
        home = fx.get("teams", {}).get("home", {}).get("name", "Home")
        away = fx.get("teams", {}).get("away", {}).get("name", "Away")

        plan.append({
            "fixture_id": fixture_id,
            "start_iso": start.isoformat(),
            "league": (league.get("name") or ""),
            "country": (league.get("country") or ""),
            "home": home,
            "away": away,
        })

    plan.sort(key=lambda x: x["start_iso"])
    return plan

def send_plan_to_telegram(plan: List[Dict[str, Any]]) -> None:
    if not plan:
        tg_send("📅 План на 24 часа: матчей Top-5 + UEFA не найдено.")
        return

    lines = [f"📅 План на 24 часа (Top-5 + UEFA): {len(plan)} матч(ей)\n"]
    show = plan[:25]
    for p in show:
        start = datetime.fromisoformat(p["start_iso"])
        t = start.strftime("%d.%m %H:%M")
        lines.append(f"• {t} — {p['home']} vs {p['away']} ({p['league']})")

    if len(plan) > 25:
        lines.append(f"\n…и ещё {len(plan) - 25} матч(ей)")

    tg_send("\n".join(lines))

def next_activation_time(plan: List[Dict[str, Any]]) -> Optional[datetime]:
    now = now_dt()
    best = None
    for p in plan:
        start = datetime.fromisoformat(p["start_iso"])
        active_from = start + timedelta(minutes=ACTIVE_FROM_MIN)
        active_to = start + timedelta(minutes=ACTIVE_TO_MIN)

        if now <= active_to:
            if active_from <= now <= active_to:
                return now
            if now < active_from:
                if best is None or active_from < best:
                    best = active_from
    return best

def is_any_match_active_now(plan: List[Dict[str, Any]]) -> bool:
    now = now_dt()
    for p in plan:
        start = datetime.fromisoformat(p["start_iso"])
        if start + timedelta(minutes=ACTIVE_FROM_MIN) <= now <= start + timedelta(minutes=ACTIVE_TO_MIN):
            return True
    return False

# =========================
# MAIN
# =========================
def main():
    if not APISPORTS_KEY or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("❌ Missing env vars: APISPORTS_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return

    state = load_state()
    ensure_csv()

    tg_send("✅ Бот запущен (Render/GitHub, режим: план на 24 часа + умный сон).")
    log("Bot started.")

    while True:
        try:
            reset_daily_if_needed(state)

            # 1) Build plan once per day
            today = now_dt().strftime("%Y-%m-%d")
            if state.get("plan_date") != today or not state.get("plan"):
                log("Building 24h plan...")
                plan = build_24h_plan()
                state["plan"] = plan
                state["plan_date"] = today
                save_state(state)
                send_plan_to_telegram(plan)
                log(f"Plan size: {len(plan)}")

            plan = state.get("plan", [])

            # 2) Close finished bets
            finished_ids = []
            for bet_id, bet in list(state["open_bets"].items()):
                fx = get_fixture_by_id(bet["fixture_id"])
                if not fx:
                    continue

                status = (fx.get("fixture", {}) or {}).get("status", {}).get("short", "")
                if status in ("FT", "AET", "PEN"):
                    h, a, total = current_score(fx)
                    result = eval_result(total, bet["bet_type"], bet["line"])

                    tg_send(
                        f"🏁 Матч завершён\n"
                        f"{bet['home']} vs {bet['away']}\n"
                        f"Финальный счёт: {h}-{a}\n"
                        f"Наша ставка: {bet['bet_type']} {bet['line']}\n"
                        f"Итог: {'✅ ЗАШЛА' if result=='WIN' else '❌ НЕ ЗАШЛА'}"
                    )

                    append_csv([
                        bet_id, bet["time"], bet["fixture_id"],
                        bet["league"], bet["country"],
                        bet["home"], bet["away"], bet["minute"], bet["score"],
                        bet["bet_type"], bet["line"], bet["notes"], result
                    ])
                    finished_ids.append(bet_id)
                    log(f"Bet closed: {bet_id} => {result}")

            for bet_id in finished_ids:
                state["open_bets"].pop(bet_id, None)
            save_state(state)

            # 3) Daily cap
            if state["signals_today"] >= MAX_SIGNALS_PER_DAY:
                log("Daily cap reached. Sleeping...")
                time.sleep(SLEEP_CHUNK_SECONDS)
                continue

            # 4) If no plan
            if not plan:
                time.sleep(SLEEP_CHUNK_SECONDS)
                continue

            # 5) Sleep until active window
            if not is_any_match_active_now(plan):
                nxt = next_activation_time(plan)
                if nxt is None:
                    time.sleep(SLEEP_CHUNK_SECONDS)
                    continue

                seconds = max(5, int((nxt - now_dt()).total_seconds()))
                log(f"Sleeping until next activation (~{seconds}s)...")
                time.sleep(min(seconds, SLEEP_CHUNK_SECONDS))
                continue

            # 6) Active mode: poll live fixtures
            fixtures = get_live_fixtures()

            plan_ids = {p["fixture_id"] for p in plan}
            fired = 0

            for fx in fixtures:
                if not is_target_competition(fx):
                    continue

                fixture = fx.get("fixture", {}) or {}
                fixture_id = safe_int(fixture.get("id"), 0)
                if fixture_id <= 0:
                    continue

                if fixture_id not in plan_ids:
                    continue

                minute = safe_int((fixture.get("status", {}) or {}).get("elapsed"), 0)
                if minute < MIN_MINUTE or minute > MAX_MINUTE:
                    continue

                sent_count = state["sent_per_match"].get(str(fixture_id), 0)
                if sent_count >= MAX_SIGNALS_PER_MATCH:
                    continue

                pick = pick_signal_basic(fx, minute)
                if not pick:
                    continue

                bet_type, line, notes = pick
                tg_send(build_signal_message(fx, minute, bet_type, line, notes))

                teams = fx.get("teams", {}) or {}
                league = fx.get("league", {}) or {}

                home = teams.get("home", {}).get("name", "Home")
                away = teams.get("away", {}).get("name", "Away")
                h, a, _ = current_score(fx)
                score = f"{h}-{a}"

                bet_id = f"{fixture_id}-{int(time.time())}"
                state["open_bets"][bet_id] = {
                    "bet_id": bet_id,
                    "time": now_str(),
                    "fixture_id": fixture_id,
                    "league": (league.get("name") or ""),
                    "country": (league.get("country") or ""),
                    "home": home,
                    "away": away,
                    "minute": minute,
                    "score": score,
                    "bet_type": bet_type,
                    "line": line,
                    "notes": notes
                }

                state["sent_per_match"][str(fixture_id)] = sent_count + 1
                state["signals_today"] += 1
                save_state(state)
                fired += 1

                if state["signals_today"] >= MAX_SIGNALS_PER_DAY:
                    break

            log(f"Active tick: live={len(fixtures)}, fired={fired}")
            time.sleep(POLL_SECONDS_ACTIVE)

        except requests.HTTPError as e:
            log(f"HTTPError: {e}")
            time.sleep(POLL_SECONDS_ACTIVE)
        except Exception as e:
            log(f"ERROR: {e}")
            time.sleep(POLL_SECONDS_ACTIVE)

if __name__ == "__main__":
    main()
