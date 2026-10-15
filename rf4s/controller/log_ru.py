"""Русские тексты журнала бота.

Сообщения в коде написаны по-английски (так в оригинальном проекте). Когда лаунчер запущен на русском
(переменная окружения RF4_LANG, по умолчанию RU), этот фильтр заменяет известные сообщения на русские
уже в готовом журнале, поэтому логика бота не меняется. Неизвестное сообщение выводится как есть.
Сообщения, которые и так по-русски или двуязычные, не трогаются.
"""
from __future__ import annotations

import logging
import os
import re

__all__ = ["translate", "RuLogFilter", "is_ru", "CompactFormatter"]


def is_ru() -> bool:
    return os.environ.get("RF4_LANG", "RU").strip().upper() != "EN"


_STATIC = {
    "Verifying SMTP connection": "Проверяю подключение к SMTP",
    "BOT.NOTIFICATION.DISCORD_WEBHOOK_URL is not set": "Не задан BOT.NOTIFICATION.DISCORD_WEBHOOK_URL",
    "BOT.NOTIFICATION.MIAO_CODE is not set.": "Не задан BOT.NOTIFICATION.MIAO_CODE",
    "Invalid BOT.NOTIFICATION.TELEGRAM_BOT_TOKEN": "Неверный BOT.NOTIFICATION.TELEGRAM_BOT_TOKEN",
    "BOT.NOTIFICATION.TELEGRAM_CHAT_ID is not set": "Не задан BOT.NOTIFICATION.TELEGRAM_CHAT_ID",
    "Crafting item": "Крафт предмета",
    "Harvesting baits...": "Копаю наживку...",
    "Baits harvested successfully": "Наживка накопана",
    'Window mode must be "Borderless windowed" or "Window mode"': "Режим окна должен быть «Безрамочный оконный» или «Оконный»",
    "Window mode detected. Don't move the game window": "Оконный режим. Не двигайте окно игры",
    "Window mode detected. don't move the game window": "Оконный режим. Не двигайте окно игры",
    "Supported window size. Don't change the game window size": "Размер окна поддерживается. Не меняйте размер окна игры",
    "'Fullscreen mode' is not supported": "Полноэкранный режим не поддерживается",
    "Some features require you to add the item to your favorites, make sure you have done so": "Для некоторых функций предмет должен быть в избранном, убедитесь, что вы его добавили",
    "Miaotixing doesn't support image message, no screenshot will be sent": "Miaotixing не поддерживает картинки, скриншот не будет отправлен",
    "Miaotixing doesn't support image message": "Miaotixing не поддерживает картинки",
    "Default retrieval detection mode detected, fully spool your reel": "Стандартное определение конца подмотки: намотайте леску на катушку полностью",
    "Quit key pressed (F8). Stopping bot...": "Нажата клавиша выхода (F8). Останавливаю бота...",
    "Pause key pressed (F7). Pausing bot...": "Нажата клавиша паузы (F7). Ставлю бота на паузу...",
    "Resuming bot execution...": "Продолжаю работу бота...",
    "This might get you banned, use at your own risk": "Это может привести к бану, используйте на свой риск",
    "Craft animation timeout! Recovering...": "Таймаут анимации крафта! Восстанавливаюсь...",
    "Crafting succeeded": "Крафт выполнен",
    "Crafting failed": "Крафт не удался",
    "Make button not found, please set the interface scale to 1x or move your mouse around": "Кнопка «Создать» не найдена: поставьте масштаб интерфейса 1x или подвигайте мышью",
    "Harvest timeout": "Таймаут копания наживки",
    "Refilling player stats (Post-harvest Tea)": "Восполняю состояние персонажа (чай после копания)",
    "Refilling player stats (Post-harvest skipped, human-like)": "Восполняю состояние персонажа (после копания пропущено, как человек)",
    "Refilling player stats (Tea skipped, human-like)": "Восполняю состояние персонажа (чай пропущен, как человек)",
    "Refilling player stats (Tea)": "Восполняю состояние персонажа (чай)",
    "Refilling player stats (Carrot)": "Восполняю состояние персонажа (морковь)",
    "Refilling player stats (Carrot skipped, human-like)": "Восполняю состояние персонажа (морковь пропущена, как человек)",
    "Restarting bot without resetting records": "Перезапуск бота без сброса статистики",
    "Running out of materials": "Закончились материалы",
    "Crafting limit reached": "Достигнут лимит крафта",
    "Low energy or comfort detected. Tea skipped (human-like).": "Мало энергии или комфорта. Чай пропущен (как человек).",
    "Low energy or comfort detected. Drinking tea...": "Мало энергии или комфорта. Пью чай...",
    "Low hunger detected. Eating carrot...": "Низкая сытость. Ем морковь...",
    "Low hunger detected. Carrot skipped (human-like).": "Низкая сытость. Морковь пропущена (как человек).",
    "Post-harvest idle: opening and closing T-menu": "Пауза после копания: открываю и закрываю T-меню",
    "Energy is not high enough": "Энергии недостаточно",
    "Monitoring friction brake": "Слежу за фрикционом",
    "Resetting friction brake (full)": "Сбрасываю фрикцион (полностью)",
    "Friction brake already at target": "Фрикцион уже на нужном значении",
    "Friction brake monitor stopped: no screen coordinates for this window size": "Слежение за фрикционом остановлено: для этого размера окна нет координат",
    "Friction brake monitor error": "Ошибка слежения за фрикционом",
    "Game window is not in the foreground, waiting for it to come back": "Окно игры не в фокусе, жду возврата",
    "Game window did not return for 10 minutes, continuing anyway": "Окно игры не вернулось за 10 минут, продолжаю",
    "Resetting tackle": "Сброс снасти",
    "Casting rod": "Заброс удочки",
    "Sinking lure": "Приманка тонет",
    "Retrieving fishing line": "Подмотка лески",
    "Pulling fish": "Вываживание рыбы",
    "Performing pirking": "Проводка пилькером",
    "Performing elevating": "Проводка «лифт»",
    "Lifting rod": "Подъём удилища",
    "LIFT handler entered": "Вход в обработчик подъёма",
    "Fish not netted. Hiding landing net.": "Рыба не попала в подсак. Убираю подсак.",
    "Changing gear ratio / electro assist mode": "Меняю передаточное число / режим электро",
    "Moving mouse randomly": "Случайное движение мышью",
    "Looking for favorite items": "Ищу предметы в избранном",
    "Monitoring float state": "Слежу за поплавком",
    "Monitoring clip state": "Слежу за клипсой",
    "Lure has reached bottom layer": "Приманка достигла дна",
    "Fish captured during lift (yanked out).": "Рыба поймана во время подъёма (выдернута).",
    "Float status changed": "Состояние поплавка изменилось",
    "Clip status changed": "Состояние клипсы изменилось",
    "Cast: mouse held": "Заброс: мышь зажата",
    "Cast: mouse release sent": "Заброс: мышь отпущена",
    "Sending running result to Discord": "Отправляю итоги работы в Discord",
    "Sending catch notification to Discord": "Отправляю уведомление о поимке в Discord",
    "Sending running result to email": "Отправляю итоги работы на почту",
    "Sending email notification": "Отправляю письмо",
    "Sending running result to Miaotixing": "Отправляю итоги работы в Miaotixing",
    "Sending running result to Telegram": "Отправляю итоги работы в Telegram",
    "Sending catch notification to Telegram": "Отправляю уведомление о поимке в Telegram",
    "Message sent successfully": "Сообщение отправлено",
    "Miaotixing notification sent successfully": "Уведомление Miaotixing отправлено",
    "Retrieving fishing line with pause": "Подмотка лески с паузами",
    "Retrieving fishing line with lift": "Подмотка лески с подъёмом",
    "Harvesting baits": "Копаю наживку",
    "Drinking alcohol": "Пью алкоголь",
    "Stop trolling": "Остановка троллинга",
    "Start trolling": "Начинаю троллинг",
    "Stop route trolling": "Останавливаю маршрутный троллинг",
    "Route trolling: casting the rod before departure": "Маршрутный троллинг: забрасываю удочку перед отправлением",
    "Pausing script": "Пауза скрипта",
    "Handling fish": "Обработка рыбы",
    "Game disconnected": "Потеряно соединение с игрой",
    "Boat ticket hint: selecting next ticket": "Подсказка про лодочный билет: выбираю следующий билет",
    "Replacing broken lures": "Заменяю сломанные приманки",
    "Scroll bar found, changing lures for dropshot rig": "Найдена полоса прокрутки, меняю приманки для дропшота",
    "Scroll bar not found, changing lures for normal rig": "Полоса прокрутки не найдена, меняю приманки для обычной оснастки",
    "Looking for broken lures": "Ищу сломанные приманки",
    "Spawing new process, do not quit the script": "Запускаю новый процесс, не закрывайте скрипт",
    "Control panel detected, back to the game automatically": "Обнаружена панель управления, автоматически возвращаюсь в игру",
    "Fish hooked right after the cast": "Поклёвка сразу после заброса",
    "Skipping harvest: Energy is below config threshold": "Копание пропущено: энергия ниже порога из настроек",
    "Drinking coffee": "Пью кофе",
    "Groundbait is not used up yet": "Прикормка ещё не израсходована",
    "Renewing boat ticket": "Продлеваю лодочный билет",
    "Broken lure not found": "Сломанная приманка не найдена",
    "Boat ticket expired (troll)": "Лодочный билет истёк (троллинг)",
    "Casting rod redundantly": "Лишний заброс удочки",
    "Changing lure randomly": "Случайная смена приманки",
    "Clip is not set, fall back to camera mode": "Клипса не установлена, перехожу в режим камеры",
    "Lure replaced successfully": "Приманка заменена",
    "Harvest bait timeout. Interrupting cycle.": "Таймаут копания наживки. Прерываю цикл.",
    "New dry mix not found": "Новая сухая смесь не найдена",
    "New lure not found": "Новая приманка не найдена",
    "New pva not found": "Новая ПВА-приманка не найдена",
    "New groundbait not found": "Новая прикормка не найдена",
    "Unexpected event detected": "Обнаружено неожиданное событие",
    "Adjusting lure depth": "Подстраиваю глубину приманки",
    "Plotting line chart": "Строю график",
    "Chart has been saved under logs/": "График сохранён в папке logs/",
    "No cast record, skip plotting": "Нет данных о забросах, график не строится",
    "Wheel fill: no data (centre out of the zone)": "Заполнение колеса: нет данных (центр вне зоны)",
}

_BOOL = {"True": "да", "False": "нет"}

_RULES: list[tuple[re.Pattern, str]] = []


def _r(pattern: str, repl: str) -> None:
    _RULES.append((re.compile(pattern, re.DOTALL), repl))


_r(r"Using item: (.+)", r"Использую предмет: \1")
_r(r"Invalid profile name: '(.+)'", r"Неверное имя профиля: '\1'")
_r(r"Invalid mode: '(.+)'", r"Неверный режим: '\1'")
_r(r"Could not find item '(.+)' in T-menu!", r"Не нашёл предмет '\1' в T-меню!")
_r(r"Game window is back \((\d+) s\), resuming", r"Окно игры снова в фокусе (\1 с), продолжаю")
_r(r"Resetting friction brake \((.+)\)", r"Подстраиваю фрикцион (\1)")
_r(r"Friction brake params: (.+)", r"Параметры фрикциона: \1")
_r(r"Friction brake bar zones: (.+)", r"Зоны шкалы фрикциона: \1")
_r(r"(.*?)\s*Opening landing net\. fallback=(\S+), finished=(\S+)", r"Леска выбрана, достаю подсак (fallback=\2, finished=\3)")
_r(r"Equiping new (.+) from menu", r"Достаю новый предмет (\1) из меню")
_r(r"Equiping new (.+) from inventory", r"Достаю новый предмет (\1) из инвентаря")
_r(r"New (.+) equiped successfully", r"Новый предмет (\1) установлен")
_r(r"Line fully retrieved\. Netting after ([\d.]+)s of lifting\.", r"Леска полностью выбрана. Подсак через \1 с подъёма.")
_r(r"Pull fallback after ([\d.]+)s\. Switching to lift\.", r"Вываживание: запасной вариант через \1 с, перехожу к подъёму.")
_r(r"Emergency pull timeout after ([\d.]+)s\. Switching to lift\.", r"Аварийный таймаут вываживания (\1 с), перехожу к подъёму.")
_r(r"Low tension fallback: fish hooked, tension below (\S+)% for ([\d.]+)s \((\d+) px\)\. Switching to lift\.",
   r"Слабое натяжение: рыба на крючке, натяжение ниже \1% уже \2 с (\3 px). Перехожу к подъёму.")
_r(r"Pull state: fish=(True|False) zone=(\d+) fill=(\d+)px \((\d+)%\)",
   lambda m: f"Шкала: рыба={_BOOL[m.group(1)]} зона={m.group(2)} заполнение={m.group(3)}px ({m.group(4)}%)")
_r(r"Retrieval HIT (\S+) ([\d.]+) at (\(\d+,\d+\)), threshold ([\d.]+)", r"Подмотка закончена: \1 \2 в точке \3, порог \4")
_r(r"Retrieval zone (\(.*?\)) conf=(\S+): (\S+) ([\d.]+) at (\(\d+,\d+\))", r"Зона подмотки \1 conf=\2: \3 \4 в точке \5")
_r(r"Sending running result to (.+)", r"Отправляю итоги работы: \1")
_r(r"Failed to send the message: (.+)", r"Не удалось отправить сообщение: \1")
_r(r"(\w+)Notification failed: (.+)", r"Ошибка уведомления \1: \2")
_r(r"Starting fishing mode: '(.+)'", r"Запускаю режим ловли: '\1'")
_r(r"Rod placement: step '(.+)' ([\d.]+) s", r"Установка удилищ: шаг '\1', \2 с")
_r(r"Rod placement: casting and placing (\d+) bottom rod\(s\)", r"Установка удилищ: заброс и установка донок: \1")
_r(r"Next boat ticket selected \((.+) day\)", r"Выбран следующий лодочный билет (\1 дн.)")
_r(r"Equipping main rod from slot: (.+)", r"Беру основную удочку из слота: \1")
_r(r"Equipping main rod back from slot: (.+)", r"Возвращаю основную удочку из слота: \1")
_r(r"Equipping digging tool from slot: (.+)", r"Беру лопату из слота: \1")
_r(r"Checking rod (.+)", r"Проверяю удочку \1")
_r(r"Boat ticket hint check: (.+)", r"Проверка подсказки про лодочный билет: \1")
_r(r"Bottom rods in use: (\d+) of (\d+) \(keys (.+)\)", r"Донок в работе: \1 из \2 (клавиши \3)")
_r(r"Rod (\d+) cannot be placed here \(red banner\), try (\d+)/(\d+)", r"Удилище \1 здесь не поставить (красная плашка), попытка \2/\3")
_r(r"Stuck retrying '(.+)' \((\d+) attempts, (\d+)s elapsed\) -- giving up", r"Застряли на '\1' (\2 попыток, \3 с) -- сдаёмся")
_r(r"Sinking takes (.+) seconds", r"Погружение занимает \1 с")
_r(r"Hardware mouse move timed out: (.+)", r"Таймаут перемещения мыши платой: \1")
_r(r"Route trolling: (.+)", r"Маршрутный троллинг: \1")
_r(r"Fishing mode '(.+)' doesn't support window size '(.+)'", r"Режим ловли '\1' не поддерживает размер окна '\2'")
_r(r"Unsupported window size '(.+)', use '2560x1440', '1920x1080' or '1600x900'", r"Размер окна '\1' не поддерживается, используйте '2560x1440', '1920x1080' или '1600x900'")
_r(r"Invalid setting: '(.+)'", r"Неверная настройка: '\1'")
_r(r"Missing setting: '(.+)'", r"Нет настройки: '\1'")

_MULTI = [
    ("Snag detection will be disabled", "Определение зацепа будет отключено"),
    ("Spooling detection will be disabled", "Определение конца лески будет отключено"),
    ("Auto friction brake will be disabled", "Автофрикцион будет отключён"),
]


def translate(msg: str) -> str:
    text = msg.strip()
    hit = _STATIC.get(text)
    if hit:
        return hit
    if "\n" in text and all(line.strip() in dict(_MULTI) or not line.strip() for line in text.splitlines()):
        return "\n".join(dict(_MULTI).get(line.strip(), line) for line in text.splitlines() if line.strip())
    for rx, repl in _RULES:
        m = rx.fullmatch(text)
        if m:
            return rx.sub(repl, text) if isinstance(repl, str) else repl(m)
    return msg


class RuLogFilter(logging.Filter):
    """Заменяет английские сообщения на русские, когда лаунчер русский."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not is_ru():
            return True
        try:
            msg = record.getMessage()
            new = translate(msg)
            if new != msg:
                record.msg = new
                record.args = ()
        except Exception:  # noqa: BLE001  журнал не должен падать из-за перевода
            pass
        return True


_MARKUP = re.compile(r"\[/?(?:dim|bold|italic|underline|red|green|yellow|blue|cyan|magenta|white)(?: (?:red|green|yellow|blue|cyan|magenta|white))?\]")
_LEVEL_RU = {"WARNING": "Внимание", "ERROR": "Ошибка", "CRITICAL": "Ошибка"}
_LEVEL_EN = {"WARNING": "Warning", "ERROR": "Error", "CRITICAL": "Error"}


class CompactFormatter(logging.Formatter):
    """Короткая строка консоли в стиле «[Бот] Заброс удочки».

    С флагом -d (подробный журнал) перед строкой добавляется время. Предупреждения и ошибки
    помечаются словами «Внимание:» / «Ошибка:». Разметка rich ([dim] и т. п.) убирается.
    """

    def __init__(self, with_time: bool = False) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self.with_time = with_time

    def format(self, record: logging.LogRecord) -> str:
        msg = _MARKUP.sub("", record.getMessage()).rstrip()
        ru = is_ru()
        tag = "[Бот]" if ru else "[Bot]"
        level = (_LEVEL_RU if ru else _LEVEL_EN).get(record.levelname)
        line = f"{tag} {level + ': ' if level else ''}{msg}"
        if self.with_time:
            line = f"{self.formatTime(record, self.datefmt)} {line}"
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            line += "\n" + record.exc_text
        return line
