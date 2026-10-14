"""Helper functions for image detection."""

import colorsys
import time
from enum import Enum
from functools import lru_cache, partial
from pathlib import Path
from typing import Generator, NamedTuple
from rf4s.component.logger import logger

import cv2
import numpy as np
import pyautogui as pag
import pyscreeze
from PIL import Image, ImageFilter
from pyscreeze import Box

# Весь код в этом файле (и все его вызывающие в player.py и т.д.) написан в
# расчёте, что "картинка не найдена" -- это None, а не исключение: везде
# паттерн "box = self._get_image_box(...); if not box: ...", без единого
# try/except вокруг отдельных вызовов locateOnScreen/_get_image_box. Но
# свежие версии pyscreeze по умолчанию (USE_IMAGE_NOT_FOUND_EXCEPTION = True)
# вместо None бросают pyscreeze.ImageNotFoundException -- и это валит бота
# целиком на первом же "не найдено" (например get_quit_position() в
# player.start_fishing(), когда диалог выхода просто не показан на экране --
# самый обычный случай). Возвращаем прежнее, ожидаемое всем остальным кодом
# поведение: не найдено -- None, без исключения.
pyscreeze.USE_IMAGE_NOT_FOUND_EXCEPTION = False

# cv2/numpy импортированы выше безусловно (и используются по всему файлу без
# проверки на None -- HUD-шаблоны, шкала натяжения и т.д.), так что отдельный
# try/except ImportError здесь был бы мёртвым кодом: если бы cv2 реально не
# было, модуль уже упал бы на импорте строкой выше, а "cv2 is None" в
# _find_hud_template никогда не могло бы стать True по этой причине.

from rf4s import utils
from rf4s.controller.window import Window
from rf4s.utils import add_jitter

CRITICAL_COLOR = (206, 56, 21)
WARNING_COLOR = (227, 149, 23)

MIN_GRAY_SCALE_LEVEL = 160
YELLOW_FRICTION_BRAKE = (200, 214, 63)
ORANGE_FRICTION_BRAKE = (229, 188, 0)
RED_FRICTION_BRAKE = (206, 56, 21)
COLOR_TOLERANCE = 32
CAMERA_OFFSET = 40
SIDE_LENGTH = 160
SIDE_LENGTH_HALF = 80
ORANGE_REEL = (227, 149, 23)

# Шкала натяжения: заливка растёт от центра экрана в обе стороны.
# На шкале есть разделители на 50% и 75% полудлины. Зона определяется тем,
# докуда дошла заливка:
#   не дошла до первого разделителя (< 50%)    -> зелёная,
#   между первым и вторым разделителем (50-75%) -> жёлтая/оранжевая,
#   за вторым разделителем (> 75%)              -> красная.
# Проверяется только наличие заливки (цветной пиксель на сером фоне шкалы),
# поэтому результат не зависит от того, как именно окрашена заливка.
BAR_MIN_SATURATION = 0.35  # ниже -- серая дорожка шкалы или разделитель
BAR_MIN_VALUE = 0.40
BAR_HALF_WIDTH = 424  # полудлина шкалы в пикселях; HUD не масштабируется с разрешением
BAR_GREEN_FRACTION = 0.15  # GREEN_FROM: с этой доли считается, что рыба тянет
BAR_AMBER_FRACTION = 0.55  # AMBER_FROM: сразу за первым разделителем (50%)
BAR_RED_FRACTION = 0.78    # сразу за вторым разделителем (75%, около 319 px): начало КРАСНОЙ зоны
# SENSITIVITY -> доля шкалы, с которой бот начинает СБРОС фрикциона (точка сброса,
# "overload"). Это отдельная от красной зоны точка: красная зона начинается с
# BAR_RED_FRACTION, а сброс -- с этой доли (те же позиции, что friction_brake_*
# в COORD_OFFSETS). При "high" они почти рядом (78% и 80%).
SENSITIVITY_FRACTIONS = {"very_high": 0.70, "high": 0.80, "medium": 0.90, "low": 0.95}
REEL_BURNING_TOLERANCE = 10
ZONE_NONE, ZONE_GREEN, ZONE_AMBER, ZONE_RED, ZONE_OVERLOAD = 0, 1, 2, 3, 4
ZONE_NAMES = ("none", "green", "amber", "red", "overload")

# --- Локальный поиск шаблонов wheel / 0m / 5m (см. Detection._find_hud_template) ---
HUD_TEMPLATES = ("wheel", "0m", "5m")  # wheel -- иконка шпули, 0m/5m -- счётчик метров
HUD_REGION_SLACK = 12  # запас вокруг шаблона, px (сдвиг окна, округление координат)
HUD_FALLBACK_HALF = 48  # половина стороны области для прежнего поиска pyautogui, px
HUD_FALLBACK_MIN_SCORE = 0.50  # ниже -- запасной метод не запускается (иконки там нет)
HUD_CORE_MARGIN = 0.20  # запасной метод сравнивает центр шаблона (без кольца и фона)
HUD_GRAY_DELTA, HUD_GRAY_FLOOR = 0.12, 0.80  # порог яркости: confidence - delta, не ниже floor
HUD_EDGE_DELTA, HUD_EDGE_FLOOR = 0.28, 0.60  # порог контуров: confidence - delta, не ниже floor
HUD_RIVAL_MARGIN = 0.03  # 0m не принимается, если 5m подходит не хуже (и наоборот)
HUD_RIVALS = {"0m": "5m", "5m": "0m"}


def _hud_edges(gray):
    """Модуль градиента: не зависит от цвета и общей яркости, только от контуров."""
    f = gray.astype(np.float32)
    return cv2.magnitude(
        cv2.Sobel(f, cv2.CV_32F, 1, 0, ksize=3),
        cv2.Sobel(f, cv2.CV_32F, 0, 1, ksize=3),
    )


class TensionState(NamedTuple):
    """Состояние HUD за один скриншот (см. Detection.get_tension_state)."""

    hooked: bool  # иконка рыбы горит
    zone: int  # ZONE_NONE / GREEN / AMBER / RED по границе заливки
    fill_px: int  # на сколько пикселей от центра дошла заливка (0 -- пусто)
    snagged: bool  # иконка зацепа (то же условие, что is_line_snagged)
    line_end: bool  # иконка шпули (то же условие, что is_line_at_end)


@lru_cache(maxsize=8192)  # на шкале всего несколько десятков разных цветов
def classify_bar_pixel(pixel: tuple) -> int:
    """Вернуть зону шкалы натяжения по цвету пикселя."""
    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in pixel[:3]))
    if s < BAR_MIN_SATURATION or v < BAR_MIN_VALUE:
        return ZONE_NONE
    deg = h * 360
    if deg < 25 or deg >= 340:
        return ZONE_RED
    if deg < 62:
        return ZONE_AMBER
    if deg < 110:
        return ZONE_GREEN
    return ZONE_NONE


ROOT = Path(__file__).resolve().parents[2]


def bar_zone_from_pixels(pairs) -> int:
    """Зона по парам пикселей (слева, справа) в точках green, amber, red, overload.

    red      -- заливка в красной зоне, но точка сброса ещё не достигнута;
    overload -- заливка дошла до точки сброса (SENSITIVITY / RED_FROM).
    """
    # Точка закрашена, только если слева и справа один и тот же цвет шкалы:
    # это отсекает яркие объекты сцены, просвечивающие сквозь дорожку.
    def lit(pair) -> bool:
        left, right = (classify_bar_pixel(p) for p in pair)
        return left != ZONE_NONE and left == right

    # Проверяем от дальней точки к ближней и останавливаемся на первой.
    zones = (ZONE_GREEN, ZONE_AMBER, ZONE_RED, ZONE_OVERLOAD)
    for zone, pair in reversed(list(zip(zones, pairs))):
        if lit(pair):
            return zone
    return ZONE_NONE


class TagColor(Enum):
    GREEN = "green_tag"
    YELLOW = "yellow_tag"
    PINK = "pink_tag"
    BLUE = "blue_tag"
    PURPLE = "purple_tag"


COORD_OFFSETS = {
    "1600x900": {
        "friction_brake_very_high": (502, 872),
        "friction_brake_high": (459, 872),
        "friction_brake_medium": (417, 872),
        "friction_brake_low": (396, 872),
        "fish_icon": (389, 844),
        "clip_icon": (1042, 844),
        "spool_icon": (1077, 844),
        "meter_icon": (1009, 844),
        "reel_burning_icon": (1112, 842),
        "snag_icon": (1147, 829),
        "float_camera": (720, 654),
        "bait_icon": (35, 31),
    },
    "1920x1080": {
        "friction_brake_very_high": (662, 1052),
        "friction_brake_high": (619, 1052),
        "friction_brake_medium": (577, 1052),
        "friction_brake_low": (556, 1052),
        "fish_icon": (549, 1024),
        "clip_icon": (1202, 1024),
        "spool_icon": (1237, 1024),
        "meter_icon": (1169, 1024),
        "reel_burning_icon": (1271, 1023),
        "snag_icon": (1307, 1009),
        "float_camera": (880, 834),
        "bait_icon": (35, 31),
    },
    "2560x1440": {
        "friction_brake_very_high": (982, 1412),
        "friction_brake_high": (939, 1412),
        "friction_brake_medium": (897, 1412),
        "friction_brake_low": (876, 1412),
        "fish_icon": (869, 1384),
        "clip_icon": (1522, 1384),
        "spool_icon": (1557, 1384),
        "meter_icon": (1489, 1384),
        "reel_burning_icon": (1593, 1383),
        "snag_icon": (1627, 1369),
        "float_camera": (1200, 1194),
        "bait_icon": (35, 31),
    },
}

# --- Поиск wheel / 0m / 5m (is_retrieval_finished) --------------------------
# Зоны заданы относительно spool_icon_coord (dx, dy, ширина, высота). Эта
# координата уже содержит положение окна (box[0], box[1]). HUD не масштабируется
# с разрешением, поэтому смещения одинаковы для всех поддерживаемых размеров.
# wheel: значок шпули (около 30 px) с запасом ±10 px.
WHEEL_ZONE = (-28, -28, 56, 56)
# 0m / 5m: надпись 000/005 в кружке метража (около 17x8 px). Кружок стоит
# либо рядом со шпулей (-35 px, значка клипсы нет), либо левее значка клипсы
# (-70 px). Зона накрывает оба положения.
METER_ZONE = (-88, -16, 72, 32)
# Запасной проход (серое + лёгкое размытие): порог на ROBUST_CONF_DROP ниже
# основного, но не ниже ROBUST_CONF_FLOOR. По логам он поднимает score чужих
# цифр (до 0.875 при пороге 0.90), поэтому по умолчанию выключен: включайте,
# только если по логу RETRIEVAL_DEBUG видно, что настоящая иконка не проходит
# цветовой проход.
ROBUST_FALLBACK = False
ROBUST_CONF_DROP = 0.05
ROBUST_CONF_FLOOR = 0.90
# --- Защита wheel (обычная леска) от ложных срабатываний -------------------------
# wheel -- кольцо с рисунком внутри, а на HUD рядом такие же кольца (шпуля, клипса,
# фрикцион). Нормированная корреляция по всему шаблону даёт им высокий балл, потому что
# одинаковое кольцо перевешивает отличающийся рисунок внутри. Поэтому помимо основного
# балла проверяется ещё и центр иконки (без кольца), и совпадение должно продержаться
# несколько опросов подряд на одном месте. У радужной лески (0m/5m) цифры и так различимы,
# для неё ничего не меняется.
# Главное: wheel -- это кольцо-индикатор, оно замыкается по мере подмотки. Шаблон -- замкнутое
# кольцо, но разрыв в 3-5% дуги почти не снижает балл (0.93+), и бот считал смотку законченной,
# пока рыба ещё далеко. Поэтому кольцо проверяется по дуге: по всему кругу яркость должна
# совпадать с шаблоном, допускается лишь WHEEL_RING_MAX_GAP доли дуги.
WHEEL_RING_CHECK = True
WHEEL_RING_SAMPLES = 180      # точек на окружности
# По логам кольцо идёт ступенями 4.4% -> 3.3% -> 2.2% (2.2% -- замкнутое, шов дуги наверху).
# 0.027 принимает только 2.2%, 0.035 -- уже 3.3% (подъём на несколько секунд раньше, когда
# рыба почти у кончика), 4.4% и больше отбрасывается в обоих случаях.
WHEEL_RING_MAX_GAP = 0.036    # доля точек кольца, которые могут быть тусклыми
WHEEL_RING_BRIGHT = 0.80      # яркость точки относительно самой яркой дуги кольца, ниже -- разрыв
WHEEL_RING_MIN_LEVEL = 0.45   # яркость самой яркой дуги относительно шаблона; ниже -- кольцо пустое
WHEEL_VERIFY = True           # сверять центр иконки
WHEEL_CORE_MARGIN = 0.28      # какую долю шаблона по краям отбросить (кольцо и фон)
WHEEL_CORE_MIN = 0.75         # минимальная корреляция центра иконки
WHEEL_STRONG = 0.985          # при таком основном балле центр не проверяем
WHEEL_CONFIRM_HITS = 2        # сколько опросов подряд должны найти wheel
WHEEL_CONFIRM_WINDOW = 2.5    # с; опросы реже этого не считаются подряд
WHEEL_CONFIRM_SHIFT = 3       # px; максимальный сдвиг иконки между опросами
# Снимки спорных и принятых совпадений для разбора (screenshots/wheel_debug/).
WHEEL_SAVE_DEBUG = False
WHEEL_DEBUG_MAX_VETO = 60
WHEEL_DEBUG_PERIOD = 4.0      # с между снимками одного вида
WHEEL_DEBUG_MAX_HIT = 15
WHEEL_DEBUG_MAX_MISS = 40     # снимков «промахов» (score ниже порога) за запуск
# Замер кольца без шаблона (_wheel_ring_contrast): иконка стоит на фиксированном месте, а фон за ней
# разный (рукав в клетку, вода, небо), поэтому сравниваем не с шаблоном, а с соседними пикселями.
WHEEL_CONTRAST_MIN = 25.0     # уровни серого; ниже - кольцо на этом фоне не читается
WHEEL_CONTRAST_SEARCH = 6     # px; на сколько искать центр кольца вокруг spool_icon_coord
# Заполнение кольца по цвету заливки (_wheel_fill_gap, BOT.WHEEL_BY_FILL). Заполненная дуга кольца
# непрозрачная, белая и нейтральная (R=G=B): 254-255 на снимке бота, ~232 на скриншотах Windows.
# Незаполненная полупрозрачная и зависит от фона (105-160). Поэтому дугу ищем по цвету, а не по
# контрасту с фоном и не шаблоном. Дуга идёт по часовой стрелке от верхней точки.
WHEEL_FILL_MIN = 224          # уровень серого, с которого точка кольца считается заливкой
WHEEL_FILL_DIM_FLOOR = 155    # тусклый HUD (рассвет, сумерки): заливка кольца может быть ~180 вместо ~238, трек ~100-145
WHEEL_FILL_DIM_DROP = 22      # порог тусклой заливки = её яркость минус это значение
WHEEL_FILL_CHROMA = 6         # макс. разброс R/G/B: заливка серая, кожа, трава и небо цветные
WHEEL_FILL_BG_WHITE = 0.5     # доля светлого нейтрального фона вокруг кольца, выше - кольцо не читается
WHEEL_FILL_SEARCH = 3         # px; поиск центра кольца вокруг spool_icon_coord
WHEEL_FILL_SAMPLES = 180      # точек по окружности (шаг 2 градуса)
WHEEL_FILL_START_TOL = 8      # отсчётов; насколько начало дуги может отстоять от верхней точки
WHEEL_FILL_NOISE_MAX = 0.04   # доля лишних светлых точек вне дуги; больше - дуга не читается
# True: раз в RETRIEVAL_DEBUG_PERIOD секунд писать в лог лучший score и место.
RETRIEVAL_DEBUG = False
RETRIEVAL_DEBUG_PERIOD = 1.0


class Detection:
    def __init__(self, cfg, window: Window):
        self.cfg = cfg
        self.window = window
        self.image_dir = ROOT / "static" / cfg.LANGUAGE

        if window.is_size_supported():
            self._set_absolute_coords()
            self.is_fish_hooked = self.is_fish_hooked_pixel
        else:
            self.is_fish_hooked = partial(
                self._get_image_box,
                image="fish_icon",
                confidence=0.9,
            )

        self.bait_icon_reference_img = Image.open(self.image_dir / "bait_icon.png")

    def _get_pixel(self, x: int, y: int) -> tuple:
        """Безопасное чтение пикселя через микро-скриншот (Anti-Cheat Bypass)."""
        return pag.screenshot(region=(int(x), int(y), 1, 1)).getpixel((0, 0))[:3]

    def _get_image_box(
        self,
        image: str,
        confidence: float,
        multiple: bool = False,
        region: tuple[int, int, int, int] | None = None,
    ) -> Box | Generator[Box, None, None] | None:
        if not multiple and image in HUD_TEMPLATES:
            # wheel / 0m / 5m: сначала локальный поиск в известной области HUD.
            # NotImplemented -- его нельзя выполнить (нет координат, opencv или
            # шаблона): тогда работает прежний поиск pyautogui, но 0m/5m и тогда
            # ищутся в области вокруг счётчика, а не по всему экрану.
            box = self._find_hud_template(image, confidence)
            if box is not NotImplemented:
                return box
            region = region or self._hud_fallback_region(image)

        image_path = str(self.image_dir / f"{image}.png")

        if multiple:
            return pag.locateAllOnScreen(
                image_path,
                confidence=confidence,
                region=region,
            )

        return pag.locateOnScreen(
            image_path,
            confidence=confidence,
            region=region,
        )

    # ------------------------------------------------------------------
    # wheel / 0m / 5m: поиск в известной области HUD
    # ------------------------------------------------------------------
    def _hud_anchor(self, image: str):
        """Экранные координаты иконки (уже с учётом положения окна) или None."""
        return getattr(
            self, "spool_icon_coord" if image == "wheel" else "meter_icon_coord", None
        )

    def _hud_fallback_region(self, image: str):
        """Область для прежнего поиска pyautogui; None -- координат нет."""
        anchor = self._hud_anchor(image)
        if anchor is None:
            return None
        x, y = anchor
        half = HUD_FALLBACK_HALF
        return (int(x - half), int(y - half), 2 * half, 2 * half)

    def _hud_template(self, image: str):
        """Шаблон, один раз прочитанный с диска и подготовленный для сравнения."""
        cache = self.__dict__.setdefault("_hud_cache", {})
        if image not in cache:
            bgr = cv2.imread(str(self.image_dir / f"{image}.png"), cv2.IMREAD_COLOR)
            if bgr is None:
                cache[image] = None
            else:
                gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                h, w = gray.shape
                my = max(1, int(h * HUD_CORE_MARGIN))
                mx = max(1, int(w * HUD_CORE_MARGIN))
                clahe = self.__dict__.setdefault(
                    "_hud_clahe", cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
                )
                core = clahe.apply(gray)[my : h - my, mx : w - mx]
                cache[image] = {
                    "bgr": bgr,
                    "shape": (h, w),
                    "core_offset": (mx, my),
                    "core": core,
                    "core_edges": _hud_edges(core),
                }
        return cache[image]

    def _hud_margin(self, hay_eq, hay_edges, tpl, gray_thr, edge_thr):
        """Запас над порогами в лучшей точке: >= 0 -- подходит и по яркости, и по контурам."""
        res_g = cv2.matchTemplate(hay_eq, tpl["core"], cv2.TM_CCOEFF_NORMED)
        res_e = cv2.matchTemplate(hay_edges, tpl["core_edges"], cv2.TM_CCOEFF_NORMED)
        # На однотонных участках opencv может вернуть nan/inf: это не совпадение.
        res_g = np.nan_to_num(res_g, nan=-1.0, posinf=-1.0, neginf=-1.0)
        res_e = np.nan_to_num(res_e, nan=-1.0, posinf=-1.0, neginf=-1.0)
        margin = np.minimum(res_g - gray_thr, res_e - edge_thr)
        _, best, _, loc = cv2.minMaxLoc(margin)
        return best, loc, float(res_g[loc[1], loc[0]]), float(res_e[loc[1], loc[0]])

    def _find_hud_template(self, image: str, confidence: float):
        """Поиск wheel/0m/5m в небольшой области вокруг иконки на HUD.

        1) быстрый: тот же цветной TM_CCOEFF_NORMED и тот же confidence, что у
           pyautogui, но на области размером с шаблон плюс запас, с шаблоном из
           памяти. Что находилось раньше, находится и теперь;
        2) запасной (только если быстрый почти попал): сравнение по яркости после
           выравнивания контраста и по контурам центральной части шаблона. Он
           переживает блики, смену яркости и фона под полупрозрачным HUD.

        Возвращает Box в экранных координатах, None (иконки нет) или
        NotImplemented (поиск не выполнен, нужен прежний путь).
        """
        if cv2 is None or self.__dict__.get("_hud_disabled"):
            return NotImplemented
        anchor = self._hud_anchor(image)
        if anchor is None:
            return NotImplemented
        try:
            tpl = self._hud_template(image)
            if tpl is None:
                return NotImplemented
            th, tw = tpl["shape"]
            half_w, half_h = tw // 2 + HUD_REGION_SLACK, th // 2 + HUD_REGION_SLACK
            left = max(0, int(anchor[0] - half_w))
            top = max(0, int(anchor[1] - half_h))
            shot = pag.screenshot(region=(left, top, 2 * half_w, 2 * half_h))
            hay = cv2.cvtColor(np.asarray(shot.convert("RGB")), cv2.COLOR_RGB2BGR)
            if hay.shape[0] < th or hay.shape[1] < tw:
                return NotImplemented

            res = cv2.matchTemplate(hay, tpl["bgr"], cv2.TM_CCOEFF_NORMED)
            res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
            _, best, _, loc = cv2.minMaxLoc(res)
            if best > confidence:
                return Box(left + loc[0], top + loc[1], tw, th)
            if best < HUD_FALLBACK_MIN_SCORE:
                return None

            gray_thr = max(HUD_GRAY_FLOOR, confidence - HUD_GRAY_DELTA)
            edge_thr = max(HUD_EDGE_FLOOR, confidence - HUD_EDGE_DELTA)
            eq = self.__dict__["_hud_clahe"].apply(cv2.cvtColor(hay, cv2.COLOR_BGR2GRAY))
            edges = _hud_edges(eq)
            margin, loc, s_gray, s_edge = self._hud_margin(eq, edges, tpl, gray_thr, edge_thr)
            if margin < 0:
                return None

            rival = HUD_RIVALS.get(image)
            rival_tpl = self._hud_template(rival) if rival else None
            if rival_tpl is not None and rival_tpl["shape"] == tpl["shape"]:
                rival_margin = self._hud_margin(eq, edges, rival_tpl, gray_thr, edge_thr)[0]
                if margin < rival_margin + HUD_RIVAL_MARGIN:
                    return None  # на экране скорее соседнее значение счётчика

            mx, my = tpl["core_offset"]
            logger.info(
                "HUD template '%s' matched by fallback (gray=%.2f edge=%.2f, fast=%.2f)",
                image, s_gray, s_edge, best,
            )
            return Box(left + loc[0] - mx, top + loc[1] - my, tw, th)
        except Exception:
            logger.exception("HUD template search failed, using the old search")
            self._hud_disabled = True
            return NotImplemented

    def _set_absolute_coords(self) -> None:
        self.coord_offsets = COORD_OFFSETS[self.window.get_resolution_str()]

        for key in self.coord_offsets:
            setattr(self, f"{key}_coord", self._get_absolute_coord(key))

        self.bait_icon_coord = self._get_absolute_coord("bait_icon") + [44, 52]
        if self.cfg.ARGS.FEATURE == "bot":
            sensitivity = self.cfg.BOT.FRICTION_BRAKE.SENSITIVITY
        else:
            sensitivity = self.cfg.FRICTION_BRAKE.SENSITIVITY
        friction_brake_key = f"friction_brake_{sensitivity}"
        self.friction_brake_coord = self._get_absolute_coord(friction_brake_key)

        bases = self._get_absolute_coord("float_camera")
        if hasattr(self.cfg.PROFILE, "MODE") and self.cfg.PROFILE.MODE in (
            "telescopic",
            "bolognese",
        ):
            match self.cfg.PROFILE.CAMERA_SHAPE:
                case "tall":
                    bases[0] += CAMERA_OFFSET
                    width, height = SIDE_LENGTH_HALF, SIDE_LENGTH
                case "wide":
                    bases[1] += CAMERA_OFFSET
                    width, height = SIDE_LENGTH, SIDE_LENGTH_HALF
                case "square":
                    width, height = SIDE_LENGTH, SIDE_LENGTH
                case _:
                    raise ValueError(self.cfg.PROFILE.CAMERA_SHAPE)
            self.float_camera_rect = (*bases, width, height)

    def _get_absolute_coord(self, offset_key: str) -> list[int]:
        box = self.window.get_box()
        return [box[i] + self.coord_offsets[offset_key][i] for i in range(2)]

    def is_tag_exist(self, color: TagColor):
        return self._get_image_box(color.value, 0.95)

    def is_fish_species_matched(self, species: str):
        return self._get_image_box(species, 0.9)

    def is_fish_hooked(self):
        pass

    def is_fish_hooked_pixel(self) -> bool:
        return all(c > MIN_GRAY_SCALE_LEVEL for c in self._get_pixel(*self.fish_icon_coord))

    def is_fish_hooked_twice(self) -> bool:
        if not self.is_fish_hooked():
            return False
        time.sleep(add_jitter(self.cfg.PROFILE.HOOK_DELAY, self.cfg.BOT.JITTER_SCALE))
        if self.is_fish_hooked():
            return True
        return False

    def is_fish_captured(self):
        return self._get_image_box("keep", 0.9)

    def is_fish_in_list(self, fish_species_list: tuple | list) -> bool:
        for species in fish_species_list:
            if self.is_fish_species_matched(species):
                return True
        return False

    def _get_template(self, name: str):
        """Вернуть (цветной, серый размытый) шаблон из static/<язык>/<name>.png.

        Шаблон читается один раз и кэшируется. Чтение через np.fromfile, чтобы
        путь с кириллицей не ломал cv2.imread на Windows.
        """
        cache = getattr(self, "_template_cache", None)
        if cache is None:
            cache = self._template_cache = {}
        if name not in cache:
            path = str(self.image_dir / f"{name}.png")
            color = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if color is None:
                raise OSError(f"Cannot read template: {path}")
            gray = cv2.GaussianBlur(cv2.cvtColor(color, cv2.COLOR_BGR2GRAY), (3, 3), 0)
            cache[name] = (color, gray)
        return cache[name]

    def _find_icon_in_zone(self, names, zone, confidence) -> Box | None:
        """Найти одну из иконок names в зоне zone рядом со spool_icon_coord.

        Один скриншот зоны. Проход 1 (быстрый): цветное сопоставление с
        confidence из конфига, как у автора, но только в зоне. Проход 2
        (запасной, только при промахе): серое изображение с лёгким размытием
        и порогом чуть ниже, на том же скриншоте.
        """
        sx, sy = self.spool_icon_coord
        x0 = max(0, int(sx) + zone[0])
        y0 = max(0, int(sy) + zone[1])
        shot = pag.screenshot(region=(x0, y0, zone[2], zone[3])).convert("RGB")
        haystack = cv2.cvtColor(np.asarray(shot), cv2.COLOR_RGB2BGR)
        gray = None
        robust_conf = min(
            confidence, max(ROBUST_CONF_FLOOR, confidence - ROBUST_CONF_DROP)
        )
        scores = []

        for robust in (False, True):
            if robust and not ROBUST_FALLBACK:
                break
            if robust:
                gray = cv2.GaussianBlur(cv2.cvtColor(haystack, cv2.COLOR_BGR2GRAY), (3, 3), 0)
            threshold = robust_conf if robust else confidence
            for name in names:
                color_tpl, gray_tpl = self._get_template(name)
                needle, image = (gray_tpl, gray) if robust else (color_tpl, haystack)
                h, w = needle.shape[:2]
                if image.shape[0] < h or image.shape[1] < w:
                    continue
                result = cv2.matchTemplate(image, needle, cv2.TM_CCOEFF_NORMED)
                _, best, _, loc = cv2.minMaxLoc(result)
                scores.append((name, "gray" if robust else "color", best, loc))
                if best >= threshold and name == "wheel" and WHEEL_VERIFY:
                    if not self._wheel_core_ok(haystack, loc, best, x0, y0):
                        continue
                if best >= threshold:
                    if self._retrieval_debug():
                        logger.info(
                            "Retrieval HIT %s/%s %.3f at (%d,%d), threshold %.2f",
                            name, "gray" if robust else "color", best,
                            x0 + loc[0], y0 + loc[1], threshold,
                        )
                        if name != "wheel":
                            self._wheel_debug_save(haystack, loc, "rainhit", best, 0.0, name)
                    return Box(x0 + loc[0], y0 + loc[1], w, h)

        if self._retrieval_debug():
            now = time.monotonic()
            if now - getattr(self, "_retrieval_debug_t", 0.0) >= RETRIEVAL_DEBUG_PERIOD:
                self._retrieval_debug_t = now
                # Кольцо в лучшем месте wheel, даже если score ниже порога: в яркие моменты
                # шаблон не проходит, и без этого видно только «промах», а не заполнение дуги.
                ring_note = ""
                wheel_best = next(
                    ((b, p) for n, m, b, p in scores if n == "wheel" and m == "color"), None
                )
                if wheel_best is not None:
                    try:
                        gap = self._wheel_ring_gap(haystack, wheel_best[1])
                    except Exception:  # noqa: BLE001 - отладка не должна ломать бота
                        gap = None
                    if gap is not None:
                        ring_note = (
                            f"; ring@best gap={100 * gap:.1f}% "
                            f"level={getattr(self, '_wheel_ring_level', -1.0):.2f}"
                        )
                    self._wheel_debug_save(
                        haystack, wheel_best[1], "miss", wheel_best[0],
                        0.0 if gap is None else 1.0 - gap,
                    )
                for r_name, r_mode, r_best, r_loc in scores:
                    if r_name != "wheel" and r_mode == "color":
                        if r_best < 0.75:  # интересны только кадры рядом с порогом (5 м и ближе)
                            break
                        kind = "rainhi" if r_best >= 0.87 else "rain"  # у порога: без паузы между снимками
                        self._wheel_debug_save(haystack, r_loc, kind, r_best, 0.0, r_name)
                        break
                if "wheel" in names:
                    try:
                        res = self._wheel_ring_contrast(haystack, zone)
                    except Exception:  # noqa: BLE001
                        res = None
                    if res is not None:
                        cgap, ctop, coff = res
                        if cgap is None:
                            ring_note += f"; anchor ring: unreadable (contrast {ctop:.0f})"
                        else:
                            ring_note += (
                                f"; anchor ring: gap={100 * cgap:.1f}% contrast={ctop:.0f} "
                                f"off=({coff[0]},{coff[1]})"
                            )
                logger.info(
                    "Retrieval zone (%d,%d,%d,%d) conf=%.2f/%.2f: %s%s",
                    x0, y0, zone[2], zone[3], confidence, robust_conf,
                    "; ".join(
                        f"{n}/{m} {b:.3f} at ({x0 + p[0]},{y0 + p[1]})"
                        for n, m, b, p in scores
                    ),
                    ring_note,
                )
        return None

    def _wheel_core_score(self, haystack, loc) -> float:
        """Корреляция центра иконки с центром шаблона wheel (кольцо и фон отброшены)."""
        color_tpl, _ = self._get_template("wheel")
        h, w = color_tpl.shape[:2]
        patch = haystack[loc[1] : loc[1] + h, loc[0] : loc[0] + w]
        if patch.shape[:2] != (h, w):
            return -1.0
        my = max(2, int(h * WHEEL_CORE_MARGIN))
        mx = max(2, int(w * WHEEL_CORE_MARGIN))
        a = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)[my : h - my, mx : w - mx].astype(np.float32)
        b = cv2.cvtColor(color_tpl, cv2.COLOR_BGR2GRAY)[my : h - my, mx : w - mx].astype(np.float32)
        a -= a.mean()
        b -= b.mean()
        den = float(np.sqrt((a * a).sum() * (b * b).sum()))
        if den < 1e-6:
            return -1.0  # однотонный участок: рисунка внутри нет
        return float((a * b).sum() / den)

    def _wheel_ring_points(self):
        """Точки окружности кольца шаблона: (список (dx, dy), яркость шаблона) или None."""
        cache = self.__dict__.get("_wheel_ring_cache")
        if cache is not None:
            return cache or None
        color_tpl, _ = self._get_template("wheel")
        g = cv2.cvtColor(color_tpl, cv2.COLOR_BGR2GRAY).astype(np.float32)
        h, w = g.shape
        cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
        angles = np.linspace(0, 2 * np.pi, WHEEL_RING_SAMPLES, endpoint=False)

        def sample(img, r):
            xs = np.clip(np.rint(cx + r * np.cos(angles)).astype(int), 0, w - 1)
            ys = np.clip(np.rint(cy + r * np.sin(angles)).astype(int), 0, h - 1)
            return img[ys, xs], xs, ys

        # радиус кольца: где средняя яркость по окружности максимальна
        radii = np.arange(max(3.0, min(w, h) * 0.25), min(w, h) / 2.0, 0.5)
        best_r = max(radii, key=lambda r: float(sample(g, r)[0].mean()))
        vals, _, _ = sample(g, best_r)
        # ширина кольца может быть 2-3 px: точка считается яркой, если ярок хоть один из
        # трёх соседних радиусов; опорную яркость берём так же
        pts = [(best_r + d) for d in (-1.0, 0.0, 1.0)]
        ref = np.max([sample(g, r)[0] for r in pts], axis=0)
        if float(np.mean(ref < 150)) > 0.02 or float(ref.mean()) < 150:
            # в самом шаблоне кольцо не замкнуто/тусклое: проверка бессмысленна
            logger.warning("wheel template has no clean closed ring; ring check disabled")
            self._wheel_ring_cache = False
            return None
        self._wheel_ring_cache = (best_r, pts, ref, cx, cy, angles)
        return self._wheel_ring_cache

    def _wheel_ring_gap(self, haystack, loc):
        """Доля тусклых точек кольца найденной иконки (0 - кольцо замкнуто); None - не проверить."""
        data = self._wheel_ring_points()
        if not data:
            return None
        _, pts, ref, cx, cy, angles = data
        color_tpl, _ = self._get_template("wheel")
        h, w = color_tpl.shape[:2]
        patch = haystack[loc[1] : loc[1] + h, loc[0] : loc[0] + w]
        if patch.shape[:2] != (h, w):
            return None
        g = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.float32)
        cols = []
        for r in pts:
            xs = np.clip(np.rint(cx + r * np.cos(angles)).astype(int), 0, w - 1)
            ys = np.clip(np.rint(cy + r * np.sin(angles)).astype(int), 0, h - 1)
            cols.append(g[ys, xs])
        cur = np.max(cols, axis=0)
        # Яркость сравниваем с самой яркой частью КОЛЬЦА ЭТОЙ ЖЕ иконки, а не с шаблоном:
        # если HUD затемнён/подкрашен целиком, замкнутое кольцо остаётся замкнутым.
        top = float(np.percentile(cur, 90))
        self._wheel_ring_level = top / max(1.0, float(ref.mean()))
        if self._wheel_ring_level < WHEEL_RING_MIN_LEVEL:
            return 1.0  # белой дуги нет вовсе: кольцо пустое/не читается
        dim = cur < top * WHEEL_RING_BRIGHT
        return float(dim.mean())

    def _wheel_core_ok(self, haystack, loc, best, x0, y0) -> bool:
        """True, если у найденного wheel кольцо замкнуто и рисунок внутри совпадает."""
        if WHEEL_RING_CHECK:
            gap = self._wheel_ring_gap(haystack, loc)
            self._wheel_last_gap = gap
            if gap is not None and gap > self._wheel_max_gap():
                now_t = time.monotonic()
                if now_t - self.__dict__.get("_wheel_reject_log_t", -1e9) >= 3.0:
                    self._wheel_reject_log_t = now_t
                    logger.info(
                        "Wheel rejected: ring is not closed (%.1f%% of the arc dim, "
                        "score %.3f, ring level %.2f) at (%d,%d)",
                        100 * gap, best, getattr(self, "_wheel_ring_level", -1.0),
                        x0 + loc[0], y0 + loc[1],
                    )
                self._wheel_debug_save(haystack, loc, "veto", best, 1.0 - gap)
                return False
        if best >= WHEEL_STRONG:
            return True
        core = self._wheel_core_score(haystack, loc)
        ok = core >= WHEEL_CORE_MIN
        if not ok:
            logger.info(
                "Wheel rejected: ring matches (%.3f), centre does not (%.2f < %.2f) at (%d,%d)",
                best, core, WHEEL_CORE_MIN, x0 + loc[0], y0 + loc[1],
            )
        self._wheel_debug_save(haystack, loc, "hit" if ok else "veto", best, core)
        return ok

    def _wheel_ring_contrast(self, haystack, zone):
        """Заполнение кольца wheel без шаблона и без привязки к фону.

        Для каждого угла сравниваем пиксели кольца с пикселями чуть снаружи и чуть внутри:
        заполненная дуга светлее соседей на любом фоне (кроме белого). Возвращает
        (gap, контраст, (dx, dy)) или None; gap=None - кольцо на этом фоне не читается.
        Пока используется только для отладочного лога (поведение бота не меняет).
        """
        data = self._wheel_ring_points()
        if not data:
            return None
        r0, _pts, _ref, _cx, _cy, angles = data
        g = cv2.cvtColor(haystack, cv2.COLOR_BGR2GRAY).astype(np.float32)
        hh, ww = g.shape
        cosa, sina = np.cos(angles), np.sin(angles)
        bx, by = -zone[0], -zone[1]  # spool_icon_coord в координатах снимка зоны

        def samp(cx, cy, r):
            xs = np.rint(cx + r * cosa).astype(int)
            ys = np.rint(cy + r * sina).astype(int)
            if xs.min() < 0 or ys.min() < 0 or xs.max() >= ww or ys.max() >= hh:
                return None
            return g[ys, xs]

        best = None
        span = WHEEL_CONTRAST_SEARCH
        for dy in range(-span, span + 1):
            for dx in range(-span, span + 1):
                cx, cy = bx + dx, by + dy
                parts = [samp(cx, cy, r0 + d) for d in (-1.0, 0.0, 1.0, 3.0, 4.0, -3.0, -4.0)]
                if any(part is None for part in parts):
                    continue
                ring = np.max(parts[0:3], axis=0)
                around = np.maximum(np.maximum(parts[3], parts[4]), np.maximum(parts[5], parts[6]))
                contrast = ring - around
                # центр выбираем по среднему контрасту всей окружности: при совпадении с кольцом
                # он максимален при любой доле заполнения, а по лучшим 10% можно промахнуться
                quality = float(np.clip(contrast, 0.0, None).mean())
                if best is None or quality > best[0]:
                    best = (quality, contrast, (dx, dy))
        if best is None:
            return None
        _quality, contrast, off = best
        top = float(np.percentile(contrast, 90))
        if top < WHEEL_CONTRAST_MIN:
            return (None, top, off)
        filled = contrast >= 0.5 * top
        return (float(1.0 - filled.mean()), top, off)

    def _wheel_fill_gap(self, haystack, center):
        """Какая доля кольца wheel не заполнена: (gap, (dx, dy), число точек заливки) или None.

        gap=1.0 - заливки нет; gap=None - картинка не похожа на дугу от верхней точки (шум).
        center - центр иконки в координатах снимка haystack.
        """
        data = self._wheel_ring_points()
        r0 = data[0] if data else 13.25
        n = WHEEL_FILL_SAMPLES
        ang = np.radians(np.arange(n) * 360.0 / n)
        cosa, sina = np.cos(ang), np.sin(ang)
        hh, ww = haystack.shape[:2]
        img = haystack.astype(np.int32)  # BGR

        def fill_mask(cx, cy, thr=WHEEL_FILL_MIN):
            ok = np.zeros(n, bool)
            for d in (-1.0, 0.0, 1.0):
                xs = np.rint(cx + (r0 + d) * cosa).astype(int)
                ys = np.rint(cy + (r0 + d) * sina).astype(int)
                if xs.min() < 0 or ys.min() < 0 or xs.max() >= ww or ys.max() >= hh:
                    return None
                px = img[ys, xs]
                mx, mn = px.max(axis=1), px.min(axis=1)
                ok |= ((mx - mn) <= WHEEL_FILL_CHROMA) & (px.mean(axis=1) >= thr)
            return ok

        best = None
        for dy in range(-WHEEL_FILL_SEARCH, WHEEL_FILL_SEARCH + 1):
            for dx in range(-WHEEL_FILL_SEARCH, WHEEL_FILL_SEARCH + 1):
                ok = fill_mask(center[0] + dx, center[1] + dy)
                if ok is not None and (best is None or int(ok.sum()) > best[0]):
                    best = (int(ok.sum()), (dx, dy), ok)
        if best is None:
            return None
        thr = WHEEL_FILL_MIN
        if best[0] == 0:
            # Яркой заливки нет. Возможно, HUD тусклый (рассвет): тогда заливка ~180, а трек темнее.
            lvl = 0.0
            for dy in range(-WHEEL_FILL_SEARCH, WHEEL_FILL_SEARCH + 1):
                for dx in range(-WHEEL_FILL_SEARCH, WHEEL_FILL_SEARCH + 1):
                    xs = np.rint(center[0] + dx + r0 * cosa).astype(int)
                    ys = np.rint(center[1] + dy + r0 * sina).astype(int)
                    if xs.min() < 0 or ys.min() < 0 or xs.max() >= ww or ys.max() >= hh:
                        continue
                    px = img[ys, xs]
                    neutral = (px.max(axis=1) - px.min(axis=1)) <= WHEEL_FILL_CHROMA
                    if neutral.sum() >= n // 10:
                        lvl = max(lvl, float(np.percentile(px.mean(axis=1)[neutral], 90)))
            if WHEEL_FILL_DIM_FLOOR <= lvl < WHEEL_FILL_MIN:
                thr = lvl - WHEEL_FILL_DIM_DROP
                best = None
                for dy in range(-WHEEL_FILL_SEARCH, WHEEL_FILL_SEARCH + 1):
                    for dx in range(-WHEEL_FILL_SEARCH, WHEEL_FILL_SEARCH + 1):
                        ok = fill_mask(center[0] + dx, center[1] + dy, thr)
                        if ok is not None and (best is None or int(ok.sum()) > best[0]):
                            best = (int(ok.sum()), (dx, dy), ok)
                if best is None:
                    return None
        count, off, ok = best
        # Белый нейтральный фон вокруг кольца (белая стена, облака): заливку от фона не отличить
        bright = 0
        for d in (-4.0, 4.0):
            xs = np.clip(np.rint(center[0] + off[0] + (r0 + d) * cosa).astype(int), 0, ww - 1)
            ys = np.clip(np.rint(center[1] + off[1] + (r0 + d) * sina).astype(int), 0, hh - 1)
            px = img[ys, xs]
            bright += int((((px.max(axis=1) - px.min(axis=1)) <= WHEEL_FILL_CHROMA)
                           & (px.mean(axis=1) >= thr)).sum())
        if bright > WHEEL_FILL_BG_WHITE * 2 * n:
            return (None, off, count)
        ok = ok | (np.roll(ok, 1) & np.roll(ok, -1))  # дырки в одну точку от сглаживания
        count = int(ok.sum())
        if count == 0:
            return (1.0, off, 0)
        if count == n:
            return (0.0, off, n)
        # самая длинная серия подряд (по кругу) и её начало
        best_len, best_start, run, start = 0, 0, 0, 0
        for i in range(2 * n):
            if ok[i % n]:
                if run == 0:
                    start = i
                run += 1
                if run > best_len and run <= n:
                    best_len, best_start = run, start % n
            else:
                run = 0
        top = (n * 3) // 4  # 270 градусов: верх иконки (y растёт вниз)
        shift = min((best_start - top) % n, (top - best_start) % n)
        if shift > WHEEL_FILL_START_TOL or (count - best_len) > WHEEL_FILL_NOISE_MAX * n:
            return (None, off, count)
        return (1.0 - best_len / n, off, count)

    def _wheel_max_gap(self):
        """Допустимый зазор кольца (доля дуги): BOT.WHEEL_RING_MAX_GAP или константа по умолчанию."""
        try:
            v = float(getattr(self.cfg.BOT, "WHEEL_RING_MAX_GAP", WHEEL_RING_MAX_GAP))
        except (TypeError, ValueError, AttributeError):
            return WHEEL_RING_MAX_GAP
        return min(0.3, max(0.0, v))

    def _wheel_finished_by_fill(self):
        """Конец смотки обычной лески по заполнению кольца (BOT.WHEEL_BY_FILL); Box или None."""
        sx, sy = int(self.spool_icon_coord[0]), int(self.spool_icon_coord[1])
        x0 = max(0, sx + WHEEL_ZONE[0])
        y0 = max(0, sy + WHEEL_ZONE[1])
        shot = pag.screenshot(region=(x0, y0, WHEEL_ZONE[2], WHEEL_ZONE[3])).convert("RGB")
        haystack = cv2.cvtColor(np.asarray(shot), cv2.COLOR_RGB2BGR)
        res = self._wheel_fill_gap(haystack, (sx - x0, sy - y0))
        gap = None if res is None else res[0]
        if self._retrieval_debug():
            now = time.monotonic()
            if now - getattr(self, "_retrieval_debug_t", 0.0) >= RETRIEVAL_DEBUG_PERIOD:
                self._retrieval_debug_t = now
                if res is None:
                    logger.info("Wheel fill: no data (centre out of the zone)")
                else:
                    logger.info(
                        "Wheel fill: gap=%s, fill points %d/%d, offset (%d,%d)",
                        "unreadable" if gap is None else f"{100 * gap:.1f}%",
                        res[2], WHEEL_FILL_SAMPLES, res[1][0], res[1][1],
                    )
                    tpl, _ = self._get_template("wheel")
                    th, tw = tpl.shape[:2]
                    loc = (max(0, (sx - x0) - tw // 2), max(0, (sy - y0) - th // 2))
                    self._wheel_debug_save(
                        haystack, loc, "fill", 0.0, 0.0 if gap is None else 1.0 - gap
                    )
        if gap is None or gap > self._wheel_max_gap():
            self._wheel_confirmed(None)
            return None
        self._wheel_last_gap = gap
        return self._wheel_confirmed(Box(x0, y0, WHEEL_ZONE[2], WHEEL_ZONE[3]))

    def _retrieval_debug(self) -> bool:
        """Отладка конца смотки: BOT.RETRIEVAL_DEBUG в конфиге (или константы выше)."""
        if RETRIEVAL_DEBUG or WHEEL_SAVE_DEBUG:
            return True
        if getattr(getattr(self.cfg, "ARGS", None), "DATA", False):  # флаг -d (логи)
            return True
        return bool(getattr(getattr(self.cfg, "BOT", None), "RETRIEVAL_DEBUG", False))

    def _wheel_debug_save(
        self, haystack, loc, kind: str, best: float, core: float, name: str = "wheel"
    ) -> None:
        """Сохранить область HUD спорного/принятого совпадения для разбора."""
        if not self._retrieval_debug():
            return
        counts = self.__dict__.setdefault("_wheel_debug_counts", {"hit": 0, "veto": 0, "miss": 0})
        limit = {
            "veto": WHEEL_DEBUG_MAX_VETO,
            "miss": WHEEL_DEBUG_MAX_MISS,
            "fill": WHEEL_DEBUG_MAX_MISS,
            "rain": 80,
            "rainhi": 60,
        }.get(
            kind, WHEEL_DEBUG_MAX_HIT
        )
        if counts.get(kind, 0) >= limit:
            return
        # не чаще раза в WHEEL_DEBUG_PERIOD секунд, чтобы лимит не уходил на первые секунды
        now = time.monotonic()
        period = 0.0 if kind == "rainhi" else WHEEL_DEBUG_PERIOD
        if now - self.__dict__.get("_wheel_debug_t", {}).get(kind, -1e9) < period:
            return
        self.__dict__.setdefault("_wheel_debug_t", {})[kind] = now
        counts[kind] = counts.get(kind, 0) + 1
        try:
            out_dir = ROOT / "screenshots" / "wheel_debug"
            out_dir.mkdir(parents=True, exist_ok=True)
            tpl, _ = self._get_template(name)
            h, w = tpl.shape[:2]
            patch = haystack[loc[1] : loc[1] + h, loc[0] : loc[0] + w]
            zone = cv2.resize(haystack, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
            patch = cv2.resize(patch, (w * 4, h * 4), interpolation=cv2.INTER_NEAREST)
            ref = cv2.resize(tpl, (w * 4, h * 4), interpolation=cv2.INTER_NEAREST)
            height = max(zone.shape[0], patch.shape[0])
            canvas = np.zeros((height, zone.shape[1] + 2 * patch.shape[1] + 20, 3), np.uint8)
            canvas[: zone.shape[0], : zone.shape[1]] = zone
            x = zone.shape[1] + 10
            canvas[: patch.shape[0], x : x + patch.shape[1]] = patch
            x += patch.shape[1] + 10
            canvas[: ref.shape[0], x : x + ref.shape[1]] = ref
            name = f"{time.strftime('%H%M%S')}_{kind}_s{best:.3f}_c{core:.2f}.png"
            ok, buf = cv2.imencode(".png", canvas)
            if ok:
                buf.tofile(str(out_dir / name))  # tofile: путь с кириллицей на Windows
        except Exception:  # noqa: BLE001 - отладочный снимок не должен ломать бота
            pass

    def _wheel_confirmed(self, box):
        """Принять wheel, только если он найден WHEEL_CONFIRM_HITS опросов подряд на одном месте."""
        hits = self.__dict__.setdefault("_wheel_hits", [])
        if not box:
            hits.clear()
            return None
        now = time.monotonic()
        x, y = box[0], box[1]
        hits[:] = [
            h for h in hits
            if now - h[0] <= WHEEL_CONFIRM_WINDOW
            and abs(h[1] - x) <= WHEEL_CONFIRM_SHIFT
            and abs(h[2] - y) <= WHEEL_CONFIRM_SHIFT
        ]
        hits.append((now, x, y))
        if len(hits) >= WHEEL_CONFIRM_HITS:
            gap = getattr(self, "_wheel_last_gap", None)
            logger.info(
                "Wheel confirmed: ring closed%s",
                "" if gap is None else f" (gap {100 * gap:.1f}%)",
            )
            return box
        return None

    def is_retrieval_finished(self):
        if self.is_tackle_ready():
            return True

        conf = self.cfg.BOT.SPOOL_CONFIDENCE
        # Обычная леска: wheel. Радужная: 0m, либо 5m с запасным 0m (порядок
        # как у автора).
        if self.cfg.ARGS.RAINBOW is None:
            names, zone = ("wheel",), WHEEL_ZONE
        elif self.cfg.ARGS.RAINBOW == 0:
            names, zone = ("0m",), METER_ZONE
        else:
            names, zone = ("5m", "0m"), METER_ZONE

        # Неподдерживаемый размер окна: координат HUD нет, остаётся прежний
        # поиск по экрану (единственное место, где он сохранён).
        if not hasattr(self, "spool_icon_coord"):
            for name in names:
                box = self._get_image_box(name, conf)
                if box:
                    return box
            return None

        try:
            if names == ("wheel",) and getattr(self.cfg.BOT, "WHEEL_BY_FILL", False):
                return self._wheel_finished_by_fill()
            box = self._find_icon_in_zone(names, zone, conf)
            if names == ("wheel",) and WHEEL_CONFIRM_HITS > 1:
                return self._wheel_confirmed(box)
            return box
        except Exception:
            # Один плохой кадр (NaN в корреляции, отсутствующий шаблон,
            # вырожденный патч и т.п.) не должен ронять цикл вываживания:
            # как и _find_hud_template, считаем, что подмотка ещё не
            # закончена, и пробуем снова на следующем опросе.
            logger.exception(
                "is_retrieval_finished: detection failed, assuming not finished yet"
            )
            return None

    def is_line_snagged(self) -> bool:
        return self._get_pixel(*self.snag_icon_coord) == CRITICAL_COLOR

    def is_line_at_end(self) -> bool:
        return self._get_pixel(*self.spool_icon_coord) in (WARNING_COLOR, CRITICAL_COLOR)

    def is_clip_open(self) -> bool:
        return not all(
            c > MIN_GRAY_SCALE_LEVEL for c in self._get_pixel(*self.clip_icon_coord)
        )

    def is_tackle_ready(self):
        return self._get_image_box("ready", 0.6)

    def is_rod_place_refused(self) -> bool:
        """Красная плашка «В этом месте невозможно установить подставку под удилище».

        Плашка справа внизу (на 1920x1080: x 1515-1895, y 965-1055), фон около
        RGB(185, 49, 18). Берём её текстовую часть и считаем долю красных пикселей.
        """
        try:
            left, top, width, height = self.window.get_box()
            if width <= 0 or height <= 0:
                return False
            sx, sy = width / 1920.0, height / 1080.0
            region = (
                int(left + 1620 * sx), int(top + 972 * sy),
                max(8, int(260 * sx)), max(8, int(76 * sy)),
            )
            arr = np.asarray(pag.screenshot(region=region).convert("RGB")).astype(np.int16)
        except Exception:
            return False
        r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
        red = (r >= 150) & (g <= 100) & (b <= 70) & (r - g >= 90)
        return float(red.mean()) >= 0.4

    def is_tackle_broken(self):
        return self._get_image_box("broke", 0.8)

    def is_lure_broken(self):
        return self._get_image_box("lure_is_broken", 0.8)

    def is_moving_in_bottom_layer(self):
        return self._get_image_box("movement", 0.7)

    def is_disconnected(self):
        return self._get_image_box("disconnected", 0.9)

    def is_ticket_expired(self):
        return self._get_image_box("ticket", 0.9)

    def _ticket_hint_mask(self):
        """Маска текста подсказки «Выбрать следующий билет [L]» (static/<язык>/ticket_next_hint.png)."""
        cached = getattr(self, "_ticket_hint_tpl", False)
        if cached is not False:
            return cached
        tpl = cv2.imread(str(self.image_dir / "ticket_next_hint.png"), cv2.IMREAD_GRAYSCALE)
        self._ticket_hint_tpl = None if tpl is None else (tpl > 127).astype(np.float32)
        return self._ticket_hint_tpl

    def is_ticket_next_hint(self) -> bool:
        """Есть ли в списке подсказок справа строка «Выбрать следующий билет [L]».

        Она появляется за ~15 реальных минут до конца аренды и пропадает, когда билет
        выбран. Текст белый на любом фоне, поэтому сравниваем не картинку, а маску ярких
        пикселей (фон не мешает). Ищем в узкой полосе справа: положение строки зависит
        от числа строк в списке.
        """
        self.last_hint_note = None
        tpl = self._ticket_hint_mask()
        if tpl is None:
            self.last_hint_note = f"нет шаблона {self.image_dir / 'ticket_next_hint.png'}"
            return False
        try:
            left, top, width, _height = self.window.get_box()
            bw, bh = 290, 170
            shot = pag.screenshot(region=(int(left + width - bw), int(top + 190), bw, bh))
            gray = np.asarray(shot.convert("RGB"), dtype=np.float32).mean(axis=2)
        except Exception as err:  # noqa: BLE001
            self.last_hint_note = f"ошибка снимка: {err!r}"
            return False
        # Яркость интерфейса зависит от времени суток в игре: днём текст ~245, ночью ~180.
        # Поэтому порог привязан к самому яркому пикселю области (но не выше 205 и не ниже 120).
        thr = min(205.0, max(120.0, 0.85 * float(gray.max())))
        mask = (gray >= thr).astype(np.float32)
        if mask.shape[0] < tpl.shape[0] or mask.shape[1] < tpl.shape[1] or not mask.any():
            self.last_hint_note = f"в области нет светлых пикселей (размер {mask.shape})"
            return False
        score = float(cv2.matchTemplate(mask, tpl, cv2.TM_CCOEFF_NORMED).max())
        self.last_hint_note = f"сходство {score:.2f} (порог яркости {thr:.0f})"
        return score >= 0.75

    def _troll_label_masks(self):
        """Маски подписей у клавиши J: (идёт, стоит) из static/<язык>/troll_j_on.png и troll_j_off.png."""
        cached = getattr(self, "_troll_label_tpl", False)
        if cached is not False:
            return cached
        out = []
        for name in ("troll_j_on", "troll_j_off"):
            tpl = cv2.imread(str(self.image_dir / f"{name}.png"), cv2.IMREAD_GRAYSCALE)
            out.append(None if tpl is None else (tpl > 127).astype(np.float32))
        self._troll_label_tpl = tuple(out) if all(t is not None for t in out) else None
        return self._troll_label_tpl

    def trolling_state(self):
        """Режим тролинга в игре по подписи у J справа сверху: True - лодка идёт («Остановиться»/«Stop»),
        False - стоит («Троллинговый ход»/«Trolling mode»), None - подпись не прочиталась."""
        self.last_troll_scores = None
        tpls = self._troll_label_masks()
        if tpls is None:
            return None
        try:
            left, top, width, _height = self.window.get_box()
            bw, bh = 290, 170
            shot = pag.screenshot(region=(int(left + width - bw), int(top + 190), bw, bh))
            gray = np.asarray(shot.convert("RGB"), dtype=np.float32).mean(axis=2)
        except Exception:  # noqa: BLE001
            return None
        thr = min(205.0, max(120.0, 0.85 * float(gray.max())))
        mask = (gray >= thr).astype(np.float32)
        scores = []
        for tpl in tpls:
            if mask.shape[0] < tpl.shape[0] or mask.shape[1] < tpl.shape[1] or not mask.any():
                return None
            scores.append(float(cv2.matchTemplate(mask, tpl, cv2.TM_CCOEFF_NORMED).max()))
        on, off = scores
        self.last_troll_scores = (round(on, 2), round(off, 2), round(thr))
        if on >= 0.85 and on > off:
            return True
        if off >= 0.85 and off > on:
            return False
        return None

    def is_keepnet_full(self):
        return self._get_image_box("keepnet_is_full", 0.9)

    def is_gift_receieved(self):
        return self._get_image_box("gift", 0.9)

    def is_card_receieved(self):
        return self._get_image_box("card", 0.9)

    def is_event_triggered(self):
        return self._get_image_box("event_ok", 0.95)

    def is_operation_failed(self):
        return self._get_image_box("warning", 0.8)

    def is_operation_success(self):
        return self._get_image_box("ok_black", 0.8) or self._get_image_box(
            "ok_white", 0.8
        )

    def is_material_complete(self):
        return not self._get_image_box("material_slot", 0.7)

    def get_make_button_position(self):
        return self._get_image_box("make", 0.9)

    def get_discard_yes_position(self):
        return self._get_image_box("discard_yes", 0.9)

    def get_quit_position(self):
        return self._get_image_box("quit", 0.8)

    def get_yes_position(self):
        return self._get_image_box("yes", 0.8)

    def get_exit_icon_position(self):
        return self._get_image_box("exit", 0.8)

    def get_confirm_button_position(self):
        return self._get_image_box("confirm", 0.8)

    def get_food_position(self, food: str):
        return self._get_image_box(food, 0.9)

    def is_energy_high(self) -> bool:
        box = self._get_image_box("energy", 0.8)
        if not box:
            return False
        x, y = utils.get_box_center_integers(box)
        last_point = int(19 + 152 * self.cfg.STAT.ENERGY_THRESHOLD) - 1
        return self._get_pixel(x + 19, y) == self._get_pixel(x + last_point, y)

    def is_hunger_low(self) -> bool:
        box = self._get_image_box("food", 0.8)
        if not box:
            return False
        x, y = utils.get_box_center_integers(box)
        last_point = int(18 + 152 * self.cfg.STAT.HUNGER_THRESHOLD) - 1
        return not self._get_pixel(x + 18, y) == self._get_pixel(x + last_point, y)

    def is_comfort_low(self) -> bool:
        box = self._get_image_box("comfort", 0.8)
        if not box:
            return False
        x, y = utils.get_box_center_integers(box)
        last_point = int(18 + 152 * self.cfg.STAT.COMFORT_THRESHOLD) - 1
        return not self._get_pixel(x + 18, y) == self._get_pixel(x + last_point, y)

    def get_scrollbar_position(self):
        return self._get_image_box("scrollbar", 0.97)

    def get_100wear_position(self):
        return self._get_image_box("100wear", 0.98)

    def get_favorite_item_positions(self):
        return self._get_image_box("favorite", 0.95, multiple=True)

    def is_pva_chosen(self):
        return self._get_image_box("pva_icon", 0.6) is None

    def is_dry_mix_chosen(self):
        return not self._get_image_box("groundbait_is_not_chosen", 0.8)

    def is_bait_chosen(self):
        if self.cfg.PROFILE.MODE in ("spin", "pirk", "elevator"):
            return True

        if self.cfg.PROFILE.MODE in ("telescopic", "bolognese"):
            return (
                pag.locate(
                    self.bait_icon_reference_img,
                    pag.screenshot(region=self.bait_icon_coord),
                    confidence=0.6,
                )
                is None
            )
        return self._get_image_box("bait_icon", 0.6) is None

    def is_groundbait_chosen(self):
        return self._get_image_box("groundbait_icon", 0.6) is None

    def get_groundbait_position(self):
        return self._get_image_box("classic_feed_mix", 0.8)

    def get_dry_mix_position(self):
        return self._get_image_box("dry_feed_mix", 0.95)

    def get_pva_position(self):
        return self._get_image_box("pva_stick_or_pva_stringer", 0.95)

    def is_friction_brake_high(self) -> bool:
        if hasattr(self, "friction_brake_coord"):
            r, g, b = self._get_pixel(*self.friction_brake_coord)
            tr, tg, tb = RED_FRICTION_BRAKE
            return (abs(r - tr) <= COLOR_TOLERANCE and 
                    abs(g - tg) <= COLOR_TOLERANCE and 
                    abs(b - tb) <= COLOR_TOLERANCE)
        return False

    def _init_friction_snapshot_region(self, fractions: tuple) -> None:
        width = int(self.window.get_resolution_str().split("x")[0])
        center_x = self.window.get_box()[0] + width // 2
        offsets = [max(1, round(BAR_HALF_WIDTH * f)) for f in fractions]

        bar_y = self.friction_brake_low_coord[1]
        fish_x, fish_y = self.fish_icon_coord
        reel_x, reel_y = self.reel_burning_icon_coord

        x0 = min(fish_x, center_x - max(offsets))
        x1 = max(reel_x, center_x + max(offsets)) + 1
        y0 = min(fish_y, reel_y, bar_y)
        y1 = max(fish_y, reel_y, bar_y) + 1

        self._fb_key = tuple(fractions)
        self._fb_region = (int(x0), int(y0), int(x1 - x0), int(y1 - y0))
        self._fb_fish = (int(fish_x - x0), int(fish_y - y0))
        self._fb_reel = (int(reel_x - x0), int(reel_y - y0))
        y = int(bar_y - y0)
        self._fb_bar = [
            ((int(center_x - o - x0), y), (int(center_x + o - x0), y)) for o in offsets
        ]

    def get_friction_brake_snapshot(self, fractions: tuple | None = None):
        """Один скриншот для монитора фрикциона.

        fractions -- доли полудлины шкалы для точек (green, amber, red, overload).
        По умолчанию (fractions=None) overload берётся из реально настроенного
        cfg.*.FRICTION_BRAKE.SENSITIVITY -- раньше дефолт был жёстко "high"
        (0.80) независимо от конфига; единственный сегодняшний вызывающий код
        (component/friction_brake.py) передаёт fractions сам, так что для него
        ничего не меняется, это подстраховка для любого другого вызова.
        Возвращает (hooked, zone, burning, bar_pixels) или None, если для
        текущего размера окна нет координат.
        """
        if fractions is None:
            if self.cfg.ARGS.FEATURE == "bot":
                sensitivity = self.cfg.BOT.FRICTION_BRAKE.SENSITIVITY
            else:
                sensitivity = self.cfg.FRICTION_BRAKE.SENSITIVITY
            fractions = (
                BAR_GREEN_FRACTION,
                BAR_AMBER_FRACTION,
                BAR_RED_FRACTION,
                SENSITIVITY_FRACTIONS[sensitivity],
            )

        if getattr(self, "_fb_key", None) != tuple(fractions):
            needed = ("fish_icon_coord", "reel_burning_icon_coord", "friction_brake_low_coord")
            if not all(hasattr(self, name) for name in needed):
                return None
            self._init_friction_snapshot_region(fractions)

        shot = pag.screenshot(region=self._fb_region)
        px = shot.getpixel

        hooked = all(c > MIN_GRAY_SCALE_LEVEL for c in px(self._fb_fish)[:3])
        burning = all(
            abs(a - b) <= REEL_BURNING_TOLERANCE
            for a, b in zip(px(self._fb_reel)[:3], ORANGE_REEL)
        )
        bar_pixels = [(px(l)[:3], px(r)[:3]) for l, r in self._fb_bar]
        return hooked, bar_zone_from_pixels(bar_pixels), burning, bar_pixels

    def get_tension_state(
        self,
        amber_from: float = BAR_AMBER_FRACTION,
        red_from: float = BAR_RED_FRACTION,
        step: int = 2,
    ):
        """Иконки рыбы, зацепа, шпули и граница заливки шкалы за ОДИН скриншот.

        Возвращает TensionState или None, если для текущего размера окна нет
        координат. fill_px -- на сколько пикселей от центра дошла заливка
        (0 -- шкала пустая). Флаги snagged и line_end считаются по тем же
        пикселям и условиям, что is_line_snagged и is_line_at_end, поэтому
        цикл pull() может обойтись без отдельных скриншотов для них.
        Кэш снимка фрикциона (_fb_*) не трогает.
        """
        needed = (
            "fish_icon_coord",
            "friction_brake_low_coord",
            "snag_icon_coord",
            "spool_icon_coord",
        )
        if not all(hasattr(self, n) for n in needed):
            return None

        width = int(self.window.get_resolution_str().split("x")[0])
        center_x = self.window.get_box()[0] + width // 2
        bar_y = self.friction_brake_low_coord[1]
        fish_x, fish_y = self.fish_icon_coord
        snag_x, snag_y = self.snag_icon_coord
        spool_x, spool_y = self.spool_icon_coord

        x0 = min(fish_x, snag_x, spool_x, center_x - BAR_HALF_WIDTH)
        x1 = max(fish_x, snag_x, spool_x, center_x + BAR_HALF_WIDTH) + 1
        y0 = min(fish_y, snag_y, spool_y, bar_y)
        y1 = max(fish_y, snag_y, spool_y, bar_y) + 1
        shot = pag.screenshot(region=(int(x0), int(y0), int(x1 - x0), int(y1 - y0)))
        if shot.mode != "RGB":
            shot = shot.convert("RGB")
        raw, w = shot.tobytes(), shot.width

        def px(x, y) -> tuple:
            i = (int(y - y0) * w + int(x - x0)) * 3
            return (raw[i], raw[i + 1], raw[i + 2])

        hooked = all(c > MIN_GRAY_SCALE_LEVEL for c in px(fish_x, fish_y))
        snagged = px(snag_x, snag_y) == CRITICAL_COLOR
        line_end = px(spool_x, spool_y) in (WARNING_COLOR, CRITICAL_COLOR)

        # Заливка идёт сплошняком от центра. Белые разделители (1 px) дают
        # короткие разрывы, их пропускаем; после более длинного разрыва поиск
        # останавливается, чтобы яркие объекты сцены за краем заливки не
        # принимались за неё. Слева и справа должен быть один и тот же цвет.
        fill_px = gap = 0
        for o in range(step, BAR_HALF_WIDTH + 1, step):
            left = classify_bar_pixel(px(center_x - o, bar_y))
            right = classify_bar_pixel(px(center_x + o, bar_y))
            if left != ZONE_NONE and left == right:
                fill_px, gap = o, 0
            else:
                gap += step
                if gap > 6:
                    break

        if fill_px >= BAR_HALF_WIDTH * red_from:
            zone = ZONE_RED
        elif fill_px >= BAR_HALF_WIDTH * amber_from:
            zone = ZONE_AMBER
        elif fill_px >= BAR_HALF_WIDTH * BAR_GREEN_FRACTION:
            zone = ZONE_GREEN
        else:
            zone = ZONE_NONE
        return TensionState(hooked, zone, fill_px, snagged, line_end)

    def is_reel_burning(self) -> bool:
        if hasattr(self, "reel_burning_icon_coord"):
            return self._get_pixel(*self.reel_burning_icon_coord) == ORANGE_REEL
        return False

    def is_float_state_changed(self, reference_img):
        current_img = pag.screenshot(region=self.float_camera_rect)
        return not pag.locate(
            current_img.filter(ImageFilter.GaussianBlur(radius=3)),
            reference_img,
            grayscale=True,
            confidence=self.cfg.PROFILE.FLOAT_SENSITIVITY,
        )

    def get_ticket_position(self, duration: int):
        return self._get_image_box(f"ticket_{duration}", 0.95)

    def is_harvest_success(self):
        return self._get_image_box("ok_black", 0.8)

    def is_stuck_at_casting(self):
        return self._get_image_box("cast", 0.7)