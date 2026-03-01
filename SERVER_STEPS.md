# Пошаговый запуск бота на сервере

Сервер: **94.156.122.124**, пользователь **root**. Пароль у тебя в письме от SpaceCore.

---

## Шаг 1. Подключись к серверу

На **Mac** открой Терминал и введи:

```bash
ssh root@94.156.122.124
```

Когда спросит пароль — вставь пароль из письма (символы не отображаются — это нормально), нажми Enter.

---

## Шаг 2. Установи Python и git

На сервере (после входа) выполни по очереди:

```bash
apt update
apt install -y python3 python3-pip python3-venv git
```

**Боту нужен Python 3.10+** (для библиотеки Gemini). Если после шага 4 при `pip install` будет ошибка про версию Python — см. раздел «Python 3.10 на сервере» в конце файла.

---

## Шаг 3. Скачай проект с GitHub

```bash
cd /root
git clone https://github.com/DonInvest/smm_bot.git
cd smm_bot
```

---

## Шаг 4. Создай виртуальное окружение и поставь зависимости

Если в системе есть **Python 3.10** (например, после `apt install python3.10 python3.10-venv` на Ubuntu с PPA deadsnakes):

```bash
python3.10 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Если пакета `python3.10` нет (ошибка «Unable to locate package» или «command not found») — используй скрипт установки через pyenv (см. раздел **«Python 3.10 на сервере»** в конце).

---

## Шаг 5. Создай файл .env на сервере

```bash
nano .env
```

Откроется редактор. Вставь туда **все строки из твоего .env с Mac** (GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, X_..., NEYNAR_... и т.д.).

- Вставить в терминале: **правый клик** или **Cmd+V**
- Сохранить: **Ctrl+O**, Enter
- Выйти: **Ctrl+X**

---

## Шаг 6. Проверь запуск вручную

```bash
python3 main.py
```

Должно появиться что-то вроде: `Бот @Don_Inv запущен...`. Напиши боту в Telegram — он должен ответить. Остановить: **Ctrl+C**.

---

## Шаг 7. Включи автозапуск (сервис)

Останови бота (Ctrl+C), затем:

```bash
sed -i "s/YOUR_USER/root/g" smm_bot.service
sed -i "s|/home/root/smm_bot|/root/smm_bot|g" smm_bot.service
cp smm_bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable smm_bot
systemctl start smm_bot
```

Проверь статус:

```bash
systemctl status smm_bot
```

Должно быть **active (running)**. Зелёным.

---

## Шаг 8. Смотреть логи (если что-то не так)

```bash
journalctl -u smm_bot -f
```

Выход: **Ctrl+C**.

---

## Полезные команды потом

| Действие        | Команда                    |
|-----------------|----------------------------|
| Перезапустить   | `systemctl restart smm_bot` |
| Остановить      | `systemctl stop smm_bot`    |
| Запустить снова | `systemctl start smm_bot`   |
| Обновить код    | `cd /root/smm_bot && git pull && systemctl restart smm_bot` |

---

Готово. Бот работает 24/7, Mac можно выключать.

---

## Python 3.10 на сервере (если apt не даёт пакет)

На части VPS (Debian, старый Ubuntu без PPA) пакета `python3.10` нет. Тогда ставим Python 3.10 через **pyenv** (одна команда).

В каталоге проекта на сервере выполни:

```bash
cd /root/smm_bot
bash install_python310_server.sh
```

Скрипт поставит зависимости, pyenv, соберёт Python 3.10, создаст `venv` и установит зависимости. Потом:

```bash
systemctl restart smm_bot
systemctl status smm_bot
```

**Важно:** в unit-файле `smm_bot.service` в `ExecStart` должен быть путь к Python из venv, например:

```ini
ExecStart=/root/smm_bot/venv/bin/python -m main
```

или с полным путём к `main.py`. Тогда после скрипта venv будет с Python 3.10 и бот запустится.
