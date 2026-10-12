"""Sabah Özeti botu.

- Her sabah SEND_AT saatinde hava, kur, takvim, deprem ve sunucu özetini Telegram'a yollar.
- Türkiye'de M≥QUAKE_MIN_MAG deprem olunca (özetten ayrı) anında haber verir.
- Komutlar: /ozet, /deprem, /yardim
"""
import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI

import sources as src

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
SEND_AT = os.getenv("SEND_AT", "08:00")
CATCHUP_HOURS = 2                        # açılış SEND_AT'ten en fazla bu kadar sonraysa yine gönder
CALENDAR_ICS = os.getenv("CALENDAR_ICS", "")
WATCH_URLS = [u.strip() for u in os.getenv("WATCH_URLS", "").split(",") if u.strip()]
QUAKE_POLL_SEC = int(os.getenv("QUAKE_POLL_SEC", "60"))
DATA_DIR = os.getenv("DATA_DIR", "/data")
STATE_PATH = os.path.join(DATA_DIR, "state.json")

log = logging.getLogger("sabah")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # URL'de bot token'ı var, günlüğe yazılmasın

state: dict = {}
health = {"started": time.time(), "last_quake_poll": None, "last_summary": None}
background: set = set()


# ---------- Durum ----------
def load_state() -> None:
    global state
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}


def save_state() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)          # yarım yazılmış dosya kalmasın


# ---------- Telegram ----------
async def send(text: str, chat_id: int = CHAT_ID) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True})
            r.raise_for_status()
        log.info("Telegram'a gönderildi: %s", text.split("\n")[0])
    except Exception as e:  # str(e) token'lı URL içerebilir; yalnızca türü yaz
        log.error("Telegram gönderilemedi (%s)", type(e).__name__)


async def send_summary(chat_id: int = CHAT_ID) -> None:
    text = await src.build_summary(state, CALENDAR_ICS, WATCH_URLS)
    save_state()                         # kur geçmişi güncellendi
    await send(text, chat_id)
    health["last_summary"] = time.time()


# ---------- Zamanlayıcı ----------
def due(now: datetime, last_sent: str | None) -> bool:
    """Bugünün özeti gönderilmeli mi? Geç açılışta en fazla CATCHUP_HOURS saat telafi eder."""
    hh, mm = map(int, SEND_AT.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return last_sent != now.date().isoformat() and target <= now < target + timedelta(hours=CATCHUP_HOURS)


async def scheduler_loop() -> None:
    while True:
        now = datetime.now(src.TZ)
        if due(now, state.get("last_sent")):
            state["last_sent"] = now.date().isoformat()
            save_state()
            try:
                await send_summary()
            except Exception as e:
                log.error("Özet hazırlanamadı (%s)", type(e).__name__)
        await asyncio.sleep(20)


# ---------- Deprem takibi ----------
async def quake_loop() -> None:
    first = "seen_quakes" not in state  # ilk çalıştırmada eski depremler için mesaj atma
    seen = set(state.get("seen_quakes", []))
    while True:
        try:
            async with httpx.AsyncClient(timeout=25, headers={"User-Agent": "sabah-ozeti/1.0"}) as c:
                qs = await src.quakes(c, hours=6)
            new = [e for e in reversed(qs) if e["eventID"] not in seen]   # eskiden yeniye
            for e in new:
                seen.add(e["eventID"])
                if not first:
                    await send(src.quake_alert(e))
            if new or first:
                state["seen_quakes"] = sorted(seen, key=int)[-500:]
                save_state()
            first = False
            health["last_quake_poll"] = time.time()
        except Exception as e:
            log.error("Deprem sorgusu başarısız (%s)", type(e).__name__)
        await asyncio.sleep(QUAKE_POLL_SEC)


# ---------- Komutlar ----------
HELP = (
    f"Her sabah {SEND_AT}'de özet gönderirim; Türkiye'de M≥{src.QUAKE_MIN_MAG:g} deprem olunca ayrıca haber veririm.\n\n"
    "/ozet – özeti şimdi gönder\n/deprem – son 24 saatin depremleri\n/yardim – bu mesaj"
)
COMMANDS = [
    {"command": "ozet", "description": "Özeti şimdi gönder"},
    {"command": "deprem", "description": "Son 24 saatin depremleri"},
    {"command": "yardim", "description": "Yardım"},
]


async def quake_list(chat_id: int) -> None:
    try:
        async with httpx.AsyncClient(timeout=25, headers={"User-Agent": "sabah-ozeti/1.0"}) as c:
            await send(src.quakes_section(await src.quakes(c, hours=24)), chat_id)
    except Exception as e:
        await send(f"Deprem verisi alınamadı ({type(e).__name__}).", chat_id)


def spawn(coro) -> None:
    t = asyncio.create_task(coro)
    background.add(t)
    t.add_done_callback(background.discard)


async def telegram_loop() -> None:
    base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"{base}/setMyCommands", json={"commands": COMMANDS})
    except Exception as e:
        log.error("Komut menüsü ayarlanamadı (%s)", type(e).__name__)
    offset = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=40) as c:
                data = (await c.get(f"{base}/getUpdates", params={"timeout": 30, "offset": offset})).json()
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = msg.get("chat", {}).get("id")
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                if chat_id != CHAT_ID:
                    await send("Bu bot özeldir.", chat_id)
                    continue
                cmd = text.split()[0].split("@")[0].lower()
                if cmd == "/ozet":
                    await send("⏳ Hazırlanıyor…", chat_id)
                    spawn(send_summary(chat_id))
                elif cmd == "/deprem":
                    spawn(quake_list(chat_id))
                elif cmd in ("/yardim", "/yardım", "/start", "/help"):
                    await send(HELP, chat_id)
        except Exception as e:
            log.error("Telegram polling hatası (%s)", type(e).__name__)
            await asyncio.sleep(5)


# ---------- Uygulama ----------
@asynccontextmanager
async def lifespan(app: FastAPI):
    load_state()
    tasks = [asyncio.create_task(f()) for f in (scheduler_loop, quake_loop, telegram_loop)]
    log.info("Sabah Özeti başladı (gönderim %s, deprem eşiği M%s, %d site izleniyor, takvim %s)",
             SEND_AT, src.QUAKE_MIN_MAG, len(WATCH_URLS), "açık" if CALENDAR_ICS else "kapalı")
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Sabah Özeti", lifespan=lifespan)


@app.get("/health")
async def health_check():
    ok = health["last_quake_poll"] is None or time.time() - health["last_quake_poll"] < QUAKE_POLL_SEC * 5
    return {"ok": ok, **health}
