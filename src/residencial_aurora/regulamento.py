"""Acesso ao regulamento por capítulo.

O texto completo nunca vai para instruções nem para a conversa: as tools só
devolvem o índice ou um capítulo de cada vez.
"""

from __future__ import annotations

import re
from functools import cache

from residencial_aurora.db import DADOS


@cache
def capitulos() -> list[tuple[str, str]]:
    """Lista de (título, texto) de cada capítulo, na ordem do regulamento."""
    texto = (DADOS / "regulamento.md").read_text(encoding="utf-8")
    partes = re.split(r"^## ", texto, flags=re.MULTILINE)[1:]
    resultado = []
    for parte in partes:
        titulo, _, corpo = parte.partition("\n")
        resultado.append((titulo.strip(), corpo.strip()))
    return resultado


def indice() -> list[str]:
    return [f"{numero}. {titulo}" for numero, (titulo, _) in enumerate(capitulos(), start=1)]


def capitulo(numero: int) -> str | None:
    lista = capitulos()
    if not 1 <= numero <= len(lista):
        return None
    titulo, corpo = lista[numero - 1]
    return f"{titulo}\n\n{corpo}"
