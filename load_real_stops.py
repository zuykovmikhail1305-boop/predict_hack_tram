import sqlite3
import pandas as pd
import os

EXCEL_FILE = "Хакатон_справочники_трамвай_10_маршрутов.xlsx"
DB_PATH = r"data\tram.db"

if not os.path.exists(EXCEL_FILE):
    print(f"❌ Файл {EXCEL_FILE} не найден в текущей папке!")
    exit(1)

print("📖 Читаем лист 'Порядок_с_координатами'...")
df = pd.read_excel(EXCEL_FILE, sheet_name="Порядок_с_координатами")

# Оставляем прямое направление (0), чтобы точки не накладывались на обратный путь
if "direction_id" in df.columns:
    df = df[df["direction_id"] == 0]

df = df.sort_values(by=["route_short_name", "stop_sequence"])

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# Получаем соответствие номера маршрута и его id в базе
cursor.execute("SELECT id, number FROM routes")
routes_in_db = {row[1]: row[0] for row in cursor.fetchall()}

# Очищаем старые случайные остановки
cursor.execute("DELETE FROM stops")
conn.commit()
print("🧹 Старые остановки удалены.")

added_count = 0
for r_num, group in df.groupby("route_short_name"):
    r_num = int(r_num)
    if r_num not in routes_in_db:
        continue
    
    route_db_id = routes_in_db[r_num]
    stops_list = group.drop_duplicates(subset=["stop_id"]).to_dict("records")
    total_stops = len(stops_list)

    for order, row in enumerate(stops_list, start=1):
        is_terminal = 1 if (order == 1 or order == total_stops) else 0
        cursor.execute(
            """
            INSERT INTO stops (route_id, name, lat, lon, order_num, is_terminal)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                route_db_id,
                str(row["stop_name"]),
                float(row["stop_lat"]),
                float(row["stop_lon"]),
                order,
                is_terminal,
            )
        )
        added_count += 1

conn.commit()
conn.close()

print(f"✅ Успешно загружено {added_count} реальных остановок с точными координатами!")