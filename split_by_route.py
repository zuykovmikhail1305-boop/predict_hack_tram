"""Split a tram dataset into one CSV file per route."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd


def extract_route(value: object) -> int | None:
    """Extract the first route number from values such as ``25 трамвай``."""
    if pd.isna(value):
        return None

    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


def split_dataset(input_path: Path, output_dir: Path) -> dict[int, int]:
    """Write one semicolon-separated CSV per route and return row counts."""
    dataset = pd.read_csv(input_path, sep=";")

    if "route" in dataset.columns:
        route_values = dataset["route"].map(extract_route)
    elif "ngpt_route" in dataset.columns:
        route_values = dataset["ngpt_route"].map(extract_route)
    else:
        raise ValueError("В CSV нет колонки 'route' или 'ngpt_route'.")

    invalid_rows = route_values.isna().sum()
    if invalid_rows:
        raise ValueError(
            f"Не удалось определить маршрут для {invalid_rows} строк. "
            "Проверьте значения в колонке маршрута."
        )

    dataset = dataset.copy()
    dataset["route"] = route_values.astype(int)
    output_dir.mkdir(parents=True, exist_ok=True)

    row_counts: dict[int, int] = {}
    for route, route_dataset in dataset.groupby("route", sort=True):
        output_path = output_dir / f"route_{route}.csv"
        route_dataset.to_csv(output_path, sep=";", index=False)
        row_counts[int(route)] = len(route_dataset)

    if sum(row_counts.values()) != len(dataset):
        raise RuntimeError("После разбиения потерялись строки датасета.")

    return row_counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Разложить датасет трамваев по отдельным файлам маршрутов."
    )
    parser.add_argument("input", type=Path, help="Путь к исходному CSV")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("routes"),
        help="Папка для файлов route_<номер>.csv (по умолчанию: routes)",
    )
    args = parser.parse_args()

    row_counts = split_dataset(args.input, args.output_dir)
    print(f"Готово: {sum(row_counts.values())} строк разделено на {len(row_counts)} маршрутов.")
    for route, count in row_counts.items():
        print(f"Маршрут {route}: {count} строк")


if __name__ == "__main__":
    main()