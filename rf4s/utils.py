"""Commonly used helper functions goes here."""

import ctypes
import datetime
import io
import logging
import logging.config
import math
import msvcrt
import os
import random
import sys
import multiprocessing
import threading
from queue import Empty
from time import sleep, time

import numpy as np
import pyautogui as pag
import rich.logging  # noqa: F401
import serial
from rich import box, print
from rich.panel import Panel

from rf4s.controller.console import console

# ---------------------------------------------------------------------------- #
#                 Исправление кодировки консоли Windows (UTF-8)                 #
# ---------------------------------------------------------------------------- #
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Инициатор логгера для вывода в консоль бота
logger = logging.getLogger("rf4s")

# ---------------------------------------------------------------------------- #
#                               Транслитерация                                 #
# ---------------------------------------------------------------------------- #
RU_KEYS = "йцукенгшщзхъфывапролджэячсмитьбюёЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮЁ"
EN_KEYS = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>~"
RU_TO_EN_KEYBOARD = str.maketrans(RU_KEYS, EN_KEYS)

def fix_cyrillic_key(key: str) -> str:
    """Переводит русские буквы в соответствующие латинские клавиши."""
    return str(key).translate(RU_TO_EN_KEYBOARD)


# ---------------------------------------------------------------------------- #
#              Инициализация платы эмуляции (Межпроцессная очередь)            #
# ---------------------------------------------------------------------------- #
rf4_lang = os.environ.get("RF4_LANG", "RU").strip().upper()

hw_board = None
_board_initialized = False
_hw_started_with_board = False  # Флаг: была ли плата успешно подключена хоть раз

# Очередь команд для com-порта: несмотря на название "межпроцессная", на
# ВАЖНО: это multiprocessing.Queue, не queue.Queue -- несмотря на то, что
# сам cmd_queue.get()/.put() используются только внутри одного процесса
# (потребитель -- демонический поток _com_worker, см. ниже). Раньше здесь
# стоял queue.Queue() как обход бага PyInstaller с _multiprocessing в
# main.exe -- но FrictionBrake (friction_brake.py) хранит эту очередь как
# self.cmd_queue и передаёт СЕБЯ ЦЕЛИКОМ в Process(target=..., args=(self,)),
# то есть cmd_queue всё равно должен пиклиться при старте friction-brake
# процесса, даже если сам он работает только внутри одного процесса.
# queue.Queue() непиклибелен (внутри threading.Lock) -- при --friction-brake
# это валило бота с "TypeError: cannot pickle '_thread.lock' object" прямо
# на Process.start(). _multiprocessing теперь нормально грузится и в main.exe
# (см. _patch_missing_stdlib_extensions()/--contents-directory в build.py),
# так что обход больше не нужен и только вредит.
cmd_queue = multiprocessing.Queue()
_hw_worker_started = False
# Трассировка HID-команд (PRESS/RELEASE мыши и клавиш) в лог: включается флагом -d.
HW_TRACE = False

# Один мьютекс на ВСЕ прямые записи в COM6
com_lock = threading.RLock()


_emergency_lock = threading.Lock()


def _emergency_release_and_exit(reason: str) -> None:
    """Аварийный выход: сначала по возможности отпустить всё, потом завершить процесс.

    os._exit остаётся, потому что функция вызывается и из рабочего потока
    (там sys.exit() завершил бы только поток). Но os._exit обходит finally,
    atexit и utils.safe_exit(), поэтому отпускание делается здесь. Каждый шаг
    ограничен по времени: зависшая плата или занятый com_lock не должны
    задерживать выход.
    """
    if not _emergency_lock.acquire(blocking=False):
        sleep(10)  # другой поток уже завершает процесс
        return

    logger.critical(reason)

    def release_board() -> None:
        board = hw_board
        if board is None:
            return
        locked = com_lock.acquire(timeout=0.5)
        try:
            try:
                board.write_timeout = 0.3
                board.write(b"RELEASE_ALL\n")
                board.flush()
            except Exception:
                pass
            try:
                board.close()  # освобождаем COM-порт для лаунчера
            except Exception:
                pass
        finally:
            if locked:
                com_lock.release()

    worker = threading.Thread(target=release_board, daemon=True)
    worker.start()
    worker.join(timeout=1.5)

    # Системные модификаторы и кнопки мыши (на случай режима без платы).
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            for vk in (0x10, 0x11, 0x12):  # Shift, Ctrl, Alt
                user32.keybd_event(vk, 0, 0x0002, 0)
            user32.mouse_event(0x0004, 0, 0, 0, 0)  # левая кнопка вверх
            user32.mouse_event(0x0010, 0, 0, 0, 0)  # правая кнопка вверх
        except Exception:
            pass

    try:
        logging.shutdown()
    except Exception:
        pass
    os._exit(1)


# Публичный ключ для манифеста целостности (build\integrity.py). Заменяется
# build.py на PUBLIC_KEY_HEX из licensing.py до компиляции; нули -- сборка без
# build.py (разработка), проверка выключена.
_INTEGRITY_PUBLIC_KEY_HEX = "0" * 64
_INTEGRITY_SUFFIXES = (".py", ".pyd", ".so")


def _integrity_problem(pkg_dir) -> str | None:
    """Описание проблемы с файлами пакета rf4s или None, если всё цело."""
    import base64
    import hashlib
    import json

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    def b64d(text: str) -> bytes:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

    try:
        raw = (pkg_dir / "integrity.dat").read_text(encoding="ascii").strip()
        payload_b64, sig_b64 = raw.split(".", 1)
        payload = b64d(payload_b64)
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(_INTEGRITY_PUBLIC_KEY_HEX))
        key.verify(b64d(sig_b64), payload)
        files = json.loads(payload)["files"]
    except Exception:  # noqa: BLE001 -- нет файла, подпись неверна, мусор
        return "manifest"

    for rel, digest in files.items():
        try:
            data = (pkg_dir / rel).read_bytes()
        except OSError:
            return "missing"
        if hashlib.sha256(data).hexdigest() != digest:
            return "modified"

    for path in pkg_dir.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix.lower() in _INTEGRITY_SUFFIXES:
            if path.relative_to(pkg_dir).as_posix() not in files:
                return "extra"
    return None


def verify_integrity(emergency: bool = False) -> None:
    """Остановить бота, если файлы пакета rf4s в сборке подменены.

    Работает только в собранной версии (utils загружен из .pyd и build.py
    вшил ключ). При запуске из исходников ничего не делает.
    emergency=True -- вызов из фонового потока: отпустить кнопки и плату.
    """
    from pathlib import Path

    here = Path(__file__).resolve()
    if here.suffix.lower() not in (".pyd", ".so"):
        return
    if _INTEGRITY_PUBLIC_KEY_HEX == "0" * 64:
        return
    if _integrity_problem(here.parent) is None:
        return

    message = (
        "Файлы программы изменены или повреждены. Переустановите программу. / "
        "Program files were modified or are corrupted. Please reinstall."
    )
    if emergency:
        _emergency_release_and_exit(message)
        return
    print(message, file=sys.stderr)
    sys.stderr.flush()
    sys.stdout.flush()
    sleep(0.2)  # лаунчер успевает вычитать сообщение из пайпа
    os._exit(1)


def _write_hw_direct(cmd_str: str) -> bool:
    """Write an interactive HID command directly to COM6."""
    global hw_board, _board_initialized, _hw_started_with_board

    board = get_hw_board()
    if board is None or not getattr(board, "is_open", False):
        return False

    try:
        clean_cmd = f"{cmd_str}\n".encode("ascii", errors="ignore")
        with com_lock:
            board.write(clean_cmd)
            board.flush()
        if not cmd_str.startswith("MOVE:"):
            logger.info(f"[HW] Sent: {cmd_str}")
        else:
            logger.debug(f"[HW] Sent: {cmd_str}")
        return True
    except serial.SerialTimeoutException:
        _emergency_release_and_exit(
            "Тайм-аут записи (COM6)! Плата зависла. Аварийная остановка бота. / "
            "COM6 write timeout! The board is frozen. Emergency stop."
        )
        return False
    except Exception as err:
        _emergency_release_and_exit(
            f"Плата эмуляции потеряна при отправке команды: {err}. Аварийная остановка бота. / "
            f"Emulation board lost while sending a command: {err}. Emergency stop."
        )
        return False


def _clear_hw_command_queue() -> None:
    """Удалить все команды, накопившиеся до/во время переподключения."""
    while True:
        try:
            cmd_queue.get_nowait()
        except Empty:
            return
        except Exception:
            return


def get_hw_board(port="COM6", baudrate=115200):
    """Ленивая и потокобезопасная инициализация COM-порта с реконнектом."""
    global hw_board, _board_initialized, _hw_started_with_board

    # Плата уже открыта и доступна
    if hw_board is not None and getattr(hw_board, "is_open", False):
        return hw_board

    # Бот работает либо с платой, либо без неё на протяжении всего запуска.
    # Плата была и пропала -- переподключения нет, аварийный выход с отпусканием.
    if _hw_started_with_board:
        _emergency_release_and_exit(
            "Плата эмуляции (COM6) отключилась во время работы. Аварийная остановка бота. / "
            "Emulation board (COM6) disconnected during operation. Emergency stop."
        )
        return None
    # Платы не было при старте (режим pyautogui) -- позднее подключение не принимаем.
    if _board_initialized:
        return None

    # Если был старый объект, но порт уже закрыт — сбрасываем его
    if hw_board is not None:
        try:
            hw_board.close()
        except Exception:
            pass
        hw_board = None

    ser = None

    try:
        ser = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=1,
            write_timeout=1,
        )
        sleep(0.3)

        with com_lock:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            _clear_hw_command_queue()
            ser.write(b"RELEASE_ALL\n")
            ser.flush()

        hw_board = ser
        _board_initialized = True
        _hw_started_with_board = True

        print(
            "[green][System] Успешное подключение к плате эмуляции (COM6)! "
            "/ Successfully connected to emulation board![/green]"
        )

        return hw_board

    except Exception as e:
        try:
            if ser is not None:
                ser.close()
        except Exception:
            pass

        hw_board = None

        # ВАЖНО:
        # _board_initialized здесь НЕ ставим в True.
        # Следующий вызов get_hw_board() снова попробует открыть COM.
        _board_initialized = False

        if _hw_started_with_board:
            logger.error(
                f"Плата эмуляции недоступна ({e}). "
                "Ожидание восстановления COM-порта..."
            )
            return None

        # Плата не была доступна уже при первом запуске —
        # сохраняем старое поведение: работаем через pyautogui.
        print(
            "[bold yellow][Warning] Не удалось открыть порт платы (COM6). "
            "Используется pyautogui / Board port not found. "
            "Switching to pyautogui.[/bold yellow]"
        )

        _board_initialized = True
        return None


def is_hw_active() -> bool:
    """Проверяет, подключена ли и готова ли к работе плата эмуляции."""
    global hw_board
    if hw_board is not None and getattr(hw_board, "is_open", False):
        return True
    if _hw_started_with_board:
        # Плата была при старте и пропала: не переключаемся молча на pyautogui.
        _emergency_release_and_exit(
            "Плата эмуляции (COM6) отключилась во время работы. Аварийная остановка бота. / "
            "Emulation board (COM6) disconnected during operation. Emergency stop."
        )
    return False


_HW_MAX_ERRORS = 10
HID_PREFIXES = ("PRESS_", "RELEASE_", "CLICK_", "PRESS:", "RELEASE:")


def _com_worker():
    """Background worker for the friction-brake scroll queue only."""
    board = get_hw_board()
    consecutive_errors = 0
    # HID-команда, которую пакет SCROLL встретил по пути. Раньше её возвращали в КОНЕЦ очереди
    # (cmd_queue.put), из-за чего PRESS/RELEASE мыши и Shift менялись местами и отставали
    # за потоком SCROLL от фрикциона (шкала силы заброса «зависала» с зажатой мышью).
    # Теперь она отправляется следующей, строго в исходном порядке.
    pending = None

    while True:
        try:
            if pending is not None:
                cmd_str, pending = pending, None
            else:
                cmd_str = cmd_queue.get()
            if cmd_str is None:
                break

            if not (board and getattr(board, "is_open", False)):
                board = get_hw_board()
                if board is None:
                    continue

            batch = [cmd_str]

            # HID-команды PRESS/RELEASE/CLICK отправляем отдельно.
            # SCROLL-команды можно объединять в пакет.
            if cmd_str.startswith(HID_PREFIXES):
                batch_size = 0
                if HW_TRACE:
                    logger.info("[HW] sending %s", cmd_str)
            else:
                batch_size = 63

            for _ in range(batch_size):
                try:
                    extra = cmd_queue.get_nowait()
                except Empty:
                    break

                if extra is None:
                    cmd_queue.put(None)  # сигнал остановки обработаем после отправки пакета
                    break

                # Не смешиваем HID-команды с пакетами SCROLL.
                if extra.startswith(HID_PREFIXES):
                    pending = extra
                    break

                batch.append(extra)

            payload = b"".join(
                f"{cmd}\n".encode("ascii", errors="ignore")
                for cmd in batch
            )

            try:
                t_write = time()
                with com_lock:
                    board.write(payload)
                    board.flush()
                spent = time() - t_write
                if spent > 0.25:
                    logger.warning(
                        "[HW] Запись на плату заняла %.2f с (%d команд: %s)",
                        spent, len(batch), batch[0],
                    )

            except (serial.SerialTimeoutException, serial.SerialException, OSError) as err:
                # Без переподключений: бот работает либо с платой, либо без неё.
                _emergency_release_and_exit(
                    f"Потеряно соединение с платой во время отправки: {err}. Аварийная остановка бота. / "
                    f"Connection to the board lost while sending: {err}. Emergency stop."
                )
                return

            for cmd in batch:
                logger.debug(f"[HW] Sent: {cmd}")

        except (EOFError, BrokenPipeError, ValueError):
            # Очередь закрыта (процесс завершается): продолжать бессмысленно,
            # иначе поток бесконечно пишет пустую ошибку в лог.
            logger.debug("[HW] Очередь команд закрыта, worker остановлен.")
            break

        except Exception as err:
            consecutive_errors += 1
            logger.error(
                f"Ошибка HW worker: {err!r}. "
                f"Пробуем продолжить работу ({consecutive_errors}/{_HW_MAX_ERRORS})."
            )
            if consecutive_errors >= _HW_MAX_ERRORS:
                _emergency_release_and_exit(
                    "HW worker многократно завершался с ошибкой. Аварийная остановка. / "
                    "HW worker failed repeatedly. Emergency stop."
                )
                return
            sleep(min(1.0 * consecutive_errors, 5.0))
            continue

        consecutive_errors = 0


def start_hw_worker():
    """Запуск обработчика очереди команд (вызывать один раз в main.py)."""
    global _hw_worker_started
    if not _hw_worker_started:
        _hw_worker_started = True
        t = threading.Thread(target=_com_worker, daemon=True)
        t.start()


def hw_send_command(cmd_str: str) -> None:
    """Помещает команду в очередь для последовательной отправки на COM6."""
    cmd_str = str(cmd_str)
    if HW_TRACE and cmd_str.startswith(HID_PREFIXES):
        try:
            backlog = cmd_queue.qsize()
        except (NotImplementedError, OSError):
            backlog = -1
        logger.info("[HW] queue <- %s (в очереди: %d)", cmd_str, backlog)
    cmd_queue.put(cmd_str)


def map_key_to_circuitpython(key: str) -> str:
    """Приведение имен клавиш к формату Keycode CircuitPython."""
    k = fix_cyrillic_key(key).upper()
    mapping = {
        'SPACE': 'SPACEBAR',
        'ENTER': 'ENTER',
        'RETURN': 'ENTER',
        'ESC': 'ESCAPE',
        'ESCAPE': 'ESCAPE',
        'BACKSPACE': 'BACKSPACE',
        'TAB': 'TAB',
        'SHIFT': 'SHIFT',
        'CTRL': 'CONTROL',
        'ALT': 'ALT',
        'UP': 'UP_ARROW',
        'DOWN': 'DOWN_ARROW',
        'LEFT': 'LEFT_ARROW',
        'RIGHT': 'RIGHT_ARROW',
        '1': 'ONE',
        '2': 'TWO',
        '3': 'THREE',
        '4': 'FOUR',
        '5': 'FIVE',
        '6': 'SIX',
        '7': 'SEVEN',
        '8': 'EIGHT',
        '9': 'NINE',
        '0': 'ZERO',
    }
    return mapping.get(k, k)


# ---------------------------------------------------------------------------- #
#                Улучшенная поведенческая модель и SessionManager                 #
# ---------------------------------------------------------------------------- #

def human_reaction_time(mean_sec: float = 0.35, std_dev: float = 0.10) -> float:
    delay = random.gauss(mean_sec, std_dev)
    return max(0.15, min(delay, 0.70))


def human_delay(mean_ms: float = 200.0, sigma: float = 0.25) -> float:
    fatigue = session_manager.get_fatigue_factor()
    adjusted_mean = mean_ms * fatigue

    mu = np.log(adjusted_mean) - (sigma ** 2) / 2
    delay_sec = np.random.lognormal(mu, sigma) / 1000.0
    return max(0.05, min(delay_sec, 3.5))


def add_jitter(time_sec: float, jitter_percent: float = 0.17) -> float:
    # Гауссово распределение вместо палевного uniform
    sigma = time_sec * jitter_percent / 3.0 
    jittered_time = random.gauss(time_sec, sigma)
    return max(0.05, jittered_time)
    
    
def sleep_with_jitter(duration_sec: float, jitter_percent: float = 0.07) -> None:
    sleep(add_jitter(duration_sec, jitter_percent=jitter_percent))


class SessionManager:
    """Продвинутое управление поведением бота."""

    def __init__(self):
        self.start_time = time()
        self.action_counter = 0
        
        # ИСТИННОЕ РЕШЕНИЕ:
        # Меняем False на True. 
        # Это навсегда заблокирует вызов случайных 30-120 секундных AFK пауз 
        # прямо посреди заброса, вываживания или подсачивания рыбы.
        self.is_busy = True

        self.next_micro_afk = random.randint(15, 30)
        self.next_medium_afk = random.randint(60, 110)
        self.next_long_afk_time = time() + random.uniform(2700, 4500)

    def get_fatigue_factor(self) -> float:
        elapsed_sec = time() - self.start_time
        elapsed_hours = elapsed_sec / 3600.0

        base_fatigue = 1.0 + min(0.35, elapsed_hours * 0.0875)
        wave = math.sin(elapsed_sec / 170.0) * 0.06
        noise = random.uniform(-0.03, 0.04)

        return max(0.92, base_fatigue + wave + noise)

    def register_action(self) -> None:
        """Только накапливает счетчик действий.

        Реальные паузы SessionManager запускаются только через
        check_for_afk() в безопасной точке основного цикла.
        """
        self.action_counter += 1

    def check_for_afk(self) -> None:
        """Проверить, пора ли сделать паузу.

        Вызывать только в безопасной точке между fishing cycles.
        Во время поклевки/вываживания/подсака этот метод не вызывается.
        """
        if self.is_busy:
            return
            
        current_time = time()

        # Длинная пауза имеет приоритет.
        if current_time >= self.next_long_afk_time:
            self._trigger_long_afk()
            self.next_long_afk_time = (
                current_time + random.uniform(2700, 4500)
            )
            self.action_counter = 0
            return

        # Средняя пауза.
        if self.action_counter >= self.next_medium_afk:
            self._trigger_medium_afk()
            self.action_counter = 0
            self.next_medium_afk = random.randint(60, 110)
            self.next_micro_afk = random.randint(15, 30)
            return

        # Короткая пауза.
        if self.action_counter >= self.next_micro_afk:
            self._trigger_micro_afk()
            self.action_counter = 0
            self.next_micro_afk = random.randint(15, 30)

    def _trigger_micro_afk(self) -> None:
        duration = random.uniform(0.5, 2.5)
        logger.info(
            "[dim]Micro-delay: %.2fs[/dim]",
            duration,
        )
        sleep(duration)

    def _trigger_medium_afk(self) -> None:
        duration = random.uniform(10.0, 30.0)
        logger.info(
            "[yellow]Medium break: %.1fs[/yellow]",
            duration,
        )
        sleep(duration)

    def _trigger_long_afk(self) -> None:
        duration = random.uniform(30.0, 120.0)
        logger.info(
            "[bold yellow]LONG AFK: %.1fm[/bold yellow]",
            duration / 60.0,
        )
        sleep(duration)


session_manager = SessionManager()

LOOP_DELAY = 1
ANIMATION_DELAY = 0.5
JITTER_SCALE = 0.07

random.seed(datetime.datetime.now().timestamp())


def setup_logging() -> logging.Logger:
    os.makedirs("logs", exist_ok=True)

    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "iso-8601-simple": {
                "format": "%(message)s",
                "datefmt": "%Y-%m-%dT%H:%M:%S%z",
            },
            "iso-8601-detailed": {
                "format": "%(asctime)s [%(levelname)s] %(message)s",
                "datefmt": "%Y-%m-%dT%H:%M:%S%z",
            },
        },
        "handlers": {
            "stdout": {
                "level": "INFO",
                "formatter": "iso-8601-simple",
                "()": "rich.logging.RichHandler",
                "rich_tracebacks": True,
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "INFO",
                "formatter": "iso-8601-detailed",
                "filename": "logs/app.log",
                "maxBytes": 10000,
                "backupCount": 0,
            },
        },
        "loggers": {"root": {"level": "INFO", "handlers": ["stdout", "file"]}},
    }
    logging.config.dictConfig(config=logging_config)
    from rf4s.controller.log_ru import RuLogFilter  # noqa: PLC0415

    ru_filter = RuLogFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(ru_filter)  # русские тексты журнала при русском лаунчере
    return logging.getLogger(__name__)


# ---------------------------------------------------------------------------- #
#                               Common Utilities                               #
# ---------------------------------------------------------------------------- #
def ask_for_confirmation(msg: str = "Ready to start") -> None:
    while True:
        ans = input(f"{msg}? [Y/n] ").strip().lower()
        if ans in ("y", ""):
            break
        if ans == "n":
            sys.exit()


def get_box_center_integers(box) -> tuple[int, int]:
    """Точный расчет центра без накопления погрешности округления."""
    if not box:
        return 0, 0
    left = getattr(box, 'left', box[0])
    top = getattr(box, 'top', box[1])
    width = getattr(box, 'width', box[2])
    height = getattr(box, 'height', box[3])
    
    return int(left + width / 2.0), int(top + height / 2.0)


# ---------------------------------------------------------------------------- #
#                                  Decorators                                  #
# ---------------------------------------------------------------------------- #
import ctypes

# DirectInput / hardware scan-codes для клавиш QWERTY
WIN_SCANCODES = {
    '1': 0x02, '2': 0x03, '3': 0x04, '4': 0x05, '5': 0x06,
    '6': 0x07, '7': 0x08, '8': 0x09, '9': 0x0A, '0': 0x0B,
    'q': 0x10, 'w': 0x11, 'e': 0x12, 'r': 0x13, 't': 0x14,
    'y': 0x15, 'u': 0x16, 'i': 0x17, 'o': 0x18, 'p': 0x19,
    'a': 0x1E, 's': 0x1F, 'd': 0x20, 'f': 0x21, 'g': 0x22,
    'h': 0x23, 'j': 0x24, 'k': 0x25, 'l': 0x26, 'z': 0x2C,
    'x': 0x2D, 'c': 0x2E, 'v': 0x2F, 'b': 0x30, 'n': 0x31, 'm': 0x32,
    'space': 0x39, 'esc': 0x01, 'enter': 0x1C, 'tab': 0x0F,
    'shift': 0x2A, 'ctrl': 0x1D, 'alt': 0x38
}


def _win_direct_key(scan_code: int, down: bool = True) -> None:
    """Отправляет физический скан-код в Windows, игнорируя раскладку."""
    flags = 0x0008  # KEYEVENTF_SCANCODE
    if not down:
        flags |= 0x0002  # KEYEVENTF_KEYUP
    ctypes.windll.user32.keybd_event(0, scan_code, flags, 0)


def generate_non_linear_steps(total_distance: int, num_steps: int) -> list[int]:
    """Генерирует список нелинейных шагов движения для имитации движений мыши."""
    if num_steps <= 0:
        return [total_distance]

    factors = [math.sin(math.pi * (i + 1) / num_steps) for i in range(num_steps)]
    sum_factors = sum(factors)

    if sum_factors == 0:
        return [int(total_distance / num_steps)] * num_steps

    steps = [int(round(total_distance * f / sum_factors)) for f in factors]
    diff = total_distance - sum(steps)
    if steps:
        steps[-1] += diff

    return steps


def toggle_right_mouse_button(func):
    def wrapper(*args, **kwargs):
        mouse_down("right")
        try:
            func(*args, **kwargs)
        finally:
            mouse_up("right")

    return wrapper


def press_before_and_after(key):
    def func_wrapper(func):
        def args_wrapper(*args, **kwargs):
            press(key)
            sleep(human_delay(mean_ms=500))
            try:
                func(*args, **kwargs)
            finally:
                press(key)
                sleep(human_delay(mean_ms=500))

        return args_wrapper

    return func_wrapper


def press(key: str) -> None:
    """Зажатие и отпускание одной клавиши с человеческими задержками и рандомом."""
    clean_key = fix_cyrillic_key(key).lower()
    hold_time = human_delay(mean_ms=120.0, sigma=0.22)

    if is_hw_active():
        cp_key = map_key_to_circuitpython(clean_key)
        hw_send_command(f"PRESS:{cp_key}")
        sleep(hold_time)
        hw_send_command(f"RELEASE:{cp_key}")
    else:
        key_down(clean_key)
        sleep(hold_time)
        key_up(clean_key)

    sleep(human_delay(mean_ms=80.0, sigma=0.22))
    session_manager.register_action()


def key_down(key: str) -> None:
    """Зажатие клавиши."""
    clean_key = fix_cyrillic_key(key).lower()
    if is_hw_active():
        cp_key = map_key_to_circuitpython(clean_key)
        hw_send_command(f"PRESS:{cp_key}")
    else:
        scan_code = WIN_SCANCODES.get(clean_key)
        if scan_code:
            _win_direct_key(scan_code, down=True)
        else:
            pag.keyDown(clean_key)


def key_up(key: str) -> None:
    """Отпускание клавиши."""
    clean_key = fix_cyrillic_key(key).lower()
    if is_hw_active():
        cp_key = map_key_to_circuitpython(clean_key)
        hw_send_command(f"RELEASE:{cp_key}")
    else:
        scan_code = WIN_SCANCODES.get(clean_key)
        if scan_code:
            _win_direct_key(scan_code, down=False)
        else:
            pag.keyUp(clean_key)


def hw_keep_holding(key: str) -> None:
    """Повторный импульс зажатия клавиши."""
    clean_key = fix_cyrillic_key(key).lower()
    if is_hw_active():
        cp_key = map_key_to_circuitpython(clean_key)
        hw_send_command(f"PRESS:{cp_key}")
    else:
        key_down(clean_key)


def hw_scroll(clicks: int) -> None:
    """Человекоподобный скролл колёсика с разбиением на микро-шаги."""
    amount = int(clicks * 120)
    if amount == 0:
        return

    if not is_hw_active():
        pag.scroll(amount)
        session_manager.register_action()
        return

    total_clicks = int(clicks)
    direction = 1 if total_clicks > 0 else -1
    remaining = abs(total_clicks)

    # Имитируем движение пальца: крутим колесико порциями по 1–3 щелчка
    while remaining > 0:
        chunk = min(remaining, random.choices([1, 2, 3, 4, 5], weights=[10, 30, 35, 20, 5])[0])
        step_clicks = chunk * direction
        delta = step_clicks * 120
        
        hw_send_command(f"SCROLL:{delta}")
        remaining -= chunk
        
        if remaining > 0:
            # Живая плавная пауза между микро-толчками (25–60 мс)
            sleep(random.uniform(0.025, 0.06))

    sleep(human_delay(mean_ms=100.0, sigma=0.22))
    session_manager.register_action()
        
        
def hw_scroll_queued(clicks: int, target_queue=None) -> None:
    """Положить человекоподобно разбитые SCROLL-команды в multiprocessing.Queue."""
    q = target_queue if target_queue is not None else cmd_queue
    total_clicks = int(clicks)
    
    if total_clicks == 0:
        return

    if not is_hw_active() and target_queue is None:
        pag.scroll(total_clicks * 120)
        return

    direction = 1 if total_clicks > 0 else -1
    remaining = abs(total_clicks)

    # Разбиваем большой скролл на мелкие порции прямо при генерации в очередь,
    # чтобы плата отрабатывала их с естественной дискретностью.
    while remaining > 0:
        chunk = min(remaining, random.choices([1, 2, 3, 4, 5], weights=[10, 30, 35, 20, 5])[0])
        step_clicks = chunk * direction
        delta = step_clicks * 120
        
        q.put(f"SCROLL:{delta}")
        remaining -= chunk
        
        # Добавляем задержку, чтобы worker не собирал команды в один пакет
        sleep(random.uniform(0.025, 0.06))
        

def scroll(amount: int) -> None:
    """Универсальный человекоподобный скролл (PyAutoGUI / Плата)."""
    amount = int(amount)
    if amount == 0:
        return

    if not is_hw_active():
        pag.scroll(amount)
        session_manager.register_action()
        return

    clicks = int(amount / 120)
    if clicks == 0:
        clicks = 1 if amount > 0 else -1

    direction = 1 if clicks > 0 else -1
    remaining = abs(clicks)

    # Имитируем движение пальца: крутим колесико небольшими порциями (по 1–3 щелчка)
    while remaining > 0:
        chunk = min(remaining, random.choices([1, 2, 3, 4, 5], weights=[10, 30, 35, 20, 5])[0])
        step = chunk * direction
        
        hw_send_command(f"SCROLL:{step}")
        remaining -= chunk
        
        if remaining > 0:
            # Живая плавающая пауза между микро-толчками колесика (25–60 мс)
            sleep(random.uniform(0.025, 0.06))

    # Небольшая пауза после завершения всего скролла для естественности
    sleep(human_delay(mean_ms=100.0, sigma=0.22))
    session_manager.register_action()



# ---------------------------------------------------------------------------- #
#          Плавное управление мышью с использованием Кривых Безье              #
# ---------------------------------------------------------------------------- #

def hw_move_relative(
    dx: int,
    dy: int,
    register_session: bool = True,
):
    """Точное относительное перемещение мыши через RP2040."""
    if dx == 0 and dy == 0:
        return

    if is_hw_active():
        _write_hw_direct(f"MOVE:{int(dx)}:{int(dy)}")
    else:
        pag.moveRel(int(dx), int(dy))

    if register_session:
        session_manager.register_action()


def hw_move_mouse_bezier(
    target_dx: int,
    target_dy: int,
    steps: int = 12,
    register_session: bool = True,
):
    """Перемещает мышь по дугообразной кривой Безье от ТЕКУЩЕГО положения."""

    if target_dx == 0 and target_dy == 0:
        return

    # Без платы — обычный PyAutoGUI.
    if not is_hw_active():
        dist = math.hypot(target_dx, target_dy)

        if dist < 10:
            steps = max(3, int(dist))
        else:
            steps = max(6, int(dist / 10))

        deviation_scale = random.uniform(0.1, 0.25)
        side_sign = random.choice([-1, 1])

        control_x = (
            target_dx * random.uniform(0.3, 0.7)
            + side_sign * target_dy * deviation_scale
        )
        control_y = (
            target_dy * random.uniform(0.3, 0.7)
            - side_sign * target_dx * deviation_scale
        )

        last_x, last_y = 0.0, 0.0

        for i in range(1, steps + 1):
            t = i / steps
            t_eased = (1 - math.cos(t * math.pi)) / 2.0

            curr_x = (
                (1 - t_eased) ** 2 * 0
                + 2 * (1 - t_eased) * t_eased * control_x
                + t_eased**2 * target_dx
            )

            curr_y = (
                (1 - t_eased) ** 2 * 0
                + 2 * (1 - t_eased) * t_eased * control_y
                + t_eased**2 * target_dy
            )

            step_x = int(round(curr_x - last_x))
            step_y = int(round(curr_y - last_y))

            if step_x != 0 or step_y != 0:
                pag.moveRel(step_x, step_y)
                last_x += step_x
                last_y += step_y

            sleep(random.uniform(0.012, 0.022))

    # С платой — Безье рассчитывается здесь,
    # а RP2040 получает только точные относительные MOVE-команды.
    else:
        dist = math.hypot(target_dx, target_dy)

        if dist < 10:
            steps = max(3, int(dist))
        else:
            steps = max(6, int(dist / 10))

        deviation_scale = random.uniform(0.1, 0.25)
        side_sign = random.choice([-1, 1])

        control_x = (
            target_dx * random.uniform(0.3, 0.7)
            + side_sign * target_dy * deviation_scale
        )
        control_y = (
            target_dy * random.uniform(0.3, 0.7)
            - side_sign * target_dx * deviation_scale
        )

        last_x, last_y = 0.0, 0.0

        for i in range(1, steps + 1):
            t = i / steps
            t_eased = (1 - math.cos(t * math.pi)) / 2.0

            curr_x = (
                2 * (1 - t_eased) * t_eased * control_x
                + t_eased**2 * target_dx
            )

            curr_y = (
                2 * (1 - t_eased) * t_eased * control_y
                + t_eased**2 * target_dy
            )

            step_x = int(round(curr_x - last_x))
            step_y = int(round(curr_y - last_y))

            if step_x != 0 or step_y != 0:
                hw_move_relative(
                    step_x,
                    step_y,
                    register_session=False,
                )
                last_x += step_x
                last_y += step_y

            sleep(random.uniform(0.003, 0.007))

    if register_session:
        session_manager.register_action()


def move_to(target_x, target_y=None):
    """Абсолютное наведение: PyAutoGUI без платы, closed-loop через плату с шумом."""

    if isinstance(target_x, (list, tuple)):
        target_x, target_y = target_x[0], target_x[1]

    target_x = int(target_x)
    target_y = int(target_y)

    if not is_hw_active():
        pag.moveTo(target_x, target_y)
        return

    deadline = time() + 1.2

    while time() < deadline:
        cur_x, cur_y = pag.position()

        dx = target_x - cur_x
        dy = target_y - cur_y

        if abs(dx) <= 1 and abs(dy) <= 1:
            session_manager.register_action()
            return

        step_x = int(dx / 1.4)
        step_y = int(dy / 1.4)

        # ЗАЩИТА: Добавляем пространственный шум, чтобы искривить линию
        if abs(dx) > 5:
            step_x += random.randint(-3, 3)
        if abs(dy) > 5:
            step_y += random.randint(-3, 3)

        if step_x == 0 and dx != 0:
            step_x = 1 if dx > 0 else -1

        if step_y == 0 and dy != 0:
            step_y = 1 if dy > 0 else -1

        step_x = max(-30, min(30, step_x))
        step_y = max(-30, min(30, step_y))

        # Ошибка записи/отсутствие платы -> аварийный выход внутри _write_hw_direct.
        # Тихо пропускать движение нельзя: следующий клик попал бы не туда.
        if not _write_hw_direct(f"MOVE:{step_x}:{step_y}"):
            _emergency_release_and_exit(
                "Плата эмуляции недоступна при движении мыши. Аварийная остановка бота. / "
                "Emulation board unavailable during mouse move. Emergency stop."
            )
            return

        # ЗАЩИТА: Рандомизация интервалов поллинга (от 35 до 65 мс)
        sleep(random.uniform(0.035, 0.065))

    logger.warning(
        "Hardware mouse move timed out: target=(%d,%d), current=%s",
        target_x,
        target_y,
        pag.position(),
    )
    session_manager.register_action()


def hw_move_mouse_random(max_dx: int = 3, max_dy: int = 3):
    # Границы -- величины сдвига; знак не важен. Без abs() отрицательное
    # значение даёт randint(-n, n) с пустым диапазоном и ValueError.
    max_dx, max_dy = abs(max_dx), abs(max_dy)
    dx = random.randint(-max_dx, max_dx)
    dy = random.randint(-max_dy, max_dy)

    if dx == 0 and dy == 0:
        return

    if is_hw_active():
        # Отправляем напрямую, чтобы не забивать cmd_queue фоновыми потоками
        _write_hw_direct(f"MOVE:{dx}:{dy}")
    else:
        ctypes.windll.user32.mouse_event(0x0001, dx, dy, 0, 0)


def hw_click_left(register_session: bool = True):
    """Человекоподобный клик ЛКМ с гауссовым распределением времени удержания."""
    # Гауссово распределение: среднее ~85 мс, стандартное отклонение 25 мс, минимум 40 мс
    hold_time = max(0.04, random.gauss(0.085, 0.025))

    if is_hw_active():
        hw_send_command("PRESS_LEFT")
        sleep(hold_time)
        hw_send_command("RELEASE_LEFT")
    else:
        pag.mouseDown(button="left")
        sleep(hold_time)
        pag.mouseUp(button="left")

    # Естественная микро-пауза после клика с учетом коэффициента усталости сессии
    sleep(human_delay(mean_ms=70.0, sigma=0.2))

    if register_session:
        session_manager.register_action()


def hw_press_mouse_left():
    if is_hw_active():
        hw_send_command("PRESS_LEFT")
    else:
        pag.mouseDown(button="left")


def hw_release_mouse_left():
    if is_hw_active():
        hw_send_command("RELEASE_LEFT")
    else:
        pag.mouseUp(button="left")


# ---------------------------------------------------------------------------- #
#                                  Алиасы ввода                                #
# ---------------------------------------------------------------------------- #

def mouse_down(button: str = "left") -> None:
    if is_hw_active():
        if button == "left":
            hw_press_mouse_left()
        elif button == "right":
            hw_send_command("PRESS_RIGHT")
    else:
        pag.mouseDown(button=button)


def mouse_up(button: str = "left") -> None:
    if is_hw_active():
        if button == "left":
            hw_release_mouse_left()
        elif button == "right":
            hw_send_command("RELEASE_RIGHT")
            session_manager.register_action()
    else:
        pag.mouseUp(button=button)
        session_manager.register_action()


def click(button: str = "left") -> None:
    """Человекоподобный клик с гауссовым распределением времени удержания."""
    # Среднее время удержания ~85 мс, стандартное отклонение 25 мс
    hold_sec = max(0.04, random.gauss(0.085, 0.025))
    
    if is_hw_active():
        btn_str = button.upper()
        hw_send_command(f"PRESS_{btn_str}")
        sleep(hold_sec)
        hw_send_command(f"RELEASE_{btn_str}")
    else:
        pag.mouseDown(button=button)
        sleep(hold_sec)
        pag.mouseUp(button=button)
        
    # Пост-кликовая пауза с учетом общей модели усталости
    sleep(human_delay(mean_ms=80.0, sigma=0.22))
    session_manager.register_action()


def hw_hold_left_with_jitter(duration_sec: float):
    """Удерживать ЛКМ заданное время без микропауз внутри удержания."""
    hw_press_mouse_left()

    try:
        end_time = time() + add_jitter(duration_sec)

        while time() < end_time:
            if random.random() < 0.05:
                hw_move_mouse_random(max_dx=2, max_dy=2)

            remaining = end_time - time()
            if remaining <= 0:
                break

            sleep(min(random.uniform(0.15, 0.35), remaining))

    finally:
        # RELEASE_LEFT ставится в очередь ДО любых дополнительных задержек.
        hw_release_mouse_left()


def is_compiled():
    return "__compiled__" in globals()


def is_run_by_clicking():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    process_array = (ctypes.c_uint * 1)()
    num_processes = kernel32.GetConsoleProcessList(process_array, 1)
    return is_compiled() and num_processes == 2


def print_logo_box(logo: str) -> None:
    print(Panel.fit(logo, box=box.HEAVY, style="bright_white"))


def print_header_box(logo: str) -> None:
    print(Panel.fit(logo, style="bright_white"))


def print_usage_box(msg: str) -> None:
    print(Panel.fit(msg, style="steel_blue1"))


def print_description_box(msg: str) -> None:
    print(Panel.fit(f"You're now using: {msg}"))


def print_hint_box(msg: str) -> None:
    print(Panel.fit(f"Hint: {msg}", style="green"))


def print_error(msg: str) -> None:
    console.print(msg, style="red")


def check_quit(s: str) -> None:
    if s.lower() == "q":
        goodbye()


def goodbye():
    print("Bye.")
    sys.exit()


def safe_exit():
    if is_run_by_clicking():
        print_usage_box("Press any key to quit.")
        msvcrt.getch()

    if is_hw_active():
        # Очередь обрабатывает демонический поток, который может не успеть
        # отработать до sys.exit(): сбрасываем очередь (чтобы запоздавший PRESS
        # не пришёл после отпускания) и пишем RELEASE_ALL в порт напрямую.
        try:
            _clear_hw_command_queue()
            _write_hw_direct("RELEASE_ALL")
        except Exception:
            pass

    # Используем нашу кастомную функцию key_up, чтобы снять системные флаги DirectInput (ctypes)
    for k in ["w", "a", "s", "d", "t", "shift", "ctrl", "alt", "space", "enter", "esc"]:
        try:
            key_up(k)
        except Exception:
            pass
            
    try:
        pag.mouseUp(button="left")
        pag.mouseUp(button="right")
    except Exception:
        pass
        
    sys.exit()