# Sabah Özeti

Her sabah Telegram'a tek mesajda **hava, döviz kuru, takvim, deprem ve sunucu** özeti gönderen,
Türkiye'de deprem olunca da **ayrıca anında** haber veren küçük bir bot.

```
☀️ Günaydın! 13 Ekim Salı

⛈ Hava · Gebze
Gök gürültülü fırtına, şimdi 16° (hissedilen 18°)
En yüksek 18° / en düşük 15° · yağış %80 · rüzgâr 16 km/sa
☔ Şemsiyeni al.

💱 Kur (TCMB, 12.10)
USD 49,34 ₺ (▲ %0,12)
...
```

## Kaynaklar (hepsi ücretsiz, anahtarsız)
| Bölüm | Kaynak |
|---|---|
| Hava | [Open-Meteo](https://open-meteo.com) |
| Kur | TCMB `today.xml` (efektif değil, döviz satış) |
| Deprem | AFAD (tarihler UTC gelir, yerel saate çevrilir) |
| Takvim | Google Takvim "iCal biçiminde gizli adres" (isteğe bağlı) |
| Sunucu | Konteynerin gördüğü ana makine RAM/disk/yük + site ve SSL kontrolü |

## Deprem uyarısı
- AFAD 60 sn'de bir sorgulanır; Türkiye'de **M≥4.0** (ayarlanabilir) yeni deprem olunca anında mesaj gelir.
- Kıyıdan 100 km'den uzak deniz depremleri (ör. Girit açıkları) elenir (`QUAKE_MAX_OFFSHORE_KM`).
- İlk açılışta mevcut depremler için mesaj atılmaz; görülenler `/data/state.json`'da tutulur.

## Komutlar
`/ozet` (şimdi gönder), `/deprem` (son 24 saat), `/yardim`. Yalnızca tanımlı sohbet kullanabilir.

## Kurulum
`.env.example`'ı doldur, sonra:
```
docker build -t sabah-ozeti . && docker run -d --env-file .env -v sabah-data:/data sabah-ozeti
```
Coolify: *Public Repository* → build pack **Dockerfile**, port `8000`, `/data` için kalıcı depolama, ortam değişkenleri.

Özet `SEND_AT`'te gider; servis o saatten en geç 2 saat sonra açılırsa o günün özetini telafi eder, daha geç açılırsa ertesi sabahı bekler.
