# Перенос на VPS (бесплатно)

Бот сейчас живёт на ПК: работает только когда компьютер включён.
Чтобы следить 24/7 — нужен бесплатный сервер.

## Что выбрать (проверено, ноябрь 2026)

| Провайдер | Статус | Плюсы | Минусы |
|---|---|---|---|
| **Oracle Cloud Free Tier** | ⭐ лучший | 4 ARM-ядра, 24 ГБ RAM навсегда, без лимита часов | Регистрация требует карту (списание $1 для проверки, возвращают) |
| **Fly.io** | бесплатный уровень | Легко деплоить, есть автостоп | Лимит часов, засыпает |
| **Railway** | $5 кредит/мес | Просто | Кредит кончается |
| **GitHub Actions** | 2000 мин/мес | Ничего не держать | Не для постоянного процесса, только cron |

**Рекомендация: Oracle Cloud Always Free.** Настоящий сервер 24/7 без срока.

## Проблемы, которые будут на Linux

1. **`C:\Code\tg-channel-tools\.env`** — путь Windows. Нужно поменять на локальный `.env`.
2. **Playwright** на сервере требует установки браузера:
   ```bash
   pip install playwright
   playwright install --with-deps chromium
   ```
   Без `--with-deps` упадёт (нет системных библиотек).
3. **Планировщик** — на Linux не Windows Task Scheduler, нужен **cron**.
4. **`wallets.json` / `config.json` / `history.db`** — перенести с собой.

## Подготовка проекта к Linux

Нужно сделать путь к `.env` настраиваемым. Сейчас в `watcher.py`:

```python
ENV_FILE = Path(r"C:\Code\tg-channel-tools\.env")
```

Правильно — искать по порядку:
1. `WATCHER_ENV` из переменной окружения
2. `.env` рядом с `watcher.py`
3. старый путь Windows (обратная совместимость)

## Пошагово: Oracle Cloud

### 1. Создать инстанс
- Регистрация: https://cloud.oracle.com/free
- Compute → Instances → Create
- **Shape: VM.Standard.A1.Flex** (ARM, Always Free)
- ОС: **Ubuntu 22.04** или 24.04
- Сохранить SSH-ключ

### 2. Подключиться
```bash
ssh -i ~/.ssh/oracle_key ubuntu@<IP>
```

### 3. Установить Python и зависимости
```bash
sudo apt update && sudo apt install -y python3-pip python3-venv git
python3 -m venv ~/venv
source ~/venv/bin/activate
pip install requests playwright matplotlib
playwright install --with-deps chromium
```

### 4. Забрать проект с ПК
```bash
# на ПК:
scp -i ~/.ssh/oracle_key -r C:/Code/wallet-watcher ubuntu@<IP>:~/

# нужны эти файлы:
#   watcher.py, debank.py, bot.py
#   config.json, wallets.json
#   history.db (опционально — история)
#   .env  (создать заново с BOT_TOKEN)
```

### 5. Создать `.env` на сервере
```bash
cd ~/wallet-watcher
cat > .env <<'EOF'
BOT_TOKEN=вставь_токен
EOF
chmod 600 .env
```

### 6. Проверить вручную
```bash
source ~/venv/bin/activate
cd ~/wallet-watcher
python3 watcher.py --full --force
python3 bot.py   # Ctrl+C через 10 сек
```

### 7. Настроить cron
```bash
crontab -e
```
Добавить:
```cron
# быстрая проверка каждые 15 минут
*/15 * * * * cd /home/ubuntu/wallet-watcher && /home/ubuntu/venv/bin/python watcher.py --quick >> cron.log 2>&1

# полный отчёт раз в 10 часов
0 */10 * * * cd /home/ubuntu/wallet-watcher && /home/ubuntu/venv/bin/python watcher.py --full >> cron.log 2>&1
```

### 8. Бот как служба (systemd)
```bash
sudo tee /etc/systemd/system/walletbot.service > /dev/null <<'EOF'
[Unit]
Description=Wallet watcher Telegram bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/wallet-watcher
ExecStart=/home/ubuntu/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now walletbot
sudo systemctl status walletbot
```

### 9. Проверить
- Написать боту `/status` — должен ответить
- Через 15 минут должен прийти первый quick-прогон в лог

## Важно про Playwright на ARM

Oracle A1 — архитектура **ARM64**. Playwright для Chromium ARM64 работает, но:
- нужен `playwright install --with-deps chromium` (скачает ARM-сборку)
- RAM: занять может 300-500 МБ при работе — у A1.Flex хватает

Если не заработает — альтернатива: использовать **x86-инстанс** (`VM.Standard.E2.1.Micro`, тоже Always Free, но слабее: 1 ГБ RAM).

## Риск: DeBank блокирует IP

DeBank отдаёт 429 при частых запросах. С бесплатного VPS (общий IP) риск выше, чем с домашнего.

**Что делать, если заблокируют:**
1. Увеличить интервал full до 12-24 ч (`config.json` → но интервал в cron)
2. Добавить паузу между запросами
3. Резервный источник позиций — пока нет, только DeBank

Проверить заранее с VPS:
```bash
curl -s "https://api.debank.com/token/balance_list?user_addr=0xE9D49344e8404A5DaCA0ae4e7FfAC1cfa584f7B1&chain=eth" -H "x-api-ver: v2" | head -c 200
```

## Что даст перенос

| Сейчас (ПК) | На VPS |
|---|---|
| Работает только при включённом ПК | 24/7 |
| Пропуски, если выключен | Ничего не теряется |
| Планировщик Windows | cron |
| Логи в файле | Логи + systemd journal |

## Файлы для переноса

```
watcher.py      — основной цикл
debank.py       — парсер позиций
bot.py          — команды Telegram
config.json     — пороги
wallets.json    — список кошельков
history.db      — история (опционально)
.env            — ТОЛЬКО BOT_TOKEN (создать заново)
```

Не переносить: `*.bat`, `watcher.log*`, `chart_*.png`, `__pycache__`.