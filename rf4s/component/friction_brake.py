"""Reel friction brake controller."""

import random
from dataclasses import dataclass, replace
from multiprocessing import Process, Value
from time import sleep, time

from rf4s.component.logger import logger
from rf4s.controller.detection import (
    BAR_HALF_WIDTH,
    BAR_RED_FRACTION,
    SENSITIVITY_FRACTIONS,
    ZONE_AMBER,
    ZONE_GREEN,
    ZONE_NAMES,
    ZONE_OVERLOAD,
    Detection,
)
from rf4s.utils import add_jitter, hw_scroll_queued

MAX_FRICTION_BRAKE = 30  # физическое количество делений катушки в игре
MIN_FRICTION_BRAKE = 0  # ниже нуля фрикцион не бывает, в конфиг не выносится

UP = 1
DOWN = -1

FRICTION_BRAKE_CHANGE_DELAY = 0.06  # пауза между щелчками колеса, с

_TRUE = {"1", "true", "yes", "on", "да"}
_FALSE = {"0", "false", "no", "off", "нет"}


def _to_bool(value) -> bool:
    # bool("false") == True, поэтому строки разбираем явно.
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ValueError(value)
    return bool(value)


def _cfg_value(cfg, key: str, default, cast):
    """Значение из конфига; если ключа нет или он пустой, берётся default."""
    try:
        value = getattr(cfg, key)
    except (AttributeError, KeyError):
        return default
    if value is None or isinstance(value, (dict, list)):
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        logger.warning(f"FRICTION_BRAKE.{key}: bad value {value!r}, using {default}")
        return default


# Ключ конфига -> (поле Params, тип). Все ключи необязательны.
_KEYS = {
    "MAX": ("max", int),
    "START_DELAY": ("start_delay", float),
    "INCREASE_STEP": ("increase_step", int),
    "ORANGE_INCREASE": ("orange_increase", _to_bool),
    "INCREASE_DELAY": ("increase_delay", float),
    "GREEN_INCREASE": ("green_increase", _to_bool),
    "GREEN_INCREASE_DELAY": ("green_increase_delay", float),
    "GREEN_FROM": ("green_from", float),
    "AMBER_FROM": ("amber_from", float),
    "DECREASE_STEP": ("decrease_step", int),
    "DECREASE_DELAY": ("decrease_delay", float),
    "BURNING_DECREASE": ("burning_decrease", _to_bool),
    "LIMIT_RELAX_DELAY": ("limit_relax_delay", float),
    "POLL_DELAY": ("poll_delay", float),
    "IDLE_POLL_DELAY": ("idle_poll_delay", float),
    "FULL_RESYNC_EVERY": ("full_resync_every", int),
    "DEBUG": ("debug", _to_bool),
}
_DELAYS = ("start_delay", "increase_delay", "green_increase_delay",
           "decrease_delay", "limit_relax_delay")


@dataclass(frozen=True)
class Params:
    """Настройки регулятора. По умолчанию -- поведение прежней рабочей версии."""

    max: int = MAX_FRICTION_BRAKE
    start_delay: float = 2.0
    increase_step: int = 1
    orange_increase: bool = True
    increase_delay: float = 1.0
    green_increase: bool = False
    green_increase_delay: float = 3.0
    green_from: float = 0.15
    amber_from: float = 0.55
    overload_from: float = 0.80  # с какой доли шкалы СБРОС; по умолчанию из SENSITIVITY
    decrease_step: int = 1
    decrease_delay: float = 1.0
    burning_decrease: bool = True
    limit_relax_delay: float = 10.0
    poll_delay: float = 0.1
    idle_poll_delay: float = 0.5  # опрос, пока рыба не на крючке, с
    full_resync_every: int = 10
    debug: bool = True

    def describe_zones(self) -> str:
        """Пороги зон в долях шкалы и в пикселях от центра (для лога)."""

        def fmt(name: str, fraction: float) -> str:
            return f"{name} {fraction:.0%} ({round(BAR_HALF_WIDTH * fraction)}px)"

        return (
            f"{fmt('green', self.green_from)} | {fmt('amber', self.amber_from)} | "
            f"{fmt('red', BAR_RED_FRACTION)} | {fmt('DECREASE', self.overload_from)}"
        )

    @property
    def fractions(self) -> tuple:
        """Точки шкалы для Detection: green, amber, red (видимая), overload (сброс)."""
        return (self.green_from, self.amber_from, BAR_RED_FRACTION, self.overload_from)

    @classmethod
    def from_cfg(cls, cfg) -> "Params":
        sens = _cfg_value(cfg, "SENSITIVITY", "high", str).strip().lower()
        if sens not in SENSITIVITY_FRACTIONS:
            logger.warning(f"FRICTION_BRAKE.SENSITIVITY: unknown {sens!r}, using 'high'")
            sens = "high"
        d = cls(overload_from=SENSITIVITY_FRACTIONS[sens])
        p = replace(d, **{
            field: _cfg_value(cfg, key, getattr(d, field), cast)
            for key, (field, cast) in _KEYS.items()
        })
        # OVERLOAD_FROM заменяет SENSITIVITY; прежнее имя RED_FROM принимается как синоним.
        legacy = _cfg_value(cfg, "RED_FROM", p.overload_from, float)
        p = replace(p, overload_from=_cfg_value(cfg, "OVERLOAD_FROM", legacy, float))

        # Защита от неверных значений.
        fixes = {}
        if not MIN_FRICTION_BRAKE < p.max <= MAX_FRICTION_BRAKE:
            fixes["max"] = d.max
        if not (0.05 <= p.green_from < p.amber_from < 0.75
                and p.amber_from < p.overload_from <= 0.98):
            logger.warning(
                "FRICTION_BRAKE: need 0.05 <= GREEN_FROM < AMBER_FROM < 0.75 "
                "and AMBER_FROM < OVERLOAD_FROM <= 0.98, using defaults"
            )
            fixes.update(green_from=d.green_from, amber_from=d.amber_from,
                         overload_from=d.overload_from)
        for field in ("increase_step", "decrease_step"):
            if getattr(p, field) < 1:
                fixes[field] = getattr(d, field)
        for field in _DELAYS:
            if getattr(p, field) < 0:
                fixes[field] = getattr(d, field)
        if p.poll_delay < 0.02:
            fixes["poll_delay"] = d.poll_delay
        if p.idle_poll_delay < 0.1:
            fixes["idle_poll_delay"] = d.idle_poll_delay
        if p.full_resync_every < 0:
            fixes["full_resync_every"] = d.full_resync_every
        if fixes:
            logger.warning(f"FRICTION_BRAKE: invalid values replaced: {fixes}")
            p = replace(p, **fixes)
        for key in ("GREEN_FROM", "AMBER_FROM", "OVERLOAD_FROM"):
            value = getattr(p, key.lower())
            if 0.48 <= value <= 0.52 or 0.73 <= value <= 0.77:
                logger.warning(
                    f"FRICTION_BRAKE.{key}={value}: this point lies on a bar "
                    "divider and may never light up"
                )
        return p


class FrictionBrake:
    """Friction brake controller."""

    def __init__(self, cfg, lock, detection: Detection, cmd_queue) -> None:
        self.cfg = cfg
        self.lock = lock
        self.detection = detection
        self.cmd_queue = cmd_queue

        if self.cfg.ARGS.FEATURE == "bot":
            self.fb_cfg = self.cfg.BOT.FRICTION_BRAKE
        else:
            self.fb_cfg = self.cfg.FRICTION_BRAKE

        self.params = Params.from_cfg(self.fb_cfg)
        if getattr(self.cfg.ARGS, "DATA", False) and not self.params.debug:
            self.params = replace(self.params, debug=True)  # флаг -d включает и отладку фрикциона
        logger.info(f"Friction brake params: {self.params}")
        logger.info("Friction brake bar zones: " + self.params.describe_zones())

        initial = _cfg_value(self.fb_cfg, "INITIAL", self.params.max, int)
        self.cur = Value("i", max(MIN_FRICTION_BRAKE, min(initial, MAX_FRICTION_BRAKE)))

        # Состояние ресинхронизации; используется только в основном процессе.
        self._synced = False  # известно ли реальное положение в игре
        self._fast_resets = 0
        self._next_resync_at = self._random_resync_interval()

        self.monitor_process = Process(
            target=monitor_friction_brake,
            args=(self,),
            daemon=True,
        )
        
    def _random_resync_interval(self) -> int:
        """Случайный интервал ресинка вокруг значения из конфига (±20%)."""
        every = self.params.full_resync_every
        if every <= 0:
            return 0
        jitter = max(1, int(every * 0.2))
        return max(1, random.randint(every - jitter, every + jitter))        

    def _scroll(self, direction: int, times: int) -> None:
        for _ in range(times):
            hw_scroll_queued(direction, self.cmd_queue)
            sleep(add_jitter(FRICTION_BRAKE_CHANGE_DELAY))

    def reset(self, target: int, full: bool = False) -> None:
        """Reset the friction brake to the target value.

        Usually only the difference between the tracked value and the target
        is scrolled. A full reset (to the maximum and back) is done on the
        first call, on request, and every FULL_RESYNC_EVERY calls.
        """
        target = max(MIN_FRICTION_BRAKE, min(target, MAX_FRICTION_BRAKE))
        every = self.params.full_resync_every
        with self.lock:
            if full or not self._synced or (every and self._fast_resets >= self._next_resync_at):
                logger.info("Resetting friction brake (full)")
                sleep(add_jitter(FRICTION_BRAKE_CHANGE_DELAY))
                self._scroll(UP, MAX_FRICTION_BRAKE)
                self._scroll(DOWN, MAX_FRICTION_BRAKE - target)
                self._synced = True
                self._fast_resets = 0
                # Генерируем новый случайный интервал для следующего ресинка
                self._next_resync_at = self._random_resync_interval()
            else:
                delta = self.cur.value - target
                if delta == 0:
                    logger.info("Friction brake already at target")
                else:
                    logger.info(f"Resetting friction brake ({delta:+d})")
                    sleep(add_jitter(FRICTION_BRAKE_CHANGE_DELAY))
                    self._scroll(DOWN if delta > 0 else UP, abs(delta))
                self._fast_resets += 1

            self.cur.value = target

    def change(self, increase: bool, steps: int = 1) -> int:
        """Change the friction brake by up to `steps`; return clicks made."""
        cur = self.cur.value
        if increase:
            new = min(cur + steps, self.params.max)
            count = max(0, new - cur)
        else:
            new = max(cur - steps, MIN_FRICTION_BRAKE)
            count = max(0, cur - new)

        direction = UP if increase else DOWN
        for i in range(count):
            if i:
                sleep(add_jitter(FRICTION_BRAKE_CHANGE_DELAY))
            hw_scroll_queued(direction, self.cmd_queue)
        if count:
            self.cur.value = new
        return count


def _increase(fb: FrictionBrake, limit: int, step: int) -> None:
    """Прибавить не больше step и не выше текущего потолка limit."""
    with fb.lock:
        room = limit - fb.cur.value
        if room > 0:
            fb.change(increase=True, steps=min(step, room))


def monitor_friction_brake(friction_brake: FrictionBrake) -> None:
    """Monitor the tension bar and react to its zone.

      * empty bar                  -> nothing;
      * green zone                 -> +INCREASE_STEP every GREEN_INCREASE_DELAY
                                      of continuous green (if GREEN_INCREASE);
      * yellow-orange (amber) zone -> +INCREASE_STEP every INCREASE_DELAY
                                      of continuous amber (if ORANGE_INCREASE);
      * red zone before SENSITIVITY point -> nothing;
      * SENSITIVITY point reached or reel burning -> -DECREASE_STEP, the
        ceiling drops below the overload value and rises by 1 every
        LIMIT_RELAX_DELAY seconds without overload.
    """
    logger.info("Monitoring friction brake")

    fb = friction_brake
    p = fb.params
    jitter = fb.cfg.BOT.JITTER_SCALE
    fractions = p.fractions
    fish_hooked = False
    last_state = None
    limit = p.max
    amber_since = green_since = relax_since = time()

    try:
        while True:
            try:
                snapshot = fb.detection.get_friction_brake_snapshot(fractions)
                if snapshot is None:
                    logger.error(
                        "Friction brake monitor stopped: no screen "
                        "coordinates for this window size"
                    )
                    return
                hooked, zone, burning, pixels = snapshot

                if not hooked:
                    fish_hooked = False
                    last_state = None
                    sleep(add_jitter(p.idle_poll_delay))
                    continue

                if not fish_hooked:
                    fish_hooked = True
                    sleep(add_jitter(p.start_delay, jitter))
                    limit = p.max
                    amber_since = green_since = relax_since = time()
                    continue  # снимок устарел после задержки

                if p.debug and (zone, burning) != last_state:
                    last_state = (zone, burning)
                    logger.info(
                        f"fb zone={ZONE_NAMES[zone]} burning={burning} "
                        f"pixels={pixels} cur={fb.cur.value} limit={limit}"
                    )

                if zone == ZONE_OVERLOAD or (burning and p.burning_decrease):
                    with fb.lock:
                        limit = max(MIN_FRICTION_BRAKE, min(limit, fb.cur.value - 1))
                        fb.change(increase=False, steps=p.decrease_step)
                    sleep(add_jitter(p.decrease_delay, jitter))
                    amber_since = green_since = relax_since = time()
                    continue

                now = time()
                # Потолок после сброса постепенно возвращается к MAX.
                if limit >= p.max:
                    relax_since = now
                elif now - relax_since >= p.limit_relax_delay:
                    relax_since = now
                    limit += 1

                # Отсчёт идёт только пока шкала НЕПРЕРЫВНО в своей зоне.
                if zone != ZONE_AMBER:
                    amber_since = now
                if zone != ZONE_GREEN:
                    green_since = now

                if burning:
                    pass  # перегрев при BURNING_DECREASE=false: просто не прибавляем
                elif (zone == ZONE_AMBER and p.orange_increase
                      and now - amber_since >= p.increase_delay):
                    amber_since = now
                    _increase(fb, limit, p.increase_step)
                elif (zone == ZONE_GREEN and p.green_increase
                      and now - green_since >= p.green_increase_delay):
                    green_since = now
                    _increase(fb, limit, p.increase_step)

                sleep(p.poll_delay)

            except Exception:
                logger.exception("Friction brake monitor error")
                sleep(1)

    except KeyboardInterrupt:
        pass
