"""Язык сообщений маршрутного троллинга.

Лаунчер передаёт язык в переменной окружения RF4_LANG (RU / EN). Тексты в коде написаны
по-русски; при RF4_LANG=EN они переводятся здесь по шаблонам, поэтому логика не затронута.
Неизвестное сообщение выводится как есть (по-русски), ничего не теряется.
"""
from __future__ import annotations

import os
import re

__all__ = ["is_en", "tr", "action_name"]


def is_en() -> bool:
    return os.environ.get("RF4_LANG", "RU").strip().upper() == "EN"


# Фрагменты, которые подставляются в шаблоны как значения.
_FRAGMENTS = {
    "на ходу после паузы": "on the move after the pause",
    "после паузы": "after the pause",
    "отстаёт": "behind",
    "впереди": "ahead",
    "нет цифр на экране": "no digits on screen",
    "цифры не распознаны": "digits not recognised",
    "прочее": "other",
    "нет": "none",
    "всё есть": "nothing",
    "не откалиброван": "not calibrated",
    "откалиброван": "calibrated",
    "по координатам": "by coordinates",
    "только по времени": "time only",
    "влево": "left",
    "вправо": "right",
    "прямо": "straight",
    "позиция": "position",
    "чт.": "reads",
    "градусов": "degrees",
    "курс по метке N": "heading from the N marker",
}
_FR_RX = re.compile(
    "|".join(r"(?<![А-Яа-яЁё])" + re.escape(k) + r"(?![А-Яа-яЁё])"
             for k in sorted(_FRAGMENTS, key=len, reverse=True)))


def _fr(text: str) -> str:
    return _FR_RX.sub(lambda m: _FRAGMENTS[m.group(0)], text)


_RULES: list[tuple[re.Pattern, str]] = []


def _R(ru: str, en: str) -> None:
    parts = re.split(r"\{(\d+)\}", ru)
    rx = ""
    for i, part in enumerate(parts):
        rx += f"(?P<g{part}>.+?)" if i % 2 else re.escape(part)
    _RULES.append((re.compile(rx, re.DOTALL), en))


# --- запись маршрута ------------------------------------------------------------------
_R("Запись началась. Плывите круг клавишами A и D, затем нажмите J ещё раз.",
   "Recording started. Sail the lap with A and D, then press J again.")
_R("Лодка стоит в начале маршрута носом по ходу движения. Нажмите J, чтобы начать запись.",
   "The boat is at the start of the route, facing the direction of travel. Press J to start recording.")
_R("Запись: {0} с, {1} начало, позиция {2}, курс {3} градусов", "Rec: {0} s, {1} start, position {2}, heading {3} degrees")
_R("Запись: {0} с, {1} конец, позиция {2}, курс {3} градусов", "Rec: {0} s, {1} end, position {2}, heading {3} degrees")
_R("Курс с компаса сверен с записанным путём: сдвиг {0} градусов "
   "(камера смотрит не точно по носу), разброс по участкам {1}; поправка внесена в запись.",
   "Compass heading checked against the recorded path: offset {0} degrees "
   "(camera not exactly along the bow), spread over legs {1}; correction applied.")
_R("Предупреждение: курс с компаса не сходится с записанным путём (сдвиг по участкам {0}), курс в маршрут не записан; подруливание пойдёт "
   "по координатам. Запустите python main.py trollcalib и перепишите маршрут.",
   "Warning: the compass heading does not match the recorded path (offset per leg {0}), heading not saved; steering will use "
   "coordinates. Run python main.py trollcalib and re-record the route.")
_R("Предупреждение: курс с компаса записался не на всём пути (кольцо компаса не откалибровано "
   "или метка N не видна). Запись будет работать, но подруливание на этих участках будет "
   "грубее. Перепишите маршрут после калибровки курса.",
   "Warning: the compass heading was not recorded along the whole path (compass ring not calibrated "
   "or the N marker is not visible). The route will work, but steering on those parts will be "
   "coarser. Re-record the route after calibrating the heading.")
_R("Предупреждение: координаты с компаса не читаются, маршрут будет только по времени.",
   "Warning: compass coordinates cannot be read, the route will be time-based only.")
_R("Параметры лодки по записи: поворот {0} град/с, скорость {1} ед/с, точек пути {2}",
   "Boat parameters from the recording: turn rate {0} deg/s, speed {1} u/s, path points {2}")
_R("Предупреждение: по записи не удалось определить параметры поворота; "
   "будет работать только сверка по времени, без подруливания.",
   "Warning: could not determine the turn parameters from the recording; "
   "only time-based sync will work, without steering.")
_R("Нужна библиотека keyboard: pip install keyboard", "The keyboard library is required: pip install keyboard")
_R("Шаблоны цифр компаса неполные (нет: {0}). Запустите python main.py trollcalib. "
   "Пока запись пойдёт без координат.",
   "Compass digit templates are incomplete (missing: {0}). Run python main.py trollcalib. "
   "The recording will go without coordinates for now.")
_R("Маршрут не сохранён: {0}", "Route not saved: {0}")
_R("Запись отменена, маршрут не сохранён.", "Recording cancelled, route not saved.")
_R("\nМаршрут сохранён: {0}", "\nRoute saved: {0}")
_R("Предупреждение: в маршруте нет поворотов, лодка поплывёт по прямой.",
   "Warning: the route has no turns, the boat will sail straight.")

# --- описание и проверка маршрута ---------------------------------------------------------
_R("'{0}': {1} шагов, поворотов {2}, круг {3} с, {4}", "'{0}': {1} legs, {2} turns, lap {3} s, {4}")
_R("Недопустимое имя маршрута {0}: латиница, цифры, - и _",
   "Invalid route name {0}: Latin letters, digits, - and _ only")
_R("Недопустимое имя маршрута {0}", "Invalid route name {0}")
_R("В маршруте нет шагов", "The route has no legs")
_R("Неизвестное действие {0}", "Unknown action {0}")
_R("Длительность шага вне диапазона 0.1-{0} с: {1}", "Leg duration out of range 0.1-{0} s: {1}")
_R("Слишком короткий маршрут: {0} с", "Route is too short: {0} s")
_R("Файл маршрута повреждён: {0}", "Route file is corrupted: {0}")
_R("Не выбран водоём/маршрут (--troll-water)", "No water body/route selected (--troll-water)")
_R("Маршрут '{0}' не найден: {1}", "Route '{0}' not found: {1}")
_R("Не удалось прочитать {0}: {1}", "Could not read {0}: {1}")

# --- ход по маршруту ------------------------------------------------------------------
_R("Маршрут: сверка по координатам включена (без подруливания: параметры поворота не определены)",
   "Route: coordinate sync enabled (no steering: turn parameters unknown)")
_R("Маршрут: сверка по координатам включена", "Route: coordinate sync enabled")
_R("Маршрут: в записи нет координат, идём только по времени",
   "Route: the recording has no coordinates, following time only")
_R("Маршрут: пауза, лодка остановлена", "Route: paused, boat stopped")
_R("Маршрут: продолжение, шаг {0}/{1}", "Route: resuming, leg {0}/{1}")
_R("Маршрут пройден до конца, лодка остановлена", "Route finished, boat stopped")
_R("Маршрут: круг {0} пройден", "Route: lap {0} completed")
_R("Маршрут: после паузы компас не прочитан, уточню положение уже на ходу",
   "Route: compass not read after the pause, will fix the position on the move")
_R("Маршрут: {0} позиция {1} далеко от пути, часы маршрута не меняю",
   "Route: {0}, position {1} is far from the path, route clock unchanged")
_R("Маршрут: {0} позиция {1} на линии (отклонение {2}), часы верны ({3} с)",
   "Route: {0}, position {1} is on the line (offset {2}), clock is correct ({3} s)")
_R("Маршрут: {0} позиция {1}, отклонение {2}; лодка {3} на {4} с, часы маршрута {5} -> {6} с",
   "Route: {0}, position {1}, offset {2}; boat is {3} by {4} s, route clock {5} -> {6} s")
_R("Маршрут: за 40 с компас так и не прочитался, часы маршрута не менял",
   "Route: compass could not be read within 40 s, route clock unchanged")
_R("Маршрут: после нажатия J лодка поехала - режим тролинга был выключен (сбой J), дальше по маршруту",
   "Route: the boat started moving after pressing J - trolling mode was off (J failure), continuing the route")
_R("Маршрут: после J лодка не поехала, возвращаю J: значит она во что-то упёрлась",
   "Route: the boat did not move after J, pressing J back: it must be stuck on something")
_R("Маршрут: лодка стоит при включённом маршруте - нажимаю J (возможно, режим тролинга не включился)",
   "Route: the boat is standing while the route is on - pressing J (trolling mode may not have started)")
_R("Маршрут: лодка упёрлась, отворачиваю {0} {1} с (попытка {2})",
   "Route: the boat is stuck, turning {0} for {1} s (attempt {2})")
_R("Маршрут: сверка по координатам, {0} -> {1} с", "Route: coordinate sync, {0} -> {1} s")
_R("Маршрут: курс {0} градусов, ошибка {1}: подруливание {2} {3} с",
   "Route: heading {0} degrees, error {1}: steering {2} for {3} s")
_R("Маршрут: подруливание {0} {1} с", "Route: steering {0} for {1} s")
_R("Маршрут: курс по компасу прочитать не удалось: {0}", "Route: could not read the heading from the compass: {0}")
_R("Маршрут: курс по метке N откалиброван: центр ({0}), радиус {1} px, разброс {2} px, охват {3} градусов",
   "Route: heading from the N marker calibrated: centre ({0}), radius {1} px, spread {2} px, coverage {3} degrees")
_R("Маршрут: курс с компаса не сходится с реальным движением лодки (расхождение {0} градусов), по курсу не рулю",
   "Route: the compass heading does not match the boat's real movement (difference {0} degrees), not steering by heading")
_R("Маршрут: курс с компаса снова сходится с движением лодки, подруливание по курсу включено",
   "Route: the compass heading matches the boat's movement again, heading steering enabled")
_R("Маршрут: после подруливания курс изменился на {0}, ждали {1}",
   "Route: after steering the heading changed by {0}, expected {1}")
_R("Маршрут: направление курса по компасу не сходится с поворотами, переворачиваю знак",
   "Route: the compass heading direction does not match the turns, flipping the sign")
_R("Маршрут: компас прочитан, позиция {0}", "Route: compass read, position {0}")
_R("Маршрут: компас за минуту: прочитан {0} из {1}, сбои ({2}){3}, шаг {4}/{5}, время маршрута {6} с",
   "Route: compass over the last minute: read {0} of {1}, failures ({2}){3}, leg {4}/{5}, route time {6} s")
_R("Маршрут: итоги круга по координатам: уход от линии средний {0}, максимум {1}, "
   "в среднем в сторону {2} (ед.; плюс - влево), замеров {3}",
   "Route: lap summary by coordinates: mean drift from the line {0}, maximum {1}, "
   "mean side offset {2} (units; plus = left), samples {3}")
_R("Сверка по координатам выключена из-за ошибки: {0}", "Coordinate sync disabled due to an error: {0}")
_R("Маршрут остановлен из-за ошибки: {0}", "Route stopped due to an error: {0}")
_R("Маршрут: лодка не двигается {0} с (позиция {1})", "Route: the boat is not moving for {0} s (position {1})")
_R("Маршрут: позиция {0} далеко от пути ({1}), коррекция выключена",
   "Route: position {0} is far from the path ({1}), correction disabled")
_R("Маршрут: позиция {0} далеко от пути ({1}), только выправляю курс",
   "Route: position {0} is far from the path ({1}), only correcting the heading")
_R("Калибровка компаса. Недостающие цифры: {0}", "Compass calibration. Missing digits: {0}")
_R("Встаньте так, чтобы на компасе были нужные цифры, введите показанные координаты",
   "Stand so that the needed digits are shown on the compass and enter the displayed coordinates")
_R("(например 892:1990) и нажмите Enter. Пустая строка - выход.",
   "(for example 892:1990) and press Enter. An empty line exits.")
_R("Координаты на компасе: ", "Coordinates on the compass: ")
_R("Не получилось: {0}", "Failed: {0}")
_R("Добавлено шаблонов: {0}. Не хватает: {1}", "Templates added: {0}. Still missing: {1}")
_R("Маршрут: {0}", "Route: {0}")  # запасное правило: заметки курса и пр.


def tr(msg: str) -> str:
    """Перевести сообщение, если выбран английский; иначе вернуть как есть."""
    if not is_en() or not isinstance(msg, str) or not re.search(r"[А-Яа-яЁё]", msg):
        return msg
    for rx, en in _RULES:
        m = rx.fullmatch(msg)
        if m:
            args = [_fr(m.group(f"g{i}")) for i in range(len(m.groupdict()))]
            return en.format(*args)
    # составное сообщение (несколько строк): переводим по строкам
    if "\n" in msg:
        return "\n".join(tr(line) if line else line for line in msg.split("\n"))
    return msg


def action_name(action: str) -> str:
    names_en = {"forward": "straight", "left": "left", "right": "right"}
    names_ru = {"forward": "прямо", "left": "влево", "right": "вправо"}
    return (names_en if is_en() else names_ru).get(action, action)
