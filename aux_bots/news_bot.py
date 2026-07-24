#!/usr/bin/env python3
"""
news_bot.py — v1.0 | Крипто-новостной анализатор
Работает вместе с spot_bot_v186.py и smc_bot.py

БЕСПЛАТНЫЕ API (ключи не нужны):
  - CryptoCompare News: https://min-api.cryptocompare.com/data/v2/news/
  - CoinGecko Trending: https://api.coingecko.com/api/v3/search/trending
  - RSS: CoinTelegraph, CoinDesk, Decrypt

Пишет: news_signals.json (читается v186 и smc_bot)
TG:    уведомления о важных новостях
"""

import os, json, time, re, requests, datetime
import xml.etree.ElementTree as ET

# ── Конфигурация ─────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNALS_FILE = os.path.join(BASE_DIR, "news_signals.json")

# [FIX-aux-SEC] Убран зашитый в код токен-фолбэк — см. тот же фикс в correlation_bot.py.
TG_TOKEN  = os.environ.get("TG_TOKEN", "")
TG_CHAT   = os.environ.get("TG_CHAT_ID", "")

SCAN_INTERVAL_SEC = 900   # каждые 15 минут
MAX_NEWS_AGE_H    = 4     # новости старше 4 часов не учитываем

# ── Ключевые слова для сентимента ────────────────────────────────
BULLISH_WORDS = [
    "approved","approval","etf","partnership","integration","launch",
    "listing","adoption","bullish","rally","surge","pump","all-time high",
    "ath","upgrade","mainnet","breakthrough","institutional","buy",
    "одобрен","листинг","рост","памп","бычий","инвестиции",
]
# Слова которые НЕЙТРАЛИЗУЮТ негатив (контекст)
NEUTRALIZE_WORDS = [
    "not hack","no scam","prevent hack","security upgrade",
    "fixed vulnerability","patch","recovered","no ban",
]

BEARISH_WORDS = [
    "hack","hacked","exploit","scam","fraud","ban","banned","restrict",
    "crash","dump","bearish","liquidation","bankruptcy","lawsuit",
    "regulation","crackdown","seized","arrest","vulnerability","exit scam",
    "взлом","бан","запрет","крах","мошенничество","ограничение",
]
# ВАЖНО: "sell" убрано из медвежьих — слишком часто даёт ложные сигналы
PANIC_WORDS = [
    "emergency","critical vulnerability","major hack","billion stolen",
    "market crash","systemic","collapse","fatal","catastrophic",
    "global ban","криптовалюты запрещены","глобальный запрет",
]
LISTING_WORDS = ["new listing","lists","will list","listing on binance","listing on bybit"]

# Монеты которые упоминаются часто → блокируем
COIN_PATTERNS = {
    # Топ монеты
    "BTC":"bitcoin|btc","ETH":"ethereum|eth","SOL":"solana|sol",
    "XRP":"ripple|xrp","BNB":"binance|bnb","ADA":"cardano|ada",
    "DOGE":"dogecoin|doge","AVAX":"avalanche|avax","DOT":"polkadot|dot",
    "LINK":"chainlink|link","MATIC":"polygon|matic","UNI":"uniswap|uni",
    "TON":"toncoin|ton","RENDER":"render|rndr","FET":"fetch|ai|fet",
    # Расширенный список
    "XLM":"stellar|xlm","POL":"polygon 2.0|pol ","FIL":"filecoin|fil",
    "HBAR":"hedera|hbar","NEAR":"near protocol|near","OP":"optimism|op ",
    "ARB":"arbitrum|arb","APT":"aptos|apt","SUI":"sui network|sui ",
    "INJ":"injective|inj","ATOM":"cosmos|atom","TIA":"celestia|tia",
    "PEPE":"pepe","SHIB":"shiba|shib","BONK":"bonk",
}

# ── TG уведомления ───────────────────────────────────────────────
_tg_throttle: dict = {}

def tg_send(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log(f"⚠️ [TG] {e}")

def tg_throttled(key: str, msg: str, cooldown: int = 3600):
    now = time.time()
    if now - _tg_throttle.get(key, 0) < cooldown:
        return
    _tg_throttle[key] = now
    tg_send(msg)

def log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"{ts} | {msg}")

# ── Источники новостей ───────────────────────────────────────────
def fetch_cryptocompare_news() -> list:
    """CryptoCompare News API — БЕСПЛАТНО, без ключа для базового доступа.
    Если хочешь больший лимит → получи бесплатный ключ на cryptocompare.com
    """
    try:
        url = "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&sortOrder=latest"
        r = requests.get(url, timeout=15, headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code == 200:
            data = r.json().get("Data", [])
            now = time.time()
            news = []
            for item in data[:30]:
                age_h = (now - item.get("published_on", 0)) / 3600
                if age_h <= MAX_NEWS_AGE_H:
                    news.append({
                        "title": item.get("title",""),
                        "body": item.get("body","")[:500],
                        "source": "cryptocompare",
                        "ts": item.get("published_on", 0),
                        "url": item.get("url",""),
                        "categories": item.get("categories",""),
                    })
            log(f"  📡 CryptoCompare: {len(news)} новостей")
            return news
    except Exception as e:
        log(f"  ⚠️ CryptoCompare: {e}")
    return []

def fetch_rss(url: str, source: str) -> list:
    """Читает RSS ленту новостей — полностью БЕСПЛАТНО."""
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        news = []
        now = time.time()
        for item in root.findall(".//item")[:20]:
            title = item.findtext("title","")
            desc  = item.findtext("description","")[:300]
            pub   = item.findtext("pubDate","")
            try:
                import email.utils
                ts = email.utils.mktime_tz(email.utils.parsedate_tz(pub)) if pub else now
            except Exception:
                ts = now
            age_h = (now - ts) / 3600
            if age_h <= MAX_NEWS_AGE_H:
                news.append({"title":title,"body":desc,"source":source,"ts":ts,"url":""})
        log(f"  📡 {source}: {len(news)} новостей")
        return news
    except Exception as e:
        log(f"  ⚠️ {source}: {e}")
    return []

def fetch_coingecko_trending() -> list:
    """CoinGecko trending coins — БЕСПЛАТНО без ключа."""
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search/trending",
                         timeout=15, headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code == 200:
            coins = r.json().get("coins",[])
            trending = [c["item"]["symbol"].upper()+"USDT" for c in coins[:7]]
            log(f"  📈 CoinGecko trending: {trending}")
            return trending
    except Exception as e:
        log(f"  ⚠️ CoinGecko: {e}")
    return []

# ── Анализ сентимента ────────────────────────────────────────────
def analyze_sentiment(news_list: list) -> dict:
    """Анализирует список новостей и возвращает:
    - global_sentiment: bullish/bearish/neutral/panic
    - blocked_coins: монеты с негативными новостями
    - boosted_coins: монеты с позитивными новостями
    - details: список важных новостей для TG
    """
    blocked_coins = set()
    boosted_coins = set()
    details = []
    bull_score = 0
    bear_score = 0
    panic_detected = False

    for item in news_list:
        text = (item.get("title","") + " " + item.get("body","")).lower()

        # Проверяем panic слова
        if any(w in text for w in PANIC_WORDS):
            panic_detected = True
            details.append(f"🚨 ПАНИКА: {item['title'][:80]}")

        # Считаем бычьи/медвежьи слова
        bull = sum(1 for w in BULLISH_WORDS if w in text)
        # Контекстная проверка: нейтрализующие слова убирают негатив
        bear_raw = sum(1 for w in BEARISH_WORDS if w in text)
        neutralized = sum(1 for w in NEUTRALIZE_WORDS if w in text)
        bear = max(0, bear_raw - neutralized)
        bull_score += bull
        bear_score += bear

        # Определяем монеты в новости
        coins_in_news = []
        for coin_sym, pattern in COIN_PATTERNS.items():
            if re.search(pattern, text, re.IGNORECASE):
                coins_in_news.append(coin_sym)

        # Листинг → не торгуем (памп уже прошёл или будет)
        if any(w in text for w in LISTING_WORDS):
            for coin in coins_in_news:
                blocked_coins.add(coin+"USDT")
            details.append(f"📋 ЛИСТИНГ: {item['title'][:80]}")

        # Взлом/скам → блокируем монету
        if any(w in text for w in ["hack","exploit","scam","fraud","взлом"]):
            for coin in coins_in_news:
                blocked_coins.add(coin+"USDT")
            if coins_in_news:
                details.append(f"⚠️ ВЗЛОМ/СКАМ: {item['title'][:80]}")

        # Позитивные новости → буст
        if bull > bear + 2 and bear == 0 and not panic_detected:
            for coin in coins_in_news:
                boosted_coins.add(coin+"USDT")

    # Определяем глобальный сентимент
    if panic_detected:
        global_sentiment = "panic"
    elif bear_score > bull_score * 1.5:
        global_sentiment = "bearish"
    elif bull_score > bear_score * 1.5:
        global_sentiment = "bullish"
    else:
        global_sentiment = "neutral"

    return {
        "global_sentiment": global_sentiment,
        "bull_score": bull_score,
        "bear_score": bear_score,
        "blocked_coins": sorted(blocked_coins),
        "boosted_coins": sorted(boosted_coins - blocked_coins),
        "panic": panic_detected,
        "details": details[:5],  # топ 5 новостей
    }

# ── Сохранение сигналов ──────────────────────────────────────────
def save_signals(result: dict, trending: list):
    data = {
        "version": "1.0",
        "updated": datetime.datetime.now().isoformat(),
        "updated_ts": time.time(),
        "source": "news_bot_v187",
        "global_sentiment": result["global_sentiment"],
        "bull_score": result["bull_score"],
        "bear_score": result["bear_score"],
        "blocked_coins": result["blocked_coins"],
        "boosted_coins": result["boosted_coins"],
        "trending_coins": trending,
        "panic": result["panic"],
        "details": result["details"],
    }
    # [FIX-aux-ATOMIC] См. тот же фикс в correlation_bot.py.
    _tmp = SIGNALS_FILE + ".tmp"
    with open(_tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(_tmp, SIGNALS_FILE)
    log(f"💾 news_signals.json обновлён")

# ── TG уведомления ───────────────────────────────────────────────
def send_tg_report(result: dict, trending: list):
    sentiment_emoji = {
        "bullish": "🟢", "bearish": "🔴",
        "neutral": "🟡", "panic": "🚨"
    }
    emoji = sentiment_emoji.get(result["global_sentiment"], "⚪")

    if result["panic"]:
        msg = (
            f"🚨 <b>КРИПТО ПАНИКА — ТОРГОВЛЯ ПРИОСТАНОВЛЕНА</b>\n"
            f"v186 и smc_bot автоматически приостановят торговлю\n\n"
            + "\n".join(result["details"][:3])
        )
        tg_send(msg)
        return

    if result["global_sentiment"] in ("bullish","bearish"):
        msg = (
            f"📰 <b>NEWS BOT | {result['global_sentiment'].upper()}</b> {emoji}\n"
            f"{'='*30}\n"
            f"🟢 Бычьих сигналов: {result['bull_score']}\n"
            f"🔴 Медвежьих сигналов: {result['bear_score']}\n"
        )
        if result["blocked_coins"]:
            msg += f"\n⛔ Заблокированы: {', '.join(result['blocked_coins'][:5])}"
        if result["boosted_coins"]:
            msg += f"\n✅ Положительный фон: {', '.join(result['boosted_coins'][:5])}"
        if trending:
            msg += f"\n📈 Trending: {', '.join(trending[:5])}"
        if result["details"]:
            msg += f"\n\n<b>Важные новости:</b>\n" + "\n".join(result["details"][:3])
        tg_throttled("news_report", msg, cooldown=3600)

# ── Главный цикл ─────────────────────────────────────────────────
def main():
    log("="*60)
    log("  📰 NEWS BOT v1.0 ЗАПУЩЕН")
    log(f"  Сканирую новости каждые {SCAN_INTERVAL_SEC//60} минут")
    log(f"  Сигналы → {SIGNALS_FILE}")
    log("="*60)
    tg_send(
        "📰 <b>News Bot v1.0 запущен</b>\n"
        "Сканирую крипто-новости каждые 15 мин\n"
        "Работаю вместе с v186 и smc_bot ✅\n\n"
        "Источники: CryptoCompare, CoinTelegraph, CoinDesk, CoinGecko"
    )

    scan_num = 0
    while True:
        scan_num += 1
        log(f"\n{'─'*50}")
        log(f"📡 СКАН #{scan_num} | {datetime.datetime.now().strftime('%H:%M:%S')}")

        try:
            # Собираем новости из всех источников
            all_news = []
            all_news += fetch_cryptocompare_news()
            all_news += fetch_rss(
                "https://cointelegraph.com/rss",
                "CoinTelegraph"
            )
            all_news += fetch_rss(
                "https://www.coindesk.com/arc/outboundfeeds/rss/",
                "CoinDesk"
            )
            trending = fetch_coingecko_trending()

            log(f"  Всего новостей: {len(all_news)}")

            # Анализируем сентимент
            result = analyze_sentiment(all_news)

            log(f"  Сентимент: {result['global_sentiment'].upper()} "
                f"(🟢{result['bull_score']} 🔴{result['bear_score']})")

            if result["blocked_coins"]:
                log(f"  Блок: {result['blocked_coins']}")
            if result["boosted_coins"]:
                log(f"  Буст: {result['boosted_coins']}")
            if result["panic"]:
                log(f"  🚨 ПАНИКА ОБНАРУЖЕНА!")

            # Сохраняем и отправляем
            save_signals(result, trending)
            send_tg_report(result, trending)

        except Exception as e:
            log(f"  ❌ Ошибка скана: {e}")

        log(f"  Следующий скан через {SCAN_INTERVAL_SEC//60} мин")
        time.sleep(SCAN_INTERVAL_SEC)

if __name__ == "__main__":
    main()
