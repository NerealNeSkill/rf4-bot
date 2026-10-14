"""Обратная связь по позиции для маршрутного троллинга.

Запись маршрута сохраняет не только время нажатий, но и путь: отсчёты
координат компаса (t, x, y). При проплыве эти же координаты сверяются с
записанным путём:

1. Синхронизация прогресса. Позиция проецируется на касательную записанного
   пути в ожидаемый момент и сдвигает часы маршрута. Так пропадает накопленная
   ошибка по времени (разгон, пауза на вываживание, задержки).
2. Подруливание на прямых участках. По боковому смещению от пути и скорости
   его роста оценивается ошибка курса, и лодка получает короткий нажим A/D.

Координаты компаса целые и очень грубые: на троллинге лодка проходит около
0.2 единицы в секунду, то есть единица координат - это ~5 секунд хода. Поэтому
позиция и путь усредняются по времени (линейная регрессия), а все пороги
масштабируются по скорости лодки, найденной в записи, а не заданы в единицах.

Знак поворота "влево" (в системе координат карты он может быть любым),
скорость поворота и скорость лодки берутся из самой записи (estimate_motion).
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, replace

Point = tuple[float, float]


# ------------------------------------------------------------------ геометрия

def _vec(p0: Point, p1: Point) -> Point:
    return p1[0] - p0[0], p1[1] - p0[1]


def _norm(v: Point) -> float:
    return math.hypot(v[0], v[1])


def angle_between(a: Point, b: Point) -> float:
    """Угол от вектора a к b в градусах, (-180; 180]; плюс - против часовой в (x, y)."""
    return math.degrees(math.atan2(a[0] * b[1] - a[1] * b[0], a[0] * b[0] + a[1] * b[1]))


def circ_diff(a: float, b: float, lap: float) -> float:
    """Знаковая разность a - b на круге длиной lap."""
    if lap <= 0:
        return a - b
    d = (a - b) % lap
    return d - lap if d > lap / 2 else d


def _fit(samples, t_eval: float):
    """Линейная регрессия по (t, v1[, v2...]): список наклонов и значений в t_eval.

    Возвращает (slopes, values) либо None, если точек меньше двух или все t равны.
    """
    n = len(samples)
    if n < 2:
        return None
    dims = len(samples[0]) - 1
    mt = sum(s[0] for s in samples) / n
    stt = sum((s[0] - mt) ** 2 for s in samples)
    if stt < 1e-9:
        return None
    slopes, values = [], []
    for k in range(1, dims + 1):
        mv = sum(s[k] for s in samples) / n
        slope = sum((s[0] - mt) * (s[k] - mv) for s in samples) / stt
        slopes.append(slope)
        values.append(mv + slope * (t_eval - mt))
    return slopes, values


def smooth_path(path, half: float = 5.0):
    """Сгладить записанный путь регрессией в окне +-half секунд (убирает ступеньки целых координат)."""
    pts = [tuple(map(float, p)) for p in path]
    out = []
    for t, x, y in pts:
        window = [p for p in pts if abs(p[0] - t) <= half]
        fit = _fit(window, t) if len(window) >= 3 else None
        out.append((t, fit[1][0], fit[1][1]) if fit else (t, x, y))
    return out


def pos_at(path, t: float, lap: float) -> Point:
    """Позиция на пути в момент t (линейная интерполяция, по кругу длиной lap)."""
    if lap > 0:
        t = t % lap
    prev = path[0]
    if t <= prev[0]:
        return prev[1], prev[2]
    for cur in path[1:]:
        if cur[0] >= t:
            span = cur[0] - prev[0]
            k = 0.0 if span <= 0 else (t - prev[0]) / span
            return prev[1] + (cur[1] - prev[1]) * k, prev[2] + (cur[2] - prev[2]) * k
        prev = cur
    return path[-1][1], path[-1][2]


# ---------------------------------------------------- параметры из записи

@dataclass(frozen=True)
class Motion:
    left_sign: int      # +1/-1: знак angle_between для поворота влево; 0 - не определён
    turn_rate: float    # градусов в секунду при удержании A/D
    speed: float        # единиц координат в секунду на ходу

    @property
    def usable(self) -> bool:
        return self.left_sign != 0 and self.turn_rate > 1.0 and self.speed > 0.02


def _turn_time(legs) -> tuple[float, float]:
    left = sum(leg.sec for leg in legs if leg.action == "left")
    right = sum(leg.sec for leg in legs if leg.action == "right")
    return left, right


def _motion_from_loop(sp, legs, speed: float):
    """Круг замкнулся: полный оборот 360 градусов приходится на чистое время поворотов.

    Работает на грубых координатах, где курс по коротким отрезкам не измерить.
    Знак - по направлению обхода круга (знаковая площадь) и по тому, чего было
    больше, левых или правых нажатий.
    """
    xs, ys = [p[1] for p in sp], [p[2] for p in sp]
    extent = max(max(xs) - min(xs), max(ys) - min(ys))
    if extent <= 0:
        return None
    closure = math.hypot(xs[-1] - xs[0], ys[-1] - ys[0])
    if closure > 0.2 * extent:
        return None
    pts = list(zip(xs, ys))
    area = 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))
    if abs(area) < 0.1 * extent * extent:
        return None
    left, right = _turn_time(legs)
    net = left - right
    if abs(net) < 3.0:
        return None
    rate = 360.0 / abs(net)
    if not 1.0 <= rate <= 180.0:
        return None
    sign = (1 if area > 0 else -1) * (1 if net > 0 else -1)
    return Motion(sign, rate, speed)


def _motion_from_turns(path, legs, speed: float, window: float = 3.0, min_straight: float = 2.0):
    """Курс до и после каждого отдельного поворота (годится для мелких единиц координат)."""
    spans, t = [], 0.0
    for leg in legs:
        spans.append((leg.action, t, t + leg.sec))
        t += leg.sec
    pts = [tuple(map(float, p)) for p in path]
    signed_rates = []
    for i, (action, t0, t1) in enumerate(spans):
        if action == "forward" or t1 - t0 < 0.5 or i == 0 or i == len(spans) - 1:
            continue
        before, after = spans[i - 1], spans[i + 1]
        if before[0] != "forward" or after[0] != "forward":
            continue
        if before[2] - before[1] < min_straight or after[2] - after[1] < min_straight:
            continue
        wb = min(window, before[2] - before[1])
        wa = min(window, after[2] - after[1])
        h0 = _vec(pos_at(pts, t0 - wb, 0), pos_at(pts, t0, 0))
        h1 = _vec(pos_at(pts, t1, 0), pos_at(pts, t1 + wa, 0))
        if _norm(h0) < 1.0 or _norm(h1) < 1.0:
            continue
        diff = angle_between(h0, h1)
        if abs(diff) > 150:  # поворот больше 180 градусов - знак неоднозначен
            continue
        sign = 1 if action == "left" else -1
        signed_rates.append(sign * diff / (t1 - t0))
    if not signed_rates:
        return Motion(0, 0.0, speed)
    med = statistics.median(signed_rates)
    return Motion(1 if med > 0 else -1, statistics.median(abs(r) for r in signed_rates), speed)


def estimate_motion(path, legs, lap: float) -> Motion:
    """Знак поворота, скорость поворота и скорость лодки по записанному пути."""
    if len(path) < 8:
        return Motion(0, 0.0, 0.0)
    sp = smooth_path(path)
    span = sp[-1][0] - sp[0][0]
    length = sum(_norm(_vec((a[1], a[2]), (b[1], b[2]))) for a, b in zip(sp, sp[1:]))
    speed = length / span if span > 0 else 0.0
    return _motion_from_loop(sp, legs, speed) or _motion_from_turns(path, legs, speed)


def turn_rate_from_heading(legs, hdg, min_leg: float = 2.0) -> float | None:
    """Скорость поворота (градусов на секунду удержания A/D) по курсу с компаса из записи.

    Курс читается с точностью около градуса, поэтому это точнее оценки по грубым
    координатам. Берутся только отдельные повороты не короче min_leg секунд, вокруг
    которых нет других поворотов; отсчёт "до" -- последний перед нажатием, "после" --
    первый через 1-3 с после отпускания (лодка ещё доворачивает по инерции, и это
    часть реального действия удержания).
    """
    if not hdg or len(hdg) < 4:
        return None
    pts = sorted((float(t), float(h)) for t, h in hdg)
    spans, t = [], 0.0
    for leg in legs:
        spans.append((leg.action, t, t + leg.sec))
        t += leg.sec
    rates = []
    for action, t0, t1 in spans:
        if action == "forward" or t1 - t0 < min_leg:
            continue
        before = [p for p in pts if t0 - 2.0 <= p[0] <= t0]
        after = [p for p in pts if t1 + 1.0 <= p[0] <= t1 + 3.0]
        if not before or not after:
            continue
        tb, ta = before[-1][0], after[0][0]
        if any(a != "forward" and (b0 < ta and b1 > tb) and (b0, b1) != (t0, t1) for a, b0, b1 in spans):
            continue  # в окне измерения есть другой поворот
        diff = (after[0][1] - before[-1][1] + 180.0) % 360.0 - 180.0
        rates.append(abs(diff) / (t1 - t0))
    return statistics.median(rates) if len(rates) >= 2 else None


def path_bearing(path, start: float, end: float, speed: float):
    """Курс по пути (регрессия координат) на участке [start, end]: (градусы, погрешность, "xy") либо None.

    Курс в системе "камера смотрит по носу лодки": сверено по скриншотам -- у носа метка N на
    кольце компаса показывает ~231 градус, а ход лодки по координатам atan2(dx, dy) = ~241.
    (Прежняя формула atan2(-dx, -dy) подгонялась под курс при камере, смотрящей на корму, --
    отсюда сдвиг на 180 градусов между записью и заплывом.)"""
    pts = [(t, x, y) for t, x, y in path if start + 1.5 <= t <= end - 1.5]
    if len(pts) < 5 or pts[-1][0] - pts[0][0] < 8.0:
        return None
    n = len(pts)
    mt = sum(p[0] for p in pts) / n
    vt = sum((p[0] - mt) ** 2 for p in pts)
    if vt <= 1e-9:
        return None
    vx = sum((p[0] - mt) * p[1] for p in pts) / vt
    vy = sum((p[0] - mt) * p[2] for p in pts) / vt
    spd = math.hypot(vx, vy)
    if spd < 0.4 * max(speed, 1e-6):
        return None
    sigma = math.degrees(math.atan(0.7 / (spd * (pts[-1][0] - pts[0][0]))))
    return (math.degrees(math.atan2(vx, vy)) % 360.0, sigma, "xy")


# --------------------------------------------------------------- навигатор

@dataclass
class NavParams:
    sync_every: float = 1.0       # период чтения позиции, с
    smooth_sec: float | None = None   # окно усреднения позиции, с (None: по скорости лодки, 2-10 с)
    window: float = 8.0           # самый большой сдвиг часов за одну сверку, с
    max_off_sec: float = 40.0     # дальше такого пути хода от записанной линии чтение считается сбойным
    max_off_steer: float = 30.0   # при подруливании курс выправляем и на таких уходах от линии
    max_off_min: float = 6.0      # ...но не меньше стольких единиц координат
    snap_min: float | None = None     # меньшие расхождения по времени не исправляются, с (None: ~1.5 ед. хода)
    tan_half: float = 3.0         # касательная пути берётся по +-tan_half секунд
    steer: bool = True            # включено ли подруливание
    snap: bool = True             # включена ли сверка часов маршрута с позицией
    trim_deg: float = 8.0         # меньшая ошибка курса не подруливается
    offset_trim: float = 1.5      # смещение (ед.), с которого подруливаем без оценки курса
    trim_gain: float = 0.7        # доля ошибки, исправляемая за одно нажатие
    trim_max_hold: float = 4.0    # самое долгое подруливание за раз, с
    slope_sec: float | None = None    # окно оценки скорости ухода от пути, с (None: 6-20 с по скорости)
    lookahead_units: float | None = None  # на сколько единиц вперёд по пути целиться (None: max(4, 8 с хода))
    min_remaining: float = 3.0    # ближе конца прямого участка не подруливаем, с
    max_turn_frac: float = 0.1    # сверка только если за окно усреднения повернуто не дольше этой доли окна
    heading_deg: float = 6.0      # меньшая ошибка курса (по метке N) не подруливается, градусы
    heading_gain: float = 0.85    # доля ошибки курса, исправляемая за одно нажатие
    heading_max_hold: float = 8.0 # самое долгое подруливание по курсу за раз, с
    heading_settle: float = 2.5   # пауза после подруливания по курсу, с
    heading_max_sigma: float = 12.0   # прямой участок, чей курс по записи точнее этого, градусов
    stuck_min_sec: float = 10.0   # не меньше стольких секунд без движения - застряли
    heading_deg_rec: float = 4.0  # то же, когда эталон курса взят из записи (он точный, около 1-2 градусов)
    rec_min_pts: int = 3          # сколько отсчётов курса на участке нужно, чтобы доверять записи
    rec_xy_max_diff: float = 40.0  # записанный курс и курс по записанному пути расходятся сильнее - верим пути
    rec_max_std: float = 6.0      # курс на участке в записи гуляет сильнее этого (градусы) - эталон не берём
    rec_edge_start: float = 2.5   # с эталоном из записи подруливаем уже через столько секунд после начала участка
    rec_edge_end: float = 3.0     # ...и до стольких секунд до его конца
    lateral_dead: float = 0.5     # смещение от пути меньше этого (единицы) -- шум округления, поправку не даём
    corr_max: float = 35.0        # самая большая поправка курса за уход от линии, градусы (при сносе ветром/течением 20 мало)
    turn_ema: float = 0.35        # вес нового замера скорости поворота по компасу


@dataclass
class NavAction:
    new_time: float | None = None          # поставить часы маршрута на это время
    trim: tuple[str, float] | None = None  # ("left"/"right", секунд удержания)
    stuck: bool = False


class Navigator:
    """Решает, как исправить ход лодки по текущей позиции. Ввода-вывода не делает."""

    def __init__(self, path, legs, lap: float, motion: Motion, params: NavParams | None = None, log=None,
                 hdg=None):
        self.p = params or NavParams()
        # Записанный курс (t, градусы) -- точный эталон для подруливания; None -- по координатам.
        self.hdg = sorted((float(t), float(h)) for t, h in hdg) if hdg else []
        self.hdg_sign = 1.0            # +1/-1: знак курса, определённый на ходу (см. RouteRunner)
        self.h_ref: float | None = None   # эталонный курс последнего подруливания, градусы
        self.h_src: str | None = None     # откуда эталон: "rec" (запись) или "xy" (координаты)
        self._rate0 = motion.turn_rate
        self.path = smooth_path(path)
        self.legs = legs
        self.lap = lap
        self.motion = motion
        self._log = log or (lambda msg: None)
        speed = motion.speed
        clamp = lambda v, lo, hi: max(lo, min(hi, v))  # noqa: E731
        auto = speed > 0
        self.smooth_sec = self.p.smooth_sec or (clamp(3.0 / speed, 2.0, 10.0) if auto else 10.0)
        self.slope_sec = self.p.slope_sec or (clamp(8.0 / speed, 6.0, 20.0) if auto else 20.0)
        self.snap_min = self.p.snap_min or (max(2.0, 1.5 / speed) if auto else 2.0)
        self.look = self.p.lookahead_units or (max(4.0, speed * 8.0) if auto else 10.0)
        self.max_off = max(self.p.max_off_min, speed * self.p.max_off_sec) if speed > 0 else 80.0
        self.stuck_sec = max(self.p.stuck_min_sec, 2.5 / speed) if speed > 0 else self.p.stuck_min_sec
        self.stuck_dist = 0.2 * speed * self.stuck_sec if speed > 0 else 1.0
        self._spans: list[tuple[float, float]] = []   # интервалы поворотов (время маршрута)
        t = 0.0
        for leg in legs:
            if leg.action != "forward":
                self._spans.append((t, t + leg.sec))
            t += leg.sec
        self._hist: deque[tuple[float, Point]] = deque()
        self._lat: deque[tuple[float, float]] = deque()
        self._last_cand: float | None = None
        self._cool_until = 0.0
        self._last_warn = -1e9
        self.snaps = 0
        self.trims = 0
        self.rejects = 0
        self.diag: dict | None = None   # последний замер: along (с), lateral/off (ед.)
        self.heading_mode = False       # курс читается по компасу: подруливаем по нему, не по смещению
        self.h_err: float | None = None  # последняя ошибка курса, градусы (плюс - надо вправо)
        self._hcool = 0.0
        self._hv: deque = deque(maxlen=10)
        self._mv: list = []
        self._h_ok = False
        self.h_bad = False
        self.h_bad_msg = None
        self.rec_conflicts = 0
        self.hdg_offset = None          # сдвиг курса с компаса до курса хода лодки, градусы (выучивается)
        self.offset_msg = None
        self._off_vec = [0.0, 0.0]
        self._off_est = 0.0
        self._off_n = 0
        self._off_bad = 0
        self._lh = None
        self._xyb: dict = {}
        self.rec_bad = False
        self.rec_msg = None
        self._lb: dict = {}
        self._e_left: float | None = None
        self._off_last: float | None = None
        self._e_time = -1e9

    def reset_stuck(self) -> None:
        """Забыть историю позиций (после ручного вмешательства), чтобы не считать лодку стоящей."""
        self._hist.clear()

    def relocate(self, t_route: float, pos, back: float = 150.0, fwd: float = 30.0, max_off: float | None = None):
        """Время на записанном пути, ближайшее к позиции pos (лодка стоит).

        Ищется среди точек пути в окне [t_route - back, t_route + fwd] (по кругу).
        Нужен после паузы на вываживание: за паузу лодка теряет ход на разгон и
        сносится, а часы маршрута стоят, поэтому она отстаёт от них.
        Возвращает (время, расстояние до пути) либо None, если лодка слишком далеко от пути.
        """
        limit = max_off if max_off is not None else max(self.max_off, 12.0)
        best = None
        for t, x, y in self.path:
            dt = circ_diff(t, t_route, self.lap)
            if not -back <= dt <= fwd:
                continue
            cost = math.hypot(x - pos[0], y - pos[1]) + 0.002 * abs(dt)  # чуть предпочитаем ожидаемое время
            if best is None or cost < best[0]:
                best = (cost, t, math.hypot(x - pos[0], y - pos[1]))
        if best is None or best[2] > limit:
            return None
        return best[1] % self.lap if self.lap > 0 else best[1], best[2]

    # -- вспомогательное -------------------------------------------------------
    def _warn(self, now: float, msg: str) -> None:
        if now - self._last_warn >= 30.0:
            self._last_warn = now
            self._log(msg)

    def _turning_recent(self, t_route: float) -> float:
        """Сколько секунд из последних smooth_sec маршрута лодка поворачивала (с учётом круга)."""
        lo, hi = t_route - self.smooth_sec, t_route
        total = 0.0
        for a, b in self._spans:
            for shift in (0.0, self.lap, -self.lap) if self.lap > 0 else (0.0,):
                total += max(0.0, min(hi, b + shift) - max(lo, a + shift))
        return total

    def _path_straight(self, t: float, back: float, fwd: float, tol: float = 15.0, span: float | None = None) -> bool:
        """Прямой ли записанный путь на отрезке [t - back, t + fwd] (курс не уходит больше tol градусов)."""
        h = span if span is not None else self.p.tan_half

        def heading(tt: float):
            v = _vec(pos_at(self.path, tt - h, self.lap), pos_at(self.path, tt + h, self.lap))
            return v if _norm(v) > 1e-9 else None

        ref = heading(t)
        if ref is None:
            return False
        steps = max(2, int((back + fwd) // 3))
        for k in range(steps + 1):
            tt = t - back + (back + fwd) * k / steps
            if tt < 0 <= t and self.lap > 0 and t < back:
                tt = 0.0  # после конца круга не смотрим на предыдущий круг
            cur = heading(tt)
            if cur is None or abs(angle_between(ref, cur)) > tol:
                return False
        return True

    def _smoothed(self, now: float, raw: Point) -> Point:
        window = [(t, p[0], p[1]) for t, p in self._hist if now - t <= self.smooth_sec]
        if len(window) >= 4 and window[-1][0] - window[0][0] >= 0.5 * self.smooth_sec:
            fit = _fit(window, now)
            if fit:
                return fit[1][0], fit[1][1]
        return raw

    def _project(self, t_expect: float, pos: Point):
        """(время на пути, боковое смещение со знаком в системе карты, расстояние до пути)."""
        h, lap = self.p.tan_half, self.lap
        min_len = 0.3 * self.motion.speed * 2 * h
        t = t_expect
        for _ in range(3):
            here = pos_at(self.path, t, lap)
            tan = _vec(pos_at(self.path, t - h, lap), pos_at(self.path, t + h, lap))
            n = _norm(tan)
            if n <= max(min_len, 1e-9):
                break
            along = ((pos[0] - here[0]) * tan[0] + (pos[1] - here[1]) * tan[1]) / n
            t += max(-self.p.window, min(self.p.window, along / (n / (2 * h))))
        here = pos_at(self.path, t, lap)
        tan = _vec(pos_at(self.path, t - h, lap), pos_at(self.path, t + h, lap))
        n = _norm(tan)
        off = _norm(_vec(here, pos))
        lateral = 0.0
        if n > 1e-9:
            lateral = (tan[0] * (pos[1] - here[1]) - tan[1] * (pos[0] - here[0])) / n
        return t, lateral, off


    # -- подруливание по курсу с компаса -----------------------------------------
    def _leg_at(self, t: float):
        tt = t % self.lap if self.lap > 0 else t
        acc = 0.0
        for leg in self.legs:
            if acc <= tt < acc + leg.sec:
                return acc, acc + leg.sec, leg
            acc += leg.sec
        return None

    def _recorded_bearing(self, start: float, end: float):
        """Эталон курса по записанным отсчётам компаса на прямом участке: (курс, разброс) либо None."""
        if not self.hdg:
            return None
        pts = [h for t, h in self.hdg if start + 2.0 <= t <= end - 1.0]
        if len(pts) < self.p.rec_min_pts:
            return None
        sx = sum(math.sin(math.radians(h)) for h in pts)
        sy = sum(math.cos(math.radians(h)) for h in pts)
        mean = math.degrees(math.atan2(sx, sy)) % 360.0
        devs = [(h - mean + 180.0) % 360.0 - 180.0 for h in pts]
        std = math.sqrt(sum(d * d for d in devs) / len(devs))
        if std > self.p.rec_max_std:
            return None   # лодка на этом участке не держала курс -- по такому не рулим
        return (self.hdg_sign * mean) % 360.0, max(1.5, std)

    def _leg_bearing(self, start: float, end: float):
        """Курс в градусах компаса и его погрешность на прямом участке записи (см. path_bearing)."""
        key = (round(start, 2), round(end, 2), self.hdg_sign)
        if key in self._lb:
            return self._lb[key]
        rec = self._recorded_bearing(start, end)
        if rec is not None:
            xy = path_bearing(self.path, start, end, self.motion.speed)
            if xy is not None:
                diff = abs((rec[0] - xy[0] + 180.0) % 360.0 - 180.0)
                if diff > self.p.rec_xy_max_diff:
                    # курс в записи не сходится с самим записанным путём -- запись курса негодная
                    self.rec_conflicts += 1
                    if self.rec_conflicts >= 2:
                        self.rec_bad = True
                    self.rec_msg = (f"записанный курс ({rec[0]:.0f}) не сходится с записанным путём "
                                    f"({xy[0]:.0f}) на участке {start:.0f}-{end:.0f} с, беру курс по пути; "
                                    f"перепишите маршрут")
                    self._lb[key] = xy
                    return xy
            self._lb[key] = (rec[0], rec[1], "rec")
            return self._lb[key]
        res = path_bearing(self.path, start, end, self.motion.speed)
        self._lb[key] = res
        return res

    def _learn_offset(self, now: float, t_route: float, start: float, end: float, hdg: float) -> None:
        """Выучить сдвиг между курсом с компаса и курсом хода лодки.

        Кольцо компаса показывает, куда смотрит КАМЕРА, а не нос лодки: в записи рыбак смотрит по
        носу, а при троллинге бота -- на корму (сдвиг около 180 градусов). Эталон хода берётся из
        ЗАПИСАННОГО пути на текущем прямом участке (он точнее целочисленных координат лодки сейчас),
        а замер делается, только пока лодка идёт по линии и курс стоит ровно. Сдвиг усредняется;
        примерно через 6-10 с ровного хода курсу с компаса можно доверять."""
        prev, self._lh = self._lh, (now, hdg)
        if t_route < start + 4.0 or t_route > end - 1.0:
            return
        if prev is None or now - prev[0] > 3.0 or abs((hdg - prev[1] + 180.0) % 360.0 - 180.0) > 2.5:
            return
        if self._off_last is None or self._off_last > 1.5 or now - self._e_time > 6.0:
            return
        key = (round(start, 2), round(end, 2))
        if key not in self._xyb:
            self._xyb[key] = path_bearing(self.path, start, end, self.motion.speed)
        xy = self._xyb[key]
        if xy is None:
            return
        m = (xy[0] - hdg) % 360.0
        if self._off_n >= 6 and abs((m - self._off_est + 180.0) % 360.0 - 180.0) > 45.0:
            self._off_bad += 1
            if self._off_bad >= 6:   # камеру повернули (после вываживания и т.п.): учим заново
                self._off_vec, self._off_n, self._off_bad = [0.0, 0.0], 0, 0
                self.hdg_offset = None
                self.offset_msg = "камера повёрнута иначе, заново определяю сдвиг курса по компасу"
            return
        self._off_bad = 0
        self._off_vec[0] = 0.97 * self._off_vec[0] + math.cos(math.radians(m))
        self._off_vec[1] = 0.97 * self._off_vec[1] + math.sin(math.radians(m))
        self._off_n += 1
        self._off_est = math.degrees(math.atan2(self._off_vec[1], self._off_vec[0])) % 360.0
        if self._off_n >= 6:
            was = self.hdg_offset
            self.hdg_offset = self._off_est
            self._h_ok = True
            if was is None:
                self.offset_msg = (f"сдвиг курса по компасу относительно хода лодки {self._off_est:.0f} "
                                   f"градусов (камера {'на корму' if 135 <= self._off_est <= 225 else 'не по носу'}); "
                                   f"подруливание по курсу включено")

    @property
    def offset_ready(self) -> bool:
        return self.hdg_offset is not None

    def heading_trim(self, now: float, t_route: float, action: str, hdg: float):
        """Подруливание по курсу лодки hdg (градусы по часовой от севера). ("left"/"right", секунды) либо None."""
        self.h_err = None
        self.h_ref = self.h_src = None
        if action != "forward" or not self.motion.usable:
            self._lh = None
            return None
        leg = self._leg_at(t_route)
        if leg is None:
            return None
        start, end, _ = leg
        self._learn_offset(now, t_route, start, end, hdg)
        if self.hdg_offset is None:
            return None
        hdg = (hdg + self.hdg_offset) % 360.0
        lb = self._leg_bearing(start, end)
        if lb is None or lb[1] > self.p.heading_max_sigma:
            return None
        exp, sigma, src = lb
        rec = src == "rec"
        edge_a, edge_b = (self.p.rec_edge_start, self.p.rec_edge_end) if rec else (3.0, 3.5)
        if t_route - start < edge_a or end - t_route < edge_b:
            return None
        corr = 0.0
        if (self._e_left is not None and self._off_last is not None and now - self._e_time <= 6.0
                and self._off_last <= self.p.max_off_steer):
            # Смещение известно с шагом в единицу координат: меньше lateral_dead -- шум округления.
            e = math.copysign(max(0.0, abs(self._e_left) - self.p.lateral_dead), self._e_left)
            corr = max(-self.p.corr_max, min(self.p.corr_max, math.degrees(math.atan2(e, self.look))))
        self.h_ref, self.h_src = (exp + corr) % 360.0, src
        err = (exp + corr - hdg + 180.0) % 360.0 - 180.0
        self.h_err = err
        if self.h_bad:
            return None
        # Пока курс не сверен с реальным движением, большим ошибкам не верим.
        if abs(err) > (130.0 if self._h_ok else 70.0):
            return None
        base = self.p.heading_deg_rec if rec else self.p.heading_deg
        if now < self._hcool or abs(err) < max(base, 1.5 * sigma):
            return None
        hold = min(self.p.heading_max_hold, end - t_route - 1.0, self.p.heading_gain * abs(err) / self.motion.turn_rate)
        if hold < 0.3:
            return None
        self._hcool = now + hold + self.p.heading_settle
        self.trims += 1
        return ("right" if err > 0 else "left", round(hold, 2))

    def learn_turn_rate(self, hold: float, actual_deg: float) -> float | None:
        """Уточнить скорость поворота по реальному изменению курса после подруливания.

        Возвращает новое значение либо None, если замер слишком короткий или мелкий.
        Значение не уходит дальше чем в 2 раза от исходного (оценка из записи).
        """
        if hold < 0.8 or abs(actual_deg) < 4.0 or self._rate0 <= 0:
            return None
        implied = max(0.5 * self._rate0, min(2.0 * self._rate0, abs(actual_deg) / hold))
        a = self.p.turn_ema
        new = (1.0 - a) * self.motion.turn_rate + a * implied
        self.motion = replace(self.motion, turn_rate=new)
        return new

    # -- главный шаг -----------------------------------------------------------
    def step(self, now: float, t_route: float, action: str, leg_end: float, pos) -> NavAction:
        """now - монотонное время, t_route - время на часах маршрута,
        action - текущий шаг, leg_end - когда он кончится (время маршрута), pos - (x, y) либо None."""
        out = NavAction()
        self.diag = None
        if pos is None:
            return out
        raw = (float(pos[0]), float(pos[1]))
        self._hist.append((now, raw))
        horizon = max(self.smooth_sec, self.slope_sec, self.stuck_sec) + 2.0
        while self._hist and now - self._hist[0][0] > horizon:
            self._hist.popleft()

        # Застревание: лодка едет (по часам), а позиция стоит.
        old = next((h for h in self._hist if now - h[0] <= self.stuck_sec + 1.0), None)
        if old is not None and now - old[0] >= self.stuck_sec - 1.0:
            if _norm(_vec(old[1], raw)) < self.stuck_dist:
                out.stuck = True
                self._warn(now, f"Маршрут: лодка не двигается {self.stuck_sec:.0f} с (позиция {raw[0]:.0f}:{raw[1]:.0f})")

        # После заметных поворотов усреднённая позиция отстаёт на дуге: пережидаем.
        if self._turning_recent(t_route) > self.p.max_turn_frac * self.smooth_sec:
            self._last_cand = None
            self._lat.clear()
            return out

        sm = self._smoothed(now, raw)
        t_cand, lateral, off = self._project(t_route, sm)
        self.diag = {
            "along": circ_diff(t_cand, t_route, self.lap),
            "lateral": lateral * (self.motion.left_sign or 1),
            "off": off,
        }
        self._e_left, self._off_last, self._e_time = lateral * self.motion.left_sign, off, now
        far = off > self.max_off
        if far and not (self.p.steer and self.motion.usable and off <= self.p.max_off_steer):
            self.rejects += 1
            self._last_cand = None
            self._lat.clear()
            self._warn(now, f"Маршрут: позиция {raw[0]:.0f}:{raw[1]:.0f} далеко от пути ({off:.1f}), коррекция выключена")
            return out
        if far:
            # Лодка сильно ушла от линии: время не трогаем (проекция ненадёжна), но курс выправляем.
            self._last_cand = None
            self._warn(now, f"Маршрут: позиция {raw[0]:.0f}:{raw[1]:.0f} далеко от пути ({off:.1f}), только выправляю курс")

        # На изгибе записанного пути проекция по касательной неточна: ни время, ни курс не трогаем.
        if not self._path_straight(t_route, self.smooth_sec, self.p.tan_half, tol=40.0, span=8.0):
            self._last_cand = None
            self._lat.clear()
            return out

        # Время: исправляем, только когда два чтения подряд говорят одно и то же.
        diff = circ_diff(t_cand, t_route, self.lap)
        if self.p.snap and not far and abs(diff) >= self.snap_min:
            if self._last_cand is not None and abs(circ_diff(t_cand, self._last_cand, self.lap)) <= 3.0:
                out.new_time = t_cand % self.lap if self.lap > 0 else t_cand
                self.snaps += 1
                self._last_cand = None
                self._lat.clear()
                return out  # после смены времени шаг и остаток устарели
            self._last_cand = t_cand
        else:
            self._last_cand = None

        if not (self.p.steer and self.motion.usable) or self.heading_mode:
            return out
        if not self._path_straight(t_route, self.slope_sec, self.p.min_remaining, tol=45.0, span=8.0):
            self._lat.clear()
            return out
        # Боковое смещение влево от пути (в единицах); история нужна для скорости ухода.
        e_left = lateral * self.motion.left_sign
        self._lat.append((now, e_left))
        while self._lat and now - self._lat[0][0] > self.slope_sec:
            self._lat.popleft()

        if action != "forward" or now < self._cool_until:
            return out
        if leg_end - t_route < self.p.min_remaining:
            return out
        samples = list(self._lat)
        if len(samples) < 6 or samples[-1][0] - samples[0][0] < 0.5 * self.slope_sec:
            # На извилистом участке скорость ухода оценить не из чего (повороты каждые 20 с):
            # корректируем по самому смещению, считая, что курс сейчас совпадает с путём.
            if abs(e_left) >= self.p.offset_trim and samples:
                psi_des = max(-30.0, min(30.0, -math.degrees(math.atan2(e_left, self.look))))
                hold = min(self.p.trim_max_hold, self.p.trim_gain * abs(psi_des) / self.motion.turn_rate)
                if hold >= 0.5:
                    out.trim = ("left" if psi_des > 0 else "right", round(hold, 2))
                    self.trims += 1
                    self._cool_until = now + hold + 10.0
                    self._lat.clear()
            return out
        fit = _fit(samples, now)
        if fit is None:
            return out
        slope, e_now = fit[0][0], fit[1][0]

        # Курс относительно пути (влево - плюс): из скорости ухода в сторону.
        psi_now = math.degrees(math.asin(max(-0.6, min(0.6, slope / self.motion.speed))))
        psi_des = max(-30.0, min(30.0, -math.degrees(math.atan2(e_now, self.look))))
        delta = psi_des - psi_now
        if abs(delta) < self.p.trim_deg:
            return out
        hold = min(self.p.trim_max_hold, self.p.trim_gain * abs(delta) / self.motion.turn_rate)
        if hold < 0.3:
            return out
        out.trim = ("left" if delta > 0 else "right", round(hold, 2))
        self.trims += 1
        self._cool_until = now + hold + 0.8 * self.slope_sec
        self._lat.clear()  # скорость ухода после нажатия считаем заново
        return out
