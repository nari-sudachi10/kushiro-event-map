# 地域ナビ API仕様書

## 基本情報

- API Base URL: https://api.sudachi10.com
- API形式: REST API
- データ形式: JSON
- 文字コード: UTF-8

## 1. イベント一覧取得

### リクエスト

GET `/api/events`

### レスポンス例

```json
[
  {
    "id": 1,
    "organizer_id": null,
    "title": "釧路テストイベント",
    "description": "地域ナビの動作確認用イベント",
    "category": "祭り",
    "start_at": "2027-07-20T10:00:00",
    "end_at": "2027-07-20T17:00:00",
    "location_name": "釧路市内",
    "address": "北海道釧路市",
    "image_url": null,
    "status": "pending",
    "created_at": "2026-11-18T10:21:15"
  }
]
