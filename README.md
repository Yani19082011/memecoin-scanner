# Memecoin Scanner (graduated pump.fun coins)

Фаза 3 от плана. Слуша за Solana токени, които току-що са "graduated"
(преминали bonding curve-а към Raydium/PumpSwap), после смята score от
**само публични on-chain данни** (ликвидност, обем, реален моментум,
mint/freeze authority, RugCheck риск флагове) и праща email алърт с coin ID
+ оценен потенциал.

**Реално-времева версия:** вместо да чака фиксирани N минути и да провери
монетата само веднъж (което пропуска ранни pump-ове), ботът сега следи
всяка graduated монета на живо - проверява я на всеки `POLL_INTERVAL_SECONDS`
(по подразбиране 60 сек) в рамките на `MONITOR_WINDOW_MINUTES` (по
подразбиране 45 мин) и праща алърт веднага щом реално наблюдаваният
моментум + ликвидност/обем пресекат прага, не на фиксирана минута.
RugCheck се проверява само веднъж в началото (risk флаговете не се менят
всяка минута), за да не се претоварва API-то.

**Съзнателно НЕ ползва "insider"/alpha Telegram групи** — тези общности в
memecoin пространството много често самите те са pump-and-dump схеми.
Вместо това всичко тук е обективно проверимо публично.

**Важно:** не изпълнява транзакции автоматично, само сигнализира. Не е
финансов съвет — прясно graduated memecoin-и са изключително волатилни и
голяма част от тях отиват на 0.

## 1. Локално пускане

Изисква Python **3.10+** (заради `str | None` синтаксиса в кода).

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

Не изисква никакви API ключове за MVP версията — PumpPortal (graduation
събития), DexScreener (ликвидност/обем) и RugCheck (риск доклад) са
публични и безплатни за тези endpoint-и. Ще видиш в конзолата "RAW
migration payload: ..." при всяко ново graduation — това е нормално, там
се вижда суровия JSON от PumpPortal.

## 2. Важна бележка за надеждността на данните

PumpPortal и RugCheck не публикуват пълна официална JSON схема за
migration съобщенията / risk доклада — кодът в `data_sources.py` и
`pumpportal_client.py` пробва най-вероятните имена на полета, но **първите
дни след деплой трябва да следиш логовете** (Render → Logs) и да провериш:

- дали `extract_mint_address()` в `data_sources.py` реално хваща mint адреса от migration събитията (ако не — виж лога "Не разпознах mint адрес..." и добави правилния ключ);
- дали RugCheck доклада съдържа полетата, които `scoring.py` очаква (`risks[].name`, `risks[].level`) — ако RugCheck е сменил схемата, коригирай `scoring.py`.

Кажи ми след първите тестове какво виждаш в логовете и ще коригирам кода.

## 3. Deploy на Render (безплатен Web Service)

1. Нов GitHub repo (напр. `memecoin-scanner`), качи тази папка:
   ```bash
   git init && git add . && git commit -m "Initial memecoin scanner"
   git branch -M main
   git remote add origin https://github.com/<твоя-username>/memecoin-scanner.git
   git push -u origin main
   ```
2. В Render: New → Web Service → Connect repo.
3. Build Command: `pip install -r requirements.txt`. Start Command: `python main.py` (Procfile-ът вече го казва същото).
4. Environment таб → добави промените от `.env.example`, които искаш различни от defaults (най-важно: `ALERT_EMAIL_ENABLED`, `SMTP_USERNAME`, `SMTP_APP_PASSWORD`, `ALERT_EMAIL_TO`).
5. **Keep-alive (за да не заспива безплатния Render план):** регистрирай се безплатно в [cron-job.org](https://cron-job.org) или [UptimeRobot](https://uptimerobot.com) и направи HTTP GET заявка към твоя Render URL (`https://<service>.onrender.com/`) на всеки 10 минути. Ако не го направиш, service-ът заспива след 15 мин без трафик и пропуска graduation събития дотогава.

## 4. Email алърти през Resend (не Gmail SMTP)

Същите стъпки като penny stock бота — Gmail "App Passwords" не работят на
Family Link (supervised) акаунти, затова ползваме [Resend](https://resend.com):
1. Регистрация на resend.com (със същия имейл, на който искаш алъртите).
2. Dashboard → API Keys → Create API Key → копирай.
3. `ALERT_EMAIL_ENABLED=true`, `RESEND_API_KEY=<ключа>`, `RESEND_FROM_EMAIL=onboarding@resend.dev`, `ALERT_EMAIL_TO=yani.kolev2011@gmail.com` (трябва да съвпада с имейла, с който си регистриран в Resend).

## Структура

| Файл | Роля |
|---|---|
| `pumpportal_client.py` | WebSocket слушател за graduation събития (безплатно, без ключ) |
| `data_sources.py` | DexScreener + RugCheck wrapper-и, извличане на mint адрес |
| `scoring.py` | Композитен score от ликвидност/обем/риск флагове |
| `seen_store.py` | Персистентен списък вече-оценени монети (без дублирани алърти) |
| `notifier.py` | Лог + email алърт с coin ID и оценен потенциал |
| `main.py` | Flask health server + фонов asyncio loop |

## Ограничения / отворени въпроси (виж design документа)

- **Социален sentiment (X/Twitter спайкове)** — не е включен в тази версия, защото безплатното X API ниво е твърде ограничено за real-time search. Може да се добави по-късно с платен basic tier.
- **Dev/creator wallet holding %** — не е включен в MVP (изисква Helius RPC за проследяване на wallet история). Може да се добави като допълнителен score фактор.
- Прагът `HIGH_POTENTIAL_THRESHOLD` е начална преценка, не backtested — коригирай го спрямо реални резултати след няколко седмици наблюдение.
