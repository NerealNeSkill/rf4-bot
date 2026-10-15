<div align="center">

<img src="/static/readme/logo.png" alt="RF4Bot" width="128">
<h1 align="center">RF4Bot: бот для Русской рыбалки 4</h1>

[English][readme_en] | **Русский**

**Бот для Русской рыбалки 4: спиннинг, донка, море, поплавок и троллинг по записанным маршрутам.**

![GitHub License](https://img.shields.io/badge/license-GPL--3.0-blue)
[![Python: 3.11 | 3.12](https://img.shields.io/badge/python-3.11_%7C_3.12-blue)](https://www.python.org/downloads/)
[![GitHub Stars](https://img.shields.io/github/stars/NerealNeSkill/rf4-bot?style=flat&logo=github)](https://github.com/NerealNeSkill/rf4-bot/stargazers)

</div>

> [!NOTE]
> RF4Bot — форк **[RussianFishing4Script (RF4S)](https://github.com/dereklee0310/RussianFishing4Script)** авторства **Derek Lee** на базе v0.11.0.
> Автор разрешил коммерческое использование по GPL-3.0 при условии, что исходный код остаётся открытым. Основа проекта — его заслуга.

## Открытый код и лаунчер

Весь бот открыт и распространяется по лицензии **GNU GPL v3**. Его можно бесплатно запускать из исходников: `python main.py`, как в оригинале.

Удобный установщик с графическим лаунчером, проверкой лицензии, автообновлением и поддержкой доступен отдельно на **https://rf4-bot.ru/**. Лаунчер — отдельная программа: она запускает бота и не меняет его код. К каждой сборке прилагается ссылка на соответствующий исходный код.

## Чем отличается от оригинала

- Троллинг по **записанным маршрутам** (курс по компасу, координаты, время поворотов влево/вправо, готовые пресеты в `static/trolling`).
- Запись маршрута с понятным выводом в консоль (RU/EN).
- Дополнительные настройки расстановки удилищ и исправления стабильности (см. **[ИЗМЕНЕНИЯ][changelog]**).

## Установка (из исходников)
> [!WARNING]
> 1. В пути к папке не должно быть русских букв.
> 2. Python 3.13+ не поддерживается, нужен >=3.11,<=3.12.

```
git clone https://github.com/NerealNeSkill/rf4-bot.git
cd rf4-bot
pip install -r requirements.txt
python main.py
```

Через uv:
```
uv sync
uv run main.py
```
Подробнее: **[УСТАНОВКА][installation]** и **[НАСТРОЙКА][configuration]**.

## Настройки игры
- Масштаб интерфейса в системе и в игре — «1x»; режим «оконный» или «окно без рамки».
- Катушка должна быть полностью намотана (бот следит за шпулей). С радужной леской используйте флаг `-R`.
- Если включена «залипание мыши» (ClickLock) в Windows, поставьте длинное время.
- Донка: уберите удочки в слоты быстрого выбора (1~3). Остальные режимы: возьмите удочку в руки.
- Бот работает, но ничего не происходит — запустите от имени администратора.

## Документация
| | EN | RU |
| - | - | - |
| Установка | [INSTALLATION][installation_en] | [УСТАНОВКА][installation] |
| Настройка | [CONFIGURATION][configuration_en] | [НАСТРОЙКА][configuration] |
| Изменения | [CHANGELOG][changelog_en] | [ИЗМЕНЕНИЯ][changelog] |

## Лицензия и авторство
**[GNU General Public License v3][license]**. См. также [NOTICE](/NOTICE).
Основано на RussianFishing4Script, Copyright (C) Derek Lee.

[readme_en]: /docs/en/README.md
[installation]: /docs/ru/INSTALLATION.md
[configuration]: /docs/ru/CONFIGURATION.md
[changelog]: /docs/ru/CHANGELOG.md
[installation_en]: /docs/en/INSTALLATION.md
[configuration_en]: /docs/en/CONFIGURATION.md
[changelog_en]: /docs/en/CHANGELOG.md
[license]: /LICENSE
