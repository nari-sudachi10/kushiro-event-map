import os
import uuid
import json
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
import secrets
from urllib.parse import urlencode

from fastapi.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text

from database import engine

# 環境変数読み込み
load_dotenv()

app = FastAPI()

# =====================================
# CORS設定
# =====================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://chiiki.sudachi10.com"
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

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

# =====================================
# 主催者登録API
# =====================================

class OrganizerCreate(BaseModel):
    organization_name: str
    representative_name: str
    email: str


@app.post("/api/organizers")
def create_organizer(organizer: OrganizerCreate):

    with engine.begin() as connection:

        # メールアドレスの重複確認
        existing = connection.execute(
            text("""
                SELECT id
                FROM organizers
                WHERE email = :email
            """),
            {"email": organizer.email}
        ).first()

        if existing:
            raise HTTPException(
                status_code=409,
                detail="このメールアドレスは既に登録されています"
            )

        # 主催者登録
        result = connection.execute(
            text("""
                INSERT INTO organizers (
                    organization_name,
                    representative_name,
                    email,
                    status
                )
                VALUES (
                    :organization_name,
                    :representative_name,
                    :email,
                    'pending'
                )
                RETURNING id, organization_name,
                          representative_name, email,
                          status, created_at
            """),
            organizer.model_dump()
        )

        new_organizer = result.mappings().first()

    return dict(new_organizer)

# =====================================
# LINE Login設定
# =====================================

LINE_LOGIN_CHANNEL_ID = os.getenv("LINE_LOGIN_CHANNEL_ID")
LINE_LOGIN_CHANNEL_SECRET = os.getenv("LINE_LOGIN_CHANNEL_SECRET")

LINE_LOGIN_CALLBACK_URL = (
    "https://api.sudachi10.com/api/admin/login/callback"
)

SESSION_SERIALIZER = URLSafeTimedSerializer(
    os.getenv("SESSION_SECRET"),
    salt="chiiki-navi-admin-session"
)

# =====================================
# CSRF対策（Origin検証）
# =====================================

ALLOWED_ORIGIN = "https://chiiki.sudachi10.com"

def verify_admin_origin(request: Request):
    origin = request.headers.get("origin")

    if origin != ALLOWED_ORIGIN:
        raise HTTPException(
            status_code=403,
            detail="不正なリクエストです"
        )

# =====================================
# 管理者認証共通処理
# =====================================

def require_admin(request: Request):

    session_token = request.cookies.get("admin_session")

    if not session_token:
        raise HTTPException(
            status_code=401,
            detail="ログインしていません"
        )

    try:
        session = SESSION_SERIALIZER.loads(
            session_token,
            max_age=3600
        )

    except (BadSignature, SignatureExpired):
        raise HTTPException(
            status_code=401,
            detail="セッションが無効または期限切れです"
        )

    if (
        session.get("role") != "admin"
        or session.get("line_user_id") != os.getenv("ADMIN_LINE_USER_ID")
    ):
        raise HTTPException(
            status_code=403,
            detail="管理者権限がありません"
        )

    return session

# =====================================
# 主催者申請一覧（管理者用）
# =====================================

@app.get("/api/admin/organizers")
def get_pending_organizers(request: Request):
    require_admin(request)

    with engine.connect() as connection:
        result = connection.execute(text("""
            SELECT
                id,
                organization_name,
                representative_name,
                email,
                contact,
                status,
                created_at
            FROM organizers
            WHERE status = 'pending'
            ORDER BY created_at ASC
        """))

        organizers = result.mappings().all()

    return [dict(organizer) for organizer in organizers]

# =====================================
# 主催者申請の承認（管理者用）
# =====================================

@app.patch("/api/admin/organizers/{organizer_id}/approve")
def approve_organizer(organizer_id: int, request: Request):
    verify_admin_origin(request)
    require_admin(request)

    with engine.begin() as connection:
        result = connection.execute(text("""
            UPDATE organizers
            SET status = 'approved'
            WHERE id = :id AND status = 'pending'
            RETURNING id, organization_name, representative_name,
                      email, status, created_at
        """), {"id": organizer_id})

        organizer = result.mappings().first()

    if organizer is None:
        raise HTTPException(
            status_code=404,
            detail="審査中の主催者が見つかりません"
        )

    return dict(organizer)


# =====================================
# 主催者申請の却下（管理者用）
# =====================================

@app.patch("/api/admin/organizers/{organizer_id}/reject")
def reject_organizer(organizer_id: int, request: Request):
    verify_admin_origin(request)
    require_admin(request)

    with engine.begin() as connection:
        result = connection.execute(text("""
            UPDATE organizers
            SET status = 'rejected'
            WHERE id = :id AND status = 'pending'
            RETURNING id, organization_name, representative_name,
                      email, status, created_at
        """), {"id": organizer_id})

        organizer = result.mappings().first()

    if organizer is None:
        raise HTTPException(
            status_code=404,
            detail="審査中の主催者が見つかりません"
        )

    return dict(organizer)

# =====================================
# 管理者LINEログイン開始
# =====================================

@app.get("/api/admin/login")
def admin_login():

    if not LINE_LOGIN_CHANNEL_ID:
        raise HTTPException(
            status_code=500,
            detail="LINE_LOGIN_CHANNEL_IDが設定されていません"
        )

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)

    params = {
        "response_type": "code",
        "client_id": LINE_LOGIN_CHANNEL_ID,
        "redirect_uri": LINE_LOGIN_CALLBACK_URL,
        "state": state,
        "scope": "openid",
        "nonce": nonce
    }

    login_url = (
        "https://access.line.me/oauth2/v2.1/authorize?"
        + urlencode(params)
    )

    response = RedirectResponse(login_url)

    response.set_cookie(
        key="line_login_state",
        value=state,
        max_age=600,
        httponly=True,
        secure=True,
        samesite="lax"
    )

    response.set_cookie(
        key="line_login_nonce",
        value=nonce,
        max_age=600,
        httponly=True,
        secure=True,
        samesite="lax"
    )

    return response

# =====================================
# LINE Login コールバック
# =====================================

@app.get("/api/admin/login/callback")
async def admin_login_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None
):

    if error:
        raise HTTPException(
            status_code=400,
            detail="LINEログインがキャンセルされました"
        )

    # stateの検証
    saved_state = request.cookies.get("line_login_state")

    if not state or not saved_state or state != saved_state:
        raise HTTPException(
            status_code=400,
            detail="不正なログインリクエストです"
        )

    if not code:
        raise HTTPException(
            status_code=400,
            detail="認証コードがありません"
        )

    nonce = request.cookies.get("line_login_nonce")

    if not nonce:
        raise HTTPException(
            status_code=400,
            detail="nonceがありません"
        )

    if not LINE_LOGIN_CHANNEL_SECRET:
        raise HTTPException(
            status_code=500,
            detail="LINE_LOGIN_CHANNEL_SECRETが設定されていません"
        )

    # 認証コードをアクセストークンと交換
    async with httpx.AsyncClient(timeout=15.0) as client:

        token_response = await client.post(
            "https://api.line.me/oauth2/v2.1/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": LINE_LOGIN_CALLBACK_URL,
                "client_id": LINE_LOGIN_CHANNEL_ID,
                "client_secret": LINE_LOGIN_CHANNEL_SECRET
            }
        )

        if token_response.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail="LINEトークンの取得に失敗しました"
            )

        token_data = token_response.json()

        # IDトークンをLINEに検証してもらう
        verify_response = await client.post(
            "https://api.line.me/oauth2/v2.1/verify",
            data={
                "id_token": token_data.get("id_token"),
                "client_id": LINE_LOGIN_CHANNEL_ID
            }
        )

        if verify_response.status_code != 200:
            raise HTTPException(
                status_code=400,
                detail="LINE IDトークンの検証に失敗しました"
            )

        profile = verify_response.json()

    # nonceの検証
    if profile.get("nonce") != nonce:
        raise HTTPException(
            status_code=400,
            detail="nonceが一致しません"
        )

    # LINEユーザーIDの取得
    line_user_id = profile.get("sub")

    # 管理者かどうか確認
    admin_line_user_id = os.getenv("ADMIN_LINE_USER_ID")

    if not line_user_id or line_user_id != admin_line_user_id:
        raise HTTPException(
            status_code=403,
            detail="管理者権限がありません"
        )

    # 管理者セッションを発行
    session_token = SESSION_SERIALIZER.dumps({
        "line_user_id": line_user_id,
        "role": "admin"
    })

    response = RedirectResponse(
        url="https://sudachi10.com/",
        status_code=303
    )

    response.set_cookie(
        key="admin_session",
        value=session_token,
        max_age=3600,
        httponly=True,
        secure=True,
        samesite="lax"
    )

    response.delete_cookie("line_login_state")
    response.delete_cookie("line_login_nonce")

    return response


# =====================================
# 管理者ログイン状態確認
# =====================================

@app.get("/api/admin/me")
def admin_me(request: Request):

    session_token = request.cookies.get("admin_session")

    if not session_token:
        raise HTTPException(
            status_code=401,
            detail="ログインしていません"
        )

    try:
        session = SESSION_SERIALIZER.loads(
            session_token,
            max_age=3600
        )

    except (BadSignature, SignatureExpired):
        raise HTTPException(
            status_code=401,
            detail="セッションが無効または期限切れです"
        )

    if (
        session.get("role") != "admin"
        or session.get("line_user_id") != os.getenv("ADMIN_LINE_USER_ID")
    ):
        raise HTTPException(
            status_code=403,
            detail="管理者権限がありません"
        )

    return {
        "logged_in": True,
        "role": "admin"
    }


# =====================================
# 管理者ログアウト
# =====================================

@app.post("/api/admin/logout")
def admin_logout(request: Request):
    verify_admin_origin(request)
    require_admin(request)

    response = RedirectResponse(
        url="https://sudachi10.com/",
        status_code=303
    )

    response.delete_cookie(
        key="admin_session",
        httponly=True,
        secure=True,
        samesite="lax"
    )

    return response
