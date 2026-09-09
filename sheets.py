"""Google スプレッドシートを、このBotのデータベースとして扱うための部品。
このファイルでは列番号を使わず、1行目の見出し（ヘッダー）を見て読み書きする。
そのため、列の順番が変わってもデータを別の場所へ書き込む事故を防げる。
"""
import json
import os
import uuid
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
    "is_paid": "is_paid",  # 追加
    "goal_mode": "goal_mode",
    "pal": "pal(def:1.375)",
    "meal_style": "meal_style",
    "cravings_trigger": "cravings_trigger",
    "target_months": "target_months",
    "target_start_date": "target_end_date",  # ← 新規追加 plan開始日
    "target_end_date": "target_end_date",  # ← 新規追加　plan終了日
    "last_log_id": "last_log_id",
}

# 将来の列追加に備え、usersシートにまだ列が無くてもエラーにしない項目
# OPTIONAL_USER_KEYS にもis_paidを追加（列がまだ無くてもエラーにしない）
OPTIONAL_USER_KEYS = {"last_log_id", "is_paid"}
_OPTIONAL_USER_COLUMNS = {USER_COLUMNS[key] for key in OPTIONAL_USER_KEYS}

_FORMULA_TRIGGER_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

def _sheet_safe(value):
    """スプレッドシートで数式として解釈されうる先頭文字の値に、アポストロフィを付けて無害化する。
    Geminiが生成した文字列（menu_nameやadviceなど）が万一「=」などで始まっていても、
    別ツールでの閲覧・エクスポート時に数式として実行されるのを防ぐ（数式インジェクション対策）。
    """
    if isinstance(value, str) and value.startswith(_FORMULA_TRIGGER_PREFIXES):
        return "'" + value
    return value

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

def _append_by_header(sheet_name: str, values: dict[str, Any], optional_keys: set[str] | None = None) -> None:
    """辞書を、対象シートのヘッダー順に並べて1行追加する。
    optional_keys に含まれる項目は、シートにまだ列が無くても
    エラーにせず、その項目だけを外して書き込む（将来の列追加・列名変更に強くするため）。
    """
    sheet = _worksheet(sheet_name)
    headers = _headers(sheet)
    missing = set(values) - set(headers)
    if missing:
        required_missing = missing - (optional_keys or set())
        if required_missing:
            raise RuntimeError(f"シート「{sheet_name}」に見つからない列があります: {sorted(required_missing)}")
        values = {key: value for key, value in values.items() if key not in missing}
    sheet.append_row(
        [_sheet_safe(values.get(header, "")) for header in headers],
        value_input_option="USER_ENTERED",
    )

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
        # 新規行追加：optional_keysを渡して後方互換性を確保
        _append_by_header("users", sheet_values, optional_keys=_OPTIONAL_USER_COLUMNS)
        # 新規行の場合、書き込んだ値がそのまま全データ（他の列は空欄）になる。
        final_row_raw = {header: "" for header in headers}
        final_row_raw.update({k: v for k, v in sheet_values.items() if k in headers})
    else:
        # 行全体を読んで、変更する列だけを上書きする。
        # 【注意】_sheet_safe()で数式インジェクション対策の先頭アポストロフィを付けた文字列は
        # 「実際にシートに書き込む用」の一時変数(existing)にだけ使い、戻り値の組み立てには
        # 使わない（アポストロフィはGoogle Sheets側で書式指定として解釈され実際のセル値には
        # 残らないため、ここでraw値のまま保持しないとget_user()の結果と食い違ってしまう）。
        existing = all_rows[row_number - 1]
        existing += [""] * (len(headers) - len(existing))
        final_row_raw = dict(zip(headers, existing))  # 書き込み前の生値ベース
        for column, value in sheet_values.items():
            if column not in headers:
                if column in _OPTIONAL_USER_COLUMNS:
                    continue  # 列が未追加なら黙ってスキップ（後方互換）
                raise RuntimeError(f"usersシートに必要な列がありません: {column}")
            existing[headers.index(column)] = _sheet_safe(value)
            final_row_raw[column] = value  # 戻り値用には生の値を反映
        sheet.update(f"A{row_number}", [existing])

    # 【軽量化】以前はここで get_user(user_id) を呼び、usersシートを丸ごと
    # もう一度読み直していた（＝1回のsave_userにつきSheets読み取りAPIをもう1回消費）。
    # 今書き込んだ値は手元の final_row_raw に既にあるので、シート全体の再読み込みは行わず
    # その場でUSER_COLUMNS形式の辞書に組み立てて返す。
    saved = {
        internal: final_row_raw.get(sheet_column, "")
        for internal, sheet_column in USER_COLUMNS.items()
    }
    return saved

# 【Phase4.1】栄養素キーの正本。以前はここ（MICRONUTRIENT_COLUMNS）と
# main.pyのMICRONUTRIENT_KEYS、save_log()/update_last_log()の個別キーワード引数の
# 3箇所に同じ栄養素名リストが分散しており、追加・削除時に直し漏れが起きやすかった。
# 以後はこのタプルを唯一の正本とし、main.py側はここからimportして使う。
MICRONUTRIENT_KEYS = (
    "fiber", "vitamins", "vit_a", "vit_c", "zinc", "magnesium", "iron", "potassium", "calcium",
    "vit_d", "vit_e", "vit_b1", "vit_b2", "vit_b6", "vit_b12", "folate",
)
MICRONUTRIENT_COLUMNS = set(MICRONUTRIENT_KEYS)

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
    micronutrients: dict[str, float] | None = None,
) -> str:
    """ログを保存し、log_idを返す。

    【Phase4.1】以前は微量栄養素を9個の個別キーワード引数として受け取っていたため、
    栄養素が増えるたびにこの関数のシグネチャ自体を書き換える必要があった。
    以後はmicronutrients辞書で一括受け取りにし、MICRONUTRIENT_KEYSに存在するキーだけを
    書き込む（未指定のキーは0として補完する）。
    """
    micronutrients = micronutrients or {}
    log_id = uuid.uuid4().hex[:12]
    row = {
        "timestamp": _now(),
        "log_id": log_id,
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
    }
    for key in MICRONUTRIENT_KEYS:
        row[key] = micronutrients.get(key, 0)
    _append_by_header(
        "logs",
        row,
        optional_keys=MICRONUTRIENT_COLUMNS | {"log_id"},
    )
    return log_id

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
    """ユーザーの一番新しいログのシート行番号と内容を返す（後方互換用）。"""
    sheet = _worksheet("logs")
    rows = sheet.get_all_records()
    for row_number in range(len(rows) + 1, 1, -1):
        row = rows[row_number - 2]
        if str(row.get("user_id", "")) == user_id:
            return row_number, row
    return None

def get_log_by_id(user_id: str, log_id: str) -> tuple[int, dict[str, Any]] | None:
    """log_idでlogsシートの行を特定する。見つからなければNone。
    末尾から探索することで、通常は数件のスキャンでヒットする
    （直前のログを探すケースがほとんどのため）。
    """
    if not log_id:
        return None
    sheet = _worksheet("logs")
    rows = sheet.get_all_records()
    for row_number in range(len(rows) + 1, 1, -1):
        row = rows[row_number - 2]
        if str(row.get("user_id", "")) == user_id and str(row.get("log_id", "")) == str(log_id):
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
    log_id: str | None = None,
    micronutrients: dict[str, float] | None = None,
) -> bool:
    """log_id指定、または最後のログを更新する。

    【Phase4.1】save_log()と同じ理由で、微量栄養素は個別キーワード引数ではなく
    micronutrients辞書で受け取る形に統一した。
    """
    # 【優先1修正】log_idが指定されている場合はそのIDのみを検索し、見つからなければ失敗とする。
    # 勝手に別の最新ログを修正してしまう危険を防ぐ。
    if log_id:
        found = get_log_by_id(user_id, log_id)
    else:
        # log_id未対応の旧データのみ、後方互換として最後のログを使う
        found = get_last_log(user_id)

    if found is None:
        return False
    
    row_number, _ = found
    sheet = _worksheet("logs")
    headers = _headers(sheet)
    micronutrients = micronutrients or {}
    updates = {
        "menu_name": menu_name,
        "calories": calories,
        "protein": protein,
        "fat": fat,
        "carbs": carbs,
        "advice": advice,
    }
    for key in MICRONUTRIENT_KEYS:
        updates[key] = micronutrients.get(key, 0)
    # 【軽量化】以前はここで列ごとに update_cell() を個別に呼んでおり、
    # 微量栄養素項目＋主要項目を合わせると1回の修正で最大十数回もの
    # 書き込みAPIコールが発生していた（無料枠のクォータ・応答速度の両方を圧迫する）。
    # gspreadのbatch_update()で1回のAPI呼び出しにまとめる。
    # 列が連続しているとは限らないため、セル単位のrange指定を複数まとめて1リクエストにする。
    batch_data = []
    for column, value in updates.items():
        if column in headers:
            col_index = headers.index(column) + 1  # 1-indexed
            a1 = gspread.utils.rowcol_to_a1(row_number, col_index)
            batch_data.append({"range": a1, "values": [[_sheet_safe(value)]]})
    if batch_data:
        sheet.batch_update(batch_data, value_input_option="USER_ENTERED")
    return True

def save_push_log(user_id: str, reason: str) -> None:
    _append_by_header("push_logs", {"Timestamp": _now(), "User ID": user_id, "Reason": reason})

def save_error_log(
    user_id: str | None,
    function_name: str,
    error_message: str,
    *,
    stage: str = "",
) -> None:
    """エラーログを保存する（例外を握りつぶさない）。

    【修正】以前は _append_by_header をtry内と、その直後（try/exceptの外）の
    計2回呼んでいたため、エラーが起きるたびに同じ行が2行ずつerror_logsシートに
    書き込まれ、Sheets APIの呼び出し回数も無駄に倍になっていた。1回だけ書き込む。

    【Phase4.1】stage引数を追加。栄養素抽出のGeminiプロンプトを4経路
    （text:meal_add / text:meal_correction / text:legacy_classify /
    image:main / image:groq_fallback 等）で共通化した結果、function_nameだけでは
    どの経路で失敗したのか区別しづらくなったため、呼び出し元が任意でstageを渡せるようにした。
    stage省略時は空文字のまま書き込む（既存の呼び出し箇所は変更不要・後方互換）。
    error_logsシートにstage列が無い場合でも _append_by_header の optional_keys指定により
    エラーにはならず、その項目だけ外して書き込まれる。
    """
    try:
        import traceback
        # エラーメッセージにスタックトレースが含まれていない場合は追加
        if "Traceback" not in error_message:
            current_trace = traceback.format_exc()
            # save_error_logは別スレッドから呼ばれることがあり、その場合は
            # ここにアクティブな例外がなく「NoneType: None」だけになる。
            if current_trace.strip() != "NoneType: None":
                error_message = f"{error_message}\n{current_trace}"

        _append_by_header(
            "error_logs",
            {
                "timestamp": _now(),
                "user_id": user_id or "",
                "function_name": function_name,
                "stage": stage,
                "error_message": error_message,
            },
            optional_keys={"stage"},
        )
    except Exception as e:
        # Sheetsへの保存自体に失敗した場合は、標準エラー出力に記録
        print(f"ERROR: save_error_log failed: {e}")
        print(f"Original error: {error_message}")
