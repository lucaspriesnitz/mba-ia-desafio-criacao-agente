"""Comando de restauração: `uv run restaurar`."""

from residencial_aurora import db


def main() -> None:
    db.restaurar()
    print(f"Reservas e visitantes restaurados a partir de dados/ em {db.DB_PATH}")


if __name__ == "__main__":
    main()
