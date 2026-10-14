"""Логика запуска RF4S (entry point).

Исторически это был main.py в корне проекта; теперь это compiled-модуль
rf4s._main_core, а в корне остаётся тонкая заглушка main.py (см. build/entrypoints),
которая только донастраивает sys.path и зовёт run().
"""

"""Entry point for Russian Fishing 4 Script.

Parse command line arguments and config and start running the bot.
"""

import sys
import os
import time
import hashlib
from pathlib import Path

# === Проверка целостности main.py ===========================================
# main.py остаётся обычным текстовым файлом (пересборка app_gui.exe под
# PyInstaller+PyQt6 на машине сборки стабильно крашится -- BEX64/
# STATUS_STACK_BUFFER_OVERRUN в Qt6Core.dll, независимо от hidden-imports/
# exclude-module/UPX/версии PyQt6 -- а без пересборки лаунчер не умеет
# запускать main.exe). Вместо компиляции main.py в exe целостность этого
# файла проверяется здесь -- внутри уже скомпилированного (Cython) и
# обфусцированного rf4s._main_core.pyd, САМЫМ ПЕРВЫМ действием при его
# импорте, строго до import rf4s.licensing ниже.
#
# Закрываемая дыра: раньше было достаточно открыть main.py в блокноте и
# ДОПИСАТЬ код, который до `from rf4s._main_core import run` подменяет
# rf4s.licensing в sys.modules на фиктивный модуль с always-true
# require_license -- тогда настоящий rf4s.licensing никогда не
# импортировался бы ни тут, ни в BotApp.__init__ (оба берут его из одного и
# того же sys.modules, импорт -- кэширующая операция). Любая правка main.py
# меняет его байты -> не совпадает с хэшем, зашитым сюда при сборке (см.
# build.py: compile_rf4s() патчит _EXPECTED_MAIN_PY_SHA256 ДО cythonize) ->
# процесс завершается через os._exit(), а не sys.exit() -- sys.exit тоже
# можно было бы подменить кодом в main.py до этого импорта; os._exit()
# обрывает процесс на уровне ОС, в обход любых python-перехватчиков,
# раньше, чем код атакующего из main.py вообще успеет на что-то повлиять.
_EXPECTED_MAIN_PY_SHA256 = "0" * 64  # заменяется build.py при каждой сборке


def _resolve_main_py_path() -> Path:
    """Путь к main.py -- тому самому файлу, которым был запущен процесс."""
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 and argv0.name == "main.py" and argv0.exists():
        return argv0.resolve()
    # Запасной путь (например, запуск из исходников с другим argv[0]):
    # rf4s/_main_core.py -> на уровень выше -- корень проекта, где main.py.
    return Path(__file__).resolve().parents[1] / "main.py"


def _verify_main_py_integrity() -> None:
    if getattr(sys, "frozen", False):
        # Запущено как main.exe (--protect-main, см. build_main_exe() в
        # build.py): main.py сознательно НЕ едет текстом рядом (поэтому его
        # и не найти на диске -- это не ошибка), а значит открыть его в
        # блокноте и подменить rf4s.licensing до импорта попросту нельзя --
        # сама компиляция в exe уже даёт защиту, ради которой существует эта
        # хэш-проверка. Проверять тут нечего, пропускаем без лишнего шума.
        return
    if _EXPECTED_MAIN_PY_SHA256 == "0" * 64:
        # Плейсхолдер не заменён -- сборка без build.py (--skip-compile,
        # запуск из исходников для разработки) или build.py ещё не патчил
        # его. Не блокируем разработку, но явно предупреждаем, чтобы
        # случайно не отгрузить клиенту сборку без реальной проверки.
        print(
            "[integrity] ВНИМАНИЕ: проверка целостности main.py отключена "
            "(сборка без build.py / --skip-compile).",
            file=sys.stderr,
        )
        return
    main_py = _resolve_main_py_path()
    try:
        data = main_py.read_bytes()
    except OSError as err:
        print(f"[integrity] main.py не найден или не читается: {err}", file=sys.stderr)
        sys.stderr.flush()
        sys.stdout.flush()
        time.sleep(0.2)
        os._exit(1)
        return
    actual = hashlib.sha256(data).hexdigest()
    if actual != _EXPECTED_MAIN_PY_SHA256:
        print(
            "[integrity] main.py был изменён -- запуск заблокирован. / "
            "main.py has been modified -- startup blocked.",
            file=sys.stderr,
        )
        # os._exit() не делает обычную python-очистку (и это специально --
        # его нельзя подменить кодом из main.py, в отличие от sys.exit()),
        # а значит и буферизованный stderr/stdout сам по себе не долился бы
        # до конца при сносе процесса -- flush() решает только половину
        # проблемы. Лаунчер (rf4s/_launcher_core.py, execute_script())
        # читает вывод процесса по одному символу циклом
        # `while self.process.poll() is None: char = stdout.read(1)` --
        # т.е. ПРОВЕРЯЕТ, жив ли процесс, ДО каждого чтения, а не читает до
        # EOF. os._exit() убивает процесс настолько мгновенно, что этот
        # цикл иногда успевает прочитать только часть уже сброшенных байт:
        # на следующей итерации poll() уже видит, что процесс умер, и цикл
        # выходит, даже если в пайпе ещё остались непрочитанные байты --
        # отсюда обрезанная надпись в логе лаунчера. Раз сам лаунчер не
        # трогаем (отдельная история с крашем Qt при его пересборке),
        # обходим это здесь: небольшая пауза ПОСЛЕ flush(), но ДО
        # os._exit() -- она не даёт никакой лазейки атакующему (решение
        # завершиться уже принято, этот код не читает ничего из main.py
        # повторно и ничего не ждёт извне), а лаунчеру тем временем хватает
        # времени вычитать весь уже записанный в пайп текст, пока poll()
        # ещё видит процесс как "живой".
        sys.stderr.flush()
        sys.stdout.flush()
        time.sleep(0.2)
        os._exit(1)


# (проверка целостности main.py убрана: бот открытый, лицензию проверяет лаунчер)
# === конец проверки целостности =============================================

import argparse
import shlex
from packaging.version import Version
import re

import rich_argparse
from rich import box, print
from rich.prompt import Prompt
from rich.table import Table
from yacs.config import CfgNode as CN

from rf4s import config, utils
from rf4s.utils import start_hw_worker
from rf4s.app import (
    BotApp,
    CalculateApp,
    CraftApp,
    FrictionBrakeApp,
    HarvestApp,
    MoveApp,
)

VERSION = "0.11.0"
MINIMUM_COMPATIBLE_CONFIG_VERSION = "0.8.0"
LOGO = """
██████╗ ███████╗██╗  ██╗███████╗
██╔══██╗██╔════╝██║  ██║██╔════╝
██████╔╝█████╗  ███████║███████╗
██╔══██╗██╔══╝  ╚════██║╚════██║
██║  ██║██║          ██║███████║
╚═╝  ╚═╝╚═╝          ╚═╝╚══════╝"""

# https://patorjk.com/software/taag/#p=testall&f=3D-ASCII&t=RF4S%0A, ANSI Shadow

FEATURES = (
    {"name": "Fishing Bot / Бот для рыбалки", "command": "bot"},
    {"name": "Craft Items", "command": "craft"},
    {"name": "Move Forward", "command": "move"},
    {"name": "Harvest Baits", "command": "harvest"},
    {"name": "Auto Friction Brake", "command": "frictionbrake"},
    {"name": "Calculate Tackle's Stats", "command": "calculate"},
)

BOT_BOOLEAN_ARGUMENTS = (
    ("t", "tag", "keep only tagged fishes / сохранять только незачетную рыбу"),
    ("c", "coffee", "drink coffee if stamina is low during fish fight / пить кофе при низкой энергии"),
    ("a", "alcohol", "drink alcohol before keeping the fish / пить алкоголь перед сохранением рыбы"),
    ("r", "refill", "consume tea and carrot if hunger or comfort is low / пить чай и есть морковь"),
    ("H", "harvest", "harvest baits before casting the rod / копать наживку перед забросом"),
    ("L", "lure", "change current lure with a random favorite one, mode: spin / менять приманку на случайную из Избранного"),
    ("m", "mouse", "move mouse randomly before casting the rod / случайное движение мышью перед забросом"),
    ("P", "pause", "pause the script before casting the rod occasionally / случайная пауза перед забросом"),
    ("RC", "random-cast", "do a redundant rod cast randomly / случайный лишний перезаброс"),
    ("SC", "skip-cast", "skip the first rod cast / пропустить первый заброс"),
    ("l", "lift", "lift the tackle constantly during a fish fight / подтягивать удочку при вываживании"),
    ("e", "electro", "enable electric mode for Electro Raptor series reel / режим электрокатушки"),
    ("FB", "friction-brake", "adjust friction brake automatically / авто-регулировка фрикциона"),
    ("GR", "gear-ratio", "switch the gear ratio or mode after the retrieval timed out / переключение передаточного числа"),
    ("b", "bite", "save a screenshot in screenshots/ when a fish bite / скриншот при поклевке"),
    ("s", "screenshot", "save a screenshot in screenshots/ after you caught a fish / скриншот после поимки"),
    ("d", "data", "save fishing data in /logs / сохранять логи ловли"),
    ("E", "email", "send email notification after the script stop / Email уведомление при остановке"),
    ("M", "miaotixing", "send miaotixing notification after the script stop / MiaoTixing уведомление"),
    ("D", "discord", "send Discord notification after the script stop / Discord уведомление"),
    ("TG", "telegram", "send Telegram notification after the script stop / Telegram уведомление"),
    ("S", "shutdown", "shutdown computer after the script stop / выключить ПК после остановки"),
    ("SO", "signout", "sign out instead of closing the game / выйти из профиля вместо закрытия игры"),
    ("BL", "broken-lure", "replace broken lures with favorite ones / заменять сломанные приманки"),
    ("SR", "spod-rod", "recast spod rod / перезабрасывать сподовое удилище"),
    ("DM", "dry-mix", "enable dry mix refill, mode: bottom / пополнять сухой прикорм"),
    ("GB", "groundbait", "enable groundbait refill, mode: bottom / пополнять стандартный прикорм"),
    ("PVA", "pva", "enable pva refill, mode: bottom / пополнять ПВА-пакеты"),
)

EPILOG = """
Docs: https://github.com/dereklee0310/RussianFishing4Script/tree/main/docs/en
"""

# Этот модуль лежит в rf4s/_main_core.py -- на уровень глубже, чем раньше лежал
# сам main.py, поэтому до корня проекта (где config.yaml, static/, rf4s/) теперь
# два родителя, а не один.
INNER_ROOT = Path(__file__).resolve().parents[1]
if utils.is_compiled():
    OUTER_ROOT = Path(sys.executable).parent
else:
    OUTER_ROOT = INNER_ROOT


class Formatter(
    rich_argparse.RawTextRichHelpFormatter, argparse.RawDescriptionHelpFormatter
):
    pass


(OUTER_ROOT / "screenshots").mkdir(parents=True, exist_ok=True)
(OUTER_ROOT / "logs").mkdir(parents=True, exist_ok=True)
logger = utils.setup_logging()


def setup_parser(cfg: CN) -> tuple[argparse.ArgumentParser, tuple]:
    """Configure the argument parser with all supported command-line options."""
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument("opts", nargs="*", help="overwrite configuration")

    main_parser = argparse.ArgumentParser(epilog=EPILOG, formatter_class=Formatter)
    main_parser.add_argument(
        "-V", "--version", action="version", version=f"RF4S {VERSION}"
    )

    feature_parsers = main_parser.add_subparsers(title="features", dest="feature")

    bot_parser = feature_parsers.add_parser(
        "bot",
        help="start fishing bot / запустить бота для рыбалки",
        parents=[parent_parser],
        formatter_class=Formatter,
    )
    bot_parser.add_argument(
        "-V", "--version", action="version", version=f"RF4S-bot {VERSION}"
    )

    for argument in BOT_BOOLEAN_ARGUMENTS:
        flag1 = f"-{argument[0]}"
        flag2 = f"--{argument[1]}"
        help_message = argument[2]
        bot_parser.add_argument(flag1, flag2, action="store_true", help=help_message)

    profile_strategy = bot_parser.add_mutually_exclusive_group()

    def pid(_pid: str) -> int:
        return int(_pid)

    profile_strategy.add_argument(
        "-p",
        "--pid",
        type=pid,
        choices=range(len(cfg.PROFILE)),
        help="specify the id of the profile to use / указать ID профиля",
        metavar=f"{{0-{len(cfg.PROFILE) - 1}}}",
    )

    def pname(_pname: str) -> str:
        if _pname not in cfg.PROFILE:
            raise ValueError
        return _pname

    profile_strategy.add_argument(
        "-N",
        "--pname",
        type=pname,
        help="specify the name of the profile to use / указать имя профиля",
        metavar="{profile name}",
    )

    def num_fish(_num_fish: str) -> int:
        return int(_num_fish)

    bot_parser.add_argument(
        "-n",
        "--fishes-in-keepnet",
        default=0,
        type=num_fish,
        choices=range(cfg.BOT.KEEPNET.CAPACITY),
        help="specify the number of fishes in your keepnet / кол-во рыбы в садке (default: %(default)s)",
        metavar=f"{{0-{cfg.BOT.KEEPNET.CAPACITY - 1}}}",
    )
    bot_parser.add_argument(
        "-T",
        "--trolling",
        nargs="?",
        const="forward",
        default=None,
        type=str,
        choices=["forward", "left", "right", "route"],
        help=(
            "enable trolling mode and specify direction, or 'route' to follow a recorded "
            "route (see --troll-water) / режим троллинга, 'route' - по записанному маршруту\n"
            "(default: %(default)s, no argument: %(const)s)"
        ),
    )
    bot_parser.add_argument(
        "--troll-water",
        default=None,
        type=str,
        metavar="NAME",
        help="route name from static/trolling/NAME.json for '-T route' / маршрут для режима -T route",
    )
    bot_parser.add_argument(
        "--troll-nav",
        default="steer",
        choices=["observe", "resume", "steer", "sync", "full"],
        help="route navigation by compass coordinates: observe = only log the drift, "
        "resume = after a pause re-align the route clock with the boat's position, "
        "steer = resume + course corrections (default), sync = resume + continuous clock sync, full = all / "
        "навигация по координатам: observe - только журнал, resume - после паузы сверять часы "
        "маршрута с позицией лодки, steer - плюс подруливание (по умолчанию), sync - плюс постоянная "
        "сверка часов, full - всё вместе",
    )
    bot_parser.add_argument(
        "--troll-no-cast",
        action="store_true",
        help="don't cast before departure (rod already cast and in the holder) / не забрасывать перед стартом",
    )
    bot_parser.add_argument(
        "-R",
        "--rainbow",
        nargs="?",
        const=5,
        default=None,
        type=int,
        choices=[0, 5],
        help=(
            "enable rainbow line mode / режим радужной лески\n"
            "(default: %(default)s, no argument: %(const)s)"
        ),
    )

    bot_parser.add_argument(
        "-BT",
        "--boat-ticket",
        nargs="?",
        const=5,
        default=0,
        type=int,
        choices=[0, 1, 2, 3, 5],
        help=(
            "enable boat ticket renewal / автопродление билета на лодку\n"
            "(default: %(default)s, no argument: %(const)s)"
        ),
    )

    craft_parser = feature_parsers.add_parser(
        "craft", help="craft items / крафт предметов", parents=[parent_parser], formatter_class=Formatter
    )
    craft_parser.add_argument(
        "-V", "--version", action="version", version=f"RF4S-craft {VERSION}"
    )
    craft_parser.add_argument(
        "-d",
        "--discard",
        action="store_true",
        help="discard all crafted items / выбрасывать скрафченное",
    )
    craft_parser.add_argument(
        "-i",
        "--ignore",
        action="store_true",
        help="ignore unselected material slots / игнорировать пустые слоты",
    )
    craft_parser.add_argument(
        "-n",
        "--craft-limit",
        type=int,
        default=-1,
        help="specify items count to craft / количество предметов для крафта (default: %(default)s)",
        metavar="{number of items}",
    )

    move_parser = feature_parsers.add_parser(
        "move",
        help="toggle moving forward / зажать движение вперед",
        parents=[parent_parser],
        formatter_class=Formatter,
    )
    move_parser.add_argument(
        "-V", "--version", action="version", version=f"RF4S-move {VERSION}"
    )
    move_parser.add_argument(
        "-s",
        "--shift",
        action="store_true",
        help="Hold down Shift / бег с зажатым Shift",
    )

    harvest_parser = feature_parsers.add_parser(
        "harvest",
        help="harvest baits / добыча наживки",
        parents=[parent_parser],
        formatter_class=Formatter,
    )
    harvest_parser.add_argument(
        "-V", "--version", action="version", version=f"RF4S-harvest {VERSION}"
    )
    harvest_parser.add_argument(
        "-r",
        "--refill",
        action="store_true",
        help="consume tea and carrot / пить чай и есть морковь",
    )

    friction_brake_parser = feature_parsers.add_parser(
        "frictionbrake",
        help="automate friction brake / авто-фрикцион",
        aliases=["fb"],
        parents=[parent_parser],
        formatter_class=Formatter,
    )
    friction_brake_parser.add_argument(
        "-V", "--version", action="version", version=f"RF4S-frictionbrake {VERSION}"
    )

    calculate_paser = feature_parsers.add_parser(
        "calculate",
        help="calculate tackle's stats / расчет характеристик снасти",
        aliases=["cal"],
        parents=[parent_parser],
        formatter_class=Formatter,
    )
    calculate_paser.add_argument(
        "-V", "--version", action="version", version=f"RF4S-calculate {VERSION}"
    )

    troll_calib_parser = feature_parsers.add_parser(
        "trollcalib",
        help="learn compass digits for trolling / калибровка цифр компаса",
        parents=[parent_parser],
        formatter_class=Formatter,
    )

    troll_record_parser = feature_parsers.add_parser(
        "trollrecord",
        help="record a trolling route while you sail manually / записать маршрут троллинга",
        parents=[parent_parser],
        formatter_class=Formatter,
    )
    troll_record_parser.add_argument(
        "--water",
        default=None,
        type=str,
        metavar="NAME",
        help="route file name (latin letters, digits, - and _) / имя файла маршрута",
    )
    troll_record_parser.add_argument(
        "--title",
        default="",
        type=str,
        help="water body title shown in the launcher / название водоема",
    )
    troll_record_parser.add_argument(
        "--start-text",
        default="",
        type=str,
        help="start coordinates note, e.g. 477:320 / заметка о стартовых координатах",
    )

    return main_parser, (
        bot_parser,
        craft_parser,
        move_parser,
        harvest_parser,
        friction_brake_parser,
        calculate_paser,
    )


def display_features() -> None:
    """Display a table of available features for user selection."""
    table = Table(
        "Features / Функции",
        title="List of Available Features / Список доступных функций",
        show_header=False,
        box=box.HEAVY,
        min_width=42,
    )

    for i, feature in enumerate(FEATURES):
        table.add_row(f"{i:>2}. {feature['name']}")
    print(table)


def get_fid(parser: argparse.ArgumentParser) -> int:
    """Prompt the user to enter a feature ID and validate the input."""
    while True:
        user_input = Prompt.ask(
            "Enter Feature ID / Введите ID функции (q - quit/выход, h - help/справка)",
            show_default=False,
        )

        utils.check_quit(user_input)

        if user_input.lower() == "h":
            parser.print_help()
            continue

        if user_input.isdigit() and 0 <= int(user_input) < len(FEATURES):
            return int(user_input)
        utils.print_error(
            f"Invalid input / Неверный ввод. Введите число от 0 до {len(FEATURES) - 1}."
        )


def get_launch_options(parser: argparse.ArgumentParser) -> str:
    """Prompt the user for optional launch arguments."""
    while True:
        user_input = Prompt.ask(
            "Enter launch options / Параметры запуска (Enter - пропустить, h - помощь, q - выход)",
            default="",
            show_default=False,
        )

        utils.check_quit(user_input)

        if user_input.lower() == "h":
            parser.print_help()
            continue

        return user_input


def get_language() -> str:
    """Prompt the user to select their game language."""
    print("\n[1/3] Язык игры / Game Language")
    print("  Выберите язык, установленный в вашем клиенте игры Русская Рыбалка 4.")
    print("  Select the language configured in your Russian Fishing 4 game client.")
    user_input = Prompt.ask(
        "Выбор / Select: (1) English, (2) Русский (q - quit/выход)",
        choices=["1", "2", "q"],
        show_choices=False,
    )
    utils.check_quit(user_input)
    return "en" if user_input == "1" else "ru"


def get_click_lock() -> str:
    """Prompt the user to confirm Windows Mouse ClickLock status."""
    print("\n[2/3] Залипание клавиш мыши / Windows Mouse ClickLock")
    print("  Включена ли функция 'Залипание клавиш мыши' в настройках Windows?")
    print("  (По умолчанию в Windows эта функция ВЫКЛЮЧЕНА — выбирайте вариант 2)")
    print("  Is 'Mouse ClickLock' enabled in your Windows system settings?")
    print("  (By default in Windows it is DISABLED — select option 2)")
    user_input = Prompt.ask(
        "Выбор / Select: (1) Да / Yes, (2) Нет / No (q - quit/выход)",
        choices=["1", "2", "q"],
        show_choices=False,
    )
    utils.check_quit(user_input)
    return "true" if user_input == "1" else "false"


def get_catch_screen_style() -> str:
    """Prompt the user to select their in-game catch screen style."""
    print("\n[3/3] Стиль экрана улова / In-game Catch Screen Style")
    print("  Укажите стиль карточки улова из настроек вашей игры.")
    print("  Рекомендуется 'Простой' (Simple) для защиты от багов и ускорения анимаций улова.")
    print("  Select the catch screen style configured in your game settings.")
    print("  'Simple' is highly recommended to reduce waiting time for trophy animations.")

    user_input = Prompt.ask(
        "Выбор / Select: (1) Анимированный / Animated, (2) Простой / Simple (q - quit/выход)",
        choices=["1", "2", "q"],
        show_choices=False,
    )
    utils.check_quit(user_input)
    return "animated" if user_input == "1" else "simple"


def setup_cfg():
    config_path = OUTER_ROOT / "config.yaml"

    if not config_path.exists():
        utils.print_header_box(
            "Добро пожаловать в Мастер настройки RF4S / Welcome to the RF4S Configuration Wizard.\n"
            "Ответьте на 3 вопроса для генерации файла конфигурации / Answer 3 questions to generate config."
        )

        language = get_language()
        click_lock = get_click_lock()
        catch_screen_style = get_catch_screen_style()

        with open(
            INNER_ROOT / "rf4s/config/config.yaml", "r", encoding="utf-8"
        ) as file:
            content = file.read()

        # Update language, click lock, and catch screen style using regex
        content = re.sub(
            r"^(LANGUAGE:\s*).*$", rf'\1"{language}"', content, flags=re.MULTILINE
        )
        content = re.sub(
            r"^(  CLICK_LOCK:\s*).*$", rf"\1{click_lock}", content, flags=re.MULTILINE
        )
        content = re.sub(
            r"^(  CATCH_SCREEN_STYLE:\s*).*$",
            rf'\1"{catch_screen_style}"',
            content,
            flags=re.MULTILINE,
        )

        with open(config_path, "w", encoding="utf-8") as file:
            file.write(content)
        print("\n[green]Конфигурационный файл успешно сгенерирован! / Configuration file generated successfully.[/green]\n")
        # Раньше после генерации config.yaml мастер сразу продолжал запуск
        # бота -- если игра ещё не открыта (а при самом первом запуске она
        # почти никогда не открыта), это гарантированно падало с "Окно игры
        # не найдено" сразу после того, как человек только что ответил на 3
        # вопроса. Останавливаемся здесь: просим открыть игру и запустить
        # бота ещё раз, вместо немедленной (и заведомо обречённой) попытки.
        utils.print_header_box(
            "Готово! Теперь откройте игру 'Русская Рыбалка 4' (оконный или "
            "безрамочный режим) и запустите бота ещё раз.\n"
            "Done! Now start 'Russian Fishing 4' (windowed or borderless mode) "
            "and run the bot again."
        )
        sys.exit(0)

    cfg = config.load_cfg()

    if Version(cfg.VERSION) < Version(MINIMUM_COMPATIBLE_CONFIG_VERSION):
        utils.print_error(
            f"Config version {cfg.VERSION} is incompatible with RF4S {VERSION}.\n"
            f"Minimum required config version: {MINIMUM_COMPATIBLE_CONFIG_VERSION}\n"
            "Версия вашего config.yaml устарела. Пожалуйста, удалите config.yaml и запустите скрипт заново."
        )
        sys.exit()

    return cfg


def _make_quiet_print(orig_print):
    """Обёртка print для калькулятора: прячет сообщения о плате эмуляции,
    иначе они вклиниваются в строку вопроса."""

    def _quiet_print(*args, **kwargs):
        if args and isinstance(args[0], str) and ("плате эмуляции" in args[0] or "порт платы" in args[0]):
            return
        orig_print(*args, **kwargs)

    return _quiet_print


def main() -> None:
    start_hw_worker()

    # utils.print_logo_box(LOGO)
    cfg = setup_cfg()
    parser, subparsers = setup_parser(cfg)
    args = parser.parse_args()

    if args.feature == "trollcalib":
        from rf4s.controller import compass

        compass.calibrate_cli(args)
        return

    if args.feature == "trollrecord":
        from rf4s.controller import trolling

        trolling.record_cli(args)
        return

    if args.feature is None:
        display_features()
        fid = get_fid(parser)
        sys.argv = [sys.argv[0]] + [FEATURES[fid]["command"]] + sys.argv[1:]
        sys.argv += shlex.split(get_launch_options(subparsers[fid]))
        args = parser.parse_args()

    start, end = sys.argv[:2], sys.argv[2:]
    match args.feature:
        case "bot":
            sys.argv = start + shlex.split(cfg.BOT.LAUNCH_OPTIONS) + end
            App = BotApp
        case "craft":
            sys.argv = start + shlex.split(cfg.CRAFT.LAUNCH_OPTIONS) + end
            App = CraftApp
        case "move":
            sys.argv = start + shlex.split(cfg.MOVE.LAUNCH_OPTIONS) + end
            App = MoveApp
        case "harvest":
            sys.argv = start + shlex.split(cfg.HARVEST.LAUNCH_OPTIONS) + end
            App = HarvestApp
        case "frictionbrake" | "fb":
            sys.argv = start + shlex.split(cfg.FRICTION_BRAKE.LAUNCH_OPTIONS) + end
            App = FrictionBrakeApp
        case "calculate" | "cal":
            # Калькулятору плата эмуляции не нужна: сообщения о ней не выводим
            # (иначе они вклиниваются в строку вопроса). Функция вынесена на
            # уровень модуля -- Cython не умеет компилировать вложенную def
            # внутри `case` (ошибка C2065 на _quiet_print при сборке .pyd).
            utils.print = _make_quiet_print(utils.print)
            App = CalculateApp
        case _:
            raise NotImplementedError("You should not reach here.")
            
    # Отсутствие окна игры обрабатывает Window._get_game_hwnd() (двуязычное
    # сообщение + safe_exit). Любые другие ошибки не маскируем: их полный
    # текст и трассировку выводит обработчик в __main__.
    App(cfg, parser.parse_args(), parser).start()


def run() -> None:
    """Точка входа, вызывается тонкой заглушкой main.py в корне проекта."""
    try:
        main()
    except Exception as e:
        logger.critical(e, exc_info=True)
        utils.safe_exit()