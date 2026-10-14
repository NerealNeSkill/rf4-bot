"""Чтение координат карты из компаса справа внизу ("477:320").

Цифры белые, шрифт и размер фиксированы (около 7x11 px на 1080p). Текст
режется на отдельные символы и сравнивается с шаблонами цифр из
static/trolling/digits/. Шаблоны собираются калибровкой: вы вводите то, что
показано в игре, а модуль запоминает вид цифр (см. CoordReader.learn).
Нераспознанный или сомнительный символ даёт None, а не неверное число.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DIGITS_DIR = ROOT / "static" / "trolling" / "digits"

# Центр текста относительно правого нижнего угла клиентской области игры (px).
# Измерено на 1920x1080; для других разрешений HUD, по всей видимости,
# привязан к углу так же, но это не проверено.
TEXT_CENTER_FROM_RIGHT_BOTTOM = (128, 91)
REGION_SIZE = (108, 26)  # ширина, высота зоны поиска текста

CANVAS = (14, 12)  # высота, ширина холста символа
MIN_SCORE = 0.80  # минимальная корреляция с шаблоном
MIN_MARGIN = 0.04  # отрыв от второго по схожести символа
MAX_DIGITS = 5


@dataclass(frozen=True)
class Reading:
    x: int
    y: int
    score: float  # худшая уверенность по символам


def region_for_box(box) -> tuple[int, int, int, int]:
    """Зона текста (x, y, w, h) для клиентской области box=(left, top, w, h)."""
    left, top, width, height = box
    w, h = REGION_SIZE
    cx = left + width - TEXT_CENTER_FROM_RIGHT_BOTTOM[0]
    cy = top + height - TEXT_CENTER_FROM_RIGHT_BOTTOM[1]
    return int(cx - w // 2), int(cy - h // 2), w, h


def _text_mask(gray: np.ndarray) -> np.ndarray:
    """Маска белого текста; устойчива к яркому и тёмному фону."""
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    tophat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
    thr = max(35.0, 0.45 * float(np.percentile(tophat, 99.5)))
    return ((tophat >= thr) & (gray >= 185)).astype(np.uint8)


def segment(rgb: np.ndarray):
    """Символы слева направо: [("digit", canvas) | ("colon", None)]."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    mask = _text_mask(gray)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    comps = [tuple(int(v) for v in stats[i]) for i in range(1, count) if stats[i][4] >= 3]
    dots = [c for c in comps if c[2] <= 3 and c[3] <= 3]
    glyphs = [c for c in comps if c[3] >= 7 and c[2] >= 2]

    items = [(c[0], "digit", c) for c in glyphs]
    # Двоеточие: две точки друг под другом.
    dots.sort(key=lambda c: c[0])
    used = set()
    for i, a in enumerate(dots):
        for j, b in enumerate(dots):
            if i < j and j not in used and abs(a[0] - b[0]) <= 2 and 4 <= abs(a[1] - b[1]) <= 12:
                items.append((min(a[0], b[0]), "colon", None))
                used.update({i, j})
    items.sort(key=lambda t: t[0])

    result = []
    for _, kind, c in items:
        if kind == "colon":
            result.append(("colon", None))
            continue
        x, y, w, h, _ = c
        patch = gray[max(0, y - 1): y + h + 1, max(0, x - 1): x + w + 1].astype(np.float32)
        lo, hi = float(patch.min()), float(patch.max())
        patch = (patch - lo) / max(hi - lo, 1.0)
        canvas = np.zeros(CANVAS, np.float32)
        ph, pw = patch.shape
        if ph > CANVAS[0] or pw > CANVAS[1]:
            result.append(("bad", None))
            continue
        oy, ox = (CANVAS[0] - ph) // 2, (CANVAS[1] - pw) // 2
        canvas[oy:oy + ph, ox:ox + pw] = patch
        result.append(("digit", canvas))
    return result


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a - a.mean(), b - b.mean()
    den = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / den) if den > 1e-6 else 0.0


class CoordReader:
    def __init__(self, digits_dir: Path | None = None):
        self.digits_dir = Path(digits_dir) if digits_dir else DIGITS_DIR
        self.templates: dict[int, list[np.ndarray]] = {d: [] for d in range(10)}
        self.load()

    # -- шаблоны ----------------------------------------------------------
    def load(self) -> None:
        self.templates = {d: [] for d in range(10)}
        if not self.digits_dir.exists():
            return
        for path in sorted(self.digits_dir.glob("*.png")):
            digit = path.stem.split("_")[0]
            if not digit.isdigit() or len(digit) != 1:
                continue
            img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if img is not None and img.shape == CANVAS:
                self.templates[int(digit)].append(img.astype(np.float32) / 255.0)

    def missing(self) -> list[int]:
        return [d for d in range(10) if not self.templates[d]]

    def learn(self, rgb: np.ndarray, text: str, max_samples: int = 6) -> list[int]:
        """Запомнить цифры по кадру зоны текста и тому, что показано в игре.

        text -- "477:320". Возвращает список цифр, для которых добавлен шаблон.
        """
        glyphs = segment(rgb)
        expected = []
        for ch in text.strip():
            if ch.isdigit():
                expected.append(int(ch))
            elif ch == ":":
                expected.append(":")
            else:
                raise ValueError(f"Недопустимый символ в координатах: {ch!r}")
        kinds = ["colon" if g[0] == "colon" else g[0] for g in glyphs]
        want = ["colon" if e == ":" else "digit" for e in expected]
        if kinds != want:
            raise ValueError(
                f"Не удалось сопоставить символы: на экране найдено {kinds}, введено {want}")
        self.digits_dir.mkdir(parents=True, exist_ok=True)
        learned = []
        for (kind, canvas), label in zip(glyphs, expected):
            if kind != "digit":
                continue
            samples = self.templates[label]
            if len(samples) >= max_samples or any(_corr(canvas, s) > 0.985 for s in samples):
                continue
            samples.append(canvas)
            path = self.digits_dir / f"{label}_{len(samples)}.png"
            ok, buf = cv2.imencode(".png", (canvas * 255).astype(np.uint8))
            if ok:
                buf.tofile(str(path))
            learned.append(label)
        return learned

    # -- чтение --------------------------------------------------------------
    def _classify(self, canvas: np.ndarray):
        scores = []
        for digit, samples in self.templates.items():
            if samples:
                scores.append((max(_corr(canvas, s) for s in samples), digit))
        scores.sort(reverse=True)
        if not scores:
            return None
        best = scores[0]
        second = scores[1][0] if len(scores) > 1 else -1.0
        if best[0] < MIN_SCORE or best[0] - second < MIN_MARGIN:
            return None
        return best[1], best[0]

    def read_image(self, rgb: np.ndarray) -> Reading | None:
        glyphs = segment(rgb)
        if sum(1 for k, _ in glyphs if k == "colon") != 1:
            return None
        parts: list[list] = [[], []]
        side, worst = 0, 1.0
        for kind, canvas in glyphs:
            if kind == "colon":
                side = 1
                continue
            if kind != "digit":
                return None
            res = self._classify(canvas)
            if res is None:
                return None
            parts[side].append(res[0])
            worst = min(worst, res[1])
        if not (1 <= len(parts[0]) <= MAX_DIGITS and 1 <= len(parts[1]) <= MAX_DIGITS):
            return None
        return Reading(int("".join(map(str, parts[0]))), int("".join(map(str, parts[1]))), worst)

    def read_screen(self, box) -> Reading | None:
        import pyautogui  # noqa: PLC0415

        x, y, w, h = region_for_box(box)
        shot = pyautogui.screenshot(region=(x, y, w, h)).convert("RGB")
        return self.read_image(np.asarray(shot))
