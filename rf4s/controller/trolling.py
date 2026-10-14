"""Маршрутный троллинг: формат маршрута, запись и исполнение по времени.

Скорость лодки при троллинге постоянна, поэтому маршрут описывается шагами
"плыть прямо / держать A (влево) / держать D (вправо)" и их длительностями.
Модуль не зависит от остального бота: клавиши исполнитель получает снаружи
(обычно это rf4s.utils с функциями key_down, key_up, press).
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rf4s.controller.navigation import circ_diff as nav_circ  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ROUTES_DIR = ROOT / "static" / "trolling"

TROLLING_KEY = "j"  # J запускает и останавливает троллинг
LEFT_KEY = "a"
RIGHT_KEY = "d"
# Скан-коды клавиш A, D, J: рекордеру не важна раскладка клавиатуры.
SCAN_CODES = {30: LEFT_KEY, 32: RIGHT_KEY, 36: TROLLING_KEY}

FORWARD, LEFT, RIGHT = "forward", "left", "right"
ACTIONS = (FORWARD, LEFT, RIGHT)
from rf4s.controller.troll_lang import action_name as _action_name, is_en as _is_en, tr as _tr  # noqa: E402

ACTION_RU = {FORWARD: "прямо", LEFT: "влево", RIGHT: "вправо"}

SCHEMA_VERSION = 1
MIN_LEG_SEC = 0.25  # более короткие нажатия при записи считаются случайными
MAX_LEG_SEC = 600.0
MIN_LAP_SEC = 2.0
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n",
     "o", "p", "r", "s", "t", "u", "f", "h", "c", "ch", "sh", "sch", "", "y", "",
     "e", "yu", "ya"],
))


class RouteError(Exception):
    """Маршрут не найден или повреждён."""

    def __init__(self, message: str = "", *args):
        super().__init__(_tr(str(message)), *args)


@dataclass(frozen=True)
class Leg:
    action: str  # FORWARD / LEFT / RIGHT
    sec: float


@dataclass
class Route:
    name: str
    title: str
    legs: list[Leg]
    loop: bool = True
    start_text: str = ""  # где стартовать (справочно, например "477:320")
    note: str = ""
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    # Навигация по координатам компаса (необязательна: без неё маршрут идёт по времени).
    path: list[tuple[float, float, float]] = field(default_factory=list)  # (t, x, y)
    left_sign: int = 0
    turn_rate: float = 0.0
    speed: float = 0.0
    # Курс лодки с компаса во время записи, (t, градусы по часовой от севера): точный
    # эталон для подруливания (у координат шаг в целую единицу). Пусто у старых маршрутов.
    hdg: list[tuple[float, float]] = field(default_factory=list)

    def refresh_motion(self) -> None:
        """Пересчитать знак поворота, скорость поворота и скорость лодки по пути.

        Делается при каждой загрузке, поэтому маршруты, записанные старой версией,
        получают параметры без перезаписи.
        """
        if not self.has_path:
            return
        from rf4s.controller.navigation import estimate_motion  # noqa: PLC0415
        from rf4s.controller.navigation import turn_rate_from_heading  # noqa: PLC0415
        motion = estimate_motion(self.path, self.legs, self.lap_sec)
        self.left_sign, self.turn_rate, self.speed = motion.left_sign, motion.turn_rate, motion.speed
        # Скорость поворота по компасу точнее, чем по грубым координатам.
        rate = turn_rate_from_heading(self.legs, self.hdg)
        if rate is not None and 2.0 <= rate <= 40.0:
            self.turn_rate = rate

    @property
    def has_path(self) -> bool:
        return len(self.path) >= 8

    @property
    def lap_sec(self) -> float:
        return round(sum(leg.sec for leg in self.legs), 1)

    def summary(self) -> str:
        turns = sum(1 for leg in self.legs if leg.action != FORWARD)
        nav = "по координатам" if self.has_path else "только по времени"
        return _tr(f"'{self.title or self.name}': {len(self.legs)} шагов, поворотов {turns}, круг {self.lap_sec} с, {nav}")

    def describe(self) -> str:
        unit = "s" if _is_en() else "с"
        lines, t = [], 0.0
        for i, leg in enumerate(self.legs, 1):
            extra = ""
            if self.path:
                p = min(self.path, key=lambda q: abs(q[0] - t))
                extra = f" [{'pos' if _is_en() else 'поз.'} {p[1]:.0f}:{p[2]:.0f}"
                if self.hdg:
                    h = min(self.hdg, key=lambda q: abs(q[0] - t))
                    extra += f", {'hdg' if _is_en() else 'курс'} {h[1]:.0f}"
                extra += "]"
            lines.append(f"{i}. {_action_name(leg.action)} {leg.sec:.1f} {unit}{extra}")
            t += leg.sec
        return "\n".join(lines)

    def validate(self) -> None:
        if not NAME_RE.match(self.name or ""):
            raise RouteError(f"Недопустимое имя маршрута {self.name!r}: латиница, цифры, - и _")
        if not self.legs:
            raise RouteError("В маршруте нет шагов")
        for leg in self.legs:
            if leg.action not in ACTIONS:
                raise RouteError(f"Неизвестное действие {leg.action!r}")
            if not MIN_LEG_SEC <= leg.sec <= MAX_LEG_SEC:
                raise RouteError(
                    f"Длительность шага вне диапазона {MIN_LEG_SEC}-{MAX_LEG_SEC:.0f} с: {leg.sec}"
                )
        if self.lap_sec < MIN_LAP_SEC:
            raise RouteError(f"Слишком короткий маршрут: {self.lap_sec} с")

    def to_dict(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "water": self.name,
            "title": self.title,
            "created": self.created,
            "start": {"text": self.start_text},
            "note": self.note,
            "loop": self.loop,
            "lap_sec": self.lap_sec,
            "legs": [{"action": leg.action, "sec": round(leg.sec, 1)} for leg in self.legs],
            "nav": {
                "left_sign": self.left_sign,
                "turn_rate": round(self.turn_rate, 2),
                "speed": round(self.speed, 3),
                "path": [[round(t, 1), x, y] for t, x, y in self.path],
                "hdg": [[round(t, 1), round(h, 1)] for t, h in self.hdg],
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Route":
        try:
            legs = [Leg(str(item["action"]), float(item["sec"])) for item in data["legs"]]
            start = data.get("start") or {}
            nav = data.get("nav") or {}
            path = [(float(t), float(x), float(y)) for t, x, y in nav.get("path", [])]
            hdg = [(float(t), float(h)) for t, h in nav.get("hdg", [])]
            route = cls(
                name=str(data["water"]),
                title=str(data.get("title", "")),
                legs=legs,
                loop=bool(data.get("loop", True)),
                start_text=str(start.get("text", "")),
                note=str(data.get("note", "")),
                created=str(data.get("created", "")),
                path=path,
                left_sign=int(nav.get("left_sign", 0)),
                turn_rate=float(nav.get("turn_rate", 0.0)),
                speed=float(nav.get("speed", 0.0)),
                hdg=hdg,
            )
        except (KeyError, TypeError, ValueError) as err:
            raise RouteError(f"Файл маршрута повреждён: {err!r}") from err
        route.validate()
        route.refresh_motion()
        return route


# ---------------------------------------------------------------- хранилище

def make_name(title: str) -> str:
    """Имя файла из названия: "Куори" -> "kuori"."""
    text = "".join(_TRANSLIT.get(ch, ch) for ch in title.strip().lower())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:48]
    return text if NAME_RE.match(text) else f"route-{datetime.now():%Y%m%d-%H%M%S}"


def route_path(name: str, routes_dir: Path | None = None) -> Path:
    if not NAME_RE.match(name or ""):
        raise RouteError(f"Недопустимое имя маршрута {name!r}")
    return (routes_dir or ROUTES_DIR) / f"{name}.json"


def load_route(name: str | None, routes_dir: Path | None = None) -> Route:
    if not name:
        raise RouteError("Не выбран водоём/маршрут (--troll-water)")
    path = route_path(name, routes_dir)
    if not path.exists():
        raise RouteError(f"Маршрут '{name}' не найден: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise RouteError(f"Не удалось прочитать {path}: {err}") from err
    return Route.from_dict(data)


def save_route(route: Route, routes_dir: Path | None = None) -> Path:
    route.validate()
    path = route_path(route.name, routes_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(route.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def list_routes(routes_dir: Path | None = None) -> list[Route]:
    """Все читаемые маршруты; повреждённые файлы пропускаются."""
    found = []
    for path in sorted((routes_dir or ROUTES_DIR).glob("*.json")):
        try:
            found.append(load_route(path.stem, routes_dir))
        except RouteError:
            continue
    return found


# ---------------------------------------------------------- запись маршрута

def legs_from_events(events, t_end: float, min_leg: float = MIN_LEG_SEC) -> list[Leg]:
    """Шаги маршрута из событий клавиш.

    events: (t, key, is_down), key -- "a" или "d", t в секундах от начала
    записи; t_end -- длительность записи. Одновременно A и D -- это "прямо".
    """
    left = right = False
    action, start = FORWARD, 0.0
    spans: list[list] = []
    for t, key, down in sorted(events, key=lambda e: e[0]):
        if key == LEFT_KEY:
            left = down
        elif key == RIGHT_KEY:
            right = down
        else:
            continue
        new = FORWARD if left == right else (LEFT if left else RIGHT)
        if new != action:
            spans.append([action, max(0.0, t - start)])
            action, start = new, t
    spans.append([action, max(0.0, t_end - start)])
    spans = [s for s in spans if s[1] > 0]

    # Короткие нажатия присоединяются к предыдущему шагу (первый -- к следующему).
    merged: list[list] = []
    for span in spans:
        if span[1] < min_leg and merged:
            merged[-1][1] += span[1]
        else:
            merged.append(span)
    if len(merged) > 1 and merged[0][1] < min_leg:
        merged[1][1] += merged[0][1]
        merged.pop(0)

    # Соседние шаги с одним действием склеиваются.
    legs: list[list] = []
    for action, sec in merged:
        if legs and legs[-1][0] == action:
            legs[-1][1] += sec
        else:
            legs.append([action, sec])

    # Округление до 0.1 с с сохранением общей длительности.
    total = round(sum(sec for _, sec in legs), 1)
    rounded = [[action, round(sec, 1)] for action, sec in legs]
    if rounded:
        rounded[-1][1] = round(rounded[-1][1] + (total - sum(sec for _, sec in rounded)), 1)
    return [Leg(a, s) for a, s in rounded if s > 0]


class RouteRecorder:
    """Записывает маршрут по клавишам, пока вы плывёте вручную.

    Запись начинается первым нажатием J (старт троллинга) и заканчивается
    вторым (остановка). Удержание A и D между ними превращается в шаги.
    """

    def __init__(self, name, title="", start_text="", note="", min_leg=MIN_LEG_SEC,
                 keyboard=None, clock=time.monotonic, log=print, position_fn=None,
                 sample_sec: float = 1.0, heading_fn=None, heading_pt_fn=None, heading_calc_fn=None):
        self.name, self.title, self.start_text, self.note = name, title, start_text, note
        self._position_fn, self._sample_sec = position_fn, sample_sec
        self._heading_fn = heading_fn   # () -> (градусы, откалиброван ли центр) | None
        self._path: list[tuple[float, float, float]] = []
        self._hdg: list[tuple[float, float]] = []
        # Сырые точки метки N: курс пересчитывается в конце записи по самому свежему центру кольца
        # (на старте центр может быть неточным и уточняться по ходу круга).
        self._heading_pt_fn, self._heading_calc_fn = heading_pt_fn, heading_calc_fn
        self._hdg_raw: list[tuple[float, float, float]] = []
        self.min_leg, self._clock = min_leg, clock
        self._log = lambda msg: log(_tr(msg))
        self._keyboard = keyboard
        self._lock = threading.Lock()
        self._held: set[str] = set()
        self._events: list[tuple[float, str, bool]] = []
        self._t0: float | None = None
        self._t_end = 0.0
        self._done = threading.Event()

    def on_event(self, event) -> None:
        key = SCAN_CODES.get(getattr(event, "scan_code", None))
        if key is None:
            return
        down = getattr(event, "event_type", "") == "down"
        with self._lock:
            if down:
                if key in self._held:  # автоповтор клавиши
                    return
                self._held.add(key)
            else:
                self._held.discard(key)
            now = self._clock()
            if key == TROLLING_KEY:
                if not down:
                    return
                if self._t0 is None:
                    self._t0 = now
                    self._events = [(0.0, k, True) for k in sorted(self._held) if k != TROLLING_KEY]
                    self._log("Запись началась. Плывите круг клавишами A и D, затем нажмите J ещё раз.")
                elif not self._done.is_set():
                    self._t_end = now - self._t0
                    self._done.set()
                return
            if self._t0 is not None and not self._done.is_set():
                self._events.append((now - self._t0, key, down))
                self._log_key(now - self._t0, key, down)

    def _log_key(self, t: float, key: str, down: bool) -> None:
        """Показать нажатие A/D с последними прочитанными координатами и курсом компаса."""
        name = _action_name(LEFT if key == LEFT_KEY else RIGHT)
        pos = f"{self._path[-1][1]:.0f}:{self._path[-1][2]:.0f}" if self._path else "?"
        hd = f"{self._hdg[-1][1]:.0f}" if self._hdg else "?"
        self._log(f"Запись: {t:.1f} с, {name} {'начало' if down else 'конец'}, позиция {pos}, курс {hd} градусов")

    def run(self) -> Route:
        kb = self._keyboard
        if kb is None:
            import keyboard as kb  # noqa: PLC0415
        self._log("Лодка стоит в начале маршрута носом по ходу движения. Нажмите J, чтобы начать запись.")
        handle = kb.hook(self.on_event)
        try:
            while not self._done.is_set():
                if self._t0 is not None and self._position_fn is not None:
                    self._sample()
                    self._done.wait(self._sample_sec)
                else:
                    self._done.wait(0.2)
        finally:
            kb.unhook(handle)
        legs = legs_from_events(self._events, self._t_end, self.min_leg)
        route = Route(name=self.name, title=self.title, legs=legs, start_text=self.start_text, note=self.note)
        self._attach_path(route)
        return route

    def _sample(self) -> None:
        t0 = self._t0
        if t0 is None:
            return
        try:
            pos = self._position_fn()
        except Exception:  # noqa: BLE001 - сбой чтения не должен рвать запись
            pos = None
        if pos is not None:
            self._path.append((round(self._clock() - t0, 2), float(pos[0]), float(pos[1])))
        if self._heading_fn is not None:
            try:
                hd = self._heading_fn()
            except Exception:  # noqa: BLE001 - курс необязателен, запись продолжается
                hd = None
            t_now = round(self._clock() - t0, 2)
            pt = None
            if hd is not None and self._heading_pt_fn is not None and self._heading_calc_fn is not None:
                try:
                    pt = self._heading_pt_fn()
                except Exception:  # noqa: BLE001
                    pt = None
            if pt is not None:
                self._hdg_raw.append((t_now, float(pt[0]), float(pt[1])))
            # Курс до калибровки кольца компаса неточен -- такие отсчёты не берём.
            if hd is not None and hd[1]:
                self._hdg.append((t_now, round(float(hd[0]), 1)))

    def _check_heading_vs_path(self, route: Route) -> None:
        """Записанный курс должен сходиться с направлением записанного пути, иначе его выбрасываем."""
        if not route.hdg or not route.path:
            return
        from rf4s.controller.navigation import path_bearing  # noqa: PLC0415

        diffs, t = [], 0.0
        for leg in route.legs:
            start, end = t, t + leg.sec
            t = end
            if leg.action != FORWARD or end - start < 14.0:
                continue
            xy = path_bearing(route.path, start, end, 0.0)
            pts = [h for tt, h in route.hdg if start + 2.0 <= tt <= end - 1.0]
            if xy is None or len(pts) < 3:
                continue
            sx = sum(math.sin(math.radians(h)) for h in pts)
            sy = sum(math.cos(math.radians(h)) for h in pts)
            mean = math.degrees(math.atan2(sx, sy)) % 360.0
            diffs.append((xy[0] - mean) % 360.0)
        if len(diffs) < 2:
            return
        if len(diffs) < 2:
            return
        vx = sum(math.cos(math.radians(d)) for d in diffs)
        vy = sum(math.sin(math.radians(d)) for d in diffs)
        off = math.degrees(math.atan2(vy, vx)) % 360.0
        spread = max(abs((d - off + 180.0) % 360.0 - 180.0) for d in diffs)
        if spread > 30.0:
            self._log(f"Предупреждение: курс с компаса не сходится с записанным путём (сдвиг по участкам "
                      f"{', '.join(f'{d:.0f}' for d in diffs)}), курс в маршрут не записан; подруливание пойдёт "
                      f"по координатам. Запустите python main.py trollcalib и перепишите маршрут.")
            route.hdg = []
            return
        route.hdg = [(t, round((h + off) % 360.0, 1)) for t, h in route.hdg]
        self._log(f"Курс с компаса сверен с записанным путём: сдвиг {off:.0f} градусов "
                  f"(камера смотрит не точно по носу), разброс по участкам {spread:.0f}; поправка внесена в запись.")

    def _attach_path(self, route: Route) -> None:
        """Сохранить путь и вывести из него знак поворота, скорость и скорость поворота."""
        if self._position_fn is None:
            return
        path = [p for p in self._path if p[0] <= self._t_end + 0.5]
        if len(path) < 8:
            self._log("Предупреждение: координаты с компаса не читаются, маршрут будет только по времени.")
            return
        from rf4s.controller.navigation import Motion  # noqa: PLC0415
        route.path = path
        route.hdg = [h for h in self._hdg if h[0] <= self._t_end + 0.5]
        if self._hdg_raw and self._heading_calc_fn is not None:
            # центр кольца к концу записи самый точный: пересчитываем весь курс по нему
            reader_ok = getattr(getattr(self._heading_calc_fn, "__self__", None), "heading_calibrated", True)
            if reader_ok:
                route.hdg = [(t, round(float(self._heading_calc_fn(px, py)), 1))
                             for t, px, py in self._hdg_raw if t <= self._t_end + 0.5]
        self._check_heading_vs_path(route)
        route.refresh_motion()   # знак/скорость по координатам, скорость поворота -- по курсу, если он записан
        motion = Motion(route.left_sign, route.turn_rate, route.speed)
        self._log(f"Параметры лодки по записи: поворот {motion.turn_rate:.1f} град/с, "
                  f"скорость {motion.speed:.2f} ед/с, точек пути {len(path)}, отсчётов курса {len(route.hdg)}")
        if self._heading_fn is not None and len(route.hdg) < 0.5 * len(path):
            self._log("Предупреждение: курс с компаса записался не на всём пути (кольцо компаса не откалибровано "
                      "или метка N не видна). Запись будет работать, но подруливание на этих участках будет "
                      "грубее. Перепишите маршрут после калибровки курса.")
        if not motion.usable:
            self._log("Предупреждение: по записи не удалось определить параметры поворота; "
                      "будет работать только сверка по времени, без подруливания.")


def _say(msg: str) -> None:
    print(_tr(msg))


def record_cli(args) -> None:
    """Точка входа: python main.py trollrecord --water NAME [--title TEXT]."""
    name = args.water or make_name(args.title or "")
    try:
        import keyboard  # noqa: F401, PLC0415
    except ImportError:
        _say("Нужна библиотека keyboard: pip install keyboard")
        sys.exit(1)
    position_fn = None
    heading_fn = heading_pt_fn = heading_calc_fn = None
    if not getattr(args, "no_compass", False):
        from rf4s.controller.compass import CompassReader  # noqa: PLC0415
        reader = CompassReader()
        if reader.missing_digits():
            _say("Шаблоны цифр компаса неполные (нет: " + " ".join(reader.missing_digits()) +
                  "). Запустите python main.py trollcalib. Пока запись пойдёт без координат.")
        else:
            position_fn = lambda: reader.read(samples=1)  # noqa: E731
            heading_fn = reader.read_heading
            heading_pt_fn = lambda: getattr(reader, "last_heading_pt", None)  # noqa: E731
            heading_calc_fn = reader.heading_at
    recorder = RouteRecorder(name, title=args.title or name, start_text=args.start_text or "",
                             position_fn=position_fn, heading_fn=heading_fn,
                             heading_pt_fn=heading_pt_fn, heading_calc_fn=heading_calc_fn)
    try:
        route = recorder.run()
        path = save_route(route)
    except RouteError as err:
        _say(f"Маршрут не сохранён: {err}")
        sys.exit(1)
    except KeyboardInterrupt:
        _say("Запись отменена, маршрут не сохранён.")
        return
    _say(f"\nМаршрут сохранён: {path}\n{route.summary()}\n{route.describe()}")
    if all(leg.action == FORWARD for leg in route.legs):
        _say("Предупреждение: в маршруте нет поворотов, лодка поплывёт по прямой.")


# --------------------------------------------------------------- исполнение

class RouteRunner:
    """Ведёт лодку по маршруту в отдельном потоке.

    io: объект с методами key_down(key), key_up(key), press(key).
    Остановка и запуск троллинга -- нажатием J. pause() останавливает лодку
    (на время вываживания), resume() продолжает с того же места шага.
    """

    def __init__(self, route: Route, io, log=None, tick: float = 0.05, clock=time.monotonic,
                 position_fn=None, nav_params=None, diag_path: Path | None = None,
                 relocate_on_resume: bool = False, rod_busy_fn=None):
        route.validate()
        # rod_busy_fn() -> True, пока удилище в руках: J с удилищем в руках игра не принимает,
        # поэтому "лодка стоит -> нажимаю J" откладываем до того, как удилище уберут в подставку.
        self._rod_busy_fn = rod_busy_fn
        self.route, self._io, self._tick, self._clock = route, io, tick, clock
        _base_log = log or (lambda msg: None)
        self._log = lambda msg: _base_log(_tr(msg))
        self._position_fn = position_fn
        self._relocate = relocate_on_resume
        self._need_reloc = False
        self._reloc_reads: list = []
        self._reloc_since: float | None = None
        self._starts: list[float] = []
        acc = 0.0
        for leg in route.legs:
            self._starts.append(acc)
            acc += leg.sec
        self._trim_left = 0.0
        self._steer = bool(nav_params is not None and getattr(nav_params, 'steer', False))
        self._escape_until = 0.0
        self._jtest = 0            # 0 - нет, 1 - нажали J и ждём, поедет ли лодка
        self._jtest_pos = None
        self._jtest_until = 0.0
        self._jtest_block = 0.0
        self._escapes = 0
        self._hdg_sign = 1.0
        self._hdg_pending = None
        self._hdg_bad = 0
        self._rate_logged = float(route.turn_rate or 0.0)
        self._row_pending = None
        self._row_htrim = None
        self._hdg_last: float | None = None
        self._hdg_ok = 0
        self.stuck = False
        self._diag_path = diag_path
        self._diag_file = None
        self._prev_t: float | None = None
        self._lap_offs: list[float] = []
        self._lap_lat: list[float] = []
        # Метрики за круг (см. _record()): суммарный доворот A/D, суммарная
        # правка часов маршрута (snap) и доля "нечитаемых" (?) чтений компаса.
        self._lap_trim_sec = 0.0
        self._lap_snap_sec = 0.0
        self._lap_reads_total = 0
        self._lap_reads_unclear = 0
        self._reads_ok = 0
        self._reads_fail = 0
        self._hb_t: float | None = None
        self._hb_ok = 0
        self._hb_fail: dict[str, int] = {}
        self._nav = None
        self._nav_thread: threading.Thread | None = None
        if position_fn is not None and route.has_path:
            from rf4s.controller.navigation import Motion, Navigator  # noqa: PLC0415
            motion = Motion(route.left_sign, route.turn_rate, route.speed)
            self._nav = Navigator(route.path, route.legs, route.lap_sec, motion, nav_params, log=self._log,
                                  hdg=route.hdg)
            self._log("Маршрут: сверка по координатам включена"
                      + ("" if motion.usable else " (без подруливания: параметры поворота не определены)"))
        elif position_fn is not None:
            self._log("Маршрут: в записи нет координат, идём только по времени")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._idx = 0
        self._elapsed = 0.0
        self.moving = False
        self.finished = False
        self.laps = 0
        self.failed: BaseException | None = None
        self.nav_failed: BaseException | None = None

    @property
    def leg_index(self) -> int:
        return self._idx

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._idx, self._elapsed, self.laps = 0, 0.0, 0
            try:
                self._begin_moving()
            except Exception as err:
                self.failed, self.moving = err, False
                raise
            self._thread = threading.Thread(target=self._loop, name="route-runner", daemon=True)
            self._thread.start()
            if self._nav is not None:
                self._nav_thread = threading.Thread(target=self._nav_loop, name="route-nav", daemon=True)
                self._nav_thread.start()

    def pause(self) -> None:
        with self._lock:
            if self.moving:
                self._halt()
                self._log("Маршрут: пауза, лодка остановлена")

    def resume(self) -> None:
        with self._lock:
            if self._thread is None or self.moving or self.finished or self._stop.is_set():
                return
        # Чтение компаса в _relocate_on_resume() может занять до 4.5 с (реально
        # ждёт на sleep()) -- без блокировки, как и в navigate(), иначе на это
        # время встаёт advance()/_loop в фоновом потоке. Состояние перепроверяем
        # после: resume() могли отменить (stop()) пока мы ждали компас.
        self._relocate_on_resume()
        with self._lock:
            if self._thread is None or self.moving or self.finished or self._stop.is_set():
                return
            self._begin_moving()
            self._log(f"Маршрут: продолжение, шаг {self._idx + 1}/{len(self.route.legs)}")

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
            if thread.is_alive():
                # Поток не завершился за 2с (завис внутри io-вызова) -- в Python
                # его нельзя остановить принудительно, но раньше это проходило
                # совсем незаметно. Хотя бы логируем, чтобы было видно в логах,
                # что поток остался висеть как сирота.
                self._log("Маршрут: поток движения не завершился вовремя при остановке")
        nav_thread = self._nav_thread
        if nav_thread is not None and nav_thread is not threading.current_thread():
            nav_thread.join(timeout=2.0)
            if nav_thread.is_alive():
                self._log("Маршрут: поток навигации не завершился вовремя при остановке")
        with self._lock:
            if self.moving:
                self._halt()

    def advance(self, dt: float) -> None:
        """Продвинуть время на dt секунд; при необходимости сменить шаг."""
        with self._lock:
            if not self.moving:
                return
            self._elapsed += dt
            legs = self.route.legs
            if self._trim_left > 0:
                self._trim_left -= dt
                if self._trim_left <= 0:
                    self._apply(legs[self._idx].action)
            while self.moving and self._elapsed >= legs[self._idx].sec:
                self._elapsed -= legs[self._idx].sec
                self._idx += 1
                if self._idx >= len(legs):
                    if not self.route.loop:
                        self._halt()
                        self.finished = True
                        self._log("Маршрут пройден до конца, лодка остановлена")
                        return
                    self._idx = 0
                    self.laps += 1
                    self._log(f"Маршрут: круг {self.laps} пройден")
                self._apply(legs[self._idx].action)

    # -- внутреннее ---------------------------------------------------------
    def _begin_moving(self) -> None:
        self._io.press(TROLLING_KEY)
        self.moving = True
        self._apply(self.route.legs[self._idx].action)

    def _halt(self) -> None:
        # Каждый шаг отдельно и со своим try/except: если key_up(LEFT_KEY)
        # упадёт, key_up(RIGHT_KEY) и press(TROLLING_KEY) всё равно должны
        # выполниться -- иначе клавиша руления может остаться зажатой
        # физически (поток после вызова _halt() из обработчика ошибки в _loop
        # сразу завершается, отпустить её будет уже некому). self.moving
        # всегда переводится в False, даже если какой-то из шагов не удался.
        # Тот же принцип, что и в utils.safe_exit().
        for step in (
            lambda: self._io.key_up(LEFT_KEY),
            lambda: self._io.key_up(RIGHT_KEY),
            lambda: self._io.press(TROLLING_KEY),
        ):
            try:
                step()
            except Exception:  # noqa: BLE001
                self._log("Маршрут: ошибка при остановке лодки, освобождаю остальные клавиши")
        self.moving = False

    def _apply(self, action: str) -> None:
        self._trim_left = 0.0
        if action == LEFT:
            self._io.key_up(RIGHT_KEY)
            self._io.key_down(LEFT_KEY)
        elif action == RIGHT:
            self._io.key_up(LEFT_KEY)
            self._io.key_down(RIGHT_KEY)
        else:
            self._io.key_up(LEFT_KEY)
            self._io.key_up(RIGHT_KEY)

    def _relocate_on_resume(self, reads_needed: int = 3, timeout: float = 4.5) -> None:
        """После паузы лодка стоит: по её позиции находим точку пути и ставим на неё часы маршрута.

        Компас после вываживания и заброса закрывает голубая плашка уведомления
        («Рыба в садке», «Катушка закрыта») на несколько секунд. Лодка стоит, так что
        ждём, пока цифры не станут читаться (до timeout), и только потом едем. Если за
        это время компас так и не прочитан, релокацию сделает navigate() на ходу.
        """
        nav, fn = self._nav, self._position_fn
        if not self._relocate or nav is None or fn is None:
            return
        reads, started = [], time.monotonic()
        while len(reads) < reads_needed and time.monotonic() - started < timeout and not self._stop.is_set():
            pos = fn()
            if pos is not None:
                reads.append(pos)
            time.sleep(0.25)
        if len(reads) >= reads_needed and self._do_relocate(reads, apply=False, standing=True):
            return
        self._need_reloc, self._reloc_reads, self._reloc_since = True, [], None
        self._log("Маршрут: после паузы компас не прочитан, уточню положение уже на ходу")

    def _do_relocate(self, reads, apply: bool, standing: bool) -> bool:
        """Поставить часы маршрута по позиции (медиана reads). True - решение принято (в т. ч. 'часы верны')."""
        nav = self._nav
        xs, ys = sorted(r[0] for r in reads), sorted(r[1] for r in reads)
        pos = (xs[len(xs) // 2], ys[len(ys) // 2])
        t_now = self.route_time()
        found = nav.relocate(t_now, pos)
        where = "после паузы" if standing else "на ходу после паузы"
        if found is None:
            self._log(f"Маршрут: {where} позиция {pos[0]}:{pos[1]} далеко от пути, часы маршрута не меняю")
            return True
        t_new, dist = found
        shift = nav_circ(t_new, t_now, self.route.lap_sec)
        if abs(shift) < 2.0:
            self._log(f"Маршрут: {where} позиция {pos[0]}:{pos[1]} на линии (отклонение {dist:.1f}), "
                      f"часы верны ({shift:+.0f} с)")
            return True
        self._log(f"Маршрут: {where} позиция {pos[0]}:{pos[1]}, отклонение {dist:.1f}; "
                  f"лодка {'отстаёт' if shift < 0 else 'впереди'} на {abs(shift):.0f} с, "
                  f"часы маршрута {t_now:.0f} -> {t_new:.0f} с")
        self._set_time(t_new, apply=apply)
        self._last_cand_reset()
        return True

    def _relocate_after_jfix(self) -> None:
        """Лодка простояла - часы маршрута ушли вперёд; снова сверим их по позиции на ходу."""
        if self._relocate:
            self._need_reloc, self._reloc_reads, self._reloc_since = True, [], None

    def _last_cand_reset(self) -> None:
        nav = self._nav
        if nav is not None:
            nav._last_cand = None
            nav._lat.clear()

    def route_time(self) -> float:
        """Время на часах маршрута (с начала круга)."""
        return self._starts[self._idx] + self._elapsed

    def _set_time(self, t: float, apply: bool = True) -> None:
        """Поставить часы маршрута на время t внутри круга (apply=False: клавиши не трогать)."""
        legs = self.route.legs
        t = min(max(t, 0.0), self.route.lap_sec - 0.01)
        # default=0: _starts[0] == 0.0 всегда, так что при корректном t выше
        # генератор не бывает пустым; default -- просто подстраховка на случай,
        # если t когда-нибудь попадёт сюда не через clamp выше.
        idx = max((i for i, st in enumerate(self._starts) if st <= t), default=0)
        old = self._idx
        self._idx, self._elapsed = idx, min(t - self._starts[idx], legs[idx].sec)
        if idx != old and apply:
            self._apply(legs[idx].action)

    def navigate(self, now: float) -> None:
        """Один шаг сверки: прочитать позицию, при необходимости исправить время или курс."""
        self._row_htrim = None
        try:
            self._navigate_step(now)
        finally:
            self._flush_row()   # строка журнала пишется после шага: в неё попадает и подруливание по курсу

    def _navigate_step(self, now: float) -> None:
        nav = self._nav
        if nav is None:
            return
        with self._lock:
            if not self.moving or self.finished:
                return
            t_route = self.route_time()
            action = self.route.legs[self._idx].action
            leg_end = self._starts[self._idx] + self.route.legs[self._idx].sec
        pos = self._position_fn()  # чтение экрана - без блокировки
        hdg = self._read_heading()
        with self._lock:
            if not self.moving or self.finished or self._stop.is_set():
                return
            self._count_reads(pos, now, t_route)
            if self._need_reloc:
                if self._reloc_since is None:
                    self._reloc_since = now
                if pos is not None:
                    self._reloc_reads = (self._reloc_reads + [pos])[-3:]
                    if len(self._reloc_reads) >= 3:
                        self._need_reloc = False
                        self._do_relocate(self._reloc_reads, apply=True, standing=False)
                elif now - self._reloc_since > 40.0:
                    self._need_reloc = False
                    self._log("Маршрут: за 40 с компас так и не прочитался, часы маршрута не менял")
                return
            res = nav.step(now, t_route, action, leg_end, pos)
            self._record(now, t_route, action, pos, nav.diag, res)
            if self._jtest == 1:
                moved = (pos is not None and self._jtest_pos is not None
                         and math.hypot(pos[0] - self._jtest_pos[0], pos[1] - self._jtest_pos[1]) >= 1.0)
                if moved:
                    self._jtest = 0
                    nav.reset_stuck()
                    self._log("Маршрут: после нажатия J лодка поехала - режим тролинга был выключен (сбой J), дальше по маршруту")
                    self._relocate_after_jfix()
                    return
                if now > self._jtest_until:
                    self._io.press(TROLLING_KEY)  # вернуть как было
                    self._jtest = 0
                    self._jtest_block = now + 40.0
                    self._log("Маршрут: после J лодка не поехала, возвращаю J: значит она во что-то упёрлась")
                return
            if res.stuck and self._steer and self._jtest == 0 and now >= self._jtest_block \
                    and not self._rod_busy():
                self._jtest = 1
                self._jtest_pos = pos
                self._jtest_until = now + 9.0
                self._io.press(TROLLING_KEY)
                self._log("Маршрут: лодка стоит при включённом маршруте - нажимаю J (возможно, режим тролинга не включился)")
                return
            if res.stuck and not self.stuck:
                self.stuck = True
            elif not res.stuck:
                self.stuck = False
                self._escapes = 0
            if res.stuck and self._steer and now >= self._escape_until:
                # Упёрлись (скала, берег): поворачиваем носом в сторону линии маршрута, с каждой попыткой дольше.
                lat = (nav.diag or {}).get("lateral", 0.0)
                key = RIGHT if lat > 0 else LEFT
                self._escapes += 1
                hold = min(7.0, 2.5 + 1.5 * self._escapes)
                self._io.key_up(RIGHT_KEY if key == LEFT else LEFT_KEY)
                self._io.key_down(LEFT_KEY if key == LEFT else RIGHT_KEY)
                self._trim_left = hold
                self._escape_until = now + hold + 8.0
                self._log(f"Маршрут: лодка упёрлась, отворачиваю {ACTION_RU[key]} {hold:.1f} с (попытка {self._escapes})")
                return
            htrim = self._heading_step(nav, now, t_route, action, hdg)
            self._row_htrim = htrim
            if res.new_time is not None:
                self._log(f"Маршрут: сверка по координатам, {t_route:.1f} -> {res.new_time:.1f} с")
                self._lap_snap_sec += abs(res.new_time - t_route)
                self._set_time(res.new_time)
            elif (htrim is not None or res.trim is not None) and self.route.legs[self._idx].action == FORWARD:
                key, hold = htrim if htrim is not None else res.trim
                self._io.key_up(RIGHT_KEY if key == LEFT else LEFT_KEY)
                self._io.key_down(LEFT_KEY if key == LEFT else RIGHT_KEY)
                self._trim_left = hold
                self._lap_trim_sec += hold
                if htrim is not None:
                    self._hdg_pending = (now + hold + 1.5, self._hdg_last,
                                         (1 if key == RIGHT else -1) * hold * nav.motion.turn_rate, hold, self._idx)
                    self._log(f"Маршрут: курс {self._hdg_last:.0f} градусов, ошибка {nav.h_err:+.0f}: "
                              f"подруливание {ACTION_RU[key]} {hold:.1f} с")
                else:
                    self._log(f"Маршрут: подруливание {ACTION_RU[key]} {hold:.1f} с")

    def _rod_busy(self) -> bool:
        try:
            return bool(self._rod_busy_fn and self._rod_busy_fn())
        except Exception:  # noqa: BLE001
            return False

    def _read_heading(self):
        """Курс лодки по метке N на кольце компаса: (градусы, откалиброван ли центр) либо None."""
        reader = getattr(self._position_fn, "reader", None)
        if not self._steer or reader is None or not hasattr(reader, "read_heading"):
            return None
        try:
            return reader.read_heading()
        except Exception as err:  # noqa: BLE001
            if not getattr(self, "_hdg_err_logged", False):
                self._hdg_err_logged = True
                self._log(f"Маршрут: курс по компасу прочитать не удалось: {err!r}")
            return None

    def _heading_step(self, nav, now: float, t_route: float, action: str, hdg):
        """Обработать чтение курса: калибровка, проверка знака, подруливание по курсу."""
        if hdg is None:
            return None
        deg, calibrated = hdg
        deg = (self._hdg_sign * deg) % 360.0
        nav.hdg_sign = self._hdg_sign   # эталон из записи переводится в тот же знак
        self._hdg_last = deg
        self._hdg_ok += 1
        reader = getattr(self._position_fn, "reader", None)
        note = getattr(reader, "heading_note", None)
        if note:
            reader.heading_note = None
            self._log(f"Маршрут: {note}")
        nav.heading_mode = bool(calibrated) and not nav.h_bad and nav.offset_ready
        pend = self._hdg_pending
        if pend is not None and now >= pend[0]:
            self._hdg_pending = None
            before, expected, hold_p, idx_p = pend[1], pend[2], pend[3], pend[4]
            actual = (deg - before + 180.0) % 360.0 - 180.0
            if abs(expected) >= 8.0 and abs(actual) >= 4.0 and actual * expected < 0:
                self._hdg_bad += 1
                self._log(f"Маршрут: после подруливания курс изменился на {actual:+.0f}, ждали {expected:+.0f}")
                if self._hdg_bad >= 2:
                    self._hdg_sign, self._hdg_bad = -self._hdg_sign, 0
                    self._log("Маршрут: направление курса по компасу не сходится с поворотами, переворачиваю знак")
            elif actual * expected > 0:
                self._hdg_bad = 0
                if self._idx == idx_p:   # за время замера шаг маршрута не менялся
                    new = nav.learn_turn_rate(hold_p, actual)
                    if (new is not None and self._rate_logged > 0
                            and abs(new - self._rate_logged) > 0.15 * self._rate_logged):
                        self._rate_logged = new
                        self._log(f"Маршрут: скорость поворота по компасу уточнена до {new:.1f} град/с")
        if not calibrated:
            return None
        res = nav.heading_trim(now, t_route, action, deg)
        if nav.h_bad_msg:
            self._log(f"Маршрут: {nav.h_bad_msg}")
            nav.h_bad_msg = None
        if nav.offset_msg:
            self._log(f"Маршрут: {nav.offset_msg}")
            nav.offset_msg = None
        if nav.rec_msg:
            if nav.rec_conflicts <= 3:
                self._log(f"Маршрут: {nav.rec_msg}")
            nav.rec_msg = None
        nav.heading_mode = bool(calibrated) and not nav.h_bad and nav.offset_ready
        return res

    def _count_reads(self, pos, now: float, t_route: float) -> None:
        """Счётчики чтения компаса и сводка в лог раз в минуту."""
        if self._hb_t is None:
            self._hb_t = now
        self._lap_reads_total += 1
        if pos is not None:
            if self._reads_ok == 0:
                self._log(f"Маршрут: компас прочитан, позиция {pos[0]}:{pos[1]}")
            self._reads_ok += 1
            self._hb_ok += 1
        else:
            self._reads_fail += 1
            reader = getattr(self._position_fn, "reader", None)
            text = getattr(reader, "last_text", None)
            # "нет цифр на экране" -- компас просто не виден (не считаем как
            # "нечитаемый глиф"); "цифры не распознаны" -- шаблон совпал
            # неуверенно (есть "?" в прочитанном тексте) -- это и есть доля
            # нечитаемых цифр за круг, которую просил cloud: если шаблоны
            # калибровки не покрывают ночь/дождь, эта доля растёт, и
            # коррекция молча перестаёт работать.
            if text and "?" in text:
                self._lap_reads_unclear += 1
            kind = "нет цифр на экране" if not text else ("цифры не распознаны" if "?" in text else "прочее")
            self._hb_fail[kind] = self._hb_fail.get(kind, 0) + 1
        if now - self._hb_t >= 60.0:
            total = self._hb_ok + sum(self._hb_fail.values())
            fails = ", ".join(f"{k}: {v}" for k, v in self._hb_fail.items()) or "нет"
            last = f", позиция {pos[0]}:{pos[1]}" if pos is not None else ""
            hdg = ""
            if self._steer:
                state = "откалиброван" if (self._nav is not None and self._nav.heading_mode) else "не откалиброван"
                hdg = f", курс по метке N: {self._hdg_ok} чт., {state}"
                if self._hdg_last is not None:
                    hdg += f", {self._hdg_last:.0f} градусов"
            self._hdg_ok = 0
            self._log(f"Маршрут: компас за минуту: прочитан {self._hb_ok} из {total}, сбои ({fails}){last}{hdg}, "
                      f"шаг {self._idx + 1}/{len(self.route.legs)}, время маршрута {t_route:.0f} с")
            self._hb_t, self._hb_ok, self._hb_fail = now, 0, {}

    def _record(self, now, t_route, action, pos, diag, res) -> None:
        """Журнал отклонений от записанной линии: CSV и сводка по кругу в лог."""
        if self._prev_t is not None and t_route < self._prev_t - self.route.lap_sec / 2 and self._lap_offs:
            offs, lats = self._lap_offs, self._lap_lat
            unclear_pct = (
                100 * self._lap_reads_unclear / self._lap_reads_total
                if self._lap_reads_total else 0.0
            )
            self._log(
                f"Маршрут: итоги круга по координатам: уход от линии средний {sum(offs) / len(offs):.1f}, "
                f"максимум {max(offs):.1f}, в среднем в сторону {sum(lats) / len(lats):+.1f} (ед.; плюс - влево), "
                f"замеров {len(offs)}, доворот A/D {self._lap_trim_sec:.1f} с, "
                f"правка часов (snap) {self._lap_snap_sec:.1f} с, нечитаемых цифр {unclear_pct:.0f}%"
            )
            self._lap_offs, self._lap_lat = [], []
            self._lap_trim_sec = self._lap_snap_sec = 0.0
            self._lap_reads_total = self._lap_reads_unclear = 0
        self._prev_t = t_route
        if diag is not None:
            self._lap_offs.append(diag["off"])
            self._lap_lat.append(diag["lateral"])
        self._row_pending = (now, t_route, action, pos, diag, res)

    DIAG_HEADER = "clock,route_t,action,x,y,along_s,lateral,off,snap,trim,hdg,herr,htrim,href,hsrc\n"

    def _flush_row(self) -> None:
        """Записать строку журнала шага (CSV); htrim -- подруливание по курсу, href/hsrc -- эталон курса."""
        row, self._row_pending = self._row_pending, None
        if row is None or self._diag_path is None:
            return
        now, t_route, action, pos, diag, res = row
        try:
            if self._diag_file is None:
                self._diag_path.parent.mkdir(parents=True, exist_ok=True)
                if self._diag_path.exists():
                    try:
                        with open(self._diag_path, encoding="utf-8") as fh:
                            if fh.readline() != self.DIAG_HEADER:
                                self._diag_path.replace(self._diag_path.with_suffix(".csv.old"))
                    except OSError:
                        pass
                new = not self._diag_path.exists()
                self._diag_file = open(self._diag_path, "a", encoding="utf-8")  # noqa: SIM115
                if new:
                    self._diag_file.write(self.DIAG_HEADER)
            x, y = (pos if pos is not None else ("", ""))
            d = diag or {}
            hd = '' if self._hdg_last is None else f'{self._hdg_last:.0f}'
            he = '' if self._nav is None or self._nav.h_err is None else f'{self._nav.h_err:.0f}'
            ht = '' if self._row_htrim is None else f'{self._row_htrim[0]}:{self._row_htrim[1]}'
            hr = '' if self._nav is None or self._nav.h_ref is None else f'{self._nav.h_ref:.0f}'
            hs = '' if self._nav is None or self._nav.h_src is None else self._nav.h_src
            tail = f"{hd},{he},{ht},{hr},{hs}\n"
            if diag:
                self._diag_file.write(
                    f"{now:.1f},{t_route:.1f},{action},{x},{y},"
                    f"{d.get('along', ''):.1f},{d.get('lateral', ''):.2f},{d.get('off', ''):.2f},"
                    f"{'' if res.new_time is None else round(res.new_time, 1)},"
                    f"{'' if res.trim is None else res.trim[0] + ':' + str(res.trim[1])},{tail}"
                )
            else:
                self._diag_file.write(f"{now:.1f},{t_route:.1f},{action},{x},{y},,,,,,{tail}")
            self._diag_file.flush()
        except Exception:  # noqa: BLE001 - журнал не должен ломать плавание
            self._diag_path = None

    def _nav_loop(self) -> None:
        nav_period = self._nav.p.sync_every
        try:
            while not self._stop.wait(nav_period):
                self.navigate(self._clock())
        except Exception as err:  # noqa: BLE001
            # В отличие от _loop, смерть этого потока не фатальна для плавания
            # (лодка продолжает идти по времени), но раньше об этом не было
            # никакого следа снаружи, кроме строчки в логе. nav_failed даёт
            # вызывающему коду (Player._enable_route_trolling) возможность
            # заметить это и предупредить один раз явно.
            self.nav_failed = err
            self._log(f"Сверка по координатам выключена из-за ошибки: {err!r}")

    def _loop(self) -> None:
        last = self._clock()
        try:
            while not self._stop.wait(self._tick):
                now = self._clock()
                dt, last = now - last, now
                self.advance(dt)
        except Exception as err:  # noqa: BLE001
            self.failed = err
            self._log(f"Маршрут остановлен из-за ошибки: {err!r}")
            try:
                with self._lock:
                    if self.moving:
                        self._halt()
            except Exception:  # noqa: BLE001
                pass
