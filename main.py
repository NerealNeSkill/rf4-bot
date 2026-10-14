r"""Точка входа RF4S.

Вся логика лежит в rf4s._main_core (в собранном дистрибутиве -- скомпилированный
модуль .pyd). Этот файл работает в ДВУХ режимах, переключается флагом
--protect-main в build.py (copy_project_files()):

  - По умолчанию (--protect-main выключен): обычный текстовый .py, едет в
    сборку как есть и запускается напрямую встроенным Python
    (python.exe -u main.py ...). Никакой дополнительной настройки путей не
    нужно -- её делает сам python.exe через свой ._pth + site.py.

  - С --protect-main: этот же файл компилируется (PyInstaller, см.
    build_main_exe() в build\build.py) в main.exe. Тогда обе проверки
    лицензии (в rf4s._main_core и в BotApp.__init__) берут require_license
    через один и тот же импорт rf4s.licensing -- если main.py остаётся
    текстовым файлом, достаточно открыть его в блокноте и подменить
    rf4s.licensing в sys.modules до импорта rf4s._main_core, чтобы обойти обе
    проверки сразу, без какого-либо реверс-инжиниринга скомпилированных .pyd.
    Когда это сам .exe, такой подмены через текстовый редактор уже не
    сделать -- но тогда нужна вся возня ниже с sys.path/DLL (её не видно,
    пока --protect-main выключен: весь блок срабатывает только при
    sys.frozen, т.е. внутри скомпилированного .exe).

Здесь, кроме самой настройки путей: (1) быстрый --machine-id без полного
запуска; (2) --self-test для build.py (main_exe_self_test() при
--protect-main, smoke_test.py -- без него); (3) сам вызов.
"""

import os
import sys

if getattr(sys, "frozen", False):
    # Только для main.exe (--protect-main). Обычный python.exe всё это уже
    # делает сам через свой ._pth + site.py -- здесь воспроизводим то же
    # самое вручную, раз замороженный .exe в обход этого механизма.
    import site
    from pathlib import Path

    # Собранный .exe: сам exe и есть "main.py" в этой схеме путей.
    BASE_DIR = Path(sys.executable).resolve().parent

    EMBEDDED_SITE = BASE_DIR / "python" / "Lib" / "site-packages"
    # Установленная сборка самодостаточна: встроенный Python + библиотеки лежат в папке
    # проекта, и _internal_main (библиотеки main.exe) боту не нужен -- он не должен
    # затенять или подмешивать чужие версии. Без встроенного Python (на всякий
    # случай) -- запасной путь через _internal_main.
    _site_packages_dir = EMBEDDED_SITE if EMBEDDED_SITE.exists() else BASE_DIR / "_internal_main"

    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))

    if _site_packages_dir.exists():
        # site.addsitedir(), а НЕ просто sys.path.insert(): иначе .pth-файл,
        # который кладёт pywin32 (добавляет site-packages\win32, \win32\lib,
        # \Pythonwin -- там лежат win32gui/win32con/win32api и т.п.), никогда не
        # обрабатывается. При обычном запуске через python.exe это делает сам
        # site.py (встроенный Python собран с ._pth + "import site" как раз ради
        # этого), а здесь, в main.exe, путь добавляется вручную руками -- и
        # должен пройти через ту же обработку, иначе pywin32 "вроде на диске
        # есть, а ModuleNotFoundError всё равно".
        _before = list(sys.path)
        site.addsitedir(str(_site_packages_dir))
        # addsitedir() дописывает новые пути в конец sys.path; переносим их в
        # начало (как раньше делал sys.path.insert(0, ...)), чтобы библиотеки из
        # сборки имели приоритет над чем угодно другим на пути поиска, сохраняя
        # их взаимный порядок.
        _new_entries = [p for p in sys.path if p not in _before]
        for _p in _new_entries:
            sys.path.remove(_p)
        sys.path[0:0] = _new_entries

        # pywin32_system32: pip-установка pywin32 (без постинсталла, требующего
        # прав администратора) оставляет pywintypesXXX.dll/pythoncomXXX.dll в
        # этой отдельной подпапке site-packages, а не в System32 и не в самом
        # win32\. .pth-файл её не перечисляет (addsitedir() выше её не находит).
        # В "замороженном" режиме модуль pywintypes сам ищет pywintypesXXX.dll
        # простым перебором sys.path (его __import_pywin32_system_module__) --
        # без этой папки в sys.path падает с "Module 'pywintypes' isn't in
        # frozen sys.path", даже когда win32/win32\lib уже на месте.
        _pywin32_system32 = _site_packages_dir / "pywin32_system32"
        if _pywin32_system32.exists():
            p = str(_pywin32_system32)
            if p not in sys.path:
                sys.path.insert(0, p)
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(p)
                except OSError:
                    pass

        # DLL-зависимости cv2/numpy/и т.п.: при обычном запуске через python.exe
        # Windows ищет такие DLL рядом с самим .pyd без проблем. Внутри main.exe
        # (замороженный PyInstaller-процесс) действует урезанный ("safe") порядок
        # поиска DLL -- он не включает автоматически папку конкретного пакета в
        # site-packages, только папку самого .exe (её PyInstaller настраивает сам
        # под СВОИ бандлованные библиотеки в _internal_main) и System32. cv2.pyd
        # и его собственные бандлованные DLL (ffmpeg-плагин и т.п.) лежат прямо в
        # site-packages\cv2\, а делвил-пересобранные колёса (numpy, Pillow,
        # scipy...) кладут рантайм-DLL в соседнюю site-packages\<pkg>.libs\ --
        # ни то, ни другое само не попадёт в путь поиска внутри main.exe, отсюда
        # "DLL load failed while importing cv2: не найден указанный модуль" --
        # хотя тот же cv2 прекрасно грузится при запуске plain python.exe.
        # Добавляем явно: саму папку cv2\ (если есть) и все *.libs\ в
        # site-packages (общий паттерн для numpy/scipy/Pillow и т.п.).
        if hasattr(os, "add_dll_directory"):
            _dll_candidates = []
            # Сама папка встроенного Python (BASE_DIR\python\): именно ТАМ лежат
            # vcruntime140.dll/vcruntime140_1.dll/python3*.dll и т.п. из
            # embeddable-архива (рядом с python.exe). При запуске через
            # python.exe эта папка -- "папка главного exe", и Windows всегда ищет
            # DLL там автоматически, без всяких add_dll_directory. При запуске
            # через main.exe "папка главного exe" -- это build\stage\ (где лежит
            # сам main.exe), а build\stage\python\ для него уже ЧУЖАЯ папка,
            # ничем не особая -- отсюда и "DLL load failed", хотя тот же cv2.pyd
            # прекрасно грузится через plain python.exe на той же машине с теми
            # же файлами на диске.
            _embedded_python_dir = BASE_DIR / "python"
            if _embedded_python_dir.is_dir():
                _dll_candidates.append(_embedded_python_dir)
            _cv2_dir = _site_packages_dir / "cv2"
            if _cv2_dir.is_dir():
                _dll_candidates.append(_cv2_dir)
            _dll_candidates.extend(
                p for p in _site_packages_dir.glob("*.libs") if p.is_dir()
            )
            for _p in _dll_candidates:
                try:
                    os.add_dll_directory(str(_p))
                except OSError:
                    pass


if __name__ == "__main__":
    # ВАЖНО: должно стоять первой строкой, до любого разбора sys.argv.
    # При --protect-main main.exe -- это sys.frozen-процесс, а значит
    # multiprocessing (см. FrictionBrake / Process(...) в rf4s.utils) на
    # Windows перезапускает САМ ЖЕ main.exe для дочернего процесса-воркера:
    # multiprocessing/spawn.py::get_command_line() при sys.frozen строит
    # командную строку как
    #   main.exe --multiprocessing-fork parent_pid=<pid> pipe_handle=<...>
    # (при обычном запуске main.py через python.exe sys.frozen=False, и
    # multiprocessing вместо этого использует "python.exe -c <bootstrap>" --
    # поэтому раньше, без --protect-main, этой проблемы просто не было видно).
    # freeze_support() распознаёт "--multiprocessing-fork" в sys.argv[1] и
    # сам обрабатывает запуск воркера (spawn_main() + sys.exit()), не давая
    # этим служебным аргументам дойти до argparse в rf4s._main_core.main() --
    # без неё argparse получает 'parent_pid=17144' как позиционный аргумент
    # feature и падает с "invalid choice: 'parent_pid=17144'", а сам процесс
    # фрикциона (frictionbrake) так и не стартует. Для обычных запусков
    # (--machine-id, --self-test, "bot -p 0 ...") freeze_support() -- no-op:
    # она ничего не делает, если sys.argv[1] != "--multiprocessing-fork".
    import multiprocessing

    multiprocessing.freeze_support()

    if "--self-test" in sys.argv:
        # Только проверка импорта: убедиться, что rf4s._main_core и всё, что он
        # (транзитивно, через rf4s.app.app и т.д.) импортирует -- реально
        # загружается. Боевой код (run()) не вызывается.
        try:
            import rf4s._main_core  # noqa: F401
        except BaseException:
            import traceback

            try:
                traceback.print_exc()
            except Exception:
                pass
            sys.exit(1)
        sys.exit(0)

    from rf4s._main_core import run

    run()
