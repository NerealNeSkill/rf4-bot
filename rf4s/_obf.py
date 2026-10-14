r"""Раскодирование строк, спрятанных build\obfuscate.py перед компиляцией.

Этот файл не обфусцируется сам (иначе раскодировщик не смог бы раскодировать
сам себя). Ключ не секрет в криптографическом смысле -- он лишь не даёт
`strings`/strings64 по собранному .pyd показывать протокол платы и тексты
логов открытым текстом. Поменять ключ: изменить строку ниже, при следующей
сборке build\obfuscate.py перекодирует всё заново этим ключом.
"""

import base64

_KEY = bytes.fromhex("8f1ae35502c79b446da13ef8175c902b")


def _xor(data: bytes) -> bytes:
    return bytes(b ^ _KEY[i % len(_KEY)] for i, b in enumerate(data))


def decode(token: str) -> str:
    return _xor(base64.urlsafe_b64decode(token.encode("ascii"))).decode("utf-8")


def decode_bytes(token: str) -> bytes:
    return _xor(base64.urlsafe_b64decode(token.encode("ascii")))
