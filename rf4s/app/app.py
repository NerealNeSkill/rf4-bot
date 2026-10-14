"""Base application class for other tools."""

import argparse
import os
import random
import shlex
import signal
import smtplib
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from multiprocessing import Lock
from socket import gaierror
from time import sleep
from typing import Optional

import pyautogui as pag
import requests
from pynput import keyboard
from rich import box, print
from rich.prompt import Prompt
from rich.table import Table
from yacs.config import CfgNode as CN

from rf4s import config, exceptions, utils
from rf4s.app.logger import logger
from rf4s.component.friction_brake import FrictionBrake
from rf4s.config import load_cfg
from rf4s.controller.detection import Detection
from rf4s.controller.player import Player
from rf4s.controller.timer import Timer
from rf4s.controller.window import Window
from rf4s.result import BotResult, CraftResult, HarvestResult, Result
from rf4s.utils import add_jitter, press, fix_cyrillic_key

BIAS = 1e-6
ANIMATION_DELAY = 0.5
THREAD_CHECK_DELAY = 0.5
LOOP_DELAY = 1
CRAFT_DELAY = 4.0
MAX_FRICTION_BRAKE = 30

def parse_pynput_key(key) -> str:
    """Универсальный извлекатель имени клавиши из pynput."""
    if isinstance(key, keyboard.Key):
        return key.name.lower()
    
    if hasattr(key, 'char') and key.char:
        return fix_cyrillic_key(key.char).lower()
        
    clean_key = str(key).lower().replace("'", "")
    if clean_key.startswith("key."):
        clean_key = clean_key.split(".")[1]
    return fix_cyrillic_key(clean_key).lower()


class App(ABC):
    def __init__(
        self, cfg: CN, args: argparse.Namespace, parser: argparse.ArgumentParser
    ):
        self.cfg = cfg
        self.args = args
        self.parser = parser
        self.result = None

    @abstractmethod
    def _on_release(self, key: keyboard.KeyCode) -> None:
        pass

    @abstractmethod
    def start(self):
        pass

    def display_result(self) -> None:
        result_dict = self.result.as_dict()
        if not result_dict:
            return

        result = Table(title="Running Result", box=box.HEAVY, show_header=False)
        for name, value in self.result.as_dict().items():
            result.add_row(name, str(value))
        print(result)


class BotApp(App):
    def __init__(
        self, cfg: CN, args: argparse.Namespace, parser: argparse.ArgumentParser
    ):
        # Бот открытый и запускается как у автора (python main.py): лицензию проверяет
        # только лаунчер, здесь никаких проверок нет.
        super().__init__(cfg, args, parser)
        self.setup_profile()
        self.merge_args_to_cfg()
        self.window = Window()
        self.validate_cfg()
        self.cfg.freeze()
        self.display_info()

        self.result = BotResult()
        self.window = Window()
        self.player = Player(
            self.cfg, Timer(self.cfg), Detection(self.cfg, self.window), self.result
        )
        self.paused = False

    def validate_notification_settings(self) -> None:
        """Проверить, что для включённых уведомлений заполнены настройки.

        Все незаполненные параметры выводятся сразу одним списком, после чего
        бот останавливается: иначе об ошибке узнаешь только после часов работы.
        """
        n = self.cfg.BOT.NOTIFICATION
        problems = []

        if self.cfg.ARGS.EMAIL:
            if n.EMAIL in ("", "email@example.com"):
                problems.append("BOT.NOTIFICATION.EMAIL (-E)")
            if n.PASSWORD in ("", "password"):
                problems.append("BOT.NOTIFICATION.PASSWORD (-E)")
            if not n.SMTP_SERVER:
                problems.append("BOT.NOTIFICATION.SMTP_SERVER (-E)")
        if self.cfg.ARGS.MIAOTIXING and not n.MIAO_CODE:
            problems.append("BOT.NOTIFICATION.MIAO_CODE (-M)")
        if self.cfg.ARGS.DISCORD and not n.DISCORD_WEBHOOK_URL:
            problems.append("BOT.NOTIFICATION.DISCORD_WEBHOOK_URL (-D)")
        if self.cfg.ARGS.TELEGRAM:
            if not n.TELEGRAM_BOT_TOKEN:
                problems.append("BOT.NOTIFICATION.TELEGRAM_BOT_TOKEN (-TG)")
            if n.TELEGRAM_CHAT_ID == -1:
                problems.append("BOT.NOTIFICATION.TELEGRAM_CHAT_ID (-TG)")

        if not problems:
            return
        logger.critical(
            "Уведомления включены, но не заполнены настройки в config.yaml "
            "(раздел BOT.NOTIFICATION):\n  %s\n"
            "Заполните их или уберите соответствующие флаги (-E -M -D -TG). / "
            "Notifications are enabled but these settings in config.yaml "
            "(section BOT.NOTIFICATION) are not filled in:\n  %s\n"
            "Fill them in or remove the matching flags (-E -M -D -TG).",
            "\n  ".join(problems),
            "\n  ".join(problems),
        )
        utils.safe_exit()

    def validate_cfg(self):
        self.validate_notification_settings()
        self.validate_smtp()
        self.validate_discord()
        self.validate_miaotixing()
        self.validate_telegram()
        self.validate_game_window()
        self.validate_favorite_icon()
        self.validate_screenshot_notification()
        self.validate_spool_detection()

    def display_info(self):
        settings = Table(
            title="Settings", show_header=False, box=box.HEAVY, min_width=36
        )
        settings.add_row("LAUNCH OPTIONS (FINAL)", " ".join(sys.argv[1:]))
        for k, v in self.cfg.PROFILE.items():
            if k != "DESCRIPTION":
                settings.add_row(k, str(v))
        print(settings)
        if self.cfg.PROFILE.DESCRIPTION:
            utils.print_description_box(self.cfg.PROFILE.DESCRIPTION)
        utils.print_usage_box(
            f"Press {self.cfg.KEY.PAUSE} to pause, {self.cfg.KEY.QUIT} to quit."
        )

    def validate_smtp(self) -> None:
        if not self.cfg.ARGS.EMAIL or not self.cfg.BOT.SMTP_VERIFICATION:
            return
        logger.info("Verifying SMTP connection")
        try:
            with smtplib.SMTP_SSL(
                self.cfg.BOT.NOTIFICATION.SMTP_SERVER, 465, timeout=15
            ) as server:
                server.login(
                    self.cfg.BOT.NOTIFICATION.EMAIL, self.cfg.BOT.NOTIFICATION.PASSWORD
                )
        except smtplib.SMTPAuthenticationError:
            logger.critical(
                "Неверный email или пароль приложения / Invalid email address or app password\n"
                "Check BOT.NOTIFICATION.EMAIL\n"
                "Check BOT.NOTIFICATION.PASSWORD\n"
            )
            utils.safe_exit()
        except (TimeoutError, gaierror, OSError, smtplib.SMTPException) as err:
            # Почта недоступна/сервер оборвал соединение: учётные данные не
            # проверены, но это не повод не запускать бота.
            logger.warning(
                "Не удалось проверить SMTP (%s: %s). Бот продолжит работу, письмо уйдёт "
                "при остановке, если связь восстановится. Проверьте BOT.NOTIFICATION.SMTP_SERVER. / "
                "Could not verify SMTP (%s: %s). The bot will keep running; the email will be "
                "sent on stop if the connection is back. Check BOT.NOTIFICATION.SMTP_SERVER.",
                type(err).__name__,
                err,
                type(err).__name__,
                err,
            )

    def validate_discord(self) -> None:
        if not self.cfg.ARGS.DISCORD or self.cfg.BOT.NOTIFICATION.DISCORD_WEBHOOK_URL:
            return
        logger.critical("BOT.NOTIFICATION.DISCORD_WEBHOOK_URL is not set")
        utils.safe_exit()

    def validate_miaotixing(self) -> None:
        if not self.cfg.ARGS.MIAOTIXING or self.cfg.BOT.NOTIFICATION.MIAO_CODE:
            return
        logger.critical("BOT.NOTIFICATION.MIAO_CODE is not set.")
        utils.safe_exit()

    def validate_telegram(self):
        def _is_telegram_bot_valid():
            """True/False -- ответ получен; None -- Telegram недоступен."""
            url = (
                "https://api.telegram.org/"
                f"bot{self.cfg.BOT.NOTIFICATION.TELEGRAM_BOT_TOKEN}/getMe"
            )
            try:
                return requests.get(url, timeout=10).status_code == 200
            except requests.RequestException as err:
                logger.warning(
                    "Не удалось связаться с Telegram, токен не проверен (%s). "
                    "Бот продолжит работу, уведомление уйдёт при остановке, если сеть восстановится. / "
                    "Could not reach Telegram, the token was not verified (%s). "
                    "The bot will keep running; the notification will be sent on stop if the network is back.",
                    type(err).__name__,
                    type(err).__name__,
                )
                return None

        if not self.cfg.ARGS.TELEGRAM:
            return

        valid = True
        if _is_telegram_bot_valid() is False:
            logger.critical("Invalid BOT.NOTIFICATION.TELEGRAM_BOT_TOKEN")
            valid = False
        if self.cfg.BOT.NOTIFICATION.TELEGRAM_CHAT_ID == -1:
            logger.critical("BOT.NOTIFICATION.TELEGRAM_CHAT_ID is not set")
            valid = False
        if not valid:
            utils.safe_exit()

    def validate_profile(self, profile_name: str) -> None:
        if profile_name not in self.cfg.PROFILE:
            logger.critical("Invalid profile name: '%s'", profile_name)
            utils.safe_exit()

        mode = self.cfg.PROFILE[profile_name].MODE
        if mode.upper() not in self.cfg.PROFILE:
            logger.critical("Invalid mode: '%s'", mode)
            utils.safe_exit()

        expected_keys = set(self.cfg.PROFILE[mode.upper()])
        actual_keys = set(self.cfg.PROFILE[profile_name])
        invalid_keys = actual_keys - expected_keys
        missing_keys = expected_keys - actual_keys

        if invalid_keys or missing_keys:
            for key in invalid_keys:
                logger.warning("Invalid setting: 'PROFILE.%s.%s'", profile_name, key)
            for key in missing_keys:
                logger.warning("Missing setting: 'PROFILE.%s.%s'", profile_name, key)

    def display_profiles(self) -> None:
        profiles = Table(
            title="List of Available Profiles",
            box=box.HEAVY,
            show_header=False,
            min_width=36,
        )
        for i, profile in enumerate(self.cfg.PROFILE):
            profiles.add_row(f"{i:>2}. {profile}")
        print(profiles)
        
    def get_pid(self) -> None:
        while True:
            user_input = Prompt.ask(
                "Enter the Profile ID you want to use (q to quit)", show_default=False
            )
            utils.check_quit(user_input)
            if user_input.isdigit() and 0 <= int(user_input) < len(self.cfg.PROFILE):
                break
            utils.print_error(
                f"Invalid Profile ID. Please enter a number between 0 and {len(self.cfg.PROFILE) - 1}."
            )
        self.args.pid = int(user_input)

    def setup_profile(self) -> None:
        if self.args.pname is not None:
            profile_name = self.args.pname
        else:
            if self.args.pid is None:
                self.display_profiles()
                self.get_pid()
            profile_name = list(self.cfg.PROFILE)[self.args.pid]
        self.merge_default_to_profile(profile_name)

    def merge_default_to_profile(self, profile_name) -> None:
        self.validate_profile(profile_name)
        mode = self.cfg.PROFILE[profile_name].MODE.upper()
        user_profile = CN({"NAME": profile_name}, new_allowed=True)
        user_profile.merge_from_other_cfg(self.cfg.PROFILE[mode])
        user_profile.merge_from_other_cfg(self.cfg.PROFILE[profile_name])
        self.cfg.PROFILE = user_profile

    def merge_args_to_cfg(self) -> None:
        if len(self.args.opts) % 2:
            logger.error(
                "Invalid launch options: '%s'\n"
                "These arguments are used for config override: '%s'\n",
                " ".join(sys.argv[1:]),
                " ".join(self.args.opts),
            )
            sys.exit()

        self.cfg.merge_from_list(self.args.opts)
        if self.cfg.PROFILE.LAUNCH_OPTIONS:
            new_launch_options = shlex.split(self.cfg.PROFILE.LAUNCH_OPTIONS)
            base_len = len(shlex.split(self.cfg.BOT.LAUNCH_OPTIONS)) + 2
            sys.argv = sys.argv[:base_len] + new_launch_options + sys.argv[base_len:]
            self.args = self.parser.parse_args()
        self.cfg.ARGS = config.dict_to_cfg(vars(self.args))

    def validate_game_window(self) -> None:
        if self.window.is_title_bar_exist():
            logger.info("Window mode detected. Don't move the game window")
        if self.window.is_size_supported():
            logger.info("Supported window size. Don't change the game window size")
            return

        window_resolution = self.window.get_resolution_str()
        if window_resolution == "0x0":
            logger.critical("'Fullscreen mode' is not supported")
            utils.safe_exit()

        if self.cfg.PROFILE.MODE in ("telescopic", "bolognese"):
            logger.critical(
                "Fishing mode '%s' doesn't support window size '%s'",
                self.cfg.PROFILE.MODE,
                self.window.get_resolution_str(),
            )
            utils.safe_exit()

        logger.warning(
            "Unsupported window size '%s'\n"
            "Will search the image on the screen instead of the game window",
            self.window.get_resolution_str(),
        )
        logger.error(
            "Snag detection will be disabled\n"
            "Spooling detection will be disabled\n"
            "Auto friction brake will be disabled\n"
        )
        self.cfg.ARGS.FRICTION_BRAKE = False
        self.cfg.BOT.SNAG_DETECTION = False
        self.cfg.BOT.SPOOLING_DETECTION = False

    def validate_favorite_icon(self):
        if (
            self.cfg.ARGS.COFFEE
            or self.cfg.ARGS.REFILL
            or self.cfg.ARGS.LURE
            or self.cfg.ARGS.DRY_MIX
            or self.cfg.ARGS.GROUNDBAIT
            or self.cfg.ARGS.PVA
        ):
            logger.info(
                "Some features require you to add the item to your favorites, "
                "make sure you have done so"
            )

    def validate_screenshot_notification(self):
        if self.cfg.ARGS.SCREENSHOT and self.cfg.ARGS.MIAOTIXING:
            logger.warning(
                "Miaotixing doesn't support image message, no screenshot will be sent"
            )

    def validate_spool_detection(self):
        if self.cfg.ARGS.RAINBOW is None:
            logger.warning(
                "Default retrieval detection mode detected, fully spool your reel"
            )

    def _on_release(self, key: keyboard.KeyCode) -> None:
        pressed_key = parse_pynput_key(key)
        quit_key = str(self.cfg.KEY.QUIT).lower()
        pause_key = str(self.cfg.KEY.PAUSE).lower()

        if getattr(self.cfg.ARGS, "DATA", False) and str(pressed_key).startswith("f"):
            logger.info("[Key] released %s (quit=%s, pause=%s)", pressed_key, quit_key, pause_key)

        if pressed_key == quit_key:
            logger.info("Quit key pressed (F8). Stopping bot...")
            self.paused = False
            os.kill(os.getpid(), signal.CTRL_C_EVENT)
            return False

        if pressed_key == pause_key:
            logger.info("Pause key pressed (F7). Pausing bot...")
            self.paused = True
            os.kill(os.getpid(), signal.CTRL_C_EVENT)
            return False

    def _pause_wait(self, key: keyboard.KeyCode) -> None:
        pressed_key = parse_pynput_key(key)
        pause_key = str(self.cfg.KEY.PAUSE).lower()
        if pressed_key == pause_key:
            logger.info("Resuming bot execution...")
            self.paused = False
            return False

    def reload_cfg(self) -> None:
        profile_name = self.cfg.PROFILE.NAME
        self.cfg = load_cfg()
        self.merge_default_to_profile(profile_name)
        self.merge_args_to_cfg()
        self.validate_cfg()
        self.cfg.freeze()
        self.display_info()

    def start(self) -> None:
        while True:
            general_listener = keyboard.Listener(on_release=self._on_release)
            general_listener.start()
            self.window.activate_game_window()
            try:
                self.player.start_fishing()
            except KeyboardInterrupt:
                general_listener.stop()
                if not self.paused:
                    break

                utils.print_usage_box(
                    f"Press {self.cfg.KEY.PAUSE} to reload config and restart."
                )
                utils.print_hint_box(
                    "Any modifications made to LAUNCH_OPTIONS will be ignored."
                )
                with self.player.hold_keys(mouse=False, shift=False, reset=True):
                    pause_listener = keyboard.Listener(on_release=self._pause_wait)
                    pause_listener.start()

                while pause_listener.is_alive():
                    sleep(add_jitter(THREAD_CHECK_DELAY))

                logger.info("Restarting bot without resetting records")
                self.reload_cfg()
                # Timer и Detection читают self.cfg при каждом обращении:
                # переключаем их на новый конфиг, сохраняя накопленное состояние.
                self.player.timer.cfg = self.cfg
                self.player.detection.cfg = self.cfg
                self.player = Player(
                    self.cfg,
                    self.player.timer,
                    self.player.detection,
                    self.player.result,
                )
                self.paused = False

        self.player.handle_termination("Terminated by user", shutdown=False, send=False)
        
class CraftApp(App):
    def __init__(self, cfg, args, parser):
        super().__init__(cfg, args, parser)
        args_cfg = CN({"ARGS": config.dict_to_cfg(vars(self.args))})
        self.cfg.merge_from_other_cfg(args_cfg)
        self.cfg.merge_from_list(self.args.opts)
        self.cfg.freeze()

        settings = Table(
            title="Settings", show_header=False, box=box.HEAVY, min_width=36
        )
        settings.add_row("LAUNCH OPTIONS (FINAL)", " ".join(sys.argv[1:]))
        print(settings)

        self.result = CraftResult()
        self.window = Window()
        self.detection = Detection(self.cfg, self.window)

    def craft_item(self, accept_key: str) -> None:
        logger.info("Crafting item")
        utils.click("left")
        sleep(add_jitter(CRAFT_DELAY))

        self.result.material += 1
        
        import time
        craft_deadline = time.time() + 15.0

        while True:
            # ЗАЩИТА: Если игра фризнула и анимация зависла — прерываем ожидание
            if time.time() > craft_deadline:
                logger.warning("Craft animation timeout! Recovering...")
                utils.press("esc")
                break

            if self.detection.is_operation_success():
                logger.info("Crafting succeeded")
                self.result.success += 1
                press(accept_key)
                break

            if self.detection.is_operation_failed():
                logger.warning("Crafting failed")
                self.result.fail += 1
                press("space")
                break

            sleep(add_jitter(LOOP_DELAY))

        sleep(add_jitter(ANIMATION_DELAY))

        discard_yes_position = self.detection.get_discard_yes_position()
        if discard_yes_position:
            dx, dy = utils.get_box_center_integers(discard_yes_position)
            utils.move_to(dx, dy)
            sleep(random.uniform(0.08, 0.15))
            utils.click("left")

        sleep(add_jitter(LOOP_DELAY))

    def _on_release(self, key: keyboard.KeyCode) -> None:
        if str(key).lower() == str(keyboard.KeyCode.from_char(self.cfg.KEY.QUIT)):
            os.kill(os.getpid(), signal.CTRL_C_EVENT)
            return False

    def start(self) -> None:
        listener = keyboard.Listener(on_release=self._on_release)
        listener.start()

        try:
            utils.print_usage_box(f"Press {self.cfg.KEY.QUIT} to quit.")
            logger.warning("This might get you banned, use at your own risk")
            random.seed(datetime.now().timestamp())
            accept_key = "backspace" if self.cfg.ARGS.DISCARD else "space"
            self.window.activate_game_window()
            
            make_button_position = self.detection.get_make_button_position()
            if make_button_position is None:
                logger.critical(
                    "Make button not found, please set the interface scale to "
                    "1x or move your mouse around"
                )
                self.window.activate_script_window()
                sys.exit()
                
            mx, my = utils.get_box_center_integers(make_button_position)
            utils.move_to(mx, my)
            
            while True:
                if (
                    not self.cfg.ARGS.IGNORE
                    and not self.detection.is_material_complete()
                ):
                    logger.critical("Running out of materials")
                    break
                if self.result.material == self.cfg.ARGS.CRAFT_LIMIT:
                    logger.info("Crafting limit reached")
                    break
                self.craft_item(accept_key)
                utils.move_to(mx, my)
        except KeyboardInterrupt:
            pass
        self.display_result()


class MoveApp(App):
    def __init__(self, cfg, args, parser):
        super().__init__(cfg, args, parser)
        args_cfg = CN({"ARGS": config.dict_to_cfg(vars(self.args))})
        self.cfg.merge_from_other_cfg(args_cfg)
        self.cfg.merge_from_list(self.args.opts)
        self.cfg.freeze()

        utils.print_usage_box(
            f"Press {self.cfg.KEY.MOVE_PAUSE} to pause, "
            f"{self.cfg.KEY.MOVE_QUIT} to quit.",
        )

        self.result = Result()
        self.window = Window()
        self.w_key_pressed = True

    def _on_release(self, key: keyboard.KeyCode) -> None:
        char = getattr(key, "char", None)
        if char:
            char = utils.fix_cyrillic_key(char).lower()
        else:
            char = str(key).lower()

        move_quit = utils.fix_cyrillic_key(self.cfg.KEY.MOVE_QUIT).lower()
        move_pause = utils.fix_cyrillic_key(self.cfg.KEY.MOVE_PAUSE).lower()

        if char == move_quit:
            return False

        if char == move_pause:
            if self.w_key_pressed:
                utils.key_up("w")
                self.w_key_pressed = False
            else:
                utils.key_down("w")
                self.w_key_pressed = True

    def start(self) -> None:
        listener = keyboard.Listener(on_release=self._on_release)
        listener.start()
        self.window.activate_game_window()

        try:
            if self.cfg.ARGS.SHIFT:
                utils.key_down("shift")
                sleep(random.uniform(0.04, 0.08))

            utils.key_down("w")

            while listener.is_alive():
                sleep(add_jitter(THREAD_CHECK_DELAY))

        finally:
            utils.key_up("w")
            sleep(random.uniform(0.04, 0.08))
            utils.key_up("shift")
            sleep(random.uniform(0.04, 0.08))

            if utils.is_hw_active():
                utils._clear_hw_command_queue()
                utils._write_hw_direct("RELEASE_ALL")

class HarvestApp(App):
    def __init__(self, cfg, args, parser):
        super().__init__(cfg, args, parser)
        args_cfg = CN({"ARGS": config.dict_to_cfg(vars(self.args))})
        self.cfg.merge_from_other_cfg(args_cfg)
        self.cfg.merge_from_list(self.args.opts)
        self.cfg.freeze()

        settings = Table(
            title="Settings", show_header=False, box=box.HEAVY, min_width=36
        )
        settings.add_row("LAUNCH OPTIONS (FINAL)", " ".join(sys.argv[1:]))
        settings.add_row("Power saving", str(self.cfg.HARVEST.POWER_SAVING))
        settings.add_row("Check delay", str(self.cfg.HARVEST.CHECK_DELAY))
        settings.add_row("Energy threshold", str(self.cfg.STAT.ENERGY_THRESHOLD))
        settings.add_row("Hunger threshold", str(self.cfg.STAT.HUNGER_THRESHOLD))
        settings.add_row("Comfort threshold", str(self.cfg.STAT.COMFORT_THRESHOLD))
        print(settings)
        utils.print_usage_box(f"Press {self.cfg.KEY.QUIT} to quit.")

        self.result = HarvestResult()
        self.timer = Timer(self.cfg)
        self.window = Window()
        self.detection = Detection(self.cfg, self.window)

    def _on_release(self, key: keyboard.KeyCode) -> None:
        if str(key).lower() == str(keyboard.KeyCode.from_char(self.cfg.KEY.QUIT)):
            os.kill(os.getpid(), signal.CTRL_C_EVENT)
            return False

    def refill_player_stats(self) -> None:
        if not self.cfg.ARGS.REFILL:
            return
        drank_tea = False
        if self.detection.is_comfort_low() or not self.detection.is_energy_high():
            if self.timer.is_tea_drinkable():
                # Человечность: чай ~70%, доп. скип 40% при высокой энергии
                skip = random.random() > 0.70
                if self.detection.is_energy_high() and self.detection.is_comfort_low():
                    skip = skip or random.random() < 0.40
                if skip:
                    logger.info("Low energy or comfort detected. Tea skipped (human-like).")
                else:
                    logger.info("Low energy or comfort detected. Drinking tea...")
                    self._use_item("tea")
                    drank_tea = True
        if self.detection.is_hunger_low():
            if not hasattr(self, 'last_harvest_carrot'):
                self.last_harvest_carrot = 0
            if time.time() - self.last_harvest_carrot > 30.0:
                # Человечность: пауза после чая, морковь ~75%
                if drank_tea:
                    sleep(random.uniform(0.8, 1.6))
                if random.random() <= 0.75:
                    logger.info("Low hunger detected. Eating carrot...")
                    self._use_item("carrot")
                    self.last_harvest_carrot = time.time()
                else:
                    logger.info("Low hunger detected. Carrot skipped (human-like).")

    def _use_item(self, item: str) -> None:
        logger.info("Using item: %s", item)
        key = str(self.cfg.KEY.get(item.upper(), "-1"))

        if key != "-1":
            utils.press(key)
            sleep(random.uniform(0.25, 0.35))
            if item.lower() == "tea":
                self.result.tea += 1
            elif item.lower() == "carrot":
                self.result.carrot += 1
            return

        utils.key_down("t")
        try:
            sleep(random.uniform(0.18, 0.28))
            screen_w, screen_h = pag.size()
            center_x, center_y = screen_w // 2, screen_h // 2
            
            utils.move_to(center_x, center_y)

            if item.lower() == "tea":
                for _ in range(12):
                    utils.scroll(-400)
                sleep(random.uniform(0.15, 0.25))

            food_position = self.detection.get_food_position(item)

            if not food_position:
                for _ in range(3):
                    utils.scroll(-300)
                sleep(random.uniform(0.12, 0.18))
                food_position = self.detection.get_food_position(item)

            if food_position:
                target_x, target_y = utils.get_box_center_integers(food_position)
                jitter_x = random.randint(-4, 4)
                jitter_y = random.randint(-4, 4)
                
                utils.move_to(target_x + jitter_x, target_y + jitter_y)
                sleep(random.uniform(0.08, 0.15))
                utils.click("left")
                sleep(random.uniform(0.15, 0.25))

                if item.lower() == "tea":
                    self.result.tea += 1
                elif item.lower() == "carrot":
                    self.result.carrot += 1
            else:
                logger.warning("Could not find item '%s' in T-menu!", item)
                utils.press("esc")
        finally:
            utils.key_up("t")
            sleep(random.uniform(0.35, 0.5))

    def harvest_baits(self) -> None:
        logger.info("Harvesting baits...")
        utils.click("left")
        
        harvest_deadline = time.time() + 15.0
        while not self.detection.is_harvest_success():
            if time.time() >= harvest_deadline:
                logger.warning("Harvest timeout")
                utils.press("esc")
                return
            sleep(add_jitter(LOOP_DELAY))

        sleep(random.uniform(0.4, 0.75))
        utils.press("space")
        sleep(random.uniform(0.08, 0.15))
        utils.press("enter")
        sleep(random.uniform(0.1, 0.2))
        logger.info("Baits harvested successfully")
        if self.cfg.ARGS.REFILL:
            # Человечность: 55% чай с паузой, 13.5% холостое T-меню, 31.5% ничего
            roll = random.random()
            if roll < 0.55:
                sleep(random.uniform(1.5, 3.5))
                logger.info("Refilling player stats (Post-harvest Tea)")
                self._use_item("tea")
                self.timer.last_tea_drink = time.time()
            elif roll < 0.685:
                sleep(random.uniform(1.0, 2.0))
                logger.info("Post-harvest idle: opening and closing T-menu")
                utils.key_down("t")
                sleep(random.uniform(0.4, 0.65))
                utils.key_up("t")
                sleep(add_jitter(0.5))
            else:
                logger.info("Refilling player stats (Post-harvest skipped, human-like)")

    def start(self) -> None:
        listener = keyboard.Listener(on_release=self._on_release)
        listener.start()

        self.window.activate_game_window()
        try:
            press(str(self.cfg.KEY.DIGGING_TOOL))
            sleep(add_jitter(3.0))
            while True:
                self.refill_player_stats()
                if self.detection.is_energy_high():
                    self.harvest_baits()
                    self.result.bait += 1
                else:
                    logger.info("Energy is not high enough")

                if self.cfg.HARVEST.POWER_SAVING:
                    press("esc")
                    sleep(add_jitter(self.cfg.HARVEST.CHECK_DELAY))
                    press("esc")
                    sleep(add_jitter(ANIMATION_DELAY))
                else:
                    sleep(add_jitter(self.cfg.HARVEST.CHECK_DELAY))
        except KeyboardInterrupt:
            pass
        self.display_result()


@dataclass
class Part:
    name: str
    prompt: str
    color: str
    base: float = 0.0
    load_capacity: Optional[float] = None
    wear: Optional[float] = None
    real_load_capacity: Optional[float] = None
    pre_real_load_capacity: Optional[float] = None

    def calculate_real_load_capacity(self) -> None:
        self.real_load_capacity = (
            self.load_capacity * (1 - self.base) * (1 - self.wear / 100)
            + self.load_capacity * self.base
        )


class CalculateCommand(Enum):
    RESTART = "r"
    PREVIOUS = "p"
    PREVIOUS_REMAINING = "P"
    SKIP = "s"
    SKIP_REMAINING = "S"
    QUIT = "q"


class CalculateApp:
    def __init__(self, cfg, args, parser):
        _ = cfg, args, parser
        self.result = None
        self.parts = [
            Part(name="Rod / Удилище", prompt="Load capacity / Прочность (kg)", color="orange1", base=0.3),
            Part(name="Reel mechanism / Механизм катушки", prompt="Mech / Механизм (kg)", color="plum1", base=0.3),
            Part(name="Reel friction brake / Фрикцион катушки", prompt="Drag / Фрикцион (kg)", color="gold1"),
            Part(name="Fishing line / Леска (шнур)", prompt="Load capacity / Прочность (kg)", color="salmon1"),
            Part(name="Leader / Поводок", prompt="Load capacity / Прочность (kg)", color="pale_green1"),
            Part(name="Hook / Крючок", prompt="Load capacity / Прочность (kg)", color="sky_blue1"),
        ]
        self.friction_brake = next(
            part for part in self.parts if part.name.startswith("Reel friction brake")
        )

    def calculate_tackle_stats(self):
        previous = False
        for part in self.parts:
            try:
                if previous:
                    if part.pre_real_load_capacity is not None:
                        raise exceptions.PreviousError
                    else:
                        utils.print_error(f"{part.name}: value not found / значение не найдено.")
                part.load_capacity = self.get_validated_input(part, part.prompt)
                part.wear = self.get_validated_input(part, "Wear / Износ (%)")
            except exceptions.SkipError:
                if part.real_load_capacity is not None:
                    part.real_load_capacity = None
                continue
            except exceptions.PreviousError:
                part.real_load_capacity = part.pre_real_load_capacity
            except exceptions.PreviousRemainingError:
                part.real_load_capacity = part.pre_real_load_capacity
                previous = True
            else:
                part.calculate_real_load_capacity()
            self.result.add_row(part.name, f"{part.real_load_capacity:.2f} kg / кг")

    def get_validated_input(self, part: Part, prompt: str) -> float:
        while True:
            user_input = Prompt.ask(
                f"[{part.color}][{part.name}][/{part.color}] {prompt}"
            )
            match user_input.strip():
                case CalculateCommand.RESTART.value:
                    raise exceptions.RestartError
                case CalculateCommand.PREVIOUS.value:
                    if part.pre_real_load_capacity is None:
                        utils.print_error(f"{part.name}: value not found / значение не найдено.")
                        continue
                    raise exceptions.PreviousError
                case CalculateCommand.PREVIOUS_REMAINING.value:
                    if part.pre_real_load_capacity is None:
                        utils.print_error(f"{part.name}: value not found / значение не найдено.")
                        continue
                    raise exceptions.PreviousRemainingError
                case CalculateCommand.SKIP.value:
                    raise exceptions.SkipError
                case CalculateCommand.SKIP_REMAINING.value:
                    raise exceptions.SkipRemainingError
                case CalculateCommand.QUIT.value:
                    raise exceptions.QuitError

            try:
                number = float(user_input)
                if prompt.startswith("Wear"):
                    if not (0 <= number <= 100):
                        utils.print_error("Wear must be between 0 and 100 / Износ должен быть от 0 до 100.")
                        continue
                elif number < 0:
                    utils.print_error("Value must be non-negative / Значение должно быть неотрицательным.")
                    continue
                return number
            except ValueError:
                utils.print_error("Invalid input. Please try again / Некорректный ввод. Попробуйте снова.")

    def reset_stats(self) -> None:
        for part in self.parts:
            part.pre_real_load_capacity = part.real_load_capacity
            part.real_load_capacity = None
        self.result = Table(
            "Results / Результаты",
            title="Tackle's Stats / Характеристики снасти",
            show_header=False,
            box=box.HEAVY,
            min_width=42,
        )

    def update_result(self) -> None:
        valid_parts = [p for p in self.parts if p.real_load_capacity is not None]
        if not valid_parts:
            return
        weakest_part = min(valid_parts, key=lambda x: x.real_load_capacity)
        self.result.add_row("Weakest part / Слабое звено", weakest_part.name)

        if self.friction_brake.real_load_capacity is None:
            return
        try:
            recommend_friction_brake = min(
                MAX_FRICTION_BRAKE - 1,
                (
                    weakest_part.real_load_capacity
                    * MAX_FRICTION_BRAKE
                    / self.friction_brake.real_load_capacity
                ),
            )
            self.result.add_row(
                "Recommend friction brake / Реком. фрикцион",
                f"{int(recommend_friction_brake):2d}",
            )
        except ZeroDivisionError:
            pass

    def start(self):
        utils.print_usage_box(
            "Commands / Команды:\n"
            "r: Restart / Начать заново\n"
            "s: Skip a part / Пропустить элемент\n"
            "S: Skip the remaining parts / Пропустить все оставшиеся\n"
            "p: Use previous value / Использовать прошлым значение\n"
            "P: Use previous for remaining / Прошлые значения для всех оставшихся\n"
            "q: Quit / Выйти"
        )
        utils.print_hint_box("Press V and click the gear icon to view parts / Нажмите V и кликните на шестеренку для просмотра компонентов.")

        while True:
            self.reset_stats()
            try:
                self.calculate_tackle_stats()
            except exceptions.SkipRemainingError:
                pass
            except exceptions.RestartError:
                continue
            except exceptions.QuitError:
                print("Bye / До связи.")
                break
            if self.result.rows:
                self.update_result()
                print(self.result)


class FrictionBrakeApp(App):
    def __init__(self, cfg, args, parser):
        super().__init__(cfg, args, parser)
        args_cfg = CN({"ARGS": config.dict_to_cfg(vars(self.args))})
        self.cfg.merge_from_other_cfg(args_cfg)
        self.cfg.merge_from_list(self.args.opts)
        self.cfg.freeze()

        settings = Table(
            title="Settings", show_header=False, box=box.HEAVY, min_width=36
        )
        settings.add_row("LAUNCH OPTIONS (FINAL)", " ".join(sys.argv[1:]))
        settings.add_row("Initial friction brake", str(self.cfg.FRICTION_BRAKE.INITIAL))
        settings.add_row("Max friction brake", str(self.cfg.FRICTION_BRAKE.MAX))
        settings.add_row("Start delay", str(self.cfg.FRICTION_BRAKE.START_DELAY))
        settings.add_row("Increase delay", str(self.cfg.FRICTION_BRAKE.INCREASE_DELAY))
        settings.add_row("Sensitivity", self.cfg.FRICTION_BRAKE.SENSITIVITY)
        print(settings)

        utils.print_usage_box(
            f"Press {self.cfg.KEY.FRICTION_BRAKE_RESET} to reset friction brake, "
            f"{self.cfg.KEY.FRICTION_BRAKE_QUIT} to quit."
        )

        self.window = Window()
        if not self.is_game_window_valid():
            utils.safe_exit()

        self.detection = Detection(self.cfg, self.window)
        self.friction_brake = FrictionBrake(self.cfg, Lock(), self.detection)

    def is_game_window_valid(self) -> bool:
        if self.window.is_title_bar_exist():
            logger.info("Window mode detected. don't move the game window")
        if self.window.is_size_supported():
            logger.info("Supported window size. Don't change the game window size")
            return True
        logger.critical('Window mode must be "Borderless windowed" or "Window mode"')
        logger.critical(
            "Unsupported window size '%s', use '2560x1440', '1920x1080' or '1600x900'",
            self.window.get_resolution_str(),
        )
        return False

    def _on_release(self, key: keyboard.KeyCode) -> None:
        key = str(key).lower()
        if key == str(keyboard.KeyCode.from_char(self.cfg.KEY.FRICTION_BRAKE_QUIT)):
            self.friction_brake.monitor_process.terminate()
            return False
        if key == str(keyboard.KeyCode.from_char(self.cfg.KEY.FRICTION_BRAKE_RESET)):
            self.friction_brake.reset(self.cfg.FRICTION_BRAKE.INITIAL)

    def start(self):
        listener = keyboard.Listener(on_release=self._on_release)
        listener.start()
        self.window.activate_game_window()
        self.friction_brake.monitor_process.start()
        self.friction_brake.reset(self.cfg.FRICTION_BRAKE.INITIAL)

        while listener.is_alive():
            sleep(THREAD_CHECK_DELAY)                