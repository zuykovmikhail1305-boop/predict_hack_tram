"""Заполнение test_submission.csv прогнозами обученных моделей.

Признаки строятся теми же функциями, что в labels.ipynb, масштабирование и
прогноз - как в model.ipynb (MinMaxScaler, обученный на train, и CatBoost по маршрутам).

Результат:
    OUTPUT_DIR / submission.csv           - столбцы как в test_submission.csv
    OUTPUT_DIR / submission_features.csv  - то же плюс все признаки (до масштабирования)
"""
import io
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler

# откуда берутся сырые данные (labels/labels_day_train.csv и meteo.csv)
RAW_DATA_DIR = Path("D://tram/dataset (1)")
# RAW_DATA_DIR = Path("C://Users/user/Desktop/dataset_tram")

# куда сохраняются заполненные таблицы
OUTPUT_DIR = Path(".")

SUBMISSION_PATH = Path("test_submission.csv")
ARTIFACTS_DIR = Path("artifacts")

SPORT_EVENTS_PATH = "sport_events.json"
SCHOOL_HOLIDAYS_PATH = "school_holidays.json"
CALENDAR_PATH = "new_holiday.json"
METEO_CONFIG_PATH = "meteo_config.json"

# столбцы, которые масштабируются MinMaxScaler (как nums_col в model.ipynb)
NUMS_COL = ["temperature_2m (°C)", "apparent_temperature (°C)",
            "precipitation (mm)", "rain (mm)", "snowfall_mm",
            "snow_depth_mm", "pressure_msl (hPa)",
            "wind_speed_10m (km/h)", "wind_gusts_10m (km/h)",
            ]

# маршрута 5 не было в обучении - для него берётся модель этого маршрута
FALLBACK_ROUTE = 1

# с 01:00 до 04:00 транспорт не ходит - прогноз в эти часы всегда 0
NO_SERVICE_HOURS = [1, 2, 3]


# ---------- признаки (функции из labels.ipynb) ----------

def load_sport_events(path):
    """Прочитать расписание матчей: {вид спорта: DataFrame[date, hour, team]}."""
    with open(path, encoding="utf-8") as file:
        events = json.load(file)

    schedules = {}
    for sport in ("football", "hockey"):
        matches = pd.DataFrame(events[sport]["matches"], columns=["date", "hour", "team"])
        matches["date"] = pd.to_datetime(matches["date"], format="%Y-%m-%d")
        matches["hour"] = matches["hour"].astype(int)
        matches["team"] = matches["team"] + events[sport]["column_suffix"]
        schedules[sport] = matches.drop_duplicates()
    return schedules


def add_sport_features(data, matches):
    """Флаг дня матча и пикового окна (от -2 до +3 часов от начала) для каждого клуба."""
    result = data.copy()
    record_datetime = result["date"] + pd.to_timedelta(result["hour"], unit="h")

    for team in matches["team"].unique():
        result[team] = 0
        result[f"{team}_peak"] = 0

    for match_date, match_hour, team in matches.itertuples(index=False):
        match_datetime = match_date + pd.Timedelta(hours=match_hour)
        peak_period = record_datetime.between(
            match_datetime - pd.Timedelta(hours=2),
            match_datetime + pd.Timedelta(hours=3),
        )
        result.loc[result["date"].eq(match_date), team] = 1
        result.loc[peak_period, f"{team}_peak"] = 1

    return result


def prepare_meteo(raw, config_path):
    """Почасовая погода: ключи date/hour, снег в мм, ветер через sin/cos, код погоды по группам."""
    with open(config_path, encoding="utf-8") as file:
        config = json.load(file)
    drop_columns = config["drop_columns"]
    weather_code_groups = config["weather_code_groups"]

    missing = set(drop_columns) - set(raw.columns)
    if missing:
        raise KeyError(f"В meteo нет столбцов: {sorted(missing)}")

    meteo = raw.drop(columns=drop_columns)
    timestamp = pd.to_datetime(meteo.pop("time"), format="%Y-%m-%dT%H:%M")
    meteo.insert(0, "date", timestamp.dt.normalize())
    meteo.insert(1, "hour", timestamp.dt.hour.astype(int))

    meteo["snowfall_mm"] = meteo.pop("snowfall (cm)") * 10
    meteo["snow_depth_mm"] = meteo.pop("snow_depth (m)") * 1000

    wind_direction = np.radians(meteo.pop("wind_direction_10m (°)"))
    meteo["wind_direction_sin"] = np.sin(wind_direction)
    meteo["wind_direction_cos"] = np.cos(wind_direction)

    weather_code = meteo.pop("weather_code (wmo code)")
    known_codes = {code for codes in weather_code_groups.values() for code in codes}
    unknown_codes = set(weather_code.unique()) - known_codes
    if unknown_codes:
        raise ValueError(f"Неизвестные коды погоды: {sorted(unknown_codes)}")
    for column, codes in weather_code_groups.items():
        meteo[column] = weather_code.isin(codes).astype("int8")

    return meteo


def merge_weather_data(main_df, weather_df):
    merge_keys = ["date", "hour"]
    weather = weather_df.drop_duplicates(subset=merge_keys)

    overlapping_columns = set(main_df.columns).intersection(weather.columns) - set(merge_keys)
    weather = weather.drop(columns=list(overlapping_columns))

    result = main_df.merge(weather, on=merge_keys, how="left", validate="many_to_one")

    weather_columns = weather.columns.difference(merge_keys)
    missing_rows = result[weather_columns].isna().any(axis=1).sum()
    if missing_rows:
        print(f"Внимание: {missing_rows:,} строк без погоды")
    return result


def add_calendar_features(data, calendar_path):
    """Признаки производственного календаря.

    day_off         - любой нерабочий день, включая обычные выходные
    public_holiday  - нерабочий будний день (праздник или перенос)
    working_weekend - рабочая суббота или воскресенье (перенос)
    short_day       - сокращённый предпраздничный день
    """
    with open(calendar_path, encoding="utf-8") as file:
        calendar = json.load(file)

    non_working_days = pd.to_datetime(calendar["nonWorkingDays"])
    short_days = pd.to_datetime(calendar["shortDays"])

    result = data.copy()
    is_weekend = result["date"].dt.dayofweek >= 5
    day_off = result["date"].isin(non_working_days)

    result["day_off"] = day_off.astype("int8")
    result["public_holiday"] = (day_off & ~is_weekend).astype("int8")
    result["working_weekend"] = (~day_off & is_weekend).astype("int8")
    result["short_day"] = result["date"].isin(short_days).astype("int8")
    return result


def add_school_holiday_features(data, holidays_path):
    with open(holidays_path, encoding="utf-8") as file:
        periods = json.load(file)["periods"]

    result = data.copy()
    in_holiday = pd.Series(False, index=result.index)
    for period in periods:
        in_holiday |= result["date"].between(pd.Timestamp(period["start"]), pd.Timestamp(period["end"]))

    result["school_holiday"] = in_holiday.astype("int8")
    return result


def add_time_features(data):
    """Циклическое кодирование часа, дня недели, месяца и дня года."""
    result = data.copy()
    dates = result["date"]
    cycles = {
        "hour": (result["hour"], 24),
        "dayofweek": (dates.dt.dayofweek, 7),
        "month": (dates.dt.month - 1, 12),
        "dayofyear": (dates.dt.dayofyear - 1, 365),
    }
    for name, (values, period) in cycles.items():
        angle = 2 * np.pi * values / period
        result[f"{name}_sin"] = np.sin(angle)
        result[f"{name}_cos"] = np.cos(angle)

    result["dayofweek"] = dates.dt.dayofweek
    result["day"] = dates.dt.day
    return result


def build_features(labels_path, sport_schedules, meteo):
    data = pd.read_csv(labels_path, sep=";")
    data["date"] = pd.to_datetime(data["date"], format="%Y-%m-%d")
    data["hour"] = data["hour"].astype(int)

    for matches in sport_schedules.values():
        data = add_sport_features(data, matches)
    data = merge_weather_data(data, meteo)
    data = add_calendar_features(data, CALENDAR_PATH)
    data = add_school_holiday_features(data, SCHOOL_HOLIDAYS_PATH)
    data = add_time_features(data)
    return data


# ---------- масштабирование и прогноз (как в model.ipynb) ----------

def csv_round_trip(data):
    """Записать и прочитать таблицу так же, как labels.ipynb сохраняет её, а model.ipynb читает.

    После CSV дробные значения отличаются в последнем знаке, и модели обучены
    именно на прочитанных из CSV значениях.
    """
    buffer = io.StringIO()
    data.to_csv(buffer, sep=";", index=False)
    buffer.seek(0)
    return pd.read_csv(buffer, sep=";")


def fit_scaler(train_features):
    """MinMaxScaler по train, как в ячейке с nums_col в model.ipynb."""
    scaler = MinMaxScaler()
    scaler.fit(train_features[NUMS_COL])
    return scaler


def scale_features(features, scaler):
    """Матрица признаков для модели: без date/route/целевого столбца, NUMS_COL через MinMaxScaler."""
    X = features.drop(columns=["date", "route", "boardings", "prediction"], errors="ignore").copy()
    X[NUMS_COL] = scaler.transform(X[NUMS_COL])
    return X


def load_models(routes):
    models = {}
    for route in routes:
        model_path = ARTIFACTS_DIR / f"catboost_route_{route}.joblib"
        if model_path.exists():
            models[route] = joblib.load(model_path)
    return models


def check_feature_order(X, model):
    # CatBoost берёт признаки по порядку, поэтому порядок должен совпадать с обучением
    expected = list(model.feature_names_)
    actual = list(X.columns)
    if actual != expected:
        raise ValueError(f"Признаки не совпадают с обучением модели:\n{actual}\n{expected}")


def predict(features, models, scaler):
    X = scale_features(features, scaler)
    prediction = pd.Series(np.nan, index=features.index)

    for route in sorted(features["route"].unique()):
        model_route = route if route in models else FALLBACK_ROUTE
        if model_route != route:
            print(f"Маршрут {route}: своей модели нет, используется модель маршрута {model_route}")
        model = models[model_route]
        check_feature_order(X, model)

        mask = features["route"].eq(route)
        prediction[mask] = model.predict(X[mask])

    prediction[features["hour"].isin(NO_SERVICE_HOURS)] = 0
    # CatBoost с MAE может дать число меньше нуля, а по формату сабмита prediction >= 0
    return prediction.clip(lower=0)


def main():
    submission = pd.read_csv(SUBMISSION_PATH, sep=";")

    sport_schedules = load_sport_events(SPORT_EVENTS_PATH)
    meteo = prepare_meteo(pd.read_csv(RAW_DATA_DIR / "meteo.csv"), METEO_CONFIG_PATH)
    features = csv_round_trip(build_features(SUBMISSION_PATH, sport_schedules, meteo))
    train_features = csv_round_trip(
        build_features(RAW_DATA_DIR / "labels" / "labels_day_train.csv", sport_schedules, meteo)
    )

    scaler = fit_scaler(train_features)
    models = load_models(sorted(set(features["route"].unique()) | {FALLBACK_ROUTE}))
    features["prediction"] = predict(features, models, scaler)

    if features["prediction"].isna().any():
        raise ValueError("Не для всех строк получен прогноз")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    result = submission[["route", "date", "hour"]].merge(
        features[["route", "date", "hour", "prediction"]],
        on=["route", "date", "hour"],
        how="left",
        validate="one_to_one",
    )[submission.columns]
    result.to_csv(OUTPUT_DIR / "submission.csv", sep=";", index=False)

    features_columns = ["route", "date", "hour", "prediction"] + [
        column for column in features.columns if column not in ("route", "date", "hour", "prediction")
    ]
    features[features_columns].to_csv(
        OUTPUT_DIR / "submission_features.csv", sep=";", index=False, encoding="utf-8-sig"
    )

    print(f"Сохранено: {OUTPUT_DIR / 'submission.csv'} ({len(result):,} строк)")
    print(f"Сохранено: {OUTPUT_DIR / 'submission_features.csv'} ({features.shape[1]} столбцов)")


if __name__ == "__main__":
    main()
