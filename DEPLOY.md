# Запуск бота на сервере (Ubuntu)

Чтобы бот работал 24/7, перенеси проект на VPS (Ubuntu) и запусти как сервис.

## 1. Сервер

Нужен любой VPS с Ubuntu 20.04/22.04 (DigitalOcean, Timeweb, Selectel, и т.д.). Подойдёт минимальный тариф.

## 2. Подключение и подготовка

```bash
ssh root@IP_ТВОЕГО_СЕРВЕРА
# или: ssh твой_юзер@IP_ТВОЕГО_СЕРВЕРА
```

Установи Python и git (если ещё нет):

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git
```

## 3. Клонирование или копирование проекта

**Вариант A — с GitHub (если код уже в репо):**

```bash
cd ~
git clone https://github.com/DonInvest/smm_bot.git
cd smm_bot
```

**Вариант B — копирование с Mac через scp (если не пушил последние изменения):**

На **Mac** в терминале:

```bash
cd /Users/donmm/Desktop/smm_bot
scp -r main.py auth_x.py auth_farcaster.py requirements.txt .env.example .gitignore README.md smm_bot.service DEPLOY.md твой_юзер@IP_СЕРВЕРА:~/smm_bot/
```

На сервере предварительно создай папку: `mkdir -p ~/smm_bot`.

**.env не копируй по scp** (секреты). Создай его на сервере вручную (см. ниже).

## 4. Виртуальное окружение и зависимости

На **сервере**:

```bash
cd ~/smm_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 5. Файл .env на сервере

Создай `.env` в папке `~/smm_bot` и вставь те же переменные, что и на Mac (все ключи и токены):

```bash
nano ~/smm_bot/.env
```

Вставь содержимое, сохрани (Ctrl+O, Enter, Ctrl+X). Либо создай файл локально (без лишних глаз) и один раз скопируй через scp, предварительно проверив, что в нём нет лишних прав доступа.

## 6. Запуск как сервис (systemd)

Подставь своего пользователя (под которым заходишь по ssh) вместо `YOUR_USER`:

```bash
export ME=$(whoami)
sed -i "s/YOUR_USER/$ME/g" ~/smm_bot/smm_bot.service
```

Установи сервис:

```bash
sudo cp ~/smm_bot/smm_bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable smm_bot
sudo systemctl start smm_bot
```

Проверка:

```bash
sudo systemctl status smm_bot
# Логи в реальном времени:
sudo journalctl -u smm_bot -f
```

Команды для управления:

- Перезапуск: `sudo systemctl restart smm_bot`
- Остановка: `sudo systemctl stop smm_bot`

## 7. Если не хочешь systemd (простой вариант)

Можно запустить бота в фоне через `screen` или `tmux`:

```bash
cd ~/smm_bot
source venv/bin/activate
screen -S smm_bot
python3 main.py
# Отключиться: Ctrl+A, затем D
# Вернуться: screen -r smm_bot
```

После перезагрузки сервера такой процесс пропадёт — для постоянной работы лучше systemd.

---

Кратко: переносишь файлы на сервер, ставишь зависимости, создаёшь `.env`, поднимаешь сервис — бот будет работать всегда, даже когда Mac выключен.
