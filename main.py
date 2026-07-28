import nest_asyncio
nest_asyncio.apply()

import os
import asyncio
from fastapi import FastAPI, Request, HTTPException
from linebot.v3 import WebhookParser
from linebot.v3.messaging import (
    AsyncApiClient, AsyncMessagingApi, Configuration, 
    ReplyMessageRequest, PushMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, ImageMessageContent
from google import genai
import sheets

app = FastAPI()

# 環境変数
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

parser = WebhookParser(CHANNEL_SECRET)
config = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
async_api_client = AsyncApiClient(config)
line_messaging_api = AsyncMessagingApi(async_api_client)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

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
    
    events = parser.parse(body, signature)
    for event in events:
        if isinstance(event, MessageEvent) and isinstance(event.message, ImageMessageContent):
            asyncio.create_task(process_image_event(event))
            
    return "OK"

async def process_image_event(event):
    user_id = event.source.user_id
    reply_token = event.reply_token
    message_id = event.message.id

    # LINEから画像データをバイナリで取得
    # ※SDK経由で画像を取得する処理
    # (ここでは簡略化のため処理の流れを記述)
    # image_bytes = await get_line_image_bytes(message_id)

    # 1. Gemini解析タスク開始
    task = asyncio.create_task(analyze_image(image_bytes))

    try:
        # 2. 7秒のタイムアウト付きで結果を待つ
        result_text = await asyncio.wait_for(asyncio.shield(task), timeout=7.0)
        
        # 7秒以内に完了した場合 ➔ 無料のReplyで返信
        await line_messaging_api.reply_message(
            ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=result_text)])
        )
        sheets.save_to_sheet(user_id, result_text)

    except asyncio.TimeoutError:
        # 7秒を超えた場合 ➔ 遅延アナウンスをReplyで返し、完了後にPushで送信
        await line_messaging_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token, 
                messages=[TextMessage(text="⏳ 解析に少し時間がかかっております。完了次第、Push通知でお知らせします！")]
            )
        )
        
        # バックグラウンドで解析完了を待つ
        result_text = await task
        
        # Push通知で結果を送信
        await line_messaging_api.push_message(
            PushMessageRequest(to=user_id, messages=[TextMessage(text=result_text)])
        )
        sheets.save_to_sheet(user_id, result_text)