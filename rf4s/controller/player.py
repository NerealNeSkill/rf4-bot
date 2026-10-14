"""Monolithic module for the bot logic."""

import json
import os
import random
import sys
from contextlib import contextmanager
from multiprocessing import Lock
from pathlib import Path

from time import sleep, time
from typing import Callable

import pyautogui as pag
from rich import box, print
from rich.table import Table

from rf4s import config, exceptions, utils
from rf4s.component.friction_brake import FrictionBrake
from rf4s.component.tackle import Tackle
from rf4s.controller.logger import logger
from rf4s.controller import trolling
from rf4s.controller.detection import Detection, TagColor
from rf4s.controller.notification import send_result, send_screenshot
from rf4s.controller.timer import Timer
from rf4s.result.result import BotResult
from rf4s.utils import add_jitter, press

FISH_CHECK_DURATION = 0.5
ANIMATION_DELAY = 0.5
# Пауза между постановкой удилища в подставку и нажатием J: пока идёт анимация ("руки заняты"),
# игра J не принимает и троллинг не включается.
TROLL_HOLDER_SETTLE = 3.5
# Пауза после укладки удилища в подставку (press("0")) перед J: пока идёт анимация укладки, J игра не принимает
ROD_STOW_DELAY = 1.5
# Пауза после J (старт лодки) перед тем, как снова взять удилище в руки
AFTER_J_DELAY = 1.0
# Проверка рыбы сразу после заброса: пауза перед проверкой и предел подряд идущих поклёвок
RECAST_RECHECK_DELAY = 1.5
RECAST_RECHECK_LIMIT = 3
LOOP_DELAY = 1
USE_ITEM_DELAY = 1
BAD_CAST_DELAY = 1
WEAR_TEXT_UPDATE_DELAY = 2
GET_DIGGING_TOOL_DELAY = 3
CLICK_LOCK_DURATION = 2.2
TAG_ANIMATION_DELAY = 10
DROP_ROD_DELAY = 4
TICKET_EXPIRE_DELAY = 8
DISCONNECTED_DELAY = 8

# Ограничение на повтор действия через error_handler() (reset/retrieve/pull/
# pirk/elevate/lift): раньше `while True: with error_handler(): ...; break`
# повторял без паузы и без предела, пока error_handler не перестанет ловить
# исключение. Если исправление не помогает (например, нужный флаг в конфиге
# выключен, и error_handler просто ничего не делает), бот крутился вечно.
MAX_ERROR_HANDLER_ATTEMPTS = 10
MAX_ERROR_HANDLER_SECONDS = 120.0

# handle_events(): если is_event_triggered() почему-то остаётся True (ложное
# срабатывание детекта/завис оверлей), раньше бот нажимал Enter бесконечно.
MAX_EVENT_ITERATIONS = 20

TROLLING_KEY = "j"
# «Выбрать следующий билет» (подсказка справа): выбранный билет встаёт в очередь
# и включится сам, когда кончится текущая аренда, так что выбирать можно заранее.
TICKET_NEXT_KEY = "l"
TICKET_HINT_CHECK_EVERY = 20.0   # с между проверками подсказки
TICKET_HINT_RETRY_DELAY = 60.0   # с до повтора, если подсказка осталась после выбора
TICKET_HINT_MAX_FAILS = 3        # столько неудач подряд -- дальше только запасной путь
LEFT_KEY = "a"
RIGHT_KEY = "d"

if utils.is_compiled():
    OUTER_ROOT = Path(sys.executable).parent
else:
    OUTER_ROOT = Path(__file__).resolve().parents[2]


class Player:
    def __init__(self, cfg, timer: Timer, detection: Detection, result: BotResult):
        self.cfg = cfg
        self.timer = timer
        self.detection = detection
        self.result = result

        self.tackle_idx = 0
        if self.cfg.PROFILE.MODE == "bottom":
            self.num_tackle = self._bottom_rod_count()
        else:
            self.num_tackle = 1
        self.tackles = [
            Tackle(cfg, self.timer, self.detection) for _ in range(self.num_tackle)
        ]
        self.tackle = self.tackles[self.tackle_idx]
        utils.HW_TRACE = bool(getattr(self.cfg.ARGS, "DATA", False))  # флаг -d: трассировка ввода
        for tackle in self.tackles:
            tackle.focus_hooks = (self._suspend_inputs, self._resume_inputs)

        self.cur_coffee = 0
        self.have_new_lure = True
        self.have_new_groundbait = True
        self.have_new_dry_mix = True
        self.have_new_pva = True
        self.friction_brake = None

        self.trolling_started = False
        self.route_runner = None
        self._route_mode_warned = False
        self._nav_failed_warned = False
        self.mouse_pressed = False
        self.shift_pressed = False
        self.using_spod_rod = False
        self.skip_cast = self.cfg.ARGS.SKIP_CAST
        self.rod_in_hands = False   # маршрутный троллинг: J с удилищем в руках не нажимаем

    def _bottom_rod_count(self) -> int:
        """Сколько донных удилищ реально используется.

        PROFILE.ROD_COUNT (необязательный) берёт первые N клавиш из KEY.BOTTOM_RODS;
        без него используются все. Лишние удилища не опрашиваются.
        """
        keys = list(self.cfg.KEY.BOTTOM_RODS)
        total = len(keys)
        try:
            raw = self.cfg.PROFILE.get("ROD_COUNT", None)
        except AttributeError:
            raw = getattr(self.cfg.PROFILE, "ROD_COUNT", None)
        if raw is None or raw == "":
            return total
        try:
            count = int(raw)
        except (TypeError, ValueError):
            logger.warning("PROFILE.ROD_COUNT=%r is not a number, using all %d rods", raw, total)
            return total
        if count < 1 or count > total:
            logger.warning(
                "PROFILE.ROD_COUNT=%d is out of range 1..%d (KEY.BOTTOM_RODS), using %d",
                count, total, min(max(count, 1), total),
            )
            count = min(max(count, 1), total)
        if count != total:
            logger.info("Bottom rods in use: %d of %d (keys %s)", count, total, keys[:count])
        return count

    def start_fishing(self) -> None:
        utils.start_hw_worker()
        if self.cfg.ARGS.FRICTION_BRAKE:
            self.friction_brake_lock = Lock()
            self.friction_brake = FrictionBrake(
                self.cfg,
                self.friction_brake_lock,
                self.detection,
                utils.cmd_queue,
            )
            logger.info("Spawing new process, do not quit the script")
            self.friction_brake.monitor_process.start()

        if self.detection.get_quit_position():
            logger.warning("Control panel detected, back to the game automatically")
            press("esc")
            sleep(add_jitter(ANIMATION_DELAY))

        if self.cfg.PROFILE.MODE != "bottom":
            # В руках может быть лопата/пусто: без удочки reset_tackle() ждёт
            # иконку готовности вечно. Повторное нажатие уже выбранного слота
            # игра игнорирует. Донка берёт удочки сама (расстановка).
            rod_key = str(self.cfg.KEY.get("MAIN_ROD", 1))
            logger.info("Equipping main rod from slot: %s", rod_key)
            press(rod_key)
            sleep(add_jitter(ANIMATION_DELAY * 2))

        logger.info("Starting fishing mode: '%s'", self.cfg.PROFILE.MODE)
        getattr(self, f"start_{self.cfg.PROFILE.MODE}_mode")()

    def hold_down_left_mouse_button(self):
        utils.mouse_down("left")
        if self.cfg.BOT.CLICK_LOCK:
            sleep(add_jitter(CLICK_LOCK_DURATION))
        self.mouse_pressed = True

    def release_left_mouse_button(self):
        self.mouse_pressed = False
        sleep(random.uniform(0.04, 0.08))
        if self.cfg.BOT.CLICK_LOCK:
            utils.click("left")
        else:
            utils.mouse_up("left")

    def hold_down_shift_key(self):
        utils.key_down("shift")
        self.shift_pressed = True

    def release_shift_key(self):
        utils.key_up("shift")
        self.shift_pressed = False

    def _suspend_inputs(self) -> None:
        """Отпустить зажатые кнопки на время, пока игра не в фокусе (флаги состояния не трогаем)."""
        if self.mouse_pressed and not self.cfg.BOT.CLICK_LOCK:
            utils.mouse_up("left")
        if self.shift_pressed:
            utils.key_up("shift")

    def _resume_inputs(self) -> None:
        """Нажать заново то, что бот считает зажатым: игра теряет его при alt+tab."""
        if self.shift_pressed:
            utils.key_down("shift")
            sleep(random.uniform(0.04, 0.08))
        if self.mouse_pressed and not self.cfg.BOT.CLICK_LOCK:
            utils.mouse_down("left")

    @contextmanager
    def hold_keys(self, mouse: bool, shift: bool, reset: bool = False):
        mouse_pressed_before = self.mouse_pressed
        shift_pressed_before = self.shift_pressed

        if mouse and not self.mouse_pressed:
            self.hold_down_left_mouse_button()
        if not mouse and self.mouse_pressed:
            self.release_left_mouse_button()

        if shift and not self.shift_pressed:
            self.hold_down_shift_key()
        if not shift and self.shift_pressed:
            self.release_shift_key()

        try:
            yield
        except BaseException:
            if self.shift_pressed and not shift_pressed_before:
                self.release_shift_key()

            if self.mouse_pressed and not mouse_pressed_before:
                self.release_left_mouse_button()

            raise
        finally:
            if reset:
                if self.mouse_pressed and not mouse_pressed_before:
                    self.release_left_mouse_button()
                if not self.mouse_pressed and mouse_pressed_before:
                    self.hold_down_left_mouse_button()

                if self.shift_pressed and not shift_pressed_before:
                    self.release_shift_key()
                if not self.shift_pressed and shift_pressed_before:
                    self.hold_down_shift_key()

    def _hybrid_click_at(
        self,
        position: tuple[int, int] | list[int],
        clicks: int = 1,
        jitter: int = 6,
    ) -> None:
        if not position:
            return

        if len(position) >= 4:
            target_x, target_y = utils.get_box_center_integers(position)
        else:
            target_x, target_y = int(position[0]), int(position[1])

        # При двойном клике убираем джиттер координат, чтобы мышь не соскользнула с иконки
        current_jitter = jitter if clicks == 1 else 0
        final_x = target_x + random.randint(-current_jitter, current_jitter)
        final_y = target_y + random.randint(-current_jitter, current_jitter)

        utils.move_to(final_x, final_y)
        sleep(random.uniform(0.06, 0.10))

        if clicks == 1:
            utils.mouse_down("left")
            sleep(random.uniform(0.045, 0.085))
            utils.mouse_up("left")
        else:
            for i in range(clicks):
                utils.mouse_down("left")
                sleep(random.uniform(0.045, 0.085))
                utils.mouse_up("left")
                if i < clicks - 1:
                    sleep(random.uniform(0.06, 0.12))

        sleep(random.uniform(0.16, 0.24))
            
    @contextmanager
    def loop_restart_handler(self):
        try:
            yield
        except exceptions.FishCapturedError:
            self._pause_route()
            self.handle_fish()
            if self.cfg.PROFILE.MODE in ("bottom", "troll"):
                with self.loop_restart_handler():
                    self.recast_tackle()
                    self._stow_after_recast()
        except exceptions.LureBrokenError:
            self._handle_broken_lure()
        except exceptions.TicketExpiredError:
            # Билет кончился посреди вываживания/заброса: лодку на паузу,
            # докупаем билет; маршрут продолжит enable_trolling().
            self._pause_route()
            self._handle_expired_ticket()
        except exceptions.FishHookedError:
            self._pause_route()
            with self.loop_restart_handler():
                if self.cfg.PROFILE.MODE != "telescopic":
                    self.pull_fish()
                else:
                    self.save_bite_screenshot()
                self.lift_fish()
                
                if self.cfg.PROFILE.MODE in ("bottom", "troll"):
                    self.recast_tackle()
                    self._stow_after_recast()
        except exceptions.StuckAtCastingError:
            self._pause_route()
            with self.hold_keys(mouse=False, shift=False):
                pass
            if self.cfg.PROFILE.MODE in ("bottom", "troll"):
                with self.loop_restart_handler():
                    self.recast_tackle()
                    self._stow_after_recast()
        except exceptions.LineAtEndError:
            if self.cfg.ARGS.FRICTION_BRAKE:
                with self.friction_brake.lock:
                    self.friction_brake.change(increase=False)
            self.general_quit("Fishing line is at its end")
        except exceptions.LineSnaggedError:
            self._handle_snagged_line()
        except exceptions.DisconnectedError:
            self.disconnected_quit()
        except exceptions.TackleBrokenError:
            self.general_quit("Tackle is broken")
        except exceptions.ResetTimeoutError:
            if len(self.tackles) == 1:
                self.general_quit("Tackle reset timeout")
            self.tackle.available = False
        except exceptions.BaitNotChosenError:
            self.handle_bait_not_chosen()
        except exceptions.DryMixNotFoundError:
            pass
        except exceptions.DriftTimeoutError:
            pass

    def start_spin_mode(self) -> None:
        """Main spin fishing loop for 'spin' and 'spin_with_pause' modes."""
        while True:
            # ============================================================
            # SAFE POINT:
            # SessionManager может работать только здесь. Предыдущий заброс
            # уже полностью завершён (lift_fish/handle_fish отработали),
            # новый ещё не начался, снасть не нагружена. По аналогии со
            # start_bottom_mode()/start_troll_mode(). Привязано к флагу -m
            # (cfg.ARGS.MOUSE): AFK-паузы -- часть того же режима
            # "человечности", что и случайные движения мыши.
            # ============================================================
            if self.cfg.ARGS.MOUSE:
                utils.session_manager.is_busy = False
                utils.session_manager.check_for_afk()
                utils.session_manager.is_busy = True
            with self.loop_restart_handler():
                self.enable_trolling()
                if not self.skip_cast:
                    self.reset_tackle()
                    self.refill_stats()
                    self.harvest_baits(pickup=True)
                    self.change_tackle_lure()
                    self.cast_tackle()
                self.skip_cast = False

                if self.cfg.PROFILE.TYPE != "normal":
                    self.tackle.hold_mouse_button(self.cfg.PROFILE.TIGHTEN_DURATION)
                    getattr(self, f"retrieve_with_{self.cfg.PROFILE.TYPE}")()
                self.retrieve_line()
                self.lift_fish()

    def retrieve_with_pause(self) -> None:
        logger.info("Retrieving fishing line with pause")
        with self.hold_keys(mouse=False, shift=self.cfg.PROFILE.PRE_ACCELERATION):
            self.tackle.special_retrieve(button="left")

    def retrieve_with_lift(self) -> None:
        logger.info("Retrieving fishing line with lift")
        with self.hold_keys(mouse=True, shift=self.cfg.PROFILE.PRE_ACCELERATION):
            self.tackle.special_retrieve(button="right")

    # ------------------------------------------------------------------
    # Авто-расстановка донных удилищ на старте
    # ------------------------------------------------------------------
    def _place_cfg(self):
        return self.cfg.BOT.ROD_PLACEMENT

    def _placement_step(self, key: str, seconds: float | None = None) -> None:
        """Один короткий шаг в сторону (S - назад, D - вправо, A - влево)."""
        key = str(key).strip().lower()
        if not key:
            return
        base = float(self._place_cfg().STEP_SECONDS if seconds is None else seconds)
        duration = add_jitter(base, jitter_percent=0.15)
        logger.info("Rod placement: step '%s' %.2f s", key, duration)
        utils.key_down(key)
        try:
            sleep(duration)
        finally:
            utils.key_up(key)
        sleep(add_jitter(0.4))

    def _wait_place_banner_gone(self, timeout: float = 8.0) -> None:
        end = time() + timeout
        while time() < end and self.detection.is_rod_place_refused():
            sleep(0.3)

    def _press_and_place(self, key: str, placing_idx: int) -> bool:
        """Нажать слот: игра ставит удилище в руках на подставку и берёт следующее.

        Красная плашка = ставить негде: шаг в сторону и повтор. Первый шаг берётся
        из STEPS по номеру ставимого удилища, дальше чередуется с FALLBACK_STEP.
        """
        cfg = self._place_cfg()
        steps = list(cfg.STEPS)
        own = steps[placing_idx] if placing_idx < len(steps) else cfg.FALLBACK_STEP
        self._wait_place_banner_gone()
        for attempt in range(int(cfg.MAX_TRIES) + 1):
            press(str(key))
            sleep(add_jitter(ANIMATION_DELAY))
            sleep(add_jitter(0.5))
            if not self.detection.is_rod_place_refused():
                return True
            if attempt >= int(cfg.MAX_TRIES):
                break
            step = own if attempt % 2 == 0 else cfg.FALLBACK_STEP
            logger.warning(
                "Rod %d cannot be placed here (red banner), try %d/%d",
                placing_idx + 1, attempt + 1, int(cfg.MAX_TRIES),
            )
            self._wait_place_banner_gone()
            self._placement_step(step, float(cfg.STEP_SECONDS) * (1 + 0.5 * attempt))
        return False

    def _place_bottom_rods(self) -> None:
        keys = list(self.cfg.KEY.BOTTOM_RODS)[: self._bottom_rod_count()]
        logger.info("Rod placement: casting and placing %d bottom rod(s)", len(keys))
        for i, key in enumerate(keys):
            if i == 0:
                press(str(key))
                sleep(add_jitter(ANIMATION_DELAY))
            elif not self._press_and_place(key, i - 1):
                self.general_quit(f"Rod {i} could not be placed (no free spot)")
            shifts = list(self._place_cfg().SHIFT_BEFORE_CAST)
            if i > 0 and i < len(shifts) and str(shifts[i]).strip():
                # Предыдущая удочка уже на подставке: отходим и только потом забрасываем.
                self._placement_step(shifts[i], float(self._place_cfg().SHIFT_BEFORE_CAST_SECONDS))
            self.tackle_idx = i
            self.tackle = self.tackles[i]
            self._fb_force_full = True  # у каждой удочки свой фрикцион: сверяем полностью
            self.reset_tackle()
            self.cast_tackle(lock=True)
        if not self._press_and_place("0", len(keys) - 1):
            self.general_quit(f"Rod {len(keys)} could not be placed (no free spot)")
        self._update_tackle()

    def start_bottom_mode(self) -> None:
        check_miss_counts = [0] * self.num_tackle
        if self.cfg.BOT.ROD_PLACEMENT.ENABLED and not self.skip_cast:
            self._place_bottom_rods()
        self.skip_cast = False

        while True:
            self.enable_trolling()

            if self.cfg.ARGS.SPOD_ROD and self.timer.is_spod_rod_castable():
                self.cast_spod_rod()

            self.refill_stats()

            # ============================================================
            # SAFE POINT:
            # SessionManager может работать только здесь.
            # Дальше начинается полный цикл удочки. Привязано к флагу -m
            # (cfg.ARGS.MOUSE), как и остальные safe-point'ы.
            # ============================================================
            if self.cfg.ARGS.MOUSE:
                utils.session_manager.is_busy = False
                utils.session_manager.check_for_afk()
                utils.session_manager.is_busy = True

            logger.info("Checking rod %s", self.tackle_idx + 1)
            press(str(self.cfg.KEY.BOTTOM_RODS[self.tackle_idx]))

            sleep(add_jitter(ANIMATION_DELAY))

            with self.loop_restart_handler():
                if self.detection.is_fish_hooked():
                    check_miss_counts[self.tackle_idx] = 0
                    self.retrieve_and_recast()
                else:
                    sleep(add_jitter(self.cfg.PROFILE.PUT_DOWN_DELAY))

                    if self.detection.is_fish_hooked():
                        check_miss_counts[self.tackle_idx] = 0
                        self.retrieve_and_recast()
                    else:
                        self._put_down_tackle(check_miss_counts)
            self._update_tackle()            

    def start_troll_mode(self) -> None:
        """Маршрутный троллинг: лодка идёт по маршруту, проверка удилища как в донном цикле."""
        check_miss_counts = [0] * self.num_tackle
        while True:
            # Билет на лодку: диалог перекрывает игру, а маршрутный поток при
            # этом продолжает держать A/D/W. Поэтому сначала пауза, потом
            # продление (или остановка, если -BT не задан).
            self._check_next_ticket_hint()
            if self.detection.is_ticket_expired():
                logger.info("Boat ticket expired (troll)")
                self._pause_route()
                self._handle_expired_ticket()
                sleep(add_jitter(ANIMATION_DELAY))
            if getattr(self, "_ticket_ui_used", False):
                # После окна билетов в руках может оказаться удилище, а J с удилищем в руках игра не
                # принимает: убираем руки ("0" безвредна, если там пусто) и ждём конец анимации.
                self._ticket_ui_used = False
                press("0")
                sleep(add_jitter(ROD_STOW_DELAY))
            self.enable_trolling()
            if getattr(self, "_wait_before_check", False):
                # После рыбы проверка удилища шла сразу после старта лодки: ждём обычный интервал проверок
                # (в ветке без рыбы эту паузу даёт _put_down_tackle), и удилище не берётся сразу после J.
                self._wait_before_check = False
                sleep(add_jitter(self.cfg.PROFILE.CHECK_DELAY, jitter_percent=0.15))
            self.refill_stats()
            # SAFE POINT: SessionManager может работать только здесь. Привязано к -m.
            if self.cfg.ARGS.MOUSE:
                utils.session_manager.is_busy = False
                utils.session_manager.check_for_afk()
                utils.session_manager.is_busy = True
            logger.info("Checking rod %s", self.tackle_idx + 1)
            self.rod_in_hands = True
            try:
                press(self._rod_key())
                sleep(add_jitter(ANIMATION_DELAY))
                with self.loop_restart_handler():
                    if self.detection.is_fish_hooked():
                        check_miss_counts[self.tackle_idx] = 0
                        self.retrieve_and_recast()
                        self._recheck_after_recast(check_miss_counts)
                        self._wait_before_check = True
                        press("0")  # удилище в подставку перед отплытием
                        # Ждём конец анимации укладки: J, нажатая во время анимации,
                        # игрой проглатывается, и троллинг не включается.
                        sleep(add_jitter(ROD_STOW_DELAY))
                    else:
                        sleep(add_jitter(self.cfg.PROFILE.PUT_DOWN_DELAY))
                        if self.detection.is_fish_hooked():
                            check_miss_counts[self.tackle_idx] = 0
                            self.retrieve_and_recast()
                            self._recheck_after_recast(check_miss_counts)
                            self._wait_before_check = True
                            press("0")
                            sleep(add_jitter(ROD_STOW_DELAY))
                        else:
                            self._put_down_tackle(check_miss_counts)
            finally:
                self.rod_in_hands = False
            self._update_tackle()

    def _stow_after_recast(self) -> None:
        """Маршрутный троллинг, заброс через обработчик исключений: рыба-после-заброса, "0", пауза укладки,
        иначе J нажмётся во время анимации ("руки заняты") и удилище проверится сразу после J."""
        if self.cfg.ARGS.TROLLING != "route":
            return
        self._recheck_after_recast([0] * max(len(self.tackles), 1))
        self._wait_before_check = True
        press("0")
        sleep(add_jitter(ROD_STOW_DELAY + 0.5))

    def _recheck_after_recast(self, check_miss_counts: list[int]) -> None:
        """Сразу после заброса рыба могла клюнуть: проверяем, пока удилище ещё в руках (до J и укладки)."""
        for _ in range(RECAST_RECHECK_LIMIT):
            sleep(add_jitter(RECAST_RECHECK_DELAY))
            if not self.detection.is_fish_hooked():
                return
            logger.info("Fish hooked right after the cast")
            check_miss_counts[self.tackle_idx] = 0
            self.retrieve_and_recast()

    def _rod_key(self) -> str:
        """Клавиша слота удилища: донка берёт из BOTTOM_RODS, троллинг - MAIN_ROD."""
        if self.cfg.PROFILE.MODE == "bottom":
            return str(self.cfg.KEY.BOTTOM_RODS[self.tackle_idx])
        return str(self.cfg.KEY.MAIN_ROD)

    def retrieve_and_recast(self) -> None:
        self._pause_route()
        self.retrieve_line()
        self.lift_fish()
        self.recast_tackle()

    def recast_tackle(self) -> None:
        sleep(add_jitter(3.0))
        self.reset_tackle()
        self._refill_groundbait()
        self._refill_pva()
        self.cast_tackle(lock=True)
        
    def start_pirk_mode(self) -> None:
        self._start_sink_mode(pirk=True)

    def start_elevator_mode(self) -> None:
        self._start_sink_mode(pirk=False)

    def _start_sink_mode(self, pirk: bool) -> None:
        perform_technique = self.do_pirking if pirk else self.do_elevating
        while True:
            # SAFE POINT: SessionManager может работать только здесь, см.
            # комментарий в start_spin_mode(). Привязано к -m.
            if self.cfg.ARGS.MOUSE:
                utils.session_manager.is_busy = False
                utils.session_manager.check_for_afk()
                utils.session_manager.is_busy = True
            with self.loop_restart_handler():
                self.enable_trolling()
                if not self.skip_cast:
                    self.reset_tackle()
                    self.refill_stats()
                    self.cast_tackle()
                    self.tackle.sink()
                self.skip_cast = False

                if not self.detection.is_fish_hooked():
                    perform_technique()
                self.retrieve_line()
                self.lift_fish()

    def start_telescopic_mode(self) -> None:
        self._start_float_mode(telescopic=True)

    def start_bolognese_mode(self) -> None:
        self._start_float_mode(telescopic=False)

    def _start_float_mode(self, telescopic: bool) -> None:
        """Main float fishing loop."""
        monitor, _ = self._get_controllers(telescopic)
        while True:
            # SAFE POINT: SessionManager может работать только здесь, см.
            # комментарий в start_spin_mode(). Привязано к -m.
            if self.cfg.ARGS.MOUSE:
                utils.session_manager.is_busy = False
                utils.session_manager.check_for_afk()
                utils.session_manager.is_busy = True
            with self.loop_restart_handler():
                self.enable_trolling()
                
                if not self.skip_cast:
                    self.reset_tackle()
                    self.refill_stats()
                    self.harvest_baits(pickup=True)
                    self.cast_tackle()
                
                self.skip_cast = False

                with self.error_handler():
                    monitor()

                sleep(self.cfg.PROFILE.LIFT_DELAY)

                # ПРАВИЛЬНАЯ ПОДСЕЧКА: зажимаем ЛКМ и НЕ ОТПУСКАЕМ!
                with self.hold_keys(mouse=True, shift=False, reset=True):
                    if not telescopic:
                        utils.mouse_down("right")

                    try:
                        # Даем время игре засчитать подсечку
                        sleep(FISH_CHECK_DURATION)
                        
                        # Защита от задержек: ждем отрисовку значка крючка (до 1.5 сек)
                        for _ in range(3):
                            if self.detection.is_fish_hooked():
                                break
                            sleep(0.5)

                        if not telescopic:
                            self.pull_fish()
                        else:
                            self.save_bite_screenshot()

                        # Вызываем подсак и ожидание садка (ЛКМ всё это время зажата!)
                        self.lift_fish()
                    finally:
                        if not telescopic:
                            utils.mouse_up("right")

    def harvest_baits(self, pickup: bool = False, force: bool = False) -> None:
        if not self.cfg.ARGS.HARVEST:
            return

        # Проверяем энергию по твоему конфигу (0.74). Если мало — цикл копки пропускается.
        if not force and not self.detection.is_energy_high():
            logger.info("Skipping harvest: Energy is below config threshold")
            return

        logger.info("Harvesting baits...")

        with self.hold_keys(mouse=False, shift=False):
            dig_key = str(self.cfg.KEY.get("DIGGING_TOOL", 5))
            logger.info("Equipping digging tool from slot: %s", dig_key)
            utils.press(dig_key)
            sleep(add_jitter(GET_DIGGING_TOOL_DELAY))

            utils.hw_click_left(register_session=False)

            harvest_deadline = time() + 15.0
            harvest_wait_counter = 0

            while not self.detection.is_harvest_success():
                if time() >= harvest_deadline:
                    logger.warning("Harvest bait timeout. Interrupting cycle.")
                    utils.hw_release_mouse_left()
                    
                    # НИКАКИХ ESC! Просто убираем лопату, чтобы не открыть главное меню.
                    utils.press("backspace")
                    sleep(add_jitter(ANIMATION_DELAY))
                    
                    # Если режим требует удочку в руках (поплавок/спиннинг) — берем её обратно
                    if pickup:
                        rod_key = str(self.cfg.KEY.get("MAIN_ROD", 1))
                        logger.info("Equipping main rod back from slot: %s", rod_key)
                        utils.press(rod_key)
                        sleep(add_jitter(2.5))
                        
                    # Прерываем копку и возвращаемся в цикл ловли рыбы
                    return

                sleep(add_jitter(LOOP_DELAY))
                harvest_wait_counter += 1

                if harvest_wait_counter % 3 == 0:
                    utils.hw_click_left(register_session=False)

            sleep(random.uniform(0.4, 0.75))
            utils.press("space")
            sleep(add_jitter(ANIMATION_DELAY))

            utils.press("backspace")
            sleep(add_jitter(ANIMATION_DELAY))

            self.result.bait += 1

            if pickup:
                rod_key = str(self.cfg.KEY.get("MAIN_ROD", 1))
                logger.info("Equipping main rod back from slot: %s", rod_key)
                utils.press(rod_key)
                sleep(add_jitter(2.5))

        if self.cfg.ARGS.REFILL:
            self.refill_stats_post_harvest()

    def refill_stats_post_harvest(self) -> None:
        with self.hold_keys(mouse=False, shift=False):
            # Человечность: после копки игрок не всегда бежит к термосу.
            # 55% - чай с живой паузой, 13.5% - холостое открытие/закрытие
            # T-меню, 31.5% - просто плывём дальше.
            roll = random.random()
            if roll < 0.55:
                sleep(random.uniform(1.5, 3.5))
                logger.info("Refilling player stats (Post-harvest Tea)")
                self._use_item("tea")
                self.timer.last_tea_drink = time()
            elif roll < 0.685:
                sleep(random.uniform(1.0, 2.0))
                logger.info("Post-harvest idle: opening and closing T-menu")
                utils.key_down("t")
                sleep(random.uniform(0.4, 0.65))
                utils.key_up("t")
                sleep(add_jitter(0.5))
            else:
                logger.info("Refilling player stats (Post-harvest skipped, human-like)")

    def refill_stats(self) -> None:
        if not self.cfg.ARGS.REFILL:
            return
        with self.hold_keys(mouse=False, shift=False):
            drank_tea = False
            if (
                (self.detection.is_comfort_low() or not self.detection.is_energy_high())
                and self.timer.is_tea_drinkable()
            ):
                # Человечность: чай пьётся не всегда (~70%); если энергия ещё
                # высокая, а комфорт низкий — дополнительный шанс 40% пропустить
                skip = random.random() > 0.70
                if self.detection.is_energy_high() and self.detection.is_comfort_low():
                    skip = skip or random.random() < 0.40
                if skip:
                    logger.info("Refilling player stats (Tea skipped, human-like)")
                else:
                    logger.info("Refilling player stats (Tea)")
                    self._use_item("tea")
                    drank_tea = True
            if self.detection.is_hunger_low():
                # Человечность: пауза между чаем и морковью, морковь не всегда (~75%)
                if drank_tea:
                    sleep(random.uniform(0.8, 1.6))
                if random.random() <= 0.75:
                    logger.info("Refilling player stats (Carrot)")
                    self._use_item("carrot")
                else:
                    logger.info("Refilling player stats (Carrot skipped, human-like)")

    def _drink_alcohol(self) -> None:
        if not self.cfg.ARGS.ALCOHOL or not self.timer.is_alcohol_drinkable():
            return

        logger.info("Drinking alcohol")
        with self.hold_keys(mouse=False, shift=False):
            for _ in range(self.cfg.STAT.ALCOHOL_PER_DRINK):
                self._use_item("alcohol")
        self.result.alcohol += self.cfg.STAT.ALCOHOL_PER_DRINK

    def _drink_coffee(self) -> None:
        if not self.cfg.ARGS.COFFEE or self.detection.is_energy_high():
            return

        mouse = True if self.mouse_pressed and self.cfg.KEY["COFFEE"] != -1 else False
        with self.hold_keys(mouse=mouse, shift=False, reset=True):
            if self.cur_coffee > self.cfg.STAT.COFFEE_LIMIT:
                self.general_quit("Coffee limit reached")

            logger.info("Drinking coffee")
            for _ in range(self.cfg.STAT.COFFEE_PER_DRINK):
                self._use_item("coffee")
            self.cur_coffee += self.cfg.STAT.COFFEE_PER_DRINK
            self.result.coffee += self.cfg.STAT.COFFEE_PER_DRINK

    def _use_item(self, item: str) -> None:
        logger.info("Using item: %s", item)
        key = str(self.cfg.KEY.get(item.upper(), "-1"))

        if key != "-1":
            utils.press(key)
            sleep(add_jitter(0.3))
            if item.lower() == "tea":
                self.result.tea += 1
            elif item.lower() == "carrot":
                self.result.carrot += 1
            return

        utils.key_down("t")
        try:
            sleep(random.uniform(0.4, 0.65))
            screen_w, screen_h = utils.pag.size()
            
            center_x = (screen_w // 2) + random.randint(-5, 5)
            center_y = (screen_h // 2) + random.randint(-5, 5)
            utils.move_to(center_x, center_y)
            
            sleep(random.uniform(0.15, 0.25))

            food_position = self.detection.get_food_position(item)

            if not food_position:
                for _ in range(2):
                    utils.scroll(-360) 
                
                sleep(random.uniform(0.35, 0.5))
                food_position = self.detection.get_food_position(item)

            if food_position:
                self._hybrid_click_at(food_position, clicks=1)

                if item.lower() == "tea":
                    self.result.tea += 1
                elif item.lower() == "carrot":
                    self.result.carrot += 1
            else:
                logger.warning("Could not find item '%s' in T-menu!", item)
                utils.press("esc")

        finally:
            sleep(add_jitter(0.1))
            utils.key_up("t")
            sleep(add_jitter(USE_ITEM_DELAY))

    def reset_tackle(self) -> None:
        if self.detection.is_tackle_ready():
            return
        if self.detection.is_lure_broken():
            self._handle_broken_lure()
            return
        if not self.detection.is_dry_mix_chosen():
            self._refill_dry_mix()
            return
        if not self.detection.is_bait_chosen():
            self.handle_bait_not_chosen()
            return

        if self.cfg.PROFILE.MODE == "spin":
            shift = self.cfg.PROFILE.RESET_ACCELERATION
        else:
            shift = True
        with self.hold_keys(mouse=True, shift=shift):
            self._run_with_retries(self.tackle.reset, "reset_tackle")

    def _run_with_retries(
        self,
        action: Callable[[], None],
        label: str,
        max_attempts: int = MAX_ERROR_HANDLER_ATTEMPTS,
        max_seconds: float = MAX_ERROR_HANDLER_SECONDS,
    ) -> None:
        """Повторяет action() через error_handler(), пока не получится или не кончится лимит.

        error_handler() ловит конкретные таймауты и чинит состояние (чай,
        передачи, клипса и т.п.) и это обычно и есть fix. Но если исправление
        не срабатывает (ошибка повторяется снова и снова, например потому что
        соответствующий ARGS-флаг выключен), раньше цикл крутился без паузы и
        без предела. Теперь после MAX_ERROR_HANDLER_ATTEMPTS попыток или
        MAX_ERROR_HANDLER_SECONDS бот останавливается с понятной причиной
        вместо бесконечного зависания.
        """
        deadline = time() + max_seconds
        attempt = 0
        while True:
            attempt += 1
            with self.error_handler():
                action()
                return
            if attempt >= max_attempts or time() >= deadline:
                logger.critical(
                    "Stuck retrying '%s' (%d attempts, %.0fs elapsed) -- giving up",
                    label, attempt, max_seconds,
                )
                self.general_quit(f"Stuck retrying: {label}")
                return

    @contextmanager
    def error_handler(self):
        try:
            yield
        except exceptions.TicketExpiredError:
            self._handle_expired_ticket()
        except exceptions.CoffeeTimeoutError:
            self._drink_coffee()
        except exceptions.GearRatioTimeoutError:
            if self.cfg.ARGS.GEAR_RATIO and not self.tackle.gear_ratio_changed:
                self.tackle.change_gear_ratio_or_electro_mode()
        except exceptions.PirkTimeoutError:
            with self.hold_keys(mouse=False, shift=False, reset=True):
                if self.cfg.PROFILE.DEPTH_ADJUST_DELAY > 0:
                    logger.info("Adjusting lure depth")
                    press("enter")
                    sleep(add_jitter(self.cfg.PROFILE.DEPTH_ADJUST_DELAY))
                    self.tackle.hold_mouse_button(
                        self.cfg.PROFILE.DEPTH_ADJUST_DURATION
                    )
                else:
                    self.reset_tackle()
                    self.cast_tackle()
                    self.tackle.sink()
        except exceptions.LiftTimeoutError:
            with self.hold_keys(mouse=False, shift=False, reset=True):
                sleep(add_jitter(DROP_ROD_DELAY))
                if self.cfg.PROFILE.MODE != "telescopic":
                    self.pull_fish(save=False)
        except exceptions.DryMixNotChosenError:
            self._refill_dry_mix()

    def handle_bait_not_chosen(self) -> None:
        if len(self.tackles) == 1:
            self.general_quit("Run out of bait")
        self.tackle.available = False

    def cast_spod_rod(self) -> None:
        self.using_spod_rod = True
        self._use_item("spod_rod")
        self.reset_tackle()
        self.cast_tackle(lock=True, update=False)
        press("0")
        sleep(add_jitter(ANIMATION_DELAY))
        self.using_spod_rod = False

    def cast_tackle(self, lock: bool = False, update: bool = True) -> None:
        if self.cfg.ARGS.PAUSE and self.timer.is_script_pausable():
            self._pause_script()

        if self.cfg.ARGS.FRICTION_BRAKE:
            full = getattr(self, "_fb_force_full", False)
            self._fb_force_full = False
            self.friction_brake.reset(self.cfg.BOT.FRICTION_BRAKE.INITIAL, full=full)

        with self.hold_keys(mouse=False, shift=False):
            if (
                self.cfg.ARGS.RANDOM_CAST
                and random.random() <= self.cfg.BOT.RANDOM_CAST_PROBABILITY
            ):
                logger.info("Casting rod redundantly")
                utils.click("left")
                sleep(add_jitter(BAD_CAST_DELAY))
                self.reset_tackle()

            self.tackle.cast(lock)
            if not self.cfg.ARGS.ELECTRO and self.tackle.gear_ratio_changed:
                self.tackle.change_gear_ratio_or_electro_mode()
            if update:
                self.timer.update_cast_time()

    def retrieve_line(self) -> None:
        """Retrieve the fishing line until it is fully retrieved."""
        if self.detection.is_retrieval_finished():
            return

        if self.detection.is_fish_hooked():
            self.pull_fish()
        else:
            with self.hold_keys(mouse=True, shift=False):
                self._run_with_retries(self.tackle.retrieve, "retrieve_line")
                if self.detection.is_fish_hooked():
                    self.pull_fish()
        if self.cfg.ARGS.RAINBOW is None:
            sleep(add_jitter(self.cfg.BOT.SPOOL_RETRIEVAL_DELAY, jitter_percent=0.2))
        elif self.cfg.ARGS.RAINBOW == 5:
            sleep(add_jitter(self.cfg.BOT.RAINBOW_RETRIEVAL_DELAY))

    def pull_fish(self, save: bool = True) -> None:
        self.cur_coffee = 0
        if save:
            self.save_bite_screenshot()
        if self.cfg.ARGS.ELECTRO:
            self.tackle.change_gear_ratio_or_electro_mode()

        shift = (
            self.cfg.PROFILE.POST_ACCELERATION
            if self.cfg.PROFILE.MODE != "telescopic"
            else False
        )
        with self.hold_keys(mouse=True, shift=shift):
            # Тяжёлая рыба может идти минутами, а кофе/пауза перезапускают pull()
            # штатно, поэтому лимит мягче общего (общий ловит только зависание).
            self._run_with_retries(
                self.tackle.pull, "pull_fish", max_attempts=60, max_seconds=900.0
            )

    def save_bite_screenshot(self):
        if self.cfg.ARGS.BITE:
            self.detection.window.save_screenshot(self.timer.get_new_filepath())

    def do_pirking(self) -> None:
        if self.cfg.PROFILE.PIRK_RETRIEVAL:
            with self.hold_keys(mouse=True, shift=False):
                self._do_pirking()
        else:
            self._do_pirking()

    def _do_pirking(self) -> None:
        self._run_with_retries(self.tackle.pirk, "do_pirking")

    def do_elevating(self) -> None:
        self._run_with_retries(self.tackle.elevate, "do_elevating")

    def lift_fish(self) -> None:
        if not self.detection.is_fish_hooked():
            return
        self._drink_alcohol()
        shift = (
            self.cfg.PROFILE.POST_ACCELERATION
            if self.cfg.PROFILE.MODE != "telescopic"
            else False
        )
        with self.hold_keys(mouse=True, shift=shift):
            self._run_with_retries(self.tackle.lift, "lift_fish")
        self.handle_fish()

    def _put_down_tackle(self, check_miss_counts: list[int]) -> None:
        check_miss_counts[self.tackle_idx] += 1

        if check_miss_counts[self.tackle_idx] >= self.cfg.PROFILE.CHECK_MISS_LIMIT:
            check_miss_counts[self.tackle_idx] = 0
            self.reset_tackle()
            self._refill_groundbait()
            self._refill_pva()
            self.cast_tackle(lock=True)

        press("0")
        sleep(add_jitter(ANIMATION_DELAY))

        if self.detection.is_energy_high():
            self.harvest_baits()

        if random.random() < 0.25:  # Увеличен шанс до 25%
            self.tackle.move_mouse_randomly()

        # Дополнительный случайный разброс (плюс-минус от 0.5 до 2 секунд к базовой задержке)
        extra_random_delay = random.uniform(0.5, 2.0) if random.random() < 0.4 else 0.0
        total_check_delay = self.cfg.PROFILE.CHECK_DELAY + extra_random_delay
        
        sleep(add_jitter(total_check_delay, jitter_percent=0.15))

    def enable_trolling(self) -> None:
        mode = self.cfg.ARGS.TROLLING
        if mode is None:
            return

        if mode == "route":
            self._enable_route_trolling()
            return

        if not self.trolling_started:
            logger.info("Start trolling")
            press(TROLLING_KEY)
            # Флаг ставится для всех направлений. Раньше для "forward" он не
            # ставился, и J (она переключает троллинг) нажималась заново на
            # каждой итерации цикла.
            self.trolling_started = True

            if mode not in ("left", "right"):
                return

            key = LEFT_KEY if mode == "left" else RIGHT_KEY
            utils.key_up(key)
            utils.key_down(key)

    def disable_trolling(self) -> None:
        runner = getattr(self, "route_runner", None)
        if runner is not None:
            logger.info("Stop route trolling")
            runner.stop()
            self.route_runner = None
            self.trolling_started = False
            return

        if not self.trolling_started:
            return

        logger.info("Stop trolling")

        if self.cfg.ARGS.TROLLING == "left":
            utils.key_up(LEFT_KEY)
        elif self.cfg.ARGS.TROLLING == "right":
            utils.key_up(RIGHT_KEY)

        # J запускает и останавливает троллинг: чтобы остановить, её нужно
        # нажать (key_up её не переключает).
        press(TROLLING_KEY)
        self.trolling_started = False

    # ------------------------------------------------------------------
    # Маршрутный троллинг (-T route --troll-water NAME)
    # ------------------------------------------------------------------
    @staticmethod
    def _argv_option(name: str):
        """Значение опции командной строки (запасной путь, если её нет в cfg.ARGS)."""
        argv = sys.argv
        for i, token in enumerate(argv):
            if token == name and i + 1 < len(argv):
                return argv[i + 1]
            if token.startswith(name + "="):
                return token.split("=", 1)[1]
        return None

    def _enable_route_trolling(self) -> None:
        runner = self.route_runner
        if runner is not None:
            if runner.failed is not None:
                self.general_quit(f"Route trolling failed: {runner.failed!r}")
            if runner.nav_failed is not None and not self._nav_failed_warned:
                # Сверка по координатам сама по себе не останавливает плавание
                # (лодка продолжает идти по времени), поэтому не general_quit --
                # но раньше это было видно только в логе RouteRunner, который
                # пишется только самим потоком при падении.
                self._nav_failed_warned = True
                logger.warning(
                    "Route trolling: coordinate nav is dead (%r), continuing time-only",
                    runner.nav_failed,
                )
            # J сразу (через ~1 с после укладки удилища); позицию по компасу уточняем уже на ходу
            runner.resume(quick=True)  # ничего не делает, если лодка уже плывёт
            return

        if self.cfg.PROFILE.MODE not in ("bottom", "troll"):
            if not self._route_mode_warned:
                self._route_mode_warned = True
                logger.warning(
                    "Route trolling works only with bottom/troll-mode profile; "
                    "the boat will not move"
                )
            return

        name = getattr(self.cfg.ARGS, "TROLL_WATER", None) or self._argv_option(
            "--troll-water"
        )
        try:
            route = trolling.load_route(name)
        except trolling.RouteError as err:
            self.general_quit(f"Route trolling: {err}")
            return
        logger.info("Route trolling: %s", route.summary())

        no_cast = (
            getattr(self.cfg.ARGS, "TROLL_NO_CAST", False)
            or "--troll-no-cast" in sys.argv
        )
        # Поиск компаса занимает несколько секунд: делаем его ДО заброса, чтобы J нажималась
        # сразу после укладки удилища, а не после поиска.
        position_fn = self._make_position_fn(route)
        if not no_cast:
            self._cast_for_trolling()
        nav_mode = (
            getattr(self.cfg.ARGS, "TROLL_NAV", None)
            or self._argv_option("--troll-nav")
            or "steer"
        )
        from rf4s.controller.navigation import NavParams  # noqa: PLC0415

        nav_params = NavParams(
            steer=nav_mode in ("steer", "full"),
            snap=nav_mode in ("sync", "full"),
        )
        relocate = nav_mode != "observe"
        diag_path = None
        if position_fn is not None:
            logger.info("Route trolling: навигация по координатам, режим '%s'", nav_mode)
            if getattr(self.cfg.PROFILE, "NAV_LOG", True):
                diag_path = trolling.ROOT / "logs" / f"trolling_nav_{route.name}.csv"
                logger.info("Route trolling: журнал навигации -> %s", diag_path)
            else:
                logger.info("Route trolling: журнал навигации выключен (NAV_LOG: false)")
        self.route_runner = trolling.RouteRunner(
            route, utils, log=logger.info, position_fn=position_fn,
            nav_params=nav_params, diag_path=diag_path, relocate_on_resume=relocate,
            rod_busy_fn=lambda: self.rod_in_hands,
            state_fn=self.detection.trolling_state,
        )
        try:
            self.route_runner.start()
            self._wait_before_check = True   # удилище не берём сразу после J
        except Exception as err:  # noqa: BLE001
            self.route_runner = None
            self.general_quit(f"Route trolling could not start: {err!r}")
            return
        self.trolling_started = True

    def _make_position_fn(self, route):
        """Чтение координат с компаса для сверки маршрута; None - идём только по времени."""
        if not route.has_path:
            logger.info("Route trolling: в маршруте нет координат, только по времени "
                        "(перезапишите маршрут после калибровки компаса)")
            return None
        try:
            from rf4s.controller.compass import CompassReader  # noqa: PLC0415

            window = self.detection.window
            reader = CompassReader(box_fn=window.get_box)
            if getattr(self.cfg.PROFILE, "COMPASS_DEBUG", False) or getattr(self.cfg.ARGS, "DATA", False):
                reader.fail_dir = trolling.ROOT / "logs" / "compass_fail"
                logger.info("Route trolling: COMPASS_DEBUG включён, кадры сбоев компаса -> %s", reader.fail_dir)
            if reader.missing_digits():
                logger.warning(
                    "Route trolling: нет шаблонов цифр %s, запустите trollcalib; идём по времени",
                    " ".join(reader.missing_digits()),
                )
                return None
            # Сразу после заброса игра может на секунды прятать компас: ждём до ~8 с.
            probe = None
            for _ in range(16):
                probe = reader.read(samples=2)
                if probe is not None:
                    break
                sleep(0.5)
            if probe is None:
                # Область HUD могла сместиться (рамка окна, заголовок): ищем компас в углу.
                import pyautogui as pag  # noqa: PLC0415

                screen = pag.size()
                boxes = [tuple(window.get_box()), (0, 0, int(screen[0]), int(screen[1]))]
                logger.info("Route trolling: стандартная область компаса не читается (окно игры %s), "
                            "ищу его в углу экрана", window.get_resolution_str())
                if reader.locate(boxes):
                    probe = reader.read(samples=3)
                    logger.info("Route trolling: компас найден, смещение %s", reader.offset)
                if probe is None:
                    for note in reader.save_probe(boxes, trolling.ROOT / "logs"):
                        logger.warning("Route trolling: диагностика компаса: %s", note)
                    logger.warning(
                        "Route trolling: компас сейчас не читается; продолжаю попытки во время заплыва "
                        "(снимки угла экрана: logs/compass_probe_*.png)"
                    )
            else:
                logger.info("Route trolling: компас читается (%s:%s), окно игры %s",
                            probe[0], probe[1], window.get_resolution_str())
            def position_fn():
                return reader.read(samples=2)

            position_fn.reader = reader
            return position_fn
        except Exception as err:  # noqa: BLE001
            logger.warning("Route trolling: сверка по координатам недоступна: %r", err)
            return None

    def _cast_for_trolling(self) -> None:
        """Забросить удилище, защёлкнуть клипсу (ЛКМ после заброса) и поставить в подставку."""
        logger.info("Route trolling: casting the rod before departure")
        self.rod_in_hands = True
        try:
            press(self._rod_key())
            sleep(add_jitter(ANIMATION_DELAY))
            self.cast_tackle(lock=True)
            # рыба могла клюнуть сразу после заброса -- проверяем, пока удилище в руках
            self._recheck_after_recast([0] * max(len(self.tackles), 1))
            press("0")
            # Анимация укладки: J, нажатая во время неё, игрой проглатывается
            sleep(add_jitter(ROD_STOW_DELAY))
        finally:
            self.rod_in_hands = False

    def _pause_route(self) -> None:
        """Остановить лодку на время вываживания; продолжение -- в enable_trolling()."""
        runner = getattr(self, "route_runner", None)
        if runner is None:
            return
        runner.pause()

    def _update_tackle(self) -> None:
        candidates = self._get_available_rods()
        if not candidates:
            self.general_quit("All rods are unavailable")
        if self.cfg.PROFILE.RANDOM_ROD_SELECTION:
            self.tackle_idx = random.choice(candidates)
        else:
            self.tackle_idx = candidates[0]
        self.tackle = self.tackles[self.tackle_idx]

    def _get_available_rods(self) -> list[int]:
        if self.num_tackle == 1 and self.tackle.available:
            return [self.tackle_idx]

        candidates = list(range(len(self.tackles)))
        start = candidates.index(self.tackle_idx)
        candidates = candidates[start:] + candidates[:start]
        candidates = [i for i in candidates if self.tackles[i].available]

        if len(candidates) > 1 and self.tackle_idx in candidates:
            candidates.remove(self.tackle_idx)
        return candidates

    def change_tackle_lure(self) -> None:
        if not self.cfg.ARGS.LURE or not self.have_new_lure:
            return

        with self.hold_keys(mouse=False, shift=False):
            if self.timer.is_lure_changeable():
                logger.info("Changing lure randomly")
                try:
                    self.tackle.equip_item("lure")
                except exceptions.ItemNotFoundError:
                    logger.error("New lure not found")
                    self.have_new_lure = False

    def _refill_pva(self) -> None:
        if not self.cfg.ARGS.PVA or not self.have_new_pva:
            return

        with self.hold_keys(mouse=False, shift=False):
            if not self.detection.is_pva_chosen():
                try:
                    self.tackle.equip_item("pva")
                except exceptions.ItemNotFoundError:
                    logger.error("New pva not found")
                    self.have_new_pva = False

    def _refill_dry_mix(self) -> None:
        if not self.cfg.ARGS.DRY_MIX or not self.have_new_dry_mix:
            return

        with self.hold_keys(mouse=False, shift=False):
            try:
                self.tackle.equip_item("dry_mix")
            except exceptions.ItemNotFoundError:
                logger.error("New dry mix not found")
                if not self.using_spod_rod:
                    self.tackle.available = False
                self.have_new_dry_mix = False
                raise exceptions.DryMixNotFoundError

    def _refill_groundbait(self) -> None:
        if not self.cfg.ARGS.GROUNDBAIT or not self.have_new_groundbait:
            return

        if self.detection.is_groundbait_chosen():
            logger.info("Groundbait is not used up yet")
        else:
            with self.hold_keys(mouse=False, shift=False):
                try:
                    self.tackle.equip_item("groundbait")
                except exceptions.ItemNotFoundError:
                    logger.error("New groundbait not found")
                    self.have_new_groundbait = False

    def _get_controllers(self, telescopic: bool) -> tuple[Callable, Callable]:
        if telescopic:
            hold_mouse_button = self.tackle.hold_mouse_button
            monitor = self.tackle._monitor_float_state
        else:
            if self.detection.is_clip_open():
                logger.warning("Clip is not set, fall back to camera mode")
                monitor = self.tackle._monitor_float_state
            else:
                monitor = self.tackle._monitor_clip_state
            hold_mouse_button = self.tackle.hold_mouse_buttons

        return monitor, hold_mouse_button

    def _pause_script(self) -> None:
        logger.info("Pausing script")
        with self.hold_keys(mouse=False, shift=False):
            press("esc")
            sleep(add_jitter(self.cfg.BOT.PAUSE_DURATION))
            press("esc")
            sleep(add_jitter(ANIMATION_DELAY))

    def _handle_timeout(self) -> None:
        if self.detection.is_tackle_broken():
            self.general_quit("Tackle is broken")

        if self.detection.is_disconnected():
            self.disconnected_quit()

        if self.detection.is_ticket_expired():
            self._handle_expired_ticket()
            
    def _handle_broken_lure(self) -> None:
        if self.cfg.ARGS.BROKEN_LURE:
            with self.hold_keys(mouse=False, shift=False):
                self._replace_broken_lures()
        else:
            self.general_quit("Lure is broken")

    def handle_termination(self, msg: str, shutdown: bool, send: bool) -> None:
        result = self.get_result_dict(msg)
        result_table = self.get_result_table(result)
        if send:
            send_result(self.cfg, result)
        if self.cfg.ARGS.DATA:
            output_dir = self.timer.get_new_dir_path()
            output_dir.mkdir(parents=True, exist_ok=True)
            self.timer.save_data(output_dir)
            with open(output_dir / "result.json", "w") as f:
                json.dump(result, f, indent=4)
            with open(output_dir / "config.yaml", "w") as f:
                f.write(config.dump_cfg(self.cfg))
        if self.cfg.ARGS.SHUTDOWN and shutdown:
            os.system("shutdown /s /t 5")
        print(result_table)
        if self.cfg.ARGS.FRICTION_BRAKE:
            self.friction_brake.monitor_process.terminate()

        self.disable_trolling()

        with self.hold_keys(mouse=False, shift=False):
            utils.safe_exit()

    def _handle_snagged_line(self) -> None:
        if len(self.tackles) == 1:
            self.general_quit("Line is snagged")
        self.tackle.available = False

    def handle_fish(self) -> None:
        if not self.detection.is_fish_captured():
            return
        if self.cfg.BOT.CATCH_SCREEN_STYLE == "animated":
            sleep(add_jitter(LOOP_DELAY))
        logger.info("Handling fish")
        with self.hold_keys(mouse=False, shift=False):
            self.handle_events()
            if self.cfg.BOT.CATCH_SCREEN_STYLE == "animated":
                sleep(TAG_ANIMATION_DELAY)
            self._handle_fish()
            
            sleep(add_jitter(3.5))
            
            if self.cfg.PROFILE.MODE in ["bottom", "pirk", "elevator"]:
                self.timer.update_cast_time()
            self.timer.add_cast_time()
            limit = self.cfg.BOT.KEEPNET.CAPACITY - self.cfg.ARGS.FISHES_IN_KEEPNET
            if self.result.kept == limit:
                self.general_quit("Keepnet is full")

    def _handle_fish(self) -> None:
        self.result.total += 1
        bypass = keep = fish_tagged = False
        tag_colors = []
        for tag in TagColor:
            if self.detection.is_tag_exist(tag):
                tag_color = tag.name.lower()
                tag_colors.append(tag_color)
                if tag_color in self.cfg.BOT.KEEPNET.BYPASS_TAGS:
                    bypass = True
                if tag_color in self.cfg.BOT.KEEPNET.KEEP_TAGS:
                    keep = True
                if tag_color in self.cfg.BOT.KEEPNET.SCREENSHOT_TAGS:
                    fish_tagged = True

        if (
            self.cfg.ARGS.SCREENSHOT
            and (not self.cfg.BOT.KEEPNET.SCREENSHOT_TAGS or fish_tagged)
            and "fish" in self.cfg.BOT.KEEPNET.SCREENSHOT_EVENTS
        ):
            filepath = self.timer.get_new_filepath()
            self.detection.window.save_screenshot(filepath)
            send_screenshot(self.cfg, filepath)

        if bypass:
            press("space")
            self.result.kept += 1
        elif self.detection.is_fish_in_list(self.cfg.BOT.KEEPNET.BLACKLIST):
            press("backspace")
        elif (
            self.cfg.ARGS.TAG
            and not keep
            and not self.detection.is_fish_in_list(self.cfg.BOT.KEEPNET.WHITELIST)
        ):
            press("backspace")
        else:
            press("space")
            self.result.kept += 1

        sleep(add_jitter(ANIMATION_DELAY))
        if self.detection.is_keepnet_full():
            self.result.kept -= 1
            press("esc")
            press("backspace")
            sleep(add_jitter(ANIMATION_DELAY))
            self.general_quit("Keepnet is full")

        for tag_color in tag_colors:
            setattr(self.result, tag_color, getattr(self.result, tag_color) + 1)

    def handle_events(self) -> None:
        iterations = 0
        while self.detection.is_event_triggered():
            iterations += 1
            if iterations > MAX_EVENT_ITERATIONS:
                logger.warning(
                    "handle_events: event flag still set after %d iterations, "
                    "stopping to avoid an infinite loop",
                    MAX_EVENT_ITERATIONS,
                )
                return
            if self.detection.is_gift_receieved():
                if (
                    self.cfg.ARGS.SCREENSHOT
                    and "gift" in self.cfg.BOT.KEEPNET.SCREENSHOT_EVENTS
                ):
                    filepath = self.timer.get_new_filepath()
                    self.detection.window.save_screenshot(filepath)
                    send_screenshot(self.cfg, filepath)
                self.result.gift += 1
            elif self.detection.is_card_receieved():
                if (
                    self.cfg.ARGS.SCREENSHOT
                    and "card" in self.cfg.BOT.KEEPNET.SCREENSHOT_EVENTS
                ):
                    filepath = self.timer.get_new_filepath()
                    self.detection.window.save_screenshot(filepath)
                    send_screenshot(self.cfg, filepath)
                self.result.card += 1
            else:
                logger.warning("Unexpected event detected")
            press("enter")
            sleep(add_jitter(LOOP_DELAY))

    def general_quit(self, msg: str) -> None:
        logger.critical(msg)
        if self.friction_brake is not None:
            self.friction_brake.reset(self.cfg.BOT.FRICTION_BRAKE.INITIAL)

        with self.hold_keys(mouse=False, shift=False):
            sleep(add_jitter(ANIMATION_DELAY))
            utils.press("esc")
            sleep(add_jitter(ANIMATION_DELAY))

            if self.cfg.ARGS.SIGNOUT:
                utils.key_down("shift")
                try:
                    quit_pos = self.detection.get_quit_position()
                    if quit_pos:
                        self._hybrid_click_at(quit_pos, clicks=1)
                finally:
                    utils.key_up("shift")
            else:
                quit_pos = self.detection.get_quit_position()
                if quit_pos:
                    self._hybrid_click_at(quit_pos, clicks=1)

            sleep(add_jitter(ANIMATION_DELAY))

            yes_pos = self.detection.get_yes_position()
            if yes_pos:
                self._hybrid_click_at(yes_pos, clicks=1)

            self.handle_termination(msg, shutdown=True, send=True)

    def disconnected_quit(self) -> None:
        logger.critical("Game disconnected")
        with self.hold_keys(mouse=False, shift=False):
            utils.press("space")
            sleep(add_jitter(DISCONNECTED_DELAY))
            utils.press("space")
            sleep(add_jitter(ANIMATION_DELAY))
            
            if not self.cfg.ARGS.SIGNOUT:
                exit_pos = self.detection.get_exit_icon_position()
                if exit_pos:
                    self._hybrid_click_at(exit_pos, clicks=1)
                
                sleep(add_jitter(ANIMATION_DELAY))
                
                confirm_pos = self.detection.get_confirm_button_position()
                if confirm_pos:
                    self._hybrid_click_at(confirm_pos, clicks=1)

            self.handle_termination("Game disconnected", shutdown=True, send=True)

    def get_result_dict(self, msg: str):
        return self.result.as_dict(msg, self.timer)

    def get_result_table(self, result) -> Table:
        table = Table(title="Running Result", box=box.HEAVY, show_header=False)

        for k, v in result.items():
            table.add_row(k, str(v))
        return table

    def _check_next_ticket_hint(self) -> None:
        """Подсказка «Выбрать следующий билет [L]» -> выбираем билет заранее.

        Так аренда не доходит до голубых и красных плашек у компаса (они закрывают
        цифры координат), а выбранный билет сам включится в конце аренды. Без -BT
        ничего не делаем (как и раньше: бот остановится, когда билет кончится).
        """
        duration = self.cfg.ARGS.BOAT_TICKET
        if not duration:
            return
        now = time()
        if getattr(self, "_hint_fails", 0) >= TICKET_HINT_MAX_FAILS:
            return
        if now < getattr(self, "_hint_next_check", 0.0):
            return
        self._hint_next_check = now + TICKET_HINT_CHECK_EVERY
        found = self.detection.is_ticket_next_hint()
        # Раз в минуту пишем, что видит проверка: так по логу ясно, почему подсказка
        # не нашлась (нет шаблона, ошибка снимка или просто её ещё нет на экране).
        self._hint_checks = getattr(self, "_hint_checks", 0) + 1
        if self._hint_checks % 3 == 1:
            logger.info(
                "Boat ticket hint check: %s",
                getattr(self.detection, "last_hint_note", None),
            )
        if not found:
            self._hint_fails = 0
            return
        logger.info("Boat ticket hint: selecting next ticket")
        self._pause_route()
        if self._select_next_ticket(duration):
            self._hint_fails = 0
            self._hint_next_check = time() + TICKET_HINT_RETRY_DELAY
        else:
            self._hint_fails = getattr(self, "_hint_fails", 0) + 1
            self._hint_next_check = time() + TICKET_HINT_RETRY_DELAY
            if self._hint_fails >= TICKET_HINT_MAX_FAILS:
                logger.warning(
                    "Next boat ticket was not selected %d times; relying on the "
                    "expired-ticket dialog", self._hint_fails,
                )

    def _select_next_ticket(self, duration: int) -> bool:
        """L -> окно «Выбор предмета» -> клик по билету нужного срока. True, если выбран."""
        with self.hold_keys(mouse=False, shift=False, reset=True):
            # Клавиша переназначаемая (у одних игроков L, у других P): берём из конфига.
            press(str(self.cfg.KEY.get("NEXT_TICKET", TICKET_NEXT_KEY)))
            sleep(add_jitter(ANIMATION_DELAY * 2))
            ticket_loc = None
            for _ in range(4):
                ticket_loc = self.detection.get_ticket_position(duration)
                if ticket_loc is not None:
                    break
                sleep(add_jitter(ANIMATION_DELAY))
            if ticket_loc is None:
                logger.warning("Next boat ticket (%s day) not found in the dialog", duration)
                press("esc")
                sleep(add_jitter(ANIMATION_DELAY))
                return False
            self._hybrid_click_at(ticket_loc, clicks=2)
            self.result.ticket += 1
            sleep(add_jitter(ANIMATION_DELAY))
        logger.info("Next boat ticket selected (%s day)", duration)
        self._ticket_ui_used = True
        return True

    def _handle_expired_ticket(self) -> None:
        with self.hold_keys(mouse=False, shift=False, reset=True):
            if self.cfg.ARGS.BOAT_TICKET == 0:
                utils.press("esc")
                sleep(add_jitter(TICKET_EXPIRE_DELAY))
                self.general_quit("Boat ticket expired")

            logger.info("Renewing boat ticket")
            ticket_loc = self.detection.get_ticket_position(self.cfg.ARGS.BOAT_TICKET)
            if ticket_loc is None:
                utils.press("esc")  
                sleep(add_jitter(TICKET_EXPIRE_DELAY))
                self.general_quit("New boat ticket not found")

            self._hybrid_click_at(ticket_loc, clicks=2)
            
            self.result.ticket += 1
            self._ticket_ui_used = True
            sleep(add_jitter(ANIMATION_DELAY))

    @utils.press_before_and_after("v")
    def _replace_broken_lures(self) -> None:
        logger.info("Replacing broken lures")

        scrollbar_box = self.detection.get_scrollbar_position()
        if scrollbar_box is None:
            logger.info("Scroll bar not found, changing lures for normal rig")
            while self._open_broken_lure_menu():
                self._replace_item()
            utils.press("v")
            return

        logger.info("Scroll bar found, changing lures for dropshot rig")
        x, y = utils.get_box_center_integers(scrollbar_box)
        utils.move_to(x, y)
        for _ in range(5):
            sleep(add_jitter(ANIMATION_DELAY))
            if utils.is_hw_active():
                utils.mouse_down("left")
                utils.hw_move_mouse_bezier(0, 125)
                utils.mouse_up("left")
            else:
                pag.drag(xOffset=0, yOffset=125, duration=random.uniform(0.4, 0.7), button="left")
            y += 125

            while self._open_broken_lure_menu():
                self._replace_item()
            utils.move_to(x, y)

    def _open_broken_lure_menu(self) -> bool:
        logger.info("Looking for broken lures")
        broken_item_position = self.detection.get_100wear_position()
        if broken_item_position is None:
            logger.warning("Broken lure not found")
            return False

        self._hybrid_click_at(broken_item_position, clicks=1)
        sleep(add_jitter(ANIMATION_DELAY))
        return True

    def _replace_item(self) -> None:
        logger.info("Looking for favorite items")
        favorite_item_positions = self.detection.get_favorite_item_positions()
        while True:
            favorite_item_position = next(favorite_item_positions, None)
            if favorite_item_position is None:
                utils.press("esc")
                sleep(add_jitter(ANIMATION_DELAY))
                utils.press("esc")
                self.general_quit("stop.no_favorite")

            x, y = utils.get_box_center_integers(favorite_item_position)
            if self.detection._get_pixel(x - 70, y + 190) != (178, 59, 30):  
                logger.info("Lure replaced successfully")
                
                utils.move_to(x - 70, y + 190)
                sleep(random.uniform(0.35, 0.65))
                
                utils.mouse_down("left")
                sleep(random.uniform(0.045, 0.085))
                utils.mouse_up("left")
                sleep(random.uniform(0.06, 0.12))
                utils.mouse_down("left")
                sleep(random.uniform(0.045, 0.085))
                utils.mouse_up("left")
                
                sleep(add_jitter(WEAR_TEXT_UPDATE_DELAY))
                break    