<div align="center">

![RF4Bot][logo]
<h1 align="center">RF4Bot: Russian Fishing 4 bot</h1>

**English** | [Русский][readme_ru]

**A Russian Fishing 4 fishing bot: spin, bottom, marine and float fishing, plus trolling along recorded routes.**

![GitHub License](https://img.shields.io/badge/license-GPL--3.0-blue)
[![Python: 3.11 | 3.12](https://img.shields.io/badge/python-3.11_%7C_3.12-blue)](https://www.python.org/downloads/)

</div>

> [!NOTE]
> RF4Bot is a fork of **[RussianFishing4Script (RF4S)](https://github.com/dereklee0310/RussianFishing4Script)** by **Derek Lee**, based on v0.11.0.
> The original author allowed commercial use under GPL-3.0 as long as the source code stays open. All credit for the foundation goes to him.

## Open source and the launcher

The whole bot is open and licensed under **GNU GPL v3**. You can run it for free from source with `python main.py`, as in the original project.

A convenient installer with a graphical launcher, license check, automatic updates and support is available separately at **https://rf4-bot.ru/**. The launcher is a separate program that starts the bot and does not change its code. Every build comes with a link to the matching source code.

## What is different from the original

- Trolling along **recorded routes** (compass heading, coordinates, left/right turn timing, ready-made presets in `static/trolling`).
- Route recording with a readable console log (RU/EN).
- Additional rod placement settings and various stability fixes (see **[CHANGELOG][changelog]**).

## Installation (from source)
> [!WARNING]
> 1. The path to the folder cannot contain non-English characters.
> 2. Python 3.13+ is not supported, required version: >=3.11,<=3.12.

```
git clone https://github.com/NerealNeSkill/rf4-bot.git
cd rf4-bot
pip install -r requirements.txt
python main.py
```

With uv:
```
uv sync
uv run main.py
```
See **[INSTALLATION][installation]** and **[CONFIGURATION][configuration]** for details.

## Game settings
- Set both system and in-game interface scales to "1x"; use "window mode" or "borderless windowed".
- Make sure the reel is fully spooled (the bot watches the spool to detect retrieval). With rainbow line use the `-R` flag.
- If Windows Mouse ClickLock is enabled, set the time to long.
- Bottom fishing: put your rods in quick selection slots (1~3). Other modes: take the rod in hand.
- If the bot runs but nothing happens, run it as administrator.

## Documentation
| | EN | RU |
| - | - | - |
| Installation | [INSTALLATION][installation] | [УСТАНОВКА][installation_ru] |
| Configuration | [CONFIGURATION][configuration] | [НАСТРОЙКА][configuration_ru] |
| Changelog | [CHANGELOG][changelog] | [ИЗМЕНЕНИЯ][changelog_ru] |

## License and credits
**[GNU General Public License v3][license]**. See also [NOTICE](/NOTICE).
Based on RussianFishing4Script, Copyright (C) Derek Lee.

[logo]: /static/readme/RF4S.png
[readme_ru]: /docs/ru/README.md
[installation]: /docs/en/INSTALLATION.md
[configuration]: /docs/en/CONFIGURATION.md
[changelog]: /docs/en/CHANGELOG.md
[installation_ru]: /docs/ru/INSTALLATION.md
[configuration_ru]: /docs/ru/CONFIGURATION.md
[changelog_ru]: /docs/ru/CHANGELOG.md
[license]: /LICENSE
