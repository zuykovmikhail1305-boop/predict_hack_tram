import sqlite3
import pandas as pd
import os

EXCEL_FILE = "Хакатон_справочники_трамвай_10_маршрутов.xlsx"
DB_PATH = r"data\tram.db"

if not os.path.exists(EXCEL_FILE):
    print(f"❌ Файл {EXCEL_FILE} не найден!")
    exit(1)

# Читаем лист со списком маршрутов
df_routes = pd.read_excel(EXCEL_FILE, sheet_name="Маршруты GTFS_ROUTES")

# Преобразуем номера маршрутов и названия
# Нужные колонки: 'номер маршрута' (short_name) и 'полное название маршрута в справочнике А-Б' (long_name)
col_num = "номер маршрута"
col_name = "полное название маршрута в справочнике А-Б"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

updated_count = 0
for _, row in df_routes.iterrows():
    try:
        r_num = int(row[col_num])
        r_name = str(row[col_name]).strip()
        
        # Обновляем название маршрута в базе
        cursor.execute(
            "UPDATE routes SET name = ? WHERE number = ?",
            (r_name, r_num)
        )
        if cursor.rowcount > 0:
            updated_count += 1
            print(f" Маршрут {r_num} -> {r_name}")
    except (ValueError, TypeError):
        continue

conn.commit()
conn.close()

print(f"\n Обновлено {updated_count} названий маршрутов!")