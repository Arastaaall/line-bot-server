import nest_asyncio
nest_asyncio.apply()

import os
import asyncio
import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient, MessagingApi, Configuration, 
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.webhooks import FollowEvent, ImageMessageContent, MessageEvent, TextMessageContent
import sheets

app = FastAPI()

# 環境変数
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

parser = WebhookParser(CHANNEL_SECRET)

# 非同期APIではなく、安定した同期APIクライアントを使用（これでイベントループエラーが完全に消えます）
config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(config)
line_messaging_api = MessagingApi(api_client)


@app.get("/ping", response_class=PlainTextResponse)
async def ping():
    """UptimeRobot用。外部APIを呼ばず、サーバーが起きていることだけを返す。"""
    return "OK"

@app.get("/health")
async def health_check():
    """Renderがサーバーの生存確認に使う、鍵を見せない安全な確認口。"""
    required = [
        "LINE_CHANNEL_SECRET",
        "LINE_CHANNEL_ACCESS_TOKEN",
        "GEMINI_API_KEY",
        "SPREADSHEET_ID",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    return {"status": "ok" if not missing else "configuration_incomplete", "missing_count": len(missing)}


@app.get("/health/sheets")
async def sheets_health_check():
    """Googleスプレッドシートを読むだけで、接続と必要なシート名を確認する。"""
    try:
        sheets.verify_connection()
    except Exception:
        # 鍵やスプレッドシートIDなどの詳しい情報は、公開URLへ返さない。
        raise HTTPException(status_code=503, detail="Googleスプレッドシートへ接続できませんでした。")
    return {"status": "ok", "sheets_connected": True}

def send_reply_sync(reply_token, text):
    """LINE Reply送信（スレッド安全な同期処理）"""
    line_messaging_api.reply_message(
        ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)])
    )


def send_reply_with_quick_replies_sync(reply_token, text):
    """LINE公式の小さな選択ボタンを付けて返信する。"""
    items = [
        {"type": "action", "action": {"type": "message", "label": "📊 カロリー確認", "text": "カロリー"}},
        {"type": "action", "action": {"type": "message", "label": "🌙 一日を振り返る", "text": "振り返る"}},
        {"type": "action", "action": {"type": "message", "label": "💡 使い方", "text": "使い方"}},
    ]
    payload = {"replyToken": reply_token, "messages": [{"type": "text", "text": text, "quickReply": {"items": items}}]}
    request = urllib.request.Request(
        "https://api.line.me/v2/bot/message/reply",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20):
        pass

def send_push_sync(user_id, text):
    """LINE Push送信（スレッド安全な同期処理）"""
    line_messaging_api.push_message(
        PushMessageRequest(to=user_id, messages=[TextMessage(text=text)])
    )


def show_loading_sync(user_id, seconds=5):
    """LINEのチャット画面に、Botが考え中である表示を出す。"""
    payload = {"chatId": user_id, "loadingSeconds": seconds}
    request = urllib.request.Request(
        "https://api.line.me/v2/bot/chat/loading/start",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
    except (urllib.error.HTTPError, urllib.error.URLError):
        # ローディング表示の失敗は、本体の返信を止めない。
        pass


KCAL_PER_KG = 7200  # 体重1kgの増減に必要なカロリー差の目安値
LOSS_MAX_DAILY_DEFICIT = 750  # 減量時、1日あたりに削ってよいカロリーの安全上限
GAIN_MAX_DAILY_SURPLUS = 500  # 増量時、1日あたりに増やしてよいカロリーの安全上限（体脂肪の増えすぎを防ぐ）
DEFAULT_TARGET_MONTHS = 3  # target_monthsが未設定のときに使う目安期間
GOAL_TOLERANCE_KG = 0.5  # この範囲内の体重差は「維持」とみなす


def is_premium_user(user):
    """is_premium列の値を、有料会員かどうかの真偽値として扱う。"""
    value = str(user.get("is_premium") or "").strip().lower()
    return value in ("true", "1", "yes", "premium", "有料")


def determine_goal_mode(weight, target_weight, tolerance=GOAL_TOLERANCE_KG):
    """現体重と目標体重の差から、維持・減量・増量のどれを目指しているか判定する。"""
    diff = target_weight - weight
    if abs(diff) < tolerance:
        return "maintain"
    return "gain" if diff > 0 else "loss"


def calculate_target_calories(user):
    """goal_mode（維持・減量・増量）に応じて、1日の目標カロリーを計算する。

    減量・増量は、目標体重までの差とtarget_months（達成希望期間）から
    1日あたりに必要な過不足カロリーを逆算するが、健康を害するような
    急激なペースにならないよう、安全な範囲の上限でキャップする。
    """
    gender = user["gender"]
    age = float(user["age"])
    height = float(user["height"])
    weight = float(user["weight"])
    target_weight = (
        float(user["target_weight"]) if user.get("target_weight") not in ("", None) else weight
    )
    waist = float(user["waist"]) if user.get("waist") not in ("", None) else None
    pal = float(user.get("pal") or 1.375)

    if waist:
        if gender == "男性":
            body_fat = ((4.15 * waist / 2.54) - (0.082 * weight * 2.2) - 98.42) / (weight * 2.2) * 100
        else:
            body_fat = ((4.15 * waist / 2.54) - (0.082 * weight * 2.2) - 76.76) / (weight * 2.2) * 100
        body_fat = max(5, min(body_fat, 50))
        bmr = 370 + (21.6 * weight * (1 - body_fat / 100))
    elif gender == "男性":
        bmr = (10 * weight) + (6.25 * height) - (5 * age) + 5
    else:
        bmr = (10 * weight) + (6.25 * height) - (5 * age) - 161

    tdee = round(bmr * pal)
    goal_mode = user.get("goal_mode") or determine_goal_mode(weight, target_weight)
    months = float(user.get("target_months") or DEFAULT_TARGET_MONTHS)
    days = max(months, 0.5) * 30

    if goal_mode == "loss":
        diff_kg = max(weight - target_weight, 0)
        requested_daily_change = (diff_kg * KCAL_PER_KG) / days
        safe_daily_change = min(requested_daily_change, LOSS_MAX_DAILY_DEFICIT)
        target_calories = max(round(bmr), round(tdee - safe_daily_change))
    elif goal_mode == "gain":
        diff_kg = max(target_weight - weight, 0)
        requested_daily_change = (diff_kg * KCAL_PER_KG) / days
        safe_daily_change = min(requested_daily_change, GAIN_MAX_DAILY_SURPLUS)
        target_calories = round(tdee + safe_daily_change)
    else:
        target_calories = tdee

    return {"tdee": tdee, "target_calories": target_calories}


def build_pace_note(user):
    """希望期間が安全なペースを超えていた場合、実際にかかる目安期間を伝える一言を作る。"""
    goal_mode = user.get("goal_mode")
    if goal_mode not in ("loss", "gain"):
        return ""

    weight = float(user["weight"])
    target_weight = float(user["target_weight"]) if user.get("target_weight") not in ("", None) else weight
    diff_kg = abs(target_weight - weight)
    if diff_kg <= 0:
        return ""

    months = float(user.get("target_months") or DEFAULT_TARGET_MONTHS)
    days = max(months, 0.5) * 30
    requested_daily_change = (diff_kg * KCAL_PER_KG) / days
    cap = LOSS_MAX_DAILY_DEFICIT if goal_mode == "loss" else GAIN_MAX_DAILY_SURPLUS

    if requested_daily_change <= cap:
        return ""

    actual_months = (diff_kg * KCAL_PER_KG) / cap / 30
    action = "減量" if goal_mode == "loss" else "増量"
    return (
        f"\n⚠️ ご希望の期間（{months}ヶ月）だと健康的な{action}ペースを超えてしまうため、"
        f"安全な範囲のカロリーで計算しました。実際には目安として約{actual_months:.1f}ヶ月かかる見込みです。\n"
    )


def number_or_none(text, minimum, maximum):
    """数字として読め、指定範囲内ならその数字を返す。そうでなければNone。"""
    try:
        value = float(text)
    except ValueError:
        return None
    if not minimum <= value <= maximum:
        return None
    return value


def initial_setup_message(user_id, text):
    """初期設定の質問を1つ進め、ユーザーへ返す文章を作る。"""
    text = text.strip()
    user = sheets.get_user(user_id)

    if text == "リセット" or user is None:
        sheets.save_user(user_id, "ask_gender")
        return "初期設定を始めます。\n\n性別を教えてください。（「男性」または「女性」）"

    status = user.get("status")
    if status == "ask_gender":
        if text not in ("男性", "女性"):
            return "「男性」または「女性」で教えてください。"
        sheets.save_user(user_id, "ask_age", {"gender": text})
        return "年齢を教えてください。（例: 30）"

    if status == "ask_age":
        value = number_or_none(text, 10, 120)
        if value is None:
            return "年齢は10〜120の半角数字で教えてください。（例: 30）"
        sheets.save_user(user_id, "ask_height", {"age": int(value)})
        return "身長（cm）を教えてください。（例: 170）"

    if status == "ask_height":
        value = number_or_none(text, 80, 250)
        if value is None:
            return "身長は80〜250の半角数字で教えてください。（例: 170）"
        sheets.save_user(user_id, "ask_weight", {"height": value})
        return "現在の体重（kg）を教えてください。（例: 70.5）"

    if status == "ask_weight":
        value = number_or_none(text, 20, 400)
        if value is None:
            return "体重は20〜400の半角数字で教えてください。（例: 70.5）"
        sheets.save_user(user_id, "ask_target_weight", {"weight": value})
        return "目標とする体重（kg）を教えてください。（例: 65）"

    if status == "ask_target_weight":
        value = number_or_none(text, 20, 400)
        if value is None:
            return "目標体重は20〜400の半角数字で教えてください。（例: 65）"
        sheets.save_user(user_id, "ask_waist", {"target_weight": value})
        return "腹囲（cm）を教えてください。（例: 80）\nメジャーがない場合は「パス」と送ってください。"

    if status == "ask_waist":
        if text == "パス":
            waist = ""
        else:
            waist = number_or_none(text, 30, 250)
            if waist is None:
                return "腹囲は30〜250の半角数字、または「パス」で教えてください。"
        user_with_waist = sheets.save_user(user_id, "ask_waist", {"waist": waist})
        weight = float(user_with_waist["weight"])
        target_weight = float(user_with_waist["target_weight"])
        goal_mode = determine_goal_mode(weight, target_weight)

        if goal_mode == "gain" and not is_premium_user(user_with_waist):
            # 無料会員は増量プラン対象外のため、体重維持として計算し、有料プランを案内する。
            completed_user = sheets.save_user(user_id, "completed", {"goal_mode": "maintain"})
            calories = calculate_target_calories(completed_user)
            sheets.save_user(user_id, "completed", calories)
            return (
                "設定が完了しました！🎉\n\n"
                f"1日の目標摂取カロリー：【 {calories['target_calories']} kcal 】（体重維持モード）\n\n"
                "増量プランは有料会員限定の機能です。目標体重に向けた増量サポートをご希望の場合は、"
                "有料プランへの登録をご検討ください。\n\n"
                "次は食事写真を送ると、カロリーを記録できます。"
            )

        if goal_mode == "maintain":
            completed_user = sheets.save_user(user_id, "completed", {"goal_mode": "maintain"})
            calories = calculate_target_calories(completed_user)
            sheets.save_user(user_id, "completed", calories)
            return (
                "設定が完了しました！🎉\n\n"
                f"1日の目標摂取カロリー：【 {calories['target_calories']} kcal 】（体重維持モード）\n\n"
                "次は食事写真を送ると、カロリーを記録できます。"
            )

        sheets.save_user(user_id, "ask_target_months", {"goal_mode": goal_mode})
        return "目標体重までの達成希望期間を、月数で教えてください。（例: 3）\n※無理のないペースになるよう、自動で調整されます。"

    if status == "ask_target_months":
        value = number_or_none(text, 0.5, 24)
        if value is None:
            return "達成希望期間は0.5〜24の半角数字（月数）で教えてください。（例: 3）"
        completed_user = sheets.save_user(user_id, "completed", {"target_months": value})
        calories = calculate_target_calories(completed_user)
        sheets.save_user(user_id, "completed", calories)
        mode_label = "減量モード" if completed_user.get("goal_mode") == "loss" else "増量モード"
        pace_note = build_pace_note(completed_user)
        return (
            "設定が完了しました！🎉\n\n"
            f"1日の目標摂取カロリー：【 {calories['target_calories']} kcal 】（{mode_label}）\n"
            f"{pace_note}\n"
            "次は食事写真を送ると、カロリーを記録できます。"
        )

    if text in ("使い方", "つかいかた", "ヘルプ", "ガイド"):
        return "食事写真を送ると、料理とカロリーを記録します。判定の直後なら、料理名や量を送って修正できます。設定をやり直すときは「リセット」と送ってください。"
    if text in ("総", "総合", "トータル", "カロリー", "本日", "今日", "合計", "確認"):
        if status == "awaiting_correction":
            sheets.save_user(user_id, "completed")
        return build_today_summary(user)
    if text in ("振り返る", "振り返り"):
        if status == "awaiting_correction":
            sheets.save_user(user_id, "completed")
        return build_today_reflection(user_id, user)
    if status == "awaiting_correction":
        if correction_is_open(user):
            return correct_last_meal(user_id, user, text)
        sheets.save_user(user_id, "completed")
    return "設定は完了しています。食事写真を送ってください。設定をやり直すときは「リセット」と送ってください。"


def correction_is_open(user):
    """解析結果を修正できる5分間が、まだ終わっていないか確認する。"""
    updated_at = str(user.get("updated_at") or "")
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            updated = datetime.strptime(updated_at, pattern)
            now_jst = datetime.now(ZoneInfo("Asia/Tokyo")).replace(tzinfo=None)
            return now_jst - updated <= timedelta(minutes=5)
        except ValueError:
            pass
    return False


def build_today_summary(user):
    """今日のカロリー合計と、目標までの残りを伝える。"""
    logs = sheets.get_today_logs(user["user_id"])
    total = sum(float(log.get("calories") or 0) for log in logs)
    target = float(user.get("target_calories") or 0)
    remaining = round(target - total)
    message = (
        "📊 【本日の摂取状況】\n\n"
        f"本日累計: {round(total)} / {round(target)} kcal\n"
        f"残り可変枠: {remaining} kcal\n\n"
    )
    if remaining >= 0:
        return message + f"目標まであと {remaining} kcal です。無理のない範囲で続けましょう。"
    return message + f"目標を {abs(remaining)} kcal オーバーしています。無理のない範囲で調整しましょう。"


def build_today_reflection(user_id, user):
    """今日に記録した料理とPFCの合計を、短い文章で振り返る。"""
    logs = sheets.get_today_logs(user_id)
    if not logs:
        return "本日の記録はまだありません。食事写真を送ると、ここに記録されます。"
    total_calories = sum(float(log.get("calories") or 0) for log in logs)
    total_protein = sum(float(log.get("protein") or 0) for log in logs)
    total_fat = sum(float(log.get("fat") or 0) for log in logs)
    total_carbs = sum(float(log.get("carbs") or 0) for log in logs)
    menu_list = "".join(f"・{log.get('menu_name', '食事')}（約{log.get('calories', 0)}kcal）\n" for log in logs)
    target = float(user.get("target_calories") or 0)
    remaining = round(target - total_calories)
    comment = "目標内に収まっています。この調子でいきましょう。" if remaining >= 0 else "少しオーバーしています。明日は無理のない範囲で調整しましょう。"
    return (
        "【本日の振り返り】\n\n"
        f"{menu_list}\n"
        f"合計カロリー: {round(total_calories)} / {round(target)} kcal\n"
        f"残り可変枠: {remaining} kcal\n"
        f"(P:{round(total_protein, 1)}g / F:{round(total_fat, 1)}g / C:{round(total_carbs, 1)}g)\n\n"
        f"{comment}"
    )


async def process_follow_event(event):
    user_id = event.source.user_id
    message = await asyncio.to_thread(initial_setup_message, user_id, "リセット")
    await asyncio.to_thread(send_reply_sync, event.reply_token, message)


async def process_text_event(event):
    user_id = event.source.user_id
    await asyncio.to_thread(show_loading_sync, user_id, 5)
    try:
        message = await asyncio.to_thread(initial_setup_message, user_id, event.message.text)
    except Exception as exc:
        try:
            await asyncio.to_thread(sheets.save_error_log, user_id, "process_text_event", str(exc))
        except Exception:
            pass
        message = "処理中に問題が起きました。少し時間をおいて、もう一度送ってください。"
    current_user = await asyncio.to_thread(sheets.get_user, user_id)
    if current_user and current_user.get("status") in ("completed", "awaiting_correction"):
        await asyncio.to_thread(send_reply_with_quick_replies_sync, event.reply_token, message)
    else:
        await asyncio.to_thread(send_reply_sync, event.reply_token, message)

MEDICAL_GUARDRAIL = (
    "\n\n【重要な制約】\n"
    "・特定の疾患名を挙げた診断や、治療方針を断定する表現は行わないこと。\n"
    "・あくまで一般的な栄養バランスの観点からの参考アドバイスに留めること。\n"
    "・体調不良や持病が疑われる内容の場合は、医師や専門家への相談を勧めること。"
)


def fetch_line_image(message_id):
    """LINEに一時保存されている画像を、メモリ上へ読み込む。"""
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            mime_type = response.headers.get_content_type() or "image/jpeg"
            return response.read(), mime_type
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"LINEから画像を取得できませんでした（HTTP {exc.code}）。") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("LINEから画像を取得できませんでした。") from exc


def analyze_image(image_bytes, mime_type):
    """Geminiへ食事写真を渡し、保存できる形の結果だけを返す。"""
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    prompt = (
        "この画像に写っている食事を解析してください。\n"
        "推定される料理名、おおよそのカロリー（kcal）、PFCバランス（g）、"
        "微量栄養素の推定値、次にとるべき食事のアドバイスを、次のJSON形式のみで返してください。\n"
        "微量栄養素はすべて推定値で構いません。数値のみ（単位は付けない）で返してください。\n\n"
        "{\n"
        '  "menu_name": "料理名",\n'
        '  "calories": 600,\n'
        '  "protein": 20,\n'
        '  "fat": 15,\n'
        '  "carbs": 80,\n'
        '  "fiber": 3,\n'
        '  "vitamin": 5,\n'
        '  "vit_a": 80,\n'
        '  "vit_c": 15,\n'
        '  "zinc": 1.5,\n'
        '  "magnesium": 40,\n'
        '  "iron": 1.2,\n'
        '  "potassium": 400,\n'
        '  "calcium": 60,\n'
        '  "suggestion": "アドバイスメッセージ"\n'
        "}"
        + MEDICAL_GUARDRAIL
    )
    payload = {
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode("ascii")}},
        ]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    encoded_key = urllib.parse.quote(GEMINI_API_KEY or "", safe="")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={encoded_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Geminiの解析に失敗しました（HTTP {exc.code}）。") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError("Geminiの解析結果を受け取れませんでした。") from exc

    return parse_analysis_result(response_json)


#  微量栄養素はGeminiが省略することがあるため、必須項目には含めず、
# 数値変換に失敗した場合や欠けている場合は0として扱う。
MICRONUTRIENT_KEYS = (
    "fiber", "vitamin", "vit_a", "vit_c", "zinc", "magnesium", "iron", "potassium", "calcium",
)


def _number_or_zero(value, digits=1):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return 0


def parse_analysis_result(response_json):
    """Geminiの返事から、保存に必要な値（主要栄養素6つ＋微量栄養素9つ）を取り出す。"""
    try:
        raw_text = response_json["candidates"][0]["content"]["parts"][0]["text"]
        first_brace, last_brace = raw_text.find("{"), raw_text.rfind("}")
        data = json.loads(raw_text[first_brace:last_brace + 1])
        required = {"menu_name", "calories", "protein", "fat", "carbs", "suggestion"}
        if first_brace < 0 or last_brace < first_brace or not required.issubset(data):
            raise ValueError("必要な項目がありません")
        result = {
            "menu_name": str(data["menu_name"]).strip(),
            "calories": round(float(data["calories"])),
            "protein": round(float(data["protein"]), 1),
            "fat": round(float(data["fat"]), 1),
            "carbs": round(float(data["carbs"]), 1),
            "suggestion": str(data["suggestion"]).strip(),
        }
        for key in MICRONUTRIENT_KEYS:
            result[key] = _number_or_zero(data.get(key))
        return result
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Geminiの解析結果の形式が正しくありませんでした。") from exc


def re_analyze_meal(previous_menu, correction_text):
    """ユーザーの補足を使い、直前の食事をもう一度計算する。"""
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    prompt = (
        f"直前の判定メニュー: 「{previous_menu}」\n"
        f"ユーザーからの修正・補足入力: 「{correction_text}」\n\n"
        "ユーザーの入力は直前の食事への補足や一部修正です。元の食事に含まれていた要素は原則保持し、"
        "修正後の全体の料理名、カロリー、PFC、微量栄養素の推定値、助言を次のJSON形式のみで返してください。\n"
        "微量栄養素はすべて推定値で構いません。数値のみ（単位は付けない）で返してください。\n\n"
        "{\n"
        '  "menu_name": "料理名",\n'
        '  "calories": 600,\n'
        '  "protein": 20,\n'
        '  "fat": 15,\n'
        '  "carbs": 80,\n'
        '  "fiber": 3,\n'
        '  "vitamin": 5,\n'
        '  "vit_a": 80,\n'
        '  "vit_c": 15,\n'
        '  "zinc": 1.5,\n'
        '  "magnesium": 40,\n'
        '  "iron": 1.2,\n'
        '  "potassium": 400,\n'
        '  "calcium": 60,\n'
        '  "suggestion": "アドバイスメッセージ"\n'
        "}"
        + MEDICAL_GUARDRAIL
    )
    encoded_key = urllib.parse.quote(GEMINI_API_KEY or "", safe="")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={encoded_key}"
    request = urllib.request.Request(
        url,
        data=json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json"}}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return parse_analysis_result(json.loads(response.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Geminiの再計算に失敗しました（HTTP {exc.code}）。") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError("Geminiの再計算結果を受け取れませんでした。") from exc


def correct_last_meal(user_id, user, correction_text):
    """直前の食事ログを、Geminiの再計算結果へ入れ替える。"""
    last_log = sheets.get_last_log(user_id)
    if last_log is None:
        sheets.save_user(user_id, "completed")
        return "修正する食事記録が見つかりませんでした。食事写真を送ってください。"
    _, log = last_log
    result = re_analyze_meal(log.get("menu_name") or "", correction_text)
    sheets.update_last_log(
        user_id,
        result["menu_name"],
        result["calories"],
        result["protein"],
        result["fat"],
        result["carbs"],
        result["suggestion"],
        **{key: result[key] for key in MICRONUTRIENT_KEYS},
    )
    sheets.save_user(user_id, "completed")
    today_logs = sheets.get_today_logs(user_id)
    total = sum(float(item.get("calories") or 0) for item in today_logs)
    target = float(user.get("target_calories") or 0)
    return (
        "🔄 【修正・再計算結果】\n"
        f"メニュー: {result['menu_name']}\n"
        f"カロリー: 約{result['calories']} kcal\n"
        f"(P:{result['protein']}g / F:{result['fat']}g / C:{result['carbs']}g)\n\n"
        "【本日の状況】\n"
        f"本日累計: {round(total)} / {round(target)} kcal\n"
        f"残り可変枠: {round(target - total)} kcal\n\n"
        f"【次の食事の目安】\n{result['suggestion']}"
    )


def save_analysis_and_build_message(user_id, user, result):
    """解析結果をログへ保存し、ユーザーに返す文章を組み立てる。"""
    user_name = user.get("user_name") or "ユーザー"
    sheets.save_log(user_id, user_name, advice=result["suggestion"], **{
        key: result[key] for key in ("menu_name", "calories", "protein", "fat", "carbs")
    }, **{key: result[key] for key in MICRONUTRIENT_KEYS})
    sheets.save_user(user_id, "awaiting_correction")

    today_logs = sheets.get_today_logs(user_id)
    today_calories = sum(float(log.get("calories") or 0) for log in today_logs)
    target_calories = float(user.get("target_calories") or 0)
    remaining = round(target_calories - today_calories)
    return (
        "【判定結果】\n"
        f"メニュー: {result['menu_name']}\n"
        f"カロリー: 約{result['calories']} kcal\n"
        f"(P:{result['protein']}g / F:{result['fat']}g / C:{result['carbs']}g)\n\n"
        "【本日の状況】\n"
        f"本日累計: {round(today_calories)} / {round(target_calories)} kcal\n"
        f"残り可変枠: {remaining} kcal\n\n"
        f"【次の食事の目安】\n{result['suggestion']}\n\n"
        "内容が違う場合は、修正内容（例:「味噌ラーメン」「大盛り」）を送ってください。"
    )

@app.post("/callback")
async def handle_callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = (await request.body()).decode("utf-8")
    
    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="LINE署名を確認できませんでした。")

    for event in events:
        if isinstance(event, FollowEvent):
            await process_follow_event(event)
        elif isinstance(event, MessageEvent) and isinstance(event.message, TextMessageContent):
            await process_text_event(event)
        elif isinstance(event, MessageEvent) and isinstance(event.message, ImageMessageContent):
            asyncio.create_task(process_image_event(event))
            
    return "OK"

async def process_image_event(event):
    user_id = event.source.user_id
    reply_token = event.reply_token
    user = await asyncio.to_thread(sheets.get_user, user_id)
    if user is None or user.get("status") not in ("completed", "awaiting_correction"):
        await asyncio.to_thread(
            send_reply_sync,
            reply_token,
            "初期設定がまだ完了していません。「リセット」と送って設定を始めてください。",
        )
        return

    await asyncio.to_thread(show_loading_sync, user_id, 15)

    async def get_and_analyze():
        image_bytes, mime_type = await asyncio.to_thread(fetch_line_image, event.message.id)
        return await asyncio.to_thread(analyze_image, image_bytes, mime_type)

    task = asyncio.create_task(get_and_analyze())
    try:
        result = await asyncio.wait_for(asyncio.shield(task), timeout=15)
        message = await asyncio.to_thread(save_analysis_and_build_message, user_id, user, result)
        await asyncio.to_thread(send_reply_with_quick_replies_sync, reply_token, message)
    except asyncio.TimeoutError:
        await asyncio.to_thread(
            send_reply_sync,
            reply_token,
            "⏳ ただいま写真を解析しています。終わり次第、こちらへお知らせします。",
        )
        try:
            result = await task
            message = await asyncio.to_thread(save_analysis_and_build_message, user_id, user, result)
            await asyncio.to_thread(send_push_sync, user_id, message)
            await asyncio.to_thread(sheets.save_push_log, user_id, "画像解析の結果通知")
        except Exception as exc:
            try:
                await asyncio.to_thread(sheets.save_error_log, user_id, "process_image_event", str(exc))
            except Exception:
                pass
            await asyncio.to_thread(
                send_push_sync,
                user_id,
                "写真の解析に失敗しました。恐れ入りますが、もう一度写真を送ってください。",
            )
    except Exception as exc:
        try:
            await asyncio.to_thread(sheets.save_error_log, user_id, "process_image_event", str(exc))
        except Exception:
            pass
        await asyncio.to_thread(
            send_reply_sync,
            reply_token,
            "写真の解析に失敗しました。恐れ入りますが、もう一度写真を送ってください。",
        )