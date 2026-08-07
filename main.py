import nest_asyncio
nest_asyncio.apply()

import os
import asyncio
from fastapi import FastAPI, HTTPException, Request
from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    ApiClient, MessagingApi, Configuration, 
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.webhooks import FollowEvent, ImageMessageContent, MessageEvent, TextMessageContent
from google import genai
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

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


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

def send_push_sync(user_id, text):
    """LINE Push送信（スレッド安全な同期処理）"""
    line_messaging_api.push_message(
        PushMessageRequest(to=user_id, messages=[TextMessage(text=text)])
    )


def calculate_target_calories(user):
    """GAS版と同じルールで、1日の目標カロリーを計算する。"""
    gender = user["gender"]
    age = float(user["age"])
    height = float(user["height"])
    weight = float(user["weight"])
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
    target_calories = max(round(bmr), round(tdee - 650))
    return {"tdee": tdee, "target_calories": target_calories}


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
        completed_user = sheets.save_user(user_id, "completed", {"waist": waist})
        calories = calculate_target_calories(completed_user)
        sheets.save_user(user_id, "completed", calories)
        return (
            "設定が完了しました！🎉\n\n"
            f"1日の目標摂取カロリー：【 {calories['target_calories']} kcal 】\n\n"
            "次は食事写真を送ると、カロリーを記録できます。"
        )

    if text in ("使い方", "つかいかた", "ヘルプ", "ガイド"):
        return "食事写真を送ると、料理とカロリーを記録します。設定をやり直すときは「リセット」と送ってください。"
    return "設定は完了しています。食事写真を送ってください。設定をやり直すときは「リセット」と送ってください。"


async def process_follow_event(event):
    user_id = event.source.user_id
    message = await asyncio.to_thread(initial_setup_message, user_id, "リセット")
    await asyncio.to_thread(send_reply_sync, event.reply_token, message)


async def process_text_event(event):
    user_id = event.source.user_id
    message = await asyncio.to_thread(initial_setup_message, user_id, event.message.text)
    await asyncio.to_thread(send_reply_sync, event.reply_token, message)

async def analyze_image(image_bytes: bytes) -> str:
    """Gemini APIで画像を解析"""
    response = await asyncio.to_thread(
        gemini_client.models.generate_content,
        model='gemini-2.0-flash',
        contents=['この食事のメニュー名、概算カロリー、PFCバランスを簡潔に教えてください。', image_bytes]
    )
    return response.text

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
    # 画像解析は、LINEから本物の画像を受け取る処理を追加する次の工程で完成させる。
    await asyncio.to_thread(
        send_reply_sync,
        reply_token,
        "食事写真の解析機能は、ただいま移行作業中です。もう少しだけお待ちください。",
    )
