import os
import uuid
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from database import engine

# 環境変数読み込み
load_dotenv()

app = FastAPI()

# 画像保存先
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)


# =====================================
# LINE画像保存処理
# =====================================

async def save_line_image(message_id: str):
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

    if not token:
        raise RuntimeError("LINE_CHANNEL_ACCESS_TOKENが設定されていません")

    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"

    headers = {
        "Authorization": f"Bearer {token}"
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()

    content_type = response.headers.get("content-type", "")

    extensions = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif"
    }

    extension = extensions.get(
        content_type.split(";")[0],
        ".bin"
    )

    filename = f"{uuid.uuid4().hex}{extension}"
    file_path = UPLOAD_DIR / filename

    file_path.write_bytes(response.content)

    print(f"画像を保存しました: {file_path}")

    return str(file_path)


# =====================================
# トップページ
# =====================================

@app.get("/")
def home():
    return {"message": "地域ナビ API 起動中"}


# =====================================
# LINE Webhook
# =====================================

@app.post("/callback")
async def callback(request: Request):

    body = await request.body()

    print("===== LINE Webhook received =====")

    try:
        data = json.loads(body)

        events = data.get("events", [])

        print(f"受信イベント数: {len(events)}")

        # 画像メッセージを処理
        for event in events:

            message = event.get("message", {})
            source = event.get("source", {})

            if message.get("type") == "image":

                message_id = message.get("id")
                line_user_id = source.get("userId")

                try:
                    # 1. 主催者を検索
                    with engine.connect() as connection:

                        organizer = connection.execute(
                            text("""
                                SELECT id
                                FROM organizers
                                WHERE line_user_id = :user_id
                                AND status = 'approved'
                            """),
                            {"user_id": line_user_id}
                        ).mappings().first()

                    # 2. 未承認なら処理しない
                    if organizer is None:
                        print("未承認のユーザーから画像が送信されました")
                        continue

                    organizer_id = organizer["id"]

                    # 3. LINEから画像を取得
                    saved_path = await save_line_image(message_id)

                    # 4. DBに画像情報を登録
                    with engine.begin() as connection:

                        result = connection.execute(
                            text("""
                                INSERT INTO flyers (
                                    organizer_id,
                                    image_path,
                                    status
                                )
                                VALUES (
                                    :organizer_id,
                                    :image_path,
                                    'received'
                                )
                                RETURNING id
                            """),
                            {
                                "organizer_id": organizer_id,
                                "image_path": saved_path
                            }
                        )

                        flyer_id = result.scalar_one()

                    print(f"画像保存成功: {saved_path}")
                    print(f"チラシDB登録成功: ID={flyer_id}")

                except Exception as e:
                    print(f"画像処理失敗: {e}")

    except json.JSONDecodeError:
        print("JSONの解析に失敗しました")

    return "OK"


# =====================================
# イベント登録用データモデル
# =====================================

class EventCreate(BaseModel):
    organizer_id: Optional[int] = None
    title: str
    description: Optional[str] = None
    category: Optional[str] = None
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    location_name: Optional[str] = None
    address: Optional[str] = None
    image_url: Optional[str] = None


# =====================================
# イベント一覧取得
# =====================================

@app.get("/api/events")
def get_events():

    with engine.connect() as connection:

        result = connection.execute(
            text("SELECT * FROM events ORDER BY start_at ASC")
        )

        events = result.mappings().all()

    return [dict(event) for event in events]


# =====================================
# イベント詳細取得
# =====================================

@app.get("/api/events/{event_id}")
def get_event(event_id: int):

    with engine.connect() as connection:

        result = connection.execute(
            text("SELECT * FROM events WHERE id = :id"),
            {"id": event_id}
        )

        event = result.mappings().first()

    if event is None:
        raise HTTPException(
            status_code=404,
            detail="イベントが見つかりません"
        )

    return dict(event)


# =====================================
# イベント登録
# =====================================

@app.post("/api/events")
def create_event(event: EventCreate):

    with engine.begin() as connection:

        result = connection.execute(
            text("""
                INSERT INTO events (
                    organizer_id,
                    title,
                    description,
                    category,
                    start_at,
                    end_at,
                    location_name,
                    address,
                    image_url
                )
                VALUES (
                    :organizer_id,
                    :title,
                    :description,
                    :category,
                    :start_at,
                    :end_at,
                    :location_name,
                    :address,
                    :image_url
                )
                RETURNING *
            """),
            event.model_dump()
        )

        new_event = result.mappings().first()

    return dict(new_event)
