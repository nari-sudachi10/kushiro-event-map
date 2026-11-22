import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# .envを読み込む
load_dotenv()

# データベース接続URL
DATABASE_URL = os.getenv("DATABASE_URL")

# DB接続エンジン
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True
)

# DBセッション
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

# テーブル定義のベース
Base = declarative_base()
