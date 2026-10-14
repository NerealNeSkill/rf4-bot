"""Class to get info and control the game window."""

from pathlib import Path
from time import sleep

import pyautogui as pag
import win32con
import win32gui
import win32com.client

from rf4s import utils
from rf4s.controller.logger import logger

ANIMATION_DELAY = 0.5


class Window:
    """Controller for terminal and game windows management.

    This class handles window focus, size detection, and screenshot functionality
    for the game and terminal windows.

    Attributes:
        game_title (str): Title of the game window.
        terminal_hwnd (int): Handle of the terminal window.
    """

    def __init__(self, game_title: str = "Russian Fishing 4"):
        """Set the hwnd of the terminal where user run the script.

        We didn't retrieve the game window's hwnd here because we don't want to check
        if the window is open right away. Instead, we perform the check after the
        configuration is set.

        :param game_title: Title of the game, defaults to "Russian Fishing 4".
        :type game_title: str, optional
        """
        self.game_title = game_title
        self.terminal_hwnd = win32gui.GetForegroundWindow()

    def _get_game_hwnd(self) -> int:
        """Get the handle of the game window.

        :return: Process handle of the game window.
        :rtype: int
        """
        hwnd = win32gui.FindWindow(None, self.game_title)  # class name: UnityWndClass
        if hwnd == 0:
            logger.critical(
                "\nОШИБКА: Окно игры '%s' не найдено! Запустите 'Русская Рыбалка 4' "
                "(игра должна быть открыта в оконном или безрамочном режиме) и повторите попытку."
                "\nERROR: Game window '%s' not found! Please start 'Russian Fishing 4' "
                "(windowed or borderless mode) and try again.",
                self.game_title,
                self.game_title,
            )
            utils.safe_exit()
        return hwnd

    def is_title_bar_exist(self) -> bool:
        """Check if the game window is in windowed mode.

        :return: True if the game window has a title bar, False otherwise.
        :rtype: bool
        """
        style = win32gui.GetWindowLong(self._get_game_hwnd(), win32con.GWL_STYLE)
        return bool(style & win32con.WS_CAPTION)

    def get_box(self) -> tuple[int, int, int, int]:
        """Get the coordinates and dimensions of the game window.

        :return: Tuple containing (x, y, width, height) of the game window.
        :rtype: tuple[int, int, int, int]
        """
        # Absolute coordinates
        base_x, base_y, _, _ = win32gui.GetWindowRect(self._get_game_hwnd())
        if self.is_title_bar_exist():
            base_x += 8
            base_y += 31
        # Relative coordinates
        left, top, right, bottom = win32gui.GetClientRect(self._get_game_hwnd())
        return base_x, base_y, right - left, bottom - top

    def get_base_coordinates(self) -> tuple[int, int]:
        """Get the base coordinates of the game window.

        :return: Tuple containing (x, y) of the base coordinates.
        :rtype: tuple[int, int]
        """
        return self.get_box()[:2]

    def get_resolution_str(self) -> str:
        """Get the resolution of the game window.

        :return: Game window resolution string (axb).
        :rtype: str
        """
        width, height = self.get_box()[2:]
        return f"{width}x{height}"

    def activate_script_window(self) -> None:
        """Focus the terminal where user run the script."""
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            shell.SendKeys('%')
            win32gui.SetForegroundWindow(self.terminal_hwnd)
        except Exception:
            win32gui.ShowWindow(self.terminal_hwnd, win32con.SW_RESTORE)
        sleep(ANIMATION_DELAY)

    def activate_game_window(self) -> None:
        """Focus game window."""
        hwnd = self._get_game_hwnd()
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            shell.SendKeys('%')
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        sleep(ANIMATION_DELAY)

    def is_game_foreground(self) -> bool:
        """True, если окно игры активно и не свёрнуто (иначе ввод и скриншоты бесполезны).

        Не завершает бота, если окна нет: в этом случае возвращает False.
        """
        try:
            hwnd = win32gui.FindWindow(None, self.game_title)
            if not hwnd or win32gui.IsIconic(hwnd):
                return False
            return win32gui.GetForegroundWindow() == hwnd
        except Exception:  # noqa: BLE001 - проверка фокуса не должна ломать бота
            return True

    def is_size_supported(self) -> bool:
        """Check if the game window size is supported.

        :return: True if it's supported, False otherwise.
        :rtype: bool
        """
        return self.get_resolution_str() in (
            "2560x1440",
            "1920x1080",
            "1600x900",
        )

    def save_screenshot(self, filepath: Path) -> None:
        """Save a screenshot of the game window to the screenshots directory."""
        pag.screenshot(
            imageFilename=filepath,
            region=self.get_box(),
        )


if __name__ == "__main__":
    w = Window("Russian Fishing 4")
    # w.activate_game_window()
    print(w.get_box())
    print(w.get_base_coordinates())
    print(w.get_resolution_str())
    print(w.is_size_supported())