"""Basic tackle actions like pulling, pirking, etc."""

import random
from dataclasses import dataclass, replace
from enum import Enum, auto
import time
from time import sleep
from typing import Literal

import pyautogui as pag
from PIL import ImageFilter
from pyscreeze import Box

import win32api
import win32con
import ctypes

from rf4s import exceptions, utils
from rf4s.component.logger import logger
from rf4s.controller.detection import (
    BAR_HALF_WIDTH,
    BAR_RED_FRACTION,
    ZONE_RED,
    Detection,
)
from rf4s.controller.timer import Timer
from rf4s.utils import add_jitter, press

CAST_SCALE = 0.4
ANIMATION_DELAY = 0.6
LOOP_DELAY = 0.5
PULL_LMB_RESET_DELAY = 0.20
SINK_DELAY = 2
NUM_OF_MOVEMENT = 4
LANDING_NET_DURATION = 6

OFFSET = 100


def _to_bool(value) -> bool:
    # bool("false") == True, поэтому строки разбираем явно.
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on", "да"):
            return True
        if text in ("0", "false", "no", "off", "нет"):
            return False
        raise ValueError(value)
    return bool(value)


def _opt(cfg, key: str, default, cast):
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
        logger.warning(f"BOT.PULL.{key}: bad value {value!r}, using {default}")
        return default


@dataclass(frozen=True)
class PullOptions:
    """Настройки фолбэков pull() (секция BOT.PULL). Все ключи необязательны."""

    tension_fallback: bool = True  # решать по шкале натяжения, а не по таймеру
    emergency_delay: float = 180.0  # аварийный таймаут при рабочей шкале, с; 0 -- выкл
    low_tension_fallback: bool = True  # рыба на крючке, натяжение минимальное -> lift
    low_tension_max: float = 0.30  # доля полудлины шкалы, ниже которой натяжение "минимальное"
    low_tension_delay: float = 15.0  # сколько секунд подряд, с
    low_tension_breaks: int = 2  # замеров подряд вне условия, после которых отсчёт заново
    snag_by_tension: bool = True  # нет рыбы + красная шкала = зацеп
    snag_log_only: bool = True  # только писать в лог, исключение не поднимать
    snag_red_from: float = BAR_RED_FRACTION  # с какой доли шкалы заливка считается красной (0.78)
    snag_delay: float = 3.0  # сколько секунд подряд, с
    snag_samples: int = 3  # минимум замеров подряд
    log_state: bool = True  # писать в лог смену состояния шкалы в pull()
    lift_duration: float = 3.0  # длительность удержания ПКМ при раскачивании, с
    lift_release_delay: float = 3.0  # пауза между раскачиваниями, с
    # Смотка закончилась (рыба у кончика удилища): сколько секунд поднимать удилище до
    # подсака, с. Подсак достаётся сразу, не дожидаясь LIFT-таймаута, чтобы тяжёлая рыба
    # (карп, амур) не висела на удилище. 0 -- старое поведение (ждать таймаут).
    net_after_finish_delay: float = 0.6

    @classmethod
    def from_cfg(cls, cfg) -> "PullOptions":
        pull_cfg = getattr(cfg.BOT, "PULL", None)
        d = cls()
        keys = {
            "ENABLE_TENSION_FALLBACK": ("tension_fallback", _to_bool),
            "EMERGENCY_DELAY": ("emergency_delay", float),
            "LOW_TENSION_FALLBACK": ("low_tension_fallback", _to_bool),
            "LOW_TENSION_MAX": ("low_tension_max", float),
            "LOW_TENSION_DELAY": ("low_tension_delay", float),
            "LOW_TENSION_BREAK_SAMPLES": ("low_tension_breaks", int),
            "SNAG_BY_TENSION": ("snag_by_tension", _to_bool),
            "SNAG_LOG_ONLY": ("snag_log_only", _to_bool),
            "SNAG_RED_FROM": ("snag_red_from", float),
            "SNAG_CONFIRM_DELAY": ("snag_delay", float),
            "SNAG_CONFIRM_SAMPLES": ("snag_samples", int),
            "LOG_STATE": ("log_state", _to_bool),
            "LIFT_DURATION": ("lift_duration", float),
            "LIFT_RELEASE_DELAY": ("lift_release_delay", float),
            "NET_AFTER_FINISH_DELAY": ("net_after_finish_delay", float),
        }
        p = replace(d, **{
            field: _opt(pull_cfg, key, getattr(d, field), cast)
            for key, (field, cast) in keys.items()
        })

        fixes = {}
        if not 0.05 <= p.low_tension_max <= 0.75:
            fixes["low_tension_max"] = d.low_tension_max
        if not 0.5 <= p.snag_red_from <= 0.98:
            fixes["snag_red_from"] = d.snag_red_from
        for field in ("emergency_delay", "low_tension_delay", "snag_delay", "net_after_finish_delay"):
            if getattr(p, field) < 0:
                fixes[field] = getattr(d, field)
        for field in ("lift_duration", "lift_release_delay"):
            if getattr(p, field) < 0.5:
                fixes[field] = getattr(d, field)        
        for field in ("low_tension_breaks", "snag_samples"):
            if getattr(p, field) < 1:
                fixes[field] = getattr(d, field)
        if fixes:
            logger.warning(f"BOT.PULL: invalid values replaced: {fixes}")
            p = replace(p, **fixes)
        if 0.73 <= p.snag_red_from <= 0.77:
            logger.warning(
                f"BOT.PULL.SNAG_RED_FROM={p.snag_red_from}: this point lies on "
                "a bar divider; the red zone may not be detected"
            )
        return p


class StageId(Enum):
    RESET = auto()
    CAST = auto()
    RETRIEVE = auto()
    PULL = auto()
    PIRK = auto()
    ELEVATE = auto()
    LIFT = auto()


class Tackle:
    """Class for all tackle-dependent methods."""

    def __init__(self, cfg, timer: Timer, detection: Detection):
        self.cfg = cfg
        self.timer = timer
        self.detection = detection
        self.available = True
        self.gear_ratio_changed = False
        self.stage = None
        self._pull_fallback = False
        # (приостановить ввод, возобновить ввод): задаёт Player, чтобы зажатые мышь и Shift
        # отпускались и снова нажимались при потере фокуса игрой (alt+tab)
        self.focus_hooks = None

    def wait_for_game_focus(self) -> bool:
        """Если окно игры свернули или потеряло фокус, дождаться возврата и продолжить.

        Зажатые мышь/Shift на время ожидания отпускаются, а после возврата нажимаются
        заново: игра теряет состояние зажатой кнопки при alt+tab, и без повторного
        нажатия подмотка не возобновлялась. True, если пауза была.
        """
        if not getattr(self.cfg.BOT, "PAUSE_ON_FOCUS_LOSS", True):
            return False
        if getattr(self.cfg.PROFILE, "MODE", "") == "telescopic":
            return False  # телескоп по-прежнему перезабрасывает после возврата в игру
        window = self.detection.window
        if window.is_game_foreground():
            return False
        logger.warning("Game window is not in the foreground, waiting for it to come back")
        suspend, resume = self.focus_hooks or (None, None)
        if suspend:
            suspend()
        waited = 0.0
        while not window.is_game_foreground():
            sleep(0.5)
            waited += 0.5
            if waited >= 600.0:
                logger.warning("Game window did not return for 10 minutes, continuing anyway")
                break
        sleep(1.0)  # дать игре перерисоваться и принять ввод
        logger.info("Game window is back (%.0f s), resuming", waited)
        if resume:
            resume()
        self.timer.set_timeout_start_time()
        return True

    def check_rare_events(self) -> None:
        if self.detection.is_tackle_broken():
            raise exceptions.TackleBrokenError
        if self.detection.is_disconnected():
            raise exceptions.DisconnectedError
        if self.detection.is_ticket_expired():
            raise exceptions.TicketExpiredError
        if self.detection.is_stuck_at_casting():
            raise exceptions.StuckAtCastingError

    def reset(self) -> None:
        """Reset the tackle until ready and detect unexpected events."""
        logger.info("Resetting tackle")

        if self.stage != StageId.RESET:
            self.stage = StageId.RESET
            self.timer.set_timeout_start_time()
        while True:
            if self.detection.is_tackle_ready():
                return
            if self.detection.is_fish_hooked():
                raise exceptions.FishHookedError
            if self.detection.is_fish_captured():
                raise exceptions.FishCapturedError
            if not self.detection.is_bait_chosen():
                raise exceptions.BaitNotChosenError
            if self.cfg.BOT.SPOOLING_DETECTION and self.detection.is_line_at_end():
                raise exceptions.LineAtEndError
            if self.cfg.BOT.SNAG_DETECTION and self.detection.is_line_snagged():
                raise exceptions.LineSnaggedError
            if self.detection.is_lure_broken():
                raise exceptions.LureBrokenError
            if self.detection.is_tackle_broken():
                raise exceptions.TackleBrokenError
            if not self.detection.is_dry_mix_chosen():
                raise exceptions.DryMixNotChosenError
            if self.timer.is_rare_event_checkable():
                self.check_rare_events()
            sleep(add_jitter(LOOP_DELAY))

    def cast(self, lock: bool) -> None:
        logger.info("Casting rod")
        self.stage = StageId.CAST

        if self.cfg.ARGS.MOUSE and random.random() < 0.36:
            self.move_mouse_randomly()

        match self.cfg.PROFILE.CAST_POWER_LEVEL:
            case 1:
                utils.click(button="left")

            case 5:
                utils.key_down("shift")
                try:
                    sleep(add_jitter(0.15))

                    utils.mouse_down("left")
                    if utils.HW_TRACE:
                        logger.info("Cast: mouse held")
                    try:
                        sleep(add_jitter(1.0))
                    finally:
                        utils.mouse_up("left")
                        if utils.HW_TRACE:
                            logger.info("Cast: mouse release sent")

                    sleep(add_jitter(0.1))
                finally:
                    utils.key_up("shift")

            case _:
                duration = CAST_SCALE * (self.cfg.PROFILE.CAST_POWER_LEVEL - 1)
                self.hold_mouse_button(duration)

        sleep(
            add_jitter(
                self.cfg.PROFILE.CAST_DELAY,
                self.cfg.BOT.JITTER_SCALE,
            )
        )

        if lock:
            utils.click(button="left")

    def sink(self) -> None:
        logger.info("Sinking lure")
        self.timer.set_timeout_start_time()
        while not self.timer.is_sink_stage_timeout():
            if self.detection.is_moving_in_bottom_layer():
                logger.info("Lure has reached bottom layer")
                sleep(add_jitter(SINK_DELAY))
                self.timer.print_sink_duration()
                break

            if self.detection.is_fish_hooked_twice():
                utils.click(button="left")
                return
            sleep(add_jitter(LOOP_DELAY))
        self.hold_mouse_button(self.cfg.PROFILE.TIGHTEN_DURATION)

    def retrieve(self) -> None:
        logger.info("Retrieving fishing line")
        if self.stage != StageId.RETRIEVE:
            self.stage = StageId.RETRIEVE
            self.timer.set_timeout_start_time()
        while True:
            self.wait_for_game_focus()
            if self.detection.is_fish_hooked():
                return
            if self.detection.is_retrieval_finished():
                return
            if self.detection.is_fish_captured():
                raise exceptions.FishCapturedError
            if self.cfg.BOT.SPOOLING_DETECTION and self.detection.is_line_at_end():
                raise exceptions.LineAtEndError
            if self.cfg.BOT.SNAG_DETECTION and self.detection.is_line_snagged():
                raise exceptions.LineSnaggedError
            if self.timer.is_rare_event_checkable():
                self.check_rare_events()
            sleep(add_jitter(LOOP_DELAY))

    def pull(self) -> None:
        """Retrieve the line until the end is reached and detect unexpected events."""
        logger.info("Pulling fish")

        if self.stage != StageId.PULL:
            self.stage = StageId.PULL
            self.timer.set_timeout_start_time()
            
        # Реакция на поклёвку: человек не начинает тянуть мгновенно (0.12-0.5 с)    
        sleep(utils.human_reaction_time())
        pull_start_time = time.monotonic()
        self._pull_fallback = False
        self._pull_finished = False
        # Первое раскачивание не сразу, а после пары секунд подмотки
        next_lift_time = pull_start_time + random.uniform(1.0, 3.0)

        opt = PullOptions.from_cfg(self.cfg)
        low_since = None  # с какого момента рыба на крючке, а натяжение минимальное
        low_breaks = 0
        snag_since = None  # с какого момента нет рыбы, а шкала красная
        snag_samples = 0
        snag_logged = False
        last_state = None

        while True:
            self.wait_for_game_focus()
            # ---------------------------------------------------------
            # АВТОРСКАЯ ПРОВЕРКА ОКОНЧАНИЯ СМОТКИ
            # ---------------------------------------------------------
            if self.detection.is_retrieval_finished():
                self._pull_finished = True
                return

            # ---------------------------------------------------------
            # АВТОРСКАЯ ЛОГИКА
            # ---------------------------------------------------------
            if self.cfg.ARGS.LIFT:
                now = time.monotonic()
                if now >= next_lift_time:
                    # Рандомизация ±25% вокруг значения из конфига
                    dur = opt.lift_duration * random.uniform(0.75, 1.25)
                    rel = opt.lift_release_delay * random.uniform(0.75, 1.25)
                    self.hold_mouse_button(dur, button="right")
                    next_lift_time = time.monotonic() + rel

            if self.detection.is_fish_captured():
                raise exceptions.FishCapturedError

            # Один скриншот HUD: иконки рыбы, зацепа, шпули и шкала натяжения.
            # None -- нет координат для этого окна или фолбэк выключен;
            # тогда работают авторские проверки и таймер PULL_FALLBACK_DELAY.
            state = (
                self.detection.get_tension_state(red_from=opt.snag_red_from)
                if opt.tension_fallback
                else None
            )

            if self.cfg.BOT.SPOOLING_DETECTION and (
                state.line_end if state is not None else self.detection.is_line_at_end()
            ):
                raise exceptions.LineAtEndError

            if self.cfg.BOT.SNAG_DETECTION and (
                state.snagged if state is not None else self.detection.is_line_snagged()
            ):
                raise exceptions.LineSnaggedError

            if self.timer.is_rare_event_checkable():
                self.check_rare_events()

            # ---------------------------------------------------------
            # FALLBACK
            # ---------------------------------------------------------
            now = time.monotonic()
            if state is None:
                # Исходное поведение: просто таймер.
                timeout = self.cfg.BOT.PULL_FALLBACK_DELAY
                if now - pull_start_time >= timeout:
                    logger.warning(
                        "Pull fallback after %.1fs. Switching to lift.", timeout
                    )
                    self._pull_fallback = True
                    self.lift()
                    return
            else:
                if opt.log_state and (state.hooked, state.zone) != last_state:
                    last_state = (state.hooked, state.zone)
                    logger.info(
                        "Pull state: fish=%s zone=%d fill=%dpx (%.0f%%)",
                        state.hooked, state.zone, state.fill_px,
                        100 * state.fill_px / BAR_HALF_WIDTH,
                    )

                # Нет рыбы + красная шкала = зацеп. Подтверждается несколькими
                # замерами подряд в течение snag_delay секунд; любой замер
                # вне условия начинает отсчёт заново.
                if opt.snag_by_tension and not state.hooked and state.zone == ZONE_RED:
                    if snag_since is None:
                        snag_since, snag_samples = now, 0
                    snag_samples += 1
                    if (
                        snag_samples >= opt.snag_samples
                        and now - snag_since >= opt.snag_delay
                    ):
                        message = (
                            "Snag by tension: no fish icon, bar in red zone "
                            "(%d px, %d samples, %.1fs)."
                        )
                        if opt.snag_log_only:
                            if not snag_logged:
                                snag_logged = True
                                logger.warning(
                                    message + " Log only (SNAG_LOG_ONLY).",
                                    state.fill_px, snag_samples, now - snag_since,
                                )
                        else:
                            logger.warning(
                                message, state.fill_px, snag_samples, now - snag_since
                            )
                            raise exceptions.LineSnaggedError
                else:
                    snag_since, snag_samples, snag_logged = None, 0, False

                # Рыба на крючке, натяжение минимальное -> lift(). Короткие
                # выбросы (low_tension_breaks - 1 замеров подряд) отсчёт не
                # сбрасывают; пропажа иконки рыбы сбрасывает сразу.
                if (
                    opt.low_tension_fallback
                    and state.hooked
                    and state.fill_px < BAR_HALF_WIDTH * opt.low_tension_max
                ):
                    low_breaks = 0
                    if low_since is None:
                        low_since = now
                    elif now - low_since >= opt.low_tension_delay:
                        logger.warning(
                            "Low tension fallback: fish hooked, tension below "
                            "%.0f%% for %.1fs (%d px). Switching to lift.",
                            100 * opt.low_tension_max, now - low_since, state.fill_px,
                        )
                        self._pull_fallback = True
                        self.lift()
                        return
                elif low_since is not None:
                    low_breaks += 1
                    if not state.hooked or low_breaks >= opt.low_tension_breaks:
                        low_since, low_breaks = None, 0

                # Аварийный таймаут: защита от зависания, не обычный путь.
                if (
                    opt.emergency_delay > 0
                    and now - pull_start_time >= opt.emergency_delay
                ):
                    logger.warning(
                        "Emergency pull timeout after %.1fs. Switching to lift.",
                        opt.emergency_delay,
                    )
                    self._pull_fallback = True
                    self.lift()
                    return

            sleep(add_jitter(LOOP_DELAY))

            # Оба таймера срабатывают только если функция включена. Раньше при
            # выключенной передаче (-GR) is_gear_ratio_changeable() после первой
            # минуты вываживания был истинен ВСЕГДА, и тяжёлая рыба перезапускала
            # pull() каждую секунду, пока бот не выходил из игры по лимиту повторов.
            if self.cfg.ARGS.COFFEE and self.timer.is_coffee_drinkable():
                raise exceptions.CoffeeTimeoutError

            if (
                self.cfg.ARGS.GEAR_RATIO
                and not self.gear_ratio_changed
                and self.timer.is_gear_ratio_changeable()
            ):
                raise exceptions.GearRatioTimeoutError

    def special_retrieve(self, button: str) -> None:
        self.timer.set_timeout_start_time()
        while not self.timer.is_special_retrieve_timeout():
            self.hold_mouse_button(self.cfg.PROFILE.RETRIEVAL_DURATION, button)
            sleep(
                add_jitter(self.cfg.PROFILE.RETRIEVAL_DELAY, self.cfg.BOT.JITTER_SCALE)
            )
            if (
                self.detection.is_fish_hooked()
                or self.detection.is_retrieval_finished()
            ):
                return

    def pirk(self) -> None:
        logger.info("Performing pirking")

        if self.stage != StageId.PIRK:
            self.stage = StageId.PIRK
            self.timer.set_timeout_start_time()
        while not self.timer.is_pirk_stage_timeout():
            if self.detection.is_tackle_ready():
                return

            if self.detection.is_fish_hooked_twice():
                if not self.cfg.PROFILE.PIRK_RETRIEVAL:
                    utils.click(button="left")
                return

            if self.cfg.PROFILE.PIRK_DURATION > 0:
                if self.cfg.PROFILE.CTRL:
                    utils.key_down("ctrl")
                if self.cfg.PROFILE.SHIFT:
                    utils.key_down("shift")
                self.hold_mouse_button(self.cfg.PROFILE.PIRK_DURATION, button="right")
                if self.cfg.PROFILE.CTRL:
                    utils.key_up("ctrl")
                if self.cfg.PROFILE.SHIFT:
                    utils.key_up("shift")
                sleep(
                    add_jitter(self.cfg.PROFILE.PIRK_DELAY, self.cfg.BOT.JITTER_SCALE)
                )
            else:
                sleep(add_jitter(LOOP_DELAY))
            if self.timer.is_rare_event_checkable():
                self.check_rare_events()
        raise exceptions.PirkTimeoutError

    def elevate(self) -> None:
        logger.info("Performing elevating")
        locked = True
        dropped = False
        if self.stage != StageId.ELEVATE:
            self.stage = StageId.ELEVATE
            self.timer.set_timeout_start_time()
        while not self.timer.is_elevate_stage_timeout():
            if self.detection.is_fish_hooked_twice():
                if not self.cfg.PROFILE.PIRK_RETRIEVAL:
                    utils.click(button="left")
                return

            if self.cfg.PROFILE.DROP and not dropped:
                utils.press("enter")
                if locked:
                    delay = self.cfg.PROFILE.ELEVATE_DELAY
                else:
                    delay = self.cfg.PROFILE.ELEVATE_DURATION
                sleep(add_jitter(delay))
            else:
                if locked:
                    sleep(add_jitter(self.cfg.PROFILE.ELEVATE_DELAY))
                else:
                    self.hold_mouse_button(self.cfg.PROFILE.ELEVATE_DURATION)
            locked = not locked

            if self.timer.is_rare_event_checkable():
                self.check_rare_events()
                dropped = not dropped

    def lift(self) -> None:
        logger.info("Lifting rod")
        if self.stage != StageId.LIFT:
            self.stage = StageId.LIFT
            if self.cfg.PROFILE.MODE == "telescopic":
                utils.press("space")
            self.timer.set_timeout_start_time()
        if self.cfg.PROFILE.MODE == "telescopic":
            self._telescopic_lift()
        else:
            self._lift()

    @utils.toggle_right_mouse_button
    def _lift(self) -> None:
        logger.info("LIFT handler entered")

        is_fallback = self._pull_fallback
        self._pull_fallback = False
        # Леска смотана до конца: подсак после короткого подъёма, а не по LIFT-таймауту.
        finished = getattr(self, "_pull_finished", False) and not is_fallback
        self._pull_finished = False
        net_delay = PullOptions.from_cfg(self.cfg).net_after_finish_delay
        early = finished and net_delay > 0
        if early:
            logger.info("Line fully retrieved. Netting after %.1fs of lifting.", net_delay)
            net_at = time.monotonic() + net_delay * random.uniform(0.9, 1.2)

        while not self.timer.is_lift_stage_timeout():
            if early and time.monotonic() >= net_at:
                break
            sleep(add_jitter(LOOP_DELAY))

            if self.detection.is_fish_captured():
                logger.info("Fish captured during lift (yanked out).")
                return

            if self.cfg.BOT.SNAG_DETECTION and self.detection.is_line_snagged():
                raise exceptions.LineSnaggedError

            if self.timer.is_rare_event_checkable():
                self.check_rare_events()

            if self.cfg.ARGS.COFFEE and self.timer.is_coffee_drinkable():
                raise exceptions.CoffeeTimeoutError

        if not self.detection.is_fish_hooked():
            return

        # Если рыба на крючке есть и этап подъема завершился — всегда пробуем подсак
        logger.info(
            "%s Opening landing net. fallback=%s, finished=%s",
            "Line retrieved." if early else "Lift timeout reached.",
            is_fallback,
            finished,
        )
        # Микро-пауза перед подсаком, как человек целится
        sleep(random.uniform(0.3, 0.8))
        press("space")
        sleep(add_jitter(LANDING_NET_DURATION))

        if self.detection.is_fish_captured():
            return

        logger.info("Fish not netted. Hiding landing net.")
        press("space")
        sleep(add_jitter(ANIMATION_DELAY))

        raise exceptions.LiftTimeoutError

    def _telescopic_lift(self) -> None:
        if not self.detection.is_fish_hooked():
            return

        while not self.timer.is_lift_stage_timeout():
            sleep(add_jitter(LOOP_DELAY))
            if self.detection.is_fish_captured():
                return
            if self.cfg.BOT.SNAG_DETECTION and self.detection.is_line_snagged():
                raise exceptions.LineSnaggedError
            if self.timer.is_rare_event_checkable():
                self.check_rare_events()
            if self.cfg.ARGS.COFFEE and self.timer.is_coffee_drinkable():
                raise exceptions.CoffeeTimeoutError
        raise exceptions.LiftTimeoutError

    def change_gear_ratio_or_electro_mode(self) -> None:
        logger.info("Changing gear ratio / electro assist mode")
        utils.key_down("ctrl")
        try:
            utils.press("space")
        finally:
            utils.key_up("ctrl")
        self.gear_ratio_changed = not self.gear_ratio_changed

    def move_mouse_randomly(self) -> None:
        logger.info("Moving mouse randomly")

        dx = random.randint(-150, 150)
        dy = random.randint(-90, 90)

        if utils.is_hw_active():
            utils.hw_move_mouse_bezier(dx, dy)
            sleep(random.uniform(0.12, 0.28))
            utils.hw_move_mouse_bezier(-dx, -dy)
        else:
            steps_out = random.randint(14, 24)
            x_steps_out = utils.generate_non_linear_steps(dx, steps_out)
            y_steps_out = utils.generate_non_linear_steps(dy, steps_out)

            for step_x, step_y in zip(x_steps_out, y_steps_out):
                ctypes.windll.user32.mouse_event(
                    0x0001, int(step_x), int(step_y), 0, 0
                )
                sleep(random.gauss(0.010, 0.002))

            sleep(random.uniform(0.12, 0.32))

            steps_in = random.randint(14, 24)
            x_steps_in = utils.generate_non_linear_steps(-dx, steps_in)
            y_steps_in = utils.generate_non_linear_steps(-dy, steps_in)

            for step_x, step_y in zip(x_steps_in, y_steps_in):
                jitter_x = random.choice([-1, 0, 1]) if random.random() < 0.3 else 0
                jitter_y = random.choice([-1, 0, 1]) if random.random() < 0.3 else 0
                ctypes.windll.user32.mouse_event(
                    0x0001, int(step_x + jitter_x), int(step_y + jitter_y), 0, 0
                )
                sleep(random.gauss(0.010, 0.002))

    def equip_item(self, item) -> None:
        if item == "lure":
            self._equip_item_from_menu(item)
        else:
            self._equip_item_from_inventory(item)

    def _equip_item_from_menu(self, item: str) -> None:
        logger.info("Equiping new %s from menu", item)
        utils.key_down("b")
        try:
            self._equip_favorite_item(item)
        finally:
            utils.key_up("b")
        sleep(add_jitter(ANIMATION_DELAY))

    @utils.press_before_and_after("v")
    def _equip_item_from_inventory(
        self, item: Literal["dry_mix", "groundbait"]
    ) -> None:
        """Equip an item from the inventory.

        :param item: The item to equip (e.g., dry_mix, groundbait).
        :type item: Literal["dry_mix", "groundbait"]
        """
        logger.info("Equiping new %s from inventory", item)

        # Сначала проверяем предмет на первом экране.
        position = self.get_item_position(item)
        if position is not None:
            x, y = utils.get_box_center_integers(position)
            utils.move_to(x, y)
            utils.click(button="left")
            self._equip_favorite_item(item)
            return

        scrollbar_position = self.detection.get_scrollbar_position()
        if scrollbar_position is None:
            return

        scrollbar_x, scrollbar_y = utils.get_box_center_integers(scrollbar_position)
        utils.move_to(scrollbar_x, scrollbar_y)

        for _ in range(5):
            sleep(add_jitter(ANIMATION_DELAY))

            # Универсальный drag: utils сам решит, использовать плату или pag.
            utils.mouse_down("left")
            sleep(0.15)
            utils.hw_move_mouse_bezier(0, 125, steps=15, register_session=False)
            sleep(0.15)
            utils.mouse_up("left")

            position = self.get_item_position(item)
            if position is not None:
                px, py = utils.get_box_center_integers(position)
                utils.move_to(px, py)
                utils.click(button="left")
                self._equip_favorite_item(item)
                break

    def get_item_position(self, item: str) -> Box | None:
        if item == "groundbait":
            return self.detection.get_groundbait_position()
        elif item == "dry_mix":
            return self.detection.get_dry_mix_position()
        else:
            return self.detection.get_pva_position()

    def _equip_favorite_item(self, item: str):
        sleep(add_jitter(ANIMATION_DELAY))
        logger.info("Looking for favorite items")
        favorite_item_positions = list(self.detection.get_favorite_item_positions())
        if item == "lure":
            random.shuffle(favorite_item_positions)

        for favorite_item_position in favorite_item_positions:
            x, y = utils.get_box_center_integers(favorite_item_position)
            if item == "lure" and self.detection._get_pixel(x - 70, y + 190) == (178, 59, 30):
                continue
            
            utils.move_to(x - 70, y + 190)
            sleep(random.uniform(0.35, 0.65))
            utils.mouse_down("left")
            sleep(random.uniform(0.045, 0.085))
            utils.mouse_up("left")
            sleep(random.uniform(0.06, 0.12))
            utils.mouse_down("left")
            sleep(random.uniform(0.045, 0.085))
            utils.mouse_up("left")
            
            logger.info("New %s equiped successfully", item)
            return

        if item in ("dry_mix", "groundbait"):
            utils.press("esc")
        raise exceptions.ItemNotFoundError

    def _monitor_float_state(self) -> None:
        logger.info("Monitoring float state")
        reference_img = pag.screenshot(region=self.detection.float_camera_rect)
        blurred = reference_img.filter(ImageFilter.GaussianBlur(radius=3))
        self.timer.set_timeout_start_time()
        while not self.timer.is_drift_stage_timeout():
            sleep(add_jitter(self.cfg.PROFILE.CHECK_DELAY))
            if self.detection.is_float_state_changed(blurred):
                logger.info("Float status changed")
                return
            if self.timer.is_rare_event_checkable():
                self.check_rare_events()
        raise exceptions.DriftTimeoutError

    def _monitor_clip_state(self) -> None:
        logger.info("Monitoring clip state")
        self.timer.set_timeout_start_time()
        while not self.timer.is_drift_stage_timeout():
            sleep(add_jitter(self.cfg.PROFILE.CHECK_DELAY))
            if self.detection.is_clip_open():
                logger.info("Clip status changed")
                return
            if self.timer.is_rare_event_checkable():
                self.check_rare_events()
        raise exceptions.DriftTimeoutError

    def hold_mouse_button(self, duration: float = 1, button: str = "left") -> None:
        if duration == 0:
            return

        if button == "left":
            utils.hw_hold_left_with_jitter(duration)
        else:
            utils.mouse_down("right")
            try:
                sleep(add_jitter(duration))
            finally:
                utils.mouse_up("right")

        if self.cfg.BOT.CLICK_LOCK and button == "left" and duration >= 2.1:
            utils.click(button="left")

    def hold_mouse_buttons(self, duration: float = 1) -> None:
        utils.key_down("ctrl")
        try:
            utils.mouse_down("left")
            utils.mouse_down("right")
            try:
                sleep(add_jitter(duration))
            finally:
                utils.mouse_up("left")
                utils.mouse_up("right")
        finally:
            utils.key_up("ctrl")

        if self.cfg.BOT.CLICK_LOCK and duration >= 2.1:
            utils.click(button="left")