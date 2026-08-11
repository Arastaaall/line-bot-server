"""Google スプレッドシートを、このBotのデータベースとして扱うための部品。

このファイルでは列番号を使わず、1行目の見出し（ヘッダー）を見て読み書きする。
そのため、列の順番が変わってもデータを別の場所へ書き込む事故を防げる。
"""

import json
import os
from datetime import datetime
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials


JST = ZoneInfo("Asia/Tokyo")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# シートに書かれている列名と、プログラムで使う分かりやすい名前の対応表。
USER_COLUMNS = {
    "user_id": "user_id",
    "user_name": "User_Name",
    "status": "status",
    "gender": "gender",
    "age": "age",
    "height": "height",
    "weight": "weight",
    "waist": "waist",
    "target_weight": "target_weight",
    "tdee": "tdee",
    "target_calories": "target_calories",
    "updated_at": "updated_at",
    "is_premium": "is_premium",
    "goal_mode": "goal_mode",
    "pal": "pal(def:1.375)",
    "meal_style": "meal_style",
    "cravings_trigger": "cravings_trigger",
    "target_months": "target_months",
}


def _now() -> str:
    """スプレッドシートへ保存する日本時間の日時文字列を作る。"""
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def _date_from_sheet(value: Any):
    """GASとPythonのどちらが書いた日時でも、日付部分を取り出す。"""
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    return None


@lru_cache(maxsize=1)
def _spreadsheet():
    """Renderの環境変数から認証して、対象スプレッドシートを開く。"""
    raw_credentials = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")

    if not raw_credentials:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません。")
    if not spreadsheet_id:
        raise RuntimeError("SPREADSHEET_ID が設定されていません。")

    try:
        credentials_info = json.loads(raw_credentials)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON が正しいJSONではありません。") from exc

    credentials = Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
    client = gspread.authorize(credentials)
    return client.open_by_key(spreadsheet_id)


def _worksheet(name: str):
    return _spreadsheet().worksheet(name)


def verify_connection() -> None:
    """必要な4つのシートを、読み取りだけで開けるか確認する。"""
    spreadsheet = _spreadsheet()
    available = {sheet.title for sheet in spreadsheet.worksheets()}
    required = {"users", "logs", "error_logs", "push_logs"}
    missing = required - available
    if missing:
        raise RuntimeError(f"必要なシートが見つかりません: {sorted(missing)}")


def _headers(sheet) -> list[str]:
    headers = sheet.row_values(1)
    if not headers:
        raise RuntimeError(f"シート「{sheet.title}」に1行目の見出しがありません。")
    return headers


def _append_by_header(sheet_name: str, values: dict[str, Any]) -> None:
    """辞書を、対象シートのヘッダー順に並べて1行追加する。"""
    sheet = _worksheet(sheet_name)
    headers = _headers(sheet)
    missing = set(values) - set(headers)
    if missing:
        raise RuntimeError(f"シート「{sheet_name}」に見つからない列があります: {sorted(missing)}")
    sheet.append_row([values.get(header, "") for header in headers], value_input_option="USER_ENTERED")


def get_user(user_id: str) -> dict[str, Any] | None:
    """ユーザーIDからユーザー情報を取得する。存在しない場合はNone。"""
    sheet = _worksheet("users")
    records = sheet.get_all_records()
    for row in records:
        if str(row.get(USER_COLUMNS["user_id"], "")) == user_id:
            user = {internal: row.get(sheet_column, "") for internal, sheet_column in USER_COLUMNS.items()}
            return user
    return None


def save_user(user_id: str, status: str, updates: dict[str, Any] | None = None) -> dict[str, Any]:
    """ユーザーを新規作成または更新し、保存後の情報を返す。"""
    updates = updates or {}
    unknown = set(updates) - set(USER_COLUMNS)
    if unknown:
        raise ValueError(f"対応していないユーザー項目です: {sorted(unknown)}")

    sheet = _worksheet("users")
    headers = _headers(sheet)
    user_id_column = USER_COLUMNS["user_id"]
    status_column = USER_COLUMNS["status"]
    updated_at_column = USER_COLUMNS["updated_at"]

    required = {user_id_column, status_column, updated_at_column}
    missing_headers = required - set(headers)
    if missing_headers:
        raise RuntimeError(f"usersシートに必要な列がありません: {sorted(missing_headers)}")

    all_rows = sheet.get_all_values()
    user_id_index = headers.index(user_id_column)
    row_number = next(
        (
            index
            for index, row in enumerate(all_rows[1:], start=2)
            if len(row) > user_id_index and str(row[user_id_index]) == user_id
        ),
        None,
    )

    sheet_values = {USER_COLUMNS[key]: value for key, value in updates.items()}
    sheet_values[user_id_column] = user_id
    sheet_values[status_column] = status
    sheet_values[updated_at_column] = _now()

    if row_number is None:
        _append_by_header("users", sheet_values)
    else:
        # 行全体を読んで、変更する列だけを上書きする。
        existing = all_rows[row_number - 1]
        existing += [""] * (len(headers) - len(existing))
        for column, value in sheet_values.items():
            existing[headers.index(column)] = value
        sheet.update(f"A{row_number}", [existing])

    saved = get_user(user_id)
    if saved is None:
        raise RuntimeError("usersシートへの保存後にユーザーを読み取れませんでした。")
    return saved


def save_log(
    user_id: str,
    user_name: str,
    menu_name: str,
    calories: float,
    protein: float,
    fat: float,
    carbs: float,
    advice: str,
    *,
    log_type: str = "食事",
    image_url: str = "",
    fiber: float = 0,
    vitamins: float = 0,
    vit_a: float = 0,
    vit_c: float = 0,
    zinc: float = 0,
    magnesium: float = 0,
    iron: float = 0,
    potassium: float = 0,
    calcium: float = 0,
) -> None:
    _append_by_header(
        "logs",
        {
            "timestamp": _now(),
            "user_id": user_id,
            "user_name": user_name,
            "type": log_type,
            "menu_name": menu_name,
            "calories": calories,
            "protein": protein,
            "fat": fat,
            "carbs": carbs,
            "imgUrl": image_url,
            "advice": advice,
            "fiber": fiber,
            "vitamins": vitamins,
            "vit_a": vit_a,
            "vit_c": vit_c,
            "zinc": zinc,
            "magnesium": magnesium,
            "iron": iron,
            "potassium": potassium,
            "calcium": calcium,
        },
    )


def get_today_logs(user_id: str) -> list[dict[str, Any]]:
    """日本時間で今日の食事ログだけを返す。"""
    today = datetime.now(JST).date()
    records = _worksheet("logs").get_all_records()
    result = []
    for row in records:
        if str(row.get("user_id", "")) != user_id:
            continue
        log_date = _date_from_sheet(row.get("timestamp"))
        if log_date == today:
            result.append(row)
    return result


def get_last_log(user_id: str) -> tuple[int, dict[str, Any]] | None:
    """ユーザーの一番新しいログのシート行番号と内容を返す。"""
    sheet = _worksheet("logs")
    rows = sheet.get_all_records()
    for row_number in range(len(rows) + 1, 1, -1):
        row = rows[row_number - 2]
        if str(row.get("user_id", "")) == user_id:
            return row_number, row
    return None


def update_last_log(
    user_id: str,
    menu_name: str,
    calories: float,
    protein: float,
    fat: float,
    carbs: float,
    advice: str,
    *,
    fiber: float = 0,
    vitamins: float = 0,
    vit_a: float = 0,
    vit_c: float = 0,
    zinc: float = 0,
    magnesium: float = 0,
    iron: float = 0,
    potassium: float = 0,
    calcium: float = 0,
) -> bool:
    last_log = get_last_log(user_id)
    if last_log is None:
        return False
    row_number, _ = last_log
    sheet = _worksheet("logs")
    headers = _headers(sheet)
    updates = {
        "menu_name": menu_name,
        "calories": calories,
        "protein": protein,
        "fat": fat,
        "carbs": carbs,
        "advice": advice,
        "fiber": fiber,
        "vitamins": vitamins,
        "vit_a": vit_a,
        "vit_c": vit_c,
        "zinc": zinc,
        "magnesium": magnesium,
        "iron": iron,
        "potassium": potassium,
        "calcium": calcium,
    }
    for column, value in updates.items():
        if column in headers:
            sheet.update_cell(row_number, headers.index(column) + 1, value)
    return True


def save_push_log(user_id: str, reason: str) -> None:
    _append_by_header("push_logs", {"Timestamp": _now(), "User ID": user_id, "Reason": reason})


def save_error_log(user_id: str | None, function_name: str, error_message: str) -> None:
    _append_by_header(
        "error_logs",
        {
            "timestamp": _now(),
            "user_id": user_id or "",
            "function_name": function_name,
            "error_message": error_message,
        },
    )