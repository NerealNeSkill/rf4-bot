"""Чтение координат с компаса ("477:320") по шаблонам цифр.

Цифры белые на тёмном полупрозрачном круге. Область фиксирована относительно
правого нижнего угла окна игры (HUD не масштабируется, как и остальной HUD).

Шаблоны цифр лежат в static/compass/<цифра>.png. Недостающие цифры
добавляются командой `python main.py trollcalib`: вы вводите координаты,
которые видите на компасе, а программа запоминает форму каждой цифры.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from pathlib import Path

import numpy as np
from rf4s.controller.troll_lang import tr as _tr

ROOT = Path(__file__).resolve().parents[2]
DIGITS_DIR = ROOT / "static" / "compass"

# Область строки координат при окне 1920x1080 (левый верх, ширина, высота),
# измерено от правого нижнего угла: x = right - 170, y = bottom - 108.
REGION_FROM_BR = (-170, -108, 90, 32)

GLYPH_H = 12          # высота нормализованного глифа в пикселях
GLYPH_W = 9           # ширина холста нормализованного глифа
DIM_CONTRAST = 22     # минимальный контраст приглушённых цифр под плашкой уведомления
BRIGHT_MIN = 150      # минимальная яркость "белой" цифры
MATCH_MIN = 0.72      # минимальное сходство глифа с шаблоном
MATCH_MARGIN = 0.06   # насколько лучший кандидат должен опережать ближайшую другую цифру
COLON_MAX_W = 3       # глиф не шире этого значения - двоеточие
MAX_GLYPH_W = 11      # шире - это две слипшиеся цифры (JPEG, плотный шрифт)
MIN_PART_W = 4        # минимальная ширина части при разрезании
MIN_GLYPH_H = 7       # ниже - это не цифра, а светлая точка с карты
NOISE_MAX = 0.35      # глиф в начале строки с меньшим сходством - мусор, а не цифра
MATCH_SOFT = 0.62     # мягкий порог: цифра чуть размыта фоном, но явно лучше остальных
MARGIN_SOFT = 0.12    # ... при отрыве от второго кандидата не меньше этого

# --- курс лодки по синей метке "N" на кольце компаса -------------------------
HEAD_CENTER = (45.0, -22.0)   # центр диска относительно левого верхнего угла области цифр, px (уточняется)
HEAD_RADIUS = 103.0           # радиус кольца, px (уточняется)
HEAD_HALF = 125               # половина стороны снимка вокруг центра диска
HEAD_AREA = (120, 1500)       # допустимая площадь метки N, px
HEAD_MIN_SPREAD = 100.0       # сколько градусов кольца должно пройти метка для подгонки окружности
HEAD_MAX_RMSE = 3.0           # допустимая ошибка подгонки окружности, px


def region_for(box, offset=None) -> tuple[int, int, int, int]:
    """Область экрана по окну игры box = (left, top, width, height).

    offset - (dx, dy) от правого нижнего угла окна; по умолчанию стандартное
    для HUD, но автопоиск (CompassReader.locate) может найти другое.
    """
    left, top, w, h = box
    dx, dy = offset if offset else REGION_FROM_BR[:2]
    return left + w + dx, top + h + dy, REGION_FROM_BR[2], REGION_FROM_BR[3]


def binarize(rgb: np.ndarray) -> np.ndarray:
    """Маска светлых пикселей цифр.

    Обычно цифры белые (яркость выше BRIGHT_MIN). Под полупрозрачной плашкой уведомления
    ("Катушка закрыта") они тускнеют до ~155 на фоне ~125, поэтому при низкой яркости порог
    берётся посередине между фоном и самыми яркими пикселями, если контраст достаточный.
    """
    gray = rgb.astype(np.float32).mean(axis=2)
    bg = float(np.percentile(gray, 60))
    peak = float(np.percentile(gray, 99))
    if peak >= BRIGHT_MIN + 20:
        return gray >= max(BRIGHT_MIN, bg + 45)
    if peak - bg >= DIM_CONTRAST:
        return gray >= (bg + peak) / 2
    return np.zeros(gray.shape, dtype=bool)


def split_glyphs(mask: np.ndarray) -> list[np.ndarray]:
    """Нарезка маски на глифы по пустым столбцам; каждый обрезан по высоте."""
    cols = mask.any(axis=0)
    glyphs, start = [], None
    for x, on in enumerate(list(cols) + [False]):
        if on and start is None:
            start = x
        elif not on and start is not None:
            part = mask[:, start:x]
            rows = np.where(part.any(axis=1))[0]
            glyphs.append(part[rows[0]:rows[-1] + 1, :])
            start = None
    return glyphs


def _split_wide(glyph: np.ndarray) -> list[np.ndarray]:
    """Разрезать слипшиеся цифры по столбцу с минимумом белых пикселей."""
    w = glyph.shape[1]
    if w <= MAX_GLYPH_W:
        return [glyph]
    sums = glyph.sum(axis=0)
    lo, hi = MIN_PART_W, w - MIN_PART_W
    if hi <= lo:
        return [glyph]
    cut = lo + int(np.argmin(sums[lo:hi + 1]))
    out = []
    for part in (glyph[:, :cut], glyph[:, cut:]):
        rows = np.where(part.any(axis=1))[0]
        if rows.size:
            part = part[rows[0]:rows[-1] + 1, :]
            cols = np.where(part.any(axis=0))[0]
            out.extend(_split_wide(part[:, cols[0]:cols[-1] + 1]))
    return out


def _trim_specks(glyph: np.ndarray) -> np.ndarray:
    """Срезать по краям высоты лишние строки с 1 пикселем (брызги карты склеились с цифрой)."""
    g = glyph
    while g.shape[0] > GLYPH_H and g[0].sum() <= 1:
        g = g[1:]
    while g.shape[0] > GLYPH_H and g[-1].sum() <= 1:
        g = g[:-1]
    cols = np.where(g.any(axis=0))[0]
    return g[:, cols[0]:cols[-1] + 1] if cols.size else g


def _is_text_row(mask: np.ndarray) -> np.ndarray:
    """Оставить только строки с цифрами: самая плотная полоса высотой ~GLYPH_H."""
    rows = mask.sum(axis=1)
    if rows.max() == 0:
        return mask
    best, best_y = -1, 0
    for y in range(0, max(1, mask.shape[0] - GLYPH_H + 1)):
        s = rows[y:y + GLYPH_H + 2].sum()
        if s > best:
            best, best_y = s, y
    return mask[best_y:best_y + GLYPH_H + 2, :]


def normalize(glyph: np.ndarray) -> np.ndarray:
    """Глиф -> матрица GLYPH_H x GLYPH_W (выравнивание по левому краю)."""
    out = np.zeros((GLYPH_H, GLYPH_W), dtype=np.float32)
    h = min(GLYPH_H, glyph.shape[0])
    w = min(GLYPH_W, glyph.shape[1])
    out[:h, :w] = glyph[:h, :w]
    return out


def _pad(a: np.ndarray) -> np.ndarray:
    out = np.zeros((GLYPH_H + 4, GLYPH_W + 6), dtype=np.float32)
    out[2:2 + a.shape[0], 3:3 + a.shape[1]] = a
    return out


def similarity(a: np.ndarray, b: np.ndarray, shift_x: int = 2, shift_y: int = 1) -> float:
    """IoU двух масок с допуском на сдвиг (граница разреза слипшихся цифр плавает)."""
    pa, pb = _pad(a), _pad(b)
    best = 0.0
    for dy in range(-shift_y, shift_y + 1):
        for dx in range(-shift_x, shift_x + 1):
            sh = np.roll(np.roll(pa, dy, axis=0), dx, axis=1)
            union = float(np.maximum(sh, pb).sum())
            if union:
                best = max(best, float((sh * pb).sum()) / union)
    return best


def north_mask(rgb: np.ndarray) -> np.ndarray:
    """Маска насыщенно-синей метки N (фон воды и плашки уведомлений не такие тёмно-синие)."""
    a = rgb.astype(np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    return (r <= 40) & (b >= 105) & (b - r >= 85) & (g >= 50) & (g <= 120)


def largest_blob(mask: np.ndarray):
    """Самая большая связная область маски: (площадь, x центра, y центра) либо None."""
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    todo = {(int(x), int(y)) for x, y in zip(xs, ys)}
    best = None
    while todo:
        start = todo.pop()
        stack, pts = [start], [start]
        while stack:
            x, y = stack.pop()
            for nb in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if nb in todo:
                    todo.discard(nb)
                    stack.append(nb)
                    pts.append(nb)
        if best is None or len(pts) > len(best):
            best = pts
    arr = np.asarray(best, dtype=np.float64)
    return len(best), float(arr[:, 0].mean()), float(arr[:, 1].mean())


def fit_circle(points):
    """Окружность по точкам (метод наименьших квадратов): (cx, cy, r, rmse)."""
    p = np.asarray(points, dtype=np.float64)
    a = np.column_stack([2 * p[:, 0], 2 * p[:, 1], np.ones(len(p))])
    b = p[:, 0] ** 2 + p[:, 1] ** 2
    sol, *_ = np.linalg.lstsq(a, b, rcond=None)
    cx, cy = float(sol[0]), float(sol[1])
    r2 = float(sol[2]) + cx * cx + cy * cy
    r = math.sqrt(max(r2, 0.0))
    rmse = float(np.sqrt(np.mean((np.hypot(p[:, 0] - cx, p[:, 1] - cy) - r) ** 2)))
    return cx, cy, r, rmse


def angular_spread(points, cx: float, cy: float) -> float:
    """Сколько градусов окружности покрывают точки (360 минус самый большой пробел)."""
    angs = sorted(math.degrees(math.atan2(x - cx, -(y - cy))) % 360.0 for x, y in points)
    if len(angs) < 2:
        return 0.0
    gaps = [b - a for a, b in zip(angs, angs[1:])] + [angs[0] + 360.0 - angs[-1]]
    return 360.0 - max(gaps)


class CompassReader:
    """Читает пару координат (x, y) с компаса."""

    def __init__(self, digits_dir: Path | None = None, grab=None, box_fn=None):
        self.digits_dir = Path(digits_dir or DIGITS_DIR)
        self._grab = grab          # grab(region) -> RGB ndarray; по умолчанию pyautogui
        self._box_fn = box_fn      # box_fn() -> (left, top, w, h) окна игры
        self.templates: dict[str, list[np.ndarray]] = {}
        self.last_text = ""   # результат последнего разбора (для диагностики)
        self.offset: tuple[int, int] | None = None   # найденное смещение области от правого нижнего угла
        self._last: tuple[float, tuple[int, int]] | None = None
        self.heading_note = None
        self.fail_dir: Path | None = None   # куда складывать кадры с нераспознанными цифрами
        self._fail_saved = 0
        self._fail_t = 0.0
        self.max_speed = 12.0   # единиц координат в секунду; быстрее лодка не плывёт
        self.load_templates()
        self._load_offset()

    def _load_offset(self) -> None:
        try:
            data = json.loads((self.digits_dir / "offset.json").read_text(encoding="utf-8"))
            self.offset = (int(data["dx"]), int(data["dy"]))
        except (OSError, ValueError, KeyError, TypeError):
            self.offset = None

    def _save_offset(self) -> None:
        try:
            self.digits_dir.mkdir(parents=True, exist_ok=True)
            (self.digits_dir / "offset.json").write_text(
                json.dumps({"dx": self.offset[0], "dy": self.offset[1]}), encoding="utf-8")
        except OSError:
            pass

    # -- шаблоны ------------------------------------------------------------
    def load_templates(self) -> None:
        self.templates = {}
        if not self.digits_dir.exists():
            return
        for path in sorted(self.digits_dir.glob("*.npy")):
            digit = path.stem.split("_")[0]
            if digit.isdigit():
                self.templates.setdefault(digit, []).append(np.load(path))

    def missing_digits(self) -> list[str]:
        return [d for d in "0123456789" if d not in self.templates]

    def add_template(self, digit: str, glyph: np.ndarray) -> bool:
        """Добавить шаблон, если он заметно отличается от имеющихся."""
        norm = normalize(glyph).astype(np.float32)
        known = self.templates.setdefault(digit, [])
        if any(similarity(norm, t, 0, 0) > 0.92 for t in known):
            return False
        self.digits_dir.mkdir(parents=True, exist_ok=True)
        np.save(self.digits_dir / f"{digit}_{len(known)}.npy", norm)
        known.append(norm)
        return True

    # -- распознавание ------------------------------------------------------
    def _capture(self) -> np.ndarray:
        box = self._box_fn() if self._box_fn else (0, 0, 1920, 1080)
        region = region_for(box, self.offset)
        if self._grab:
            return np.asarray(self._grab(region))
        import pyautogui as pag  # noqa: PLC0415
        return np.asarray(pag.screenshot(region=region).convert("RGB"))

    def parse(self, rgb: np.ndarray):
        """Вернуть (строка, [глифы цифр]) либо (None, [])."""
        mask = _is_text_row(binarize(rgb))
        glyphs = [_trim_specks(p) for g in split_glyphs(mask) for p in _split_wide(g)]
        text, digit_glyphs = "", []
        started = False   # уже встретили настоящую цифру (мусор до неё отбрасываем)
        for g in glyphs:
            if g.shape[1] <= COLON_MAX_W:
                if started:
                    text += ":"
                continue
            if g.shape[0] < MIN_GLYPH_H:
                continue   # светлая точка/чёрточка с карты, не цифра
            norm = normalize(g)
            scores: dict[str, float] = {}
            for digit, items in self.templates.items():
                scores[digit] = max(similarity(norm, tpl) for tpl in items)
            ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
            if not started and (not ranked or ranked[0][1] < NOISE_MAX):
                continue   # мусор в начале строки: белые детали карты рядом с цифрами
            started = True
            digit_glyphs.append(g)
            if not ranked:
                text += "?"
                continue
            best = ranked[0][1]
            margin = best - ranked[1][1] if len(ranked) > 1 else 1.0
            if (best >= MATCH_MIN and margin >= MATCH_MARGIN) or (
                    best >= MATCH_SOFT and margin >= MARGIN_SOFT):
                text += ranked[0][0]
            else:
                text += "?"
        return text, digit_glyphs

    def _screen_grab(self, region):
        if self._grab:
            return np.asarray(self._grab(region))
        import pyautogui as pag  # noqa: PLC0415
        return np.asarray(pag.screenshot(region=region).convert("RGB"))

    def locate(self, boxes, search_w: int = 320, search_h: int = 220) -> bool:
        """Найти строку координат в правом нижнем углу каждого окна из boxes.

        Один снимок угла, дальше окно чтения скользит по нему в памяти. При успехе
        запоминается смещение от правого нижнего угла окна (и пишется в offset.json).
        """
        rw, rh = REGION_FROM_BR[2], REGION_FROM_BR[3]
        for box in boxes:
            left, top, w, h = box
            rx, ry = max(0, left + w - search_w), max(0, top + h - search_h)
            try:
                big = self._screen_grab((rx, ry, search_w, search_h))
            except Exception:  # noqa: BLE001
                continue
            H, W = big.shape[:2]
            for y in range(0, H - rh + 1, 3):
                for x in range(0, W - rw + 1, 6):
                    sub = big[y:y + rh, x:x + rw]
                    mask = binarize(sub)
                    if mask.sum() < 20:
                        continue
                    # Текст не должен упираться в края области (иначе цифра обрезана).
                    if mask[:, :3].any() or mask[:, -3:].any() or mask[:2, :].any() or mask[-2:, :].any():
                        continue
                    text, _ = self.parse(sub)
                    if re.fullmatch(r"\d{1,4}:\d{1,4}", text):
                        cols, rows = np.where(mask.any(axis=0))[0], np.where(mask.any(axis=1))[0]
                        x += int(round((cols[0] + cols[-1]) / 2 - rw / 2))   # центрируем область по тексту
                        y += int(round((rows[0] + rows[-1]) / 2 - rh / 2))
                        self.offset = (rx + x - (left + w), ry + y - (top + h))
                        self._save_offset()
                        return True
        return False

    def save_probe(self, boxes, out_dir: Path, search_w: int = 320, search_h: int = 220) -> list[str]:
        """Сохранить снимки угла экрана и результат разбора, чтобы понять, почему компас не читается."""
        out_dir.mkdir(parents=True, exist_ok=True)
        notes = []
        try:
            from PIL import Image  # noqa: PLC0415
            for i, box in enumerate(boxes):
                left, top, w, h = box
                rx, ry = max(0, left + w - search_w), max(0, top + h - search_h)
                big = self._screen_grab((rx, ry, search_w, search_h))
                path = out_dir / f"compass_probe_{i}.png"
                Image.fromarray(big).resize((search_w * 3, search_h * 3), Image.NEAREST).save(path)
                text, _ = self.parse(self._screen_grab(region_for(box, self.offset)))
                notes.append(f"{path.name}: окно {tuple(box)}, область {region_for(box, self.offset)}, разбор {text!r}")
        except Exception as err:  # noqa: BLE001
            notes.append(f"снимки не сохранены: {err!r}")
        return notes


    # -- курс по метке N --------------------------------------------------------
    def _load_heading(self) -> None:
        self._hc = HEAD_CENTER
        self._hr = HEAD_RADIUS
        self.heading_calibrated = False
        self._hpts: list[tuple[float, float]] = []
        self._hfit_n = 0
        try:
            data = json.loads((self.digits_dir / "heading.json").read_text(encoding="utf-8"))
            self._hc = (float(data["cx"]), float(data["cy"]))
            self._hr = float(data["r"])
            self.heading_calibrated = True
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save_heading(self) -> None:
        try:
            self.digits_dir.mkdir(parents=True, exist_ok=True)
            (self.digits_dir / "heading.json").write_text(
                json.dumps({"cx": self._hc[0], "cy": self._hc[1], "r": self._hr}), encoding="utf-8")
        except OSError:
            pass

    def _try_fit_heading(self) -> bool:
        """Уточнить центр и радиус кольца по точкам метки N; True, если подгонка принята."""
        pts = self._hpts
        if len(pts) < 12:
            return False
        spread = angular_spread(pts, *self._hc)
        if spread < HEAD_MIN_SPREAD or (self.heading_calibrated and spread < 200.0):
            return False
        if self.heading_calibrated and len(pts) < self._hfit_n + 40:
            return False
        cx, cy, r, rmse = fit_circle(pts)
        if rmse > HEAD_MAX_RMSE or not 40.0 <= r <= 200.0:
            return False
        self._hc, self._hr, self._hfit_n = (cx, cy), r, len(pts)
        self.heading_calibrated = True
        self.heading_note = (f"курс по метке N откалиброван: центр ({cx:.1f}, {cy:.1f}), радиус {r:.1f} px, "
                             f"разброс {rmse:.2f} px, охват {spread:.0f} градусов")
        self._save_heading()
        return True

    def read_heading(self):
        """Курс лодки: (градусы по часовой от севера, откалиброван ли центр) либо None.

        Кольцо компаса вращается вместе с лодкой, поэтому по положению синей метки N
        курс = -(угол метки от направления "вверх").
        """
        if not hasattr(self, "_hc"):
            self._load_heading()
        box = self._box_fn() if self._box_fn else (0, 0, 1920, 1080)
        rx, ry, _, _ = region_for(box, self.offset)
        cx, cy = self._hc
        x0, y0 = int(round(rx + cx - HEAD_HALF)), int(round(ry + cy - HEAD_HALF))
        img = self._screen_grab((x0, y0, 2 * HEAD_HALF, 2 * HEAD_HALF))
        blob = largest_blob(north_mask(img))
        if blob is None or not HEAD_AREA[0] <= blob[0] <= HEAD_AREA[1]:
            return None
        px, py = x0 + blob[1] - rx, y0 + blob[2] - ry     # относительно области цифр
        dist = math.hypot(px - cx, py - cy)
        if not 0.6 * self._hr <= dist <= 1.4 * self._hr:
            return None
        if not self._hpts or math.hypot(px - self._hpts[-1][0], py - self._hpts[-1][1]) >= 4.0:
            self._hpts.append((px, py))
            del self._hpts[:-300]
            self.heading_note = None
            if self._try_fit_heading():
                cx, cy = self._hc
        self.last_heading_pt = (px, py)
        ang = math.degrees(math.atan2(px - cx, -(py - cy)))
        return (-ang) % 360.0, self.heading_calibrated

    def heading_at(self, px: float, py: float) -> float:
        """Курс для сохранённой точки метки N по ТЕКУЩЕМУ (самому свежему) центру кольца."""
        cx, cy = self._hc
        return (-math.degrees(math.atan2(px - cx, -(py - cy)))) % 360.0

    def read_once(self):
        """Одно чтение: (x, y) либо None."""
        rgb = self._capture()
        text, _ = self.parse(rgb)
        self.last_text = text
        if text and "?" in text:
            self._save_fail(rgb, text)
        if not text or "?" in text or text.count(":") != 1:
            return None
        left, right = text.split(":")
        if not left or not right or len(left) > 4 or len(right) > 4:
            return None
        return int(left), int(right)

    def _save_fail(self, rgb, text: str) -> None:
        """Сохранить кадр, где цифры не распознаны (не чаще раза в 6 с, не больше 40)."""
        try:
            now = time.monotonic()
            if self.fail_dir is None or self._fail_saved >= 40 or now - self._fail_t < 6.0:
                return
            from PIL import Image  # noqa: PLC0415
            self.fail_dir.mkdir(parents=True, exist_ok=True)
            name = time.strftime("compass_fail_%H%M%S") + "_" + re.sub(r"[^0-9:?]", "", text).replace(":", "-").replace("?", "q") + ".png"
            Image.fromarray(np.asarray(rgb).astype(np.uint8)).save(self.fail_dir / name)
            self._fail_saved += 1
            self._fail_t = now
        except Exception:  # noqa: BLE001
            pass

    def read(self, samples: int = 3, delay: float = 0.03):
        """Чтение с фильтром: невозможные скачки позиции отбрасываются.

        Скачок принимается, только если два чтения подряд согласны между собой
        (так лодка, перенесённая игрой, не застрянет в отбраковке навсегда).
        """
        val = self._read_median(samples, delay)
        if val is None:
            return None
        now = time.monotonic()
        if self._last is not None:
            t0, p0 = self._last
            limit = 15.0 + self.max_speed * (now - t0)
            if max(abs(val[0] - p0[0]), abs(val[1] - p0[1])) > limit:
                pending = getattr(self, "_pending", None)
                same = pending is not None and max(
                    abs(val[0] - pending[0]), abs(val[1] - pending[1])) <= 3
                self._pending_n = getattr(self, "_pending_n", 0) + 1 if same else 1
                self._pending = val
                # Лишняя цифра (светлая деталь карты слева от координат читается как "1":
                # 154 -> 1154) держится много кадров подряд, поэтому два согласных чтения
                # её не отсекают. Скачок с ростом числа знаков принимаем только при долгом
                # устойчивом значении.
                grew = (len(str(val[0])) > len(str(p0[0]))
                        or len(str(val[1])) > len(str(p0[1])))
                if self._pending_n < (20 if grew else 2):
                    return None
        self._pending = None
        self._pending_n = 0
        self._last = (now, val)
        return val

    def _read_median(self, samples: int = 3, delay: float = 0.03):
        """Медиана по нескольким чтениям; None, если стабильного значения нет."""
        reads = []
        for i in range(samples):
            val = self.read_once()
            if val is not None:
                reads.append(val)
            if i + 1 < samples:
                time.sleep(delay)
        if not reads:
            return None
        common, count = Counter(reads).most_common(1)[0]
        if count >= 2 or len(reads) == 1:
            return common
        xs, ys = sorted(r[0] for r in reads), sorted(r[1] for r in reads)
        return xs[len(xs) // 2], ys[len(ys) // 2]


# -------------------------------------------------------------- калибровка

def learn_from_image(reader: CompassReader, rgb: np.ndarray, label: str) -> int:
    """Обучить шаблоны по кадру и известной подписи вида "477:320"."""
    text, glyphs = reader.parse(rgb)
    digits = [c for c in label if c.isdigit()]
    if len(glyphs) != len(digits):
        raise ValueError(f"Найдено глифов {len(glyphs)}, а цифр в подписи {len(digits)}")
    added = 0
    for digit, glyph in zip(digits, glyphs):
        added += reader.add_template(digit, glyph)
    return added


def calibrate_cli(args=None) -> None:
    """python main.py trollcalib: учит цифры по тому, что вы видите на компасе."""
    reader = CompassReader()
    print(_tr("Калибровка компаса. Недостающие цифры: " + (" ".join(reader.missing_digits()) or "нет")))
    print(_tr("Встаньте так, чтобы на компасе были нужные цифры, введите показанные координаты"))
    print(_tr("(например 892:1990) и нажмите Enter. Пустая строка - выход."))
    while True:
        label = input(_tr("Координаты на компасе: ")).strip()
        if not label:
            break
        try:
            added = learn_from_image(reader, reader._capture(), label)
        except ValueError as err:
            print(_tr(f"Не получилось: {err}"))
            continue
        print(_tr(f"Добавлено шаблонов: {added}. Не хватает: {' '.join(reader.missing_digits()) or 'всё есть'}"))
