import os
import gspread
from google.oauth2.service_account import Credentials

def save_to_sheet(user_id: str, text: str):
    try:
        # 認証情報
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        creds = Credentials.from_service_account_file('credentials.json', scopes=scopes)
        client = gspread.authorize(creds)
        
        # 環境変数からスプレッドシートIDを取得して開く
        sheet_id = os.environ.get("SPREADSHEET_ID")
        sheet = client.open_by_key(sheet_id).sheet1
        
        # 行を追加 (日時、ユーザーID、解析結果)
        import datetime
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([now, user_id, text])
    except Exception as e:
        print(f"Sheet Save Error: {e}")