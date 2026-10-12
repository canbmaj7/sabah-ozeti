"""Sabah özetinin veri kaynakları ve biçimlendirmesi.

Hepsi anahtarsız, ücretsiz kaynaklar:
- Hava: Open-Meteo
- Kur: TCMB today.xml
- Deprem: AFAD
- Takvim: Google Takvim "gizli iCal adresi" (isteğe bağlı)
- Sunucu: konteynerin gördüğü ana makine bilgileri + site kontrolleri
"""
import asyncio
import math
import os
import re
import shutil
import socket
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo(os.getenv("TZ", "Europe/Istanbul"))
CITY = os.getenv("CITY", "Gebze")
LAT = float(os.getenv("LAT", "40.8028"))
LON = float(os.getenv("LON", "29.4307"))
QUAKE_MIN_MAG = float(os.getenv("QUAKE_MIN_MAG", "4.0"))
QUAKE_MAX_OFFSHORE_KM = float(os.getenv("QUAKE_MAX_OFFSHORE_KM", "100"))
AFAD_URL = "https://servisnet.afad.gov.tr/apigateway/deprem/apiv2/event/filter"

GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
         "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]

# WMO hava kodu → (emoji, açıklama)
WMO = {
    0: ("☀️", "Açık"), 1: ("🌤", "Çoğunlukla açık"), 2: ("⛅", "Parçalı bulutlu"), 3: ("☁️", "Kapalı"),
    45: ("🌫", "Sisli"), 48: ("🌫", "Kırağılı sis"),
    51: ("🌦", "Hafif çisenti"), 53: ("🌦", "Çisenti"), 55: ("🌧", "Yoğun çisenti"),
    61: ("🌦", "Hafif yağmur"), 63: ("🌧", "Yağmur"), 65: ("🌧", "Kuvvetli yağmur"),
    66: ("🌧", "Dondurucu yağmur"), 67: ("🌧", "Kuvvetli dondurucu yağmur"),
    71: ("🌨", "Hafif kar"), 73: ("🌨", "Kar"), 75: ("❄️", "Yoğun kar"), 77: ("🌨", "Kar taneleri"),
    80: ("🌦", "Hafif sağanak"), 81: ("🌧", "Sağanak"), 82: ("⛈", "Şiddetli sağanak"),
    85: ("🌨", "Kar sağanağı"), 86: ("❄️", "Yoğun kar sağanağı"),
    95: ("⛈", "Gök gürültülü fırtına"), 96: ("⛈", "Dolu ile fırtına"), 99: ("⛈", "Şiddetli dolu ile fırtına"),
}


def tr_num(v: float, nd: int = 2) -> str:
    return f"{v:,.{nd}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def tr_date(d: datetime) -> str:
    return f"{d.day} {AYLAR[d.month - 1]} {GUNLER[d.weekday()]}"


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------- Hava ----------
async def weather(c: httpx.AsyncClient) -> str:
    r = await c.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": LAT, "longitude": LON, "timezone": str(TZ), "forecast_days": 1,
        "current": "temperature_2m,apparent_temperature,weather_code",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max,sunrise,sunset",
    })
    r.raise_for_status()
    d = r.json()
    cur, day = d["current"], {k: v[0] for k, v in d["daily"].items()}
    emoji, text = WMO.get(day["weather_code"], ("🌡", "Bilinmiyor"))
    lines = [
        f"{emoji} Hava · {CITY}",
        f"{text}, şimdi {round(cur['temperature_2m'])}° (hissedilen {round(cur['apparent_temperature'])}°)",
        f"En yüksek {round(day['temperature_2m_max'])}° / en düşük {round(day['temperature_2m_min'])}°"
        f" · yağış %{day['precipitation_probability_max']} · rüzgâr {round(day['wind_speed_10m_max'])} km/sa",
        f"🌅 {day['sunrise'][-5:]} · 🌇 {day['sunset'][-5:]}",
    ]
    if (day["precipitation_probability_max"] or 0) >= 50:
        lines.append("☔ Şemsiyeni al.")
    if day["temperature_2m_min"] <= 2:
        lines.append("🧣 Don/buzlanma olabilir, sıkı giyin.")
    return "\n".join(lines)


# ---------- Kur ----------
async def rates(c: httpx.AsyncClient, state: dict) -> str:
    r = await c.get("https://www.tcmb.gov.tr/kurlar/today.xml")
    r.raise_for_status()
    root = ET.fromstring(r.content)
    tarih = root.get("Tarih", "")
    now = {}
    for cur in root.findall("Currency"):
        kod = cur.get("Kod")
        if kod in ("USD", "EUR", "GBP"):
            now[kod] = float(cur.findtext("ForexSelling") or 0)
    hist = state.setdefault("rates", {})          # {"tarih": "...", "values": {...}, "prev": {...}}
    if hist.get("tarih") != tarih:                # yeni iş günü: dünkü değerleri sakla
        hist["prev"] = hist.get("values", {})
        hist["tarih"], hist["values"] = tarih, now
    prev = hist.get("prev", {})
    lines = [f"💱 Kur (TCMB, {tarih[:5]})"]
    for kod, val in now.items():
        line = f"{kod} {tr_num(val)} ₺"
        if prev.get(kod):
            ch = (val - prev[kod]) / prev[kod] * 100
            line += f" ({'▲' if ch > 0 else '▼' if ch < 0 else '='} %{tr_num(abs(ch))})"
        lines.append(line)
    return "\n".join(lines)


# ---------- Deprem ----------
def offshore_km(location: str) -> float:
    """AFAD konum metnindeki '[165.21 km]' gibi kıyı uzaklığını döndürür (yoksa 0)."""
    m = re.search(r"\[(\d+(?:\.\d+)?) km\]", location or "")
    return float(m.group(1)) if m else 0.0


def in_turkey(e: dict) -> bool:
    return e.get("country") == "Türkiye" and offshore_km(e.get("location", "")) <= QUAKE_MAX_OFFSHORE_KM


async def quakes(c: httpx.AsyncClient, hours: float = 24, min_mag: float = QUAKE_MIN_MAG) -> list[dict]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    r = await c.get(AFAD_URL, params={
        "start": start.strftime("%Y-%m-%dT%H:%M:%S"), "end": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "minmag": min_mag, "orderby": "timedesc", "limit": 100, "format": "json",
    }, follow_redirects=True)
    r.raise_for_status()
    out = []
    for e in r.json():
        try:
            e["mag"] = float(e["magnitude"])
            e["lat"], e["lon"] = float(e["latitude"]), float(e["longitude"])
            e["when"] = datetime.fromisoformat(e["date"]).replace(tzinfo=timezone.utc).astimezone(TZ)  # AFAD tarihleri UTC
            e["dist"] = distance_km(LAT, LON, e["lat"], e["lon"])
        except (KeyError, ValueError, TypeError):
            continue
        if e["mag"] >= min_mag and in_turkey(e):
            out.append(e)
    return out


def quake_place(e: dict) -> str:
    return re.sub(r"\s*-?\s*\[\d+(?:\.\d+)? km\]\s*", " ", e.get("location", "")).strip() or e.get("province") or "?"


def quake_alert(e: dict) -> str:
    icon = "🆘" if e["mag"] >= 6 else "🔴" if e["mag"] >= 5 else "🟠"
    lines = [
        f"{icon} Deprem · M{e['mag']:.1f}",
        f"📍 {quake_place(e)}",
        f"🕐 {e['when'].strftime('%d.%m %H:%M:%S')} · derinlik {tr_num(float(e.get('depth') or 0), 1)} km",
        f"📏 {CITY}'ye ~{round(e['dist'])} km",
    ]
    if e["dist"] < 100:
        lines.insert(1, f"⚠️ {CITY}'ye yakın!")
    lines.append(f"🗺 https://maps.google.com/?q={e['lat']},{e['lon']}")
    return "\n".join(lines)


def quakes_section(qs: list[dict]) -> str:
    title = f"🌍 Deprem (son 24 sa, M≥{QUAKE_MIN_MAG:g})"
    if not qs:
        return f"{title}\nYok 🙏"
    lines = [title]
    for e in qs[:8]:
        lines.append(f"{e['when'].strftime('%H:%M')} M{e['mag']:.1f} {quake_place(e)} · {round(e['dist'])} km")
    if len(qs) > 8:
        lines.append(f"… ve {len(qs) - 8} deprem daha")
    return "\n".join(lines)


# ---------- Takvim ----------
async def calendar(c: httpx.AsyncClient, url: str, day: datetime) -> str:
    import icalendar
    import recurring_ical_events

    r = await c.get(url, follow_redirects=True)
    r.raise_for_status()
    cal = icalendar.Calendar.from_ical(r.content)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    events = recurring_ical_events.of(cal).between(start, start + timedelta(days=1))
    items = []
    for ev in events:
        dt = ev.get("DTSTART").dt
        title = str(ev.get("SUMMARY") or "(başlıksız)")[:80]
        if isinstance(dt, datetime):
            dt = dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)
            items.append((dt.strftime("%H:%M"), title))
        else:
            items.append(("Tüm gün", title))
    items.sort(key=lambda x: (x[0] != "Tüm gün", x[0]))
    if not items:
        return "📅 Bugün\nTakvimde etkinlik yok."
    return "📅 Bugün\n" + "\n".join(f"{t} · {s}" for t, s in items[:12])


# ---------- Sunucu ----------
def host_stats() -> str:
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            mem[k] = int(v.split()[0])
    ram = 100 * (1 - mem["MemAvailable"] / mem["MemTotal"])
    du = shutil.disk_usage(os.getenv("DATA_DIR", "/data") if os.path.isdir(os.getenv("DATA_DIR", "/data")) else "/")
    load = os.getloadavg()[0]
    with open("/proc/uptime") as f:
        up_days = float(f.read().split()[0]) / 86400
    return (f"RAM %{round(ram)} · Disk %{round(100 * du.used / du.total)} "
            f"· Yük {tr_num(load)} ({os.cpu_count()} çekirdek) · {up_days:.0f} gündür açık")


def tls_days_left(host: str, port: int = 443) -> int | None:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=6) as s:
            with ctx.wrap_socket(s, server_hostname=host) as t:
                exp = ssl.cert_time_to_seconds(t.getpeercert()["notAfter"])
        return int((exp - time.time()) // 86400)
    except Exception:
        return None


async def check_site(c: httpx.AsyncClient, url: str) -> str:
    host = urlparse(url).hostname or url
    t0 = time.monotonic()
    try:
        r = await c.get(url, follow_redirects=True, timeout=10)
        ms = round((time.monotonic() - t0) * 1000)
        ok = r.status_code < 500
        line = f"{'✅' if ok else '❌'} {host} · {r.status_code} · {ms} ms"
    except Exception as e:
        return f"❌ {host} · {type(e).__name__}"
    if url.startswith("https://"):
        days = await asyncio.to_thread(tls_days_left, host)
        if days is not None:
            line += f" · SSL {days} gün" + (" ⚠️" if days < 14 else "")
    return line


async def server_section(c: httpx.AsyncClient, urls: list[str]) -> str:
    lines = ["🖥 Sunucu", host_stats()]
    if urls:
        lines += await asyncio.gather(*(check_site(c, u) for u in urls))
    return "\n".join(lines)


# ---------- Hepsi ----------
async def build_summary(state: dict, calendar_url: str = "", urls: list[str] | None = None) -> str:
    now = datetime.now(TZ)
    parts = [f"☀️ Günaydın! {tr_date(now)}"]
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "sabah-ozeti/1.0"}) as c:
        tasks = {
            "hava": weather(c),
            "kur": rates(c, state),
            "takvim": calendar(c, calendar_url, now) if calendar_url else None,
            "deprem": quakes(c),
            "sunucu": server_section(c, urls or []),
        }
        names = [k for k, v in tasks.items() if v is not None]
        results = await asyncio.gather(*(tasks[k] for k in names), return_exceptions=True)
    labels = {"hava": "🌤 Hava", "kur": "💱 Kur", "takvim": "📅 Takvim", "deprem": "🌍 Deprem", "sunucu": "🖥 Sunucu"}
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            parts.append(f"{labels[name]}: alınamadı ({type(res).__name__})")
        elif name == "deprem":
            parts.append(quakes_section(res))
        else:
            parts.append(res)
    return "\n\n".join(parts)
