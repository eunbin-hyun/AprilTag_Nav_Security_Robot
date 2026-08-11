"""비동기 DB 엔진·세션. 워커 1개 전제라 이벤트 루프가 하나뿐이므로 asyncpg 관통으로 간다.

WS 라우트는 세션을 오래 쥐지 않는다 — 메시지를 다룰 때만 get_session()으로 짧게 열고 닫는다.
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
# 풀 크기를 코드에 적어 둔다(L39). 안 적으면 SQLAlchemy 기본(5 + 넘침 10, 대기 30초)이 말없이
# 먹혀서 "동시 요청 몇까지 견디나"를 아무도 못 말한다. 숫자 근거는 이렇다.
#  - pool_size 10     워커 1개 + WS 재검증 루프까지 상시 잡히는 자리가 한 자릿수다.
#  - max_overflow 10  합쳐서 20. 시연 규모(관제 화면 몇 장 + 기기 셋)의 두 배 남짓이다.
#  - pool_timeout 5   기본 30초는 기다리다 500이라 화면이 멈춘 것처럼 보인다. 빨리 실패해서
#                     한계에 닿았다는 사실이 로그에 바로 뜨는 쪽이 낫다.
# ⚠ Postgres 쪽 max_connections를 넘기면 여기 숫자와 무관하게 연결이 거절된다.
engine = create_async_engine(
    _settings.database_url,
    pool_pre_ping=True,
    future=True,
    pool_size=10,
    max_overflow=10,
    pool_timeout=5,
)
SessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 라우트 의존성. 요청 한 건 동안만 세션을 연다."""
    async with SessionMaker() as session:
        yield session


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """라우트 밖(WS 처리 등)에서 짧게 세션이 필요할 때."""
    async with SessionMaker() as session:
        yield session
