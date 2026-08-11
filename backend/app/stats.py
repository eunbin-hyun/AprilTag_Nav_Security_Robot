"""미태깅 통계 집계 (AI 활용 조사 정본 2026-07-16 — 시계열 ML 없이 집계만).

조사 정본이 예측 모델을 접었다. 표본이 시연 며칠치라 학습이 과적합밖에 안 되고, 관리자가
보고 싶은 건 "언제 몰리나"라서 집계 두 가지면 답이 나온다.

- 일자별 추이 + 이동평균 — 하루 단위 톱니를 눌러 늘고 주는 방향만 보여준다.
- 시간대(0~23) × 요일 히트맵 — 등교 시간대·특정 요일 쏠림을 눈으로 잡는다.

여기에 **오늘 하루치 종류별 건수**(`today_counts`)가 하나 더 붙는다. 개발자 텔레메트리
화면이 목록을 받아 스스로 세던 값인데, 조회 상한(500건)에 걸리면 실제보다 적게 나왔다.
세는 자리를 서버로 옮겨 상한을 없앴다. 하루를 자르는 규칙은 아래 미태깅 집계와 똑같다.

세는 대상은 **미태깅 판정이 붙은 빔 통과**(`GatePassEvent.verdict='untagged'`)다. `Alert`
쪽이 아니다 — 경고는 쿨다운(`alert_cooldown_sec`)으로 삼켜지므로, 사람이 몰릴수록 실제
미태깅보다 적게 세어 통계가 거꾸로 눕는다. 통과 행은 인입 하나에 하나라 삼킴이 없다.
(경고 발생 수를 따로 보고 싶다는 요구가 나오면 그건 별도 계열이다 — 팀 확정 대기.)

시각 축은 `observed_at`(기기 관측)이다. 서버 수신(`received_at`)을 쓰면 백로그·배속 재생이
"새벽 3시에 미태깅 폭증"으로 보인다.

⚠ 버킷은 UTC가 아니라 **KST 로컬 날짜·시각**으로 자른다. UTC로 자르면 하루 경계가 오전
9시에 놓여 "7월 27일 통계"에 26일 저녁 아홉 시간이 섞인다. 변환은 전부 Postgres
`timezone()`이 맡는다 — 파이썬 zoneinfo는 윈도우에 IANA 표가 없어 기기마다 갈리고, 양쪽에서
따로 변환하면 서버와 DB가 서로 다른 날짜로 자를 수 있다. 자르는 자리를 한 곳으로 모은다.

⚠ 세는 대상에서 **시험 자료를 뺄지는 설정 스위치**(`STATS_EXCLUDE_TEST_DATA`)로 정한다. 기본은
꺼짐이라 예전과 똑같이 전부 센다. 자세한 규칙과 실서버 실측은 아래 `StatsFilterSettings`에 있다.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Date, Integer, Text, cast, func, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.credit.state_machine import Verdict
from app.models import Alert, GatePassEvent, ShuttleArrival, TaggingEvent
from app.schemas import (
    AlertBucketPoint,
    AlertBucketsOut,
    AlertResponseByType,
    AlertResponseStatsOut,
    AlertResponseSummary,
    AlertSeverityCounts,
    GatePassBucketPoint,
    GatePassBucketsOut,
    GatePassHeatmapOut,
    StatsBucketRange,
    StatsDailyPoint,
    StatsDurationSummary,
    StatsHeatmap,
    StatsPeak,
    StatsRange,
    TodayGatePassCounts,
    TodayStatsOut,
    UntaggedStatsOut,
)

logger = logging.getLogger(__name__)

# 시연 현장이 한국 한 곳이라 상수로 둔다. 설정으로 빼면 오타난 지역 이름이 기동 때 안 걸리고
# 질의 시점에 터진다(zoneinfo가 없어 파이썬 쪽에서 미리 검증할 방법이 없다).
STATS_TIMEZONE = "Asia/Seoul"
# 오프셋을 안 적은 `STATS_EXCLUDE_BEFORE`를 어느 시간대로 읽을까(아래 `_assume_kst` 참고).
# 지역 이름이 아니라 고정 오프셋으로 둔다 — 한국은 DST가 없어 +09:00이 연중 같고, 그래서
# 위 모듈 주석이 말한 "윈도우엔 IANA 표가 없다" 함정을 안 밟는다. DST가 있는 지역으로
# 시연이 옮겨가면 이 상수로는 안 되고 DB `timezone()` 변환을 타야 한다.
STATS_TZ_OFFSET = dt.timezone(dt.timedelta(hours=9))

DEFAULT_DAYS = 7
MAX_DAYS = 90
DEFAULT_WINDOW = 3
MAX_WINDOW = 30

# 게이트 통과 추이 차트가 쓰는 잔 버킷. 요구가 15분으로 못박혀 있어 파라미터가 아니라 상수다
# (부르는 쪽이 고를 수 있으면 같은 화면의 두 칩이 서로 다른 격자를 그린다 —
# `STATS_EXCLUDE_TEST_DATA`를 요청 파라미터로 안 뺀 것과 같은 이유).
GATE_PASS_BUCKET_SECONDS = 15 * 60
DEFAULT_BUCKET_HOURS = 24
# 72시간이면 15분 칸 288개다. 이 위로 올리면 한 응답이 칸 수백 개를 넘겨 차트가 못 그린다.
MAX_BUCKET_HOURS = 72

HOURS_PER_DAY = 24
WEEKDAY_LABELS = ["월", "화", "수", "목", "금", "토", "일"]  # 0=월 … 6=일 (ISO 순서)


class StatsFilterSettings(BaseSettings):
    """통계에서 시험 자료를 뺄까, 그리고 무엇을 시험으로 볼까.

    ## 왜 스위치인가

    시연에서 "센서 오검출까지 센 숫자"를 보여줄지 "사람이 실제로 지나간 숫자"를 보여줄지가
    아직 안 정해졌다(팀 결정 대기). 그래서 **기본은 꺼짐**이고, 켜지 않으면 집계는 예전과
    한 건도 안 달라진다. 이 저장소가 미확정 안건을 다루는 방식과 같다
    (`CREDIT_SCOPE_ROOM`·`ARRIVAL_EDGE_ON_RECOMMAND`도 기본 꺼짐 = 기존 동작).

    ## 무엇을 시험으로 보나 — 실서버 실측(2026-07-30)

    앞 조가 제안한 조건은 `device_id LIKE 'test-%'` 하나였는데, **실서버에서는 아무것도 안
    걸러진다.** 통과 494건·태깅 12건의 `device_id`가 전부 `raspberry01`이다. ToF 고착으로 생긴
    가짜(지라 S15P11C207-227)도 진짜 라파이가 올린 거라 기기 이름으로는 진짜와 안 갈린다.
    그래서 축을 둘로 뒀다.

    - `STATS_TEST_DEVICE_PREFIXES` — 시험 기기 접두(콤마로 여럿). 앞으로 손시험·시뮬을
      `test-` 접두로 쌓기로 하면 이 축이 산다(`tools/purge_test_data.py`의 제안과 같은 축이다).
    - `STATS_EXCLUDE_BEFORE` — 이 시각 앞 자료를 안 센다. `raspberry01`이 올린 오검출을
      실제로 빼는 축은 이거 하나다. 정리 도구로 지우는 대신 통계에서만 가릴 때 쓴다.
      ⚠ 오프셋을 안 적은 값(`2026-07-30T00:00:00`)은 **KST로 읽는다**(`_assume_kst`).
      보정이 없으면 그 값이 서버 프로세스 로컬 시간대로 읽혀 기계마다 컷이 달라진다 —
      한국 개발 기계에서는 KST, `TZ=UTC`인 컨테이너에서는 UTC다. 그게 이 검증기를 둔 이유다.

    ## 미처리 경고 수(active_alerts)에 걸리는 축

    ⚠ 이 스위치를 켜면 `/api/stats/today`와 `/api/dashboard/snapshot`의 미처리 경고 수가
    갈린다. 시각 컷을 타는 자리는 `today_counts`(아래 `alert_stmt`)뿐이고,
    `routers/query.py`의 `count_active_alerts`와 `assistant_data.py`는 `apply_stats_filter`를
    안 타서 전 기간을 그대로 센다. **켤 때 그 두 자리도 같이 통일해야 한다.** 지금 안 고쳐
    둔 이유는 기본이 꺼짐이라 숫자가 아직 안 갈리고, "켤지 말지"가 팀 결정 대기라서다 —
    안 켠 스위치의 부작용을 미리 고치면 결정이 반대로 날 때 되돌릴 코드가 늘어난다.

    ⚠ 기본 접두에 `webcall-`을 **안** 넣었다. 그건 화면 셔틀 가상 호출 접두인데
    (`app/shuttle_call.py`), 셔틀은 실기기를 안 만들기로 확정돼서 `webcall-`이 시연의 유일한
    셔틀 경로다. 시험 취급하면 시연 중 셔틀 건수가 통째로 0으로 나온다. 정리 도구가 그 접두를
    지우기 대상으로 함께 제안하는 건 "시연 전에 쌓인 걸 비운다"는 다른 목적이다.

    ⚠ `config.Settings`에 안 넣고 여기 따로 뒀다 — `app/config.py`는 다른 조 소유라 이번 수리
    범위 밖이다(`shuttle_call.py`가 `SHUTTLE_CALL_COOLDOWN_SEC`를 모듈 상수로 둔 것과 같은
    이유). 읽는 자리는 `Settings`와 똑같다 — 환경변수와 `backend/.env` 둘 다 본다. 팀이 정하면
    필드 셋을 `Settings`로 옮기고 이 클래스를 지우면 된다.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    stats_exclude_test_data: bool = False
    stats_test_device_prefixes: str = "test-"
    stats_exclude_before: dt.datetime | None = None

    @field_validator("stats_exclude_before")
    @classmethod
    def _assume_kst(cls, value: dt.datetime | None) -> dt.datetime | None:
        """오프셋 없는 컷은 KST로 못박는다. 시간대가 기계마다 갈리는 길을 막는 자리다.

        `STATS_EXCLUDE_BEFORE=2026-07-30T00:00:00`은 안 터진다 — pydantic이 tz 없는
        datetime으로 받아 주고, 그 값을 asyncpg가 `timestamptz` 칼럼과 견줄 때 **그 서버
        프로세스의 로컬 시간대**로 읽는다(naive면 `astimezone()`을 타서 OS 로컬을 붙인다).
        그래서 같은 `.env` 한 줄이 기계마다 다른 컷이 된다 — 2026-07-30 실측으로, 한국
        로케일 개발 기계에서는 KST로 읽히고 `TZ=UTC`인 프로세스에서는 UTC로 읽혀 아홉 시간
        어긋난다. 배포는 도커 컨테이너라 기본이 UTC 쪽이고, 그래서 손으로 재현이 안 되는
        어긋남이 서버에서만 난다. 값이 조용히 틀리는 게 제일 나쁜 결말이라 여기서 정한다.

        **거절이 아니라 보정을 골랐다.** 거절하면 `get_stats_filter()`가 처음 불리는
        시점(첫 통계 요청)에 500이 나는데, 그건 시연 도중이다 — 기동 때 걸리는 게 아니라서
        거절의 장점(빨리 터진다)이 안 살고 시연만 멈춘다. 대신 경고 로그를 남겨 "내가
        뭘로 읽었나"를 운영자가 볼 수 있게 한다.

        이미 오프셋이 붙은 값(`...+09:00`·`...Z`)은 손대지 않는다. 문자열에 오프셋을 갈아
        끼우는 방식이 아니라 tz만 붙이는 것이라, 적은 벽시계 숫자는 그대로 남는다.
        """
        if value is None or value.tzinfo is not None:
            return value
        fixed = value.replace(tzinfo=STATS_TZ_OFFSET)
        logger.warning(
            "STATS_EXCLUDE_BEFORE=%s 에 오프셋이 없어 KST(+09:00)로 읽었다 → %s."
            " 뜻이 이게 아니면 값에 오프셋을 적어라(예: 2026-07-30T00:00:00+09:00).",
            value.isoformat(), fixed.isoformat(),
        )
        return fixed

    @property
    def device_prefixes(self) -> tuple[str, ...]:
        """빈 토막을 걷어낸 접두 목록. `"test-, sim-"`처럼 공백이 섞여도 받는다."""
        return tuple(
            p.strip() for p in self.stats_test_device_prefixes.split(",") if p.strip()
        )


@lru_cache
def get_stats_filter() -> StatsFilterSettings:
    return StatsFilterSettings()


def apply_stats_filter(stmt, *, time_column, device_column=None, event_id_column=None):
    """집계 질의 하나에 시험 자료 거르기를 얹는다. **모든 계열이 이 함수 하나만 부른다.**

    계열마다 손으로 조건을 붙이면 한 곳을 고칠 때 다른 곳이 남아 같은 화면 안에서 숫자가
    갈라진다(조회 목록과 스냅샷이 fetch 함수를 공유하는 것과 같은 이유).

    - 기기 축은 `device_id`가 있으면 그걸 보고, 없는 계열(셔틀)은 `event_id` 접두를 본다 —
      `event_id` 첫 토막이 기기 이름이라 같은 축이다. 두 칸이 다 없는 계열(경고)은 기기 축을
      건너뛰고 시각 컷만 받는다.
    - ⚠ `device_id`가 NULL인 행은 **남긴다.** SQL에서 `device_id NOT LIKE 'test-%'`는 NULL에
      NULL(거짓)을 줘서, 가드가 없으면 기기 이름을 안 보내던 예전 인입이 통계에서 통째로
      사라진다.
    - 접두는 `startswith(autoescape=True)`로 건다. 접두에 `_`나 `%`가 들어가도 LIKE 와일드카드로
      풀리지 않는다.
    """
    settings = get_stats_filter()
    if not settings.stats_exclude_test_data:
        return stmt

    column = device_column if device_column is not None else event_id_column
    if column is not None:
        for prefix in settings.device_prefixes:
            stmt = stmt.where(
                or_(column.is_(None), ~column.startswith(prefix, autoescape=True))
            )
    if settings.stats_exclude_before is not None:
        stmt = stmt.where(time_column >= settings.stats_exclude_before)
    return stmt


def _now_utc() -> dt.datetime:
    """집계 기준 "지금". 시험이 시각을 고정할 수 있게 함수로 뺐다(state_machine._wall_now와 같은 수법)."""
    return dt.datetime.now(dt.timezone.utc)


async def _resolve_range(
    session: AsyncSession, days: int
) -> tuple[dt.date, dt.datetime, dt.datetime]:
    """조회 구간을 KST 기준으로 잡아 (시작 날짜, 시작 UTC, 끝 UTC)를 돌려준다.

    구간은 **오늘(KST)을 포함한 최근 `days`일**이다. 끝은 내일 0시라 오늘 하루가 통째로 들어온다.
    경계는 시작 이상 끝 미만 — 시작 0시 정각 이벤트는 들어오고, 끝 0시 정각은 다음 구간 몫이다.

    로컬 날짜 계산도 Postgres에 맡긴다(모듈 docstring의 ⚠ 참고). 파이썬으로 넘어오는 건
    이미 확정된 날짜와 UTC 시각뿐이라, 뒤쪽 계산은 시간대를 몰라도 된다.
    """
    stmt = text(
        """
        WITH b AS (
            SELECT timezone(CAST(:tz AS text), CAST(:now AS timestamptz))::date
                   - CAST(:back AS integer) AS start_date
        )
        SELECT
            start_date,
            timezone(CAST(:tz AS text), CAST(start_date AS timestamp)) AS start_utc,
            timezone(
                CAST(:tz AS text),
                CAST(start_date + CAST(:days AS integer) AS timestamp)
            ) AS end_utc
        FROM b
        """
    )
    row = (
        await session.execute(
            stmt,
            {"tz": STATS_TIMEZONE, "now": _now_utc(), "back": days - 1, "days": days},
        )
    ).one()
    return row.start_date, row.start_utc, row.end_utc


async def _hourly_counts(
    session: AsyncSession,
    *,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    gate_no: int | None,
    verdict: str | None = Verdict.UNTAGGED,
) -> list[tuple[dt.date, int, int]]:
    """구간 안 통과를 (로컬 날짜, 로컬 시각, 건수)로 묶어 돌려준다.

    일자별 추이와 히트맵이 이 결과 하나를 나눠 쓴다. 질의를 두 벌 두면 같은 화면 안에서
    막대 합과 히트맵 합이 갈라질 수 있다(조회 API가 목록·스냅샷에 같은 함수를 쓰는 것과 같은 이유).

    ⭐ `verdict`가 None이면 **판정을 안 가리고 통과 전체**를 센다(2026-08-05 프론트 2차 요구 —
    통과 히트맵). 기본값은 미태깅이라 예전 부르는 자리는 글자 하나 안 바뀐다.

    구간 필터는 변환 안 한 `observed_at`에 그대로 건다 — 왼쪽을 함수로 감싸면 인덱스를 못 탄다.
    탈 인덱스는 `ix_gate_pass_event_observed_at`이다(마이그레이션 0009). 이 집계와 판정 분포·
    구간 버킷 셋이 그 인덱스 하나를 나눠 쓴다.
    """
    tz = cast(literal(STATS_TIMEZONE), Text)  # timezone() 오버로드를 text 쪽으로 못박는다
    local_ts = func.timezone(tz, GatePassEvent.observed_at)
    local_date = cast(local_ts, Date)
    local_hour = cast(func.extract("hour", local_ts), Integer)

    stmt = (
        select(local_date.label("d"), local_hour.label("h"), func.count().label("n"))
        .where(
            GatePassEvent.observed_at >= start_utc,
            GatePassEvent.observed_at < end_utc,
        )
        .group_by(local_date, local_hour)
    )
    if verdict is not None:
        stmt = stmt.where(GatePassEvent.verdict == verdict)
    if gate_no is not None:
        stmt = stmt.where(GatePassEvent.gate_no == gate_no)
    stmt = apply_stats_filter(
        stmt,
        time_column=GatePassEvent.observed_at,
        device_column=GatePassEvent.device_id,
    )

    return [(r.d, r.h, r.n) for r in (await session.execute(stmt)).all()]


def _moving_average(counts: list[int], window: int) -> list[float | None]:
    """뒤쪽 `window`일 단순 이동평균. 창이 덜 찬 앞머리는 None이다.

    앞머리를 부분 평균으로 채우면 첫날 값이 그날 실측과 같아서, 화면이 "이동평균이 실측을
    따라가다 갑자기 꺾이는" 선을 그린다. 창이 찬 지점부터 그리는 쪽이 정직하다.
    """
    out: list[float | None] = []
    for i in range(len(counts)):
        if i + 1 < window:
            out.append(None)
        else:
            out.append(round(sum(counts[i + 1 - window : i + 1]) / window, 2))
    return out


def _peak_cell(matrix: list[list[int]]) -> StatsPeak | None:
    """가장 많이 몰린 칸. 다 0이면 None이다.

    ⚠ 동점이면 **요일·시각 순으로 먼저 만나는 것**을 쓴다. 그래야 같은 자료에 같은 응답이
    나온다 — 나중 것으로 덮으면 질의 순서가 바뀌는 날 응답이 흔들린다.

    ⭐ 미태깅 통계와 통과 히트맵이 이 한 함수를 나눠 쓴다. 두 벌로 두면 한쪽만 고쳐지는
    날에 같은 격자에서 다른 최고 칸이 나온다.
    """
    peak: StatsPeak | None = None
    for weekday, row in enumerate(matrix):
        for hour, count in enumerate(row):
            if count > 0 and (peak is None or count > peak.count):
                peak = StatsPeak(weekday=weekday, hour=hour, count=count)
    return peak


async def gate_pass_heatmap(
    session: AsyncSession, *, days: int, gate_no: int | None
) -> GatePassHeatmapOut:
    """요일 × 시간대 통과 히트맵 (프론트 2차 요구).

    ⭐ **판정을 안 가린다** — 미태깅만 세는 `untagged_stats`와 다른 자료다. "언제 사람이
    몰리나"를 보는 카드라 정상 통과까지 다 들어가야 뜻이 선다.

    ⚠ 격자는 **늘 7×24로 꽉 찬다.** 건수 0인 칸을 빼면 화면이 격자를 못 그린다(미태깅
    히트맵과 같은 규칙이다).

    `max_count`는 색 농도 기준이라 화면이 최댓값을 따로 훑지 않아도 된다.
    """
    start_date, start_utc, end_utc = await _resolve_range(session, days)
    rows = await _hourly_counts(
        session, start_utc=start_utc, end_utc=end_utc, gate_no=gate_no, verdict=None
    )

    matrix = [[0] * HOURS_PER_DAY for _ in range(len(WEEKDAY_LABELS))]
    for local_date, hour, count in rows:
        matrix[local_date.isoweekday() - 1][hour] += count

    return GatePassHeatmapOut(
        range=StatsRange(
            days=days,
            start_date=start_date,
            end_date=start_date + dt.timedelta(days=days - 1),
            timezone=STATS_TIMEZONE,
        ),
        gate_no=gate_no,
        weekday_labels=list(WEEKDAY_LABELS),
        matrix=matrix,
        max_count=max((max(row) for row in matrix), default=0),
        peak=_peak_cell(matrix),
    )


async def untagged_stats(
    session: AsyncSession, *, days: int, window: int, gate_no: int | None
) -> UntaggedStatsOut:
    """미태깅 통계 한 벌. 화면이 그대로 그릴 수 있게 빈 날짜·빈 칸까지 0으로 채워 돌려준다.

    건수가 0인 날을 빼면 화면이 그 날짜를 건너뛴 채 선을 이어 그려서, 조용하던 날이
    그래프에서 사라진다. 히트맵도 같은 이유로 7×24를 늘 꽉 채운다.
    """
    start_date, start_utc, end_utc = await _resolve_range(session, days)
    rows = await _hourly_counts(
        session, start_utc=start_utc, end_utc=end_utc, gate_no=gate_no
    )

    per_date: dict[dt.date, int] = defaultdict(int)
    matrix = [[0] * HOURS_PER_DAY for _ in range(len(WEEKDAY_LABELS))]
    for local_date, hour, count in rows:
        per_date[local_date] += count
        matrix[local_date.isoweekday() - 1][hour] += count

    dates = [start_date + dt.timedelta(days=i) for i in range(days)]
    counts = [per_date.get(d, 0) for d in dates]
    averages = _moving_average(counts, window)
    daily = [
        StatsDailyPoint(date=d, count=c, moving_avg=a)
        for d, c, a in zip(dates, counts, averages)
    ]

    peak = _peak_cell(matrix)

    return UntaggedStatsOut(
        range=StatsRange(
            days=days,
            start_date=start_date,
            end_date=dates[-1],
            timezone=STATS_TIMEZONE,
        ),
        gate_no=gate_no,
        window=window,
        total=sum(counts),
        daily=daily,
        heatmap=StatsHeatmap(
            weekday_labels=list(WEEKDAY_LABELS),
            matrix=matrix,
            max_count=max((max(row) for row in matrix), default=0),
        ),
        peak=peak,
    )


# ── 오늘 집계 ─────────────────────────────────────────────────────────────
# 판정 칸 이름은 응답 필드 이름과 같아야 한다. 여기 없는 값(새 판정·아직 null)은 other로 모은다.
TODAY_VERDICTS = (
    Verdict.NORMAL,
    Verdict.UNTAGGED,
    Verdict.EXIT,
    Verdict.BEAM_INCOMPLETE,
)


async def _count_in_range(
    session: AsyncSession,
    model,
    column,
    *,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    device_column=None,
    event_id_column=None,
) -> int:
    """구간 안 행 수. 경계는 시작 이상 끝 미만이다(미태깅 집계와 같은 규칙).

    필터는 변환 안 한 원본 컬럼에 그대로 건다 — 왼쪽을 timezone()으로 감싸면 인덱스를 못 탄다.
    구간 양 끝은 이미 KST 기준으로 잘라 UTC로 바뀐 값이라 여기서 또 변환할 게 없다.

    기기 축 두 칸은 시험 자료 거르기가 쓴다(`apply_stats_filter`). 계열마다 있는 칸이 달라서
    부르는 쪽이 자기 칸을 넘긴다 — 셔틀은 `device_id`가 없어 `event_id` 접두로 가른다.
    """
    stmt = (
        select(func.count())
        .select_from(model)
        .where(column >= start_utc, column < end_utc)
    )
    stmt = apply_stats_filter(
        stmt,
        time_column=column,
        device_column=device_column,
        event_id_column=event_id_column,
    )
    return (await session.execute(stmt)).scalar_one()


def _gate_pass_counts(rows) -> TodayGatePassCounts:
    """(판정, 건수) 짝들을 응답 칸으로 접는다. 사전 밖 판정과 미판정(null)은 other로 모은다.

    오늘 집계와 15분 버킷이 이 함수 하나를 나눠 쓴다. 접는 규칙을 두 벌 두면 새 판정이
    생겼을 때 한쪽만 other로 받아 같은 화면 안에서 합이 갈라진다(`apply_stats_filter`를
    한 자리에 모은 것과 같은 이유).
    """
    known = {v: 0 for v in TODAY_VERDICTS}
    other = 0
    for verdict, count in rows:
        if verdict in known:
            known[verdict] += count
        else:
            other += count

    return TodayGatePassCounts(
        total=sum(known.values()) + other,
        normal=known[Verdict.NORMAL],
        untagged=known[Verdict.UNTAGGED],
        exit=known[Verdict.EXIT],
        beam_incomplete=known[Verdict.BEAM_INCOMPLETE],
        other=other,
    )


async def _gate_pass_by_verdict(
    session: AsyncSession, *, start_utc: dt.datetime, end_utc: dt.datetime
) -> TodayGatePassCounts:
    """오늘 게이트 통과를 판정별로 센다. 사전 밖 판정과 미판정(null)은 other로 모은다."""
    stmt = (
        select(GatePassEvent.verdict, func.count().label("n"))
        .where(
            GatePassEvent.observed_at >= start_utc,
            GatePassEvent.observed_at < end_utc,
        )
        .group_by(GatePassEvent.verdict)
    )
    stmt = apply_stats_filter(
        stmt,
        time_column=GatePassEvent.observed_at,
        device_column=GatePassEvent.device_id,
    )
    return _gate_pass_counts((await session.execute(stmt)).all())


async def today_counts(session: AsyncSession) -> TodayStatsOut:
    """오늘(KST) 하루치 종류별 건수 한 벌.

    구간은 미태깅 집계의 `days=1`과 똑같이 잡는다(`_resolve_range`) — 자르는 자리를 한 곳에
    모아 두 API가 서로 다른 "오늘"을 쓰는 일이 없게 한다. KST 변환은 전부 Postgres
    `timezone()` 몫이다(모듈 docstring의 ⚠ 참고).

    계열마다 시각 축이 다르다 — 태깅·통과는 `observed_at`, 셔틀은 `signal_ts`다. 이름만
    다를 뿐 셋 다 "기기가 본 시각"이고, 조회 API가 셔틀의 signal_ts를 observed_at 자리에
    싣는 것과 같은 짝이다.

    active_alerts만 오늘 범위 밖이다. 아직 확인 안 한 경고 전수(ack=false)라 어제 것도
    들어간다 — 스냅샷 `counts.active_alerts`와 같은 값이고, 화면 카드도 "전체 기준"이라 적는다.
    """
    today, start_utc, end_utc = await _resolve_range(session, 1)

    tagging = await _count_in_range(
        session,
        TaggingEvent,
        TaggingEvent.observed_at,
        start_utc=start_utc,
        end_utc=end_utc,
        device_column=TaggingEvent.device_id,
    )
    shuttle = await _count_in_range(
        session,
        ShuttleArrival,
        ShuttleArrival.signal_ts,
        start_utc=start_utc,
        end_utc=end_utc,
        event_id_column=ShuttleArrival.event_id,
    )
    gate_pass = await _gate_pass_by_verdict(
        session, start_utc=start_utc, end_utc=end_utc
    )
    # 경고엔 기기 축이 없다(발화 이벤트를 (source_type, source_id)로 가리킬 뿐이다). 그래서
    # 시각 컷만 받는다 — 그게 ToF 오검출로 쌓인 미처리 경고 18건을 시연 첫 화면에서 빼는 축이다.
    #
    # ⚠ `STATS_EXCLUDE_TEST_DATA`를 켜면 이 줄과 `/api/dashboard/snapshot`의 미처리 경고 수가
    # 갈린다. 같은 "미처리 경고"를 세는 자리가 셋인데 컷을 타는 건 여기 하나뿐이다 —
    # `routers/query.py:count_active_alerts`와 `assistant_data.py`는 `apply_stats_filter`를
    # 안 탄다. **켜는 사람이 그 두 자리도 같이 통일해야 한다.** 기본이 꺼짐이라 지금은 세
    # 자리 숫자가 같고, 켤지 말지는 팀 결정 대기다.
    alert_stmt = apply_stats_filter(
        select(func.count()).select_from(Alert).where(Alert.ack.is_(False)),
        time_column=Alert.created_at,
    )
    active_alerts = (await session.execute(alert_stmt)).scalar_one()

    return TodayStatsOut(
        date=today,
        timezone=STATS_TIMEZONE,
        tagging=tagging,
        gate_pass=gate_pass,
        shuttle_arrival=shuttle,
        active_alerts=active_alerts,
    )


# ── 경고 대응 통계 (프론트 요구 N2) ────────────────────────────────────────
# AI 탭 "경고 대응 통계" 카드가 읽는 자리다. 경고가 난 뒤 관제가 **얼마 만에** 확인하고
# 종결했나를 종류별로 센다.
#
# 시각 축은 `created_at`(경고가 생긴 시각)이고, 소요시간은 거기서 `acked_at`·`resolved_at`
# 까지의 차다. 구간을 자르는 축도 `created_at` 하나다 — 확인 시각으로 자르면 어제 난 경고를
# 오늘 확인한 건이 "오늘 난 경고"로 세어져 종류별 건수가 조회 목록과 갈라진다.
#
# ⚠ `acked_at`이 없는 경고는 확인 표본에서 빠질 뿐 total에는 남는다. 미확인을 0초로 채우면
#   "빨리 봤다"로 읽혀 대응 통계가 거꾸로 눕는다. 두 수의 차가 곧 "아직 안 본 건"이다.
#
# ⚠ 여기 세 창구(N2·N12 둘)는 전부 **구간을 자르는 집계**라 `today_counts`의
#   `active_alerts`(전 기간 미처리 수)가 가진 어긋남을 물려받지 않는다. 그 칸은 시각 컷만
#   타고 다른 창구는 안 타서 스위치를 켜면 숫자가 갈리는데, 아래 집계는 애초에 전 기간을
#   세는 칸이 없어서 견줄 상대가 안 생긴다. 시험 자료 거르기는 다른 계열과 똑같이
#   `apply_stats_filter` 한 함수로만 붙인다.


def _elapsed_seconds(later):
    """`created_at`에서 `later`까지 몇 초. PG16의 extract는 numeric을 돌려준다."""
    return func.extract("epoch", later - Alert.created_at)


def _duration_columns(prefix: str, later):
    """건수·평균·중앙값 세 칸을 만든다.

    셋 다 SQL 집계다 — 행을 파이썬으로 끌어와 세면 경고가 많은 날 응답이 통째로 느려지고,
    조회 상한을 없애려고 만든 창구가 다시 상한을 갖게 된다.

    `count(칸)`은 NULL을 안 세고, `avg`·`percentile_cont`도 NULL 행을 건너뛴다. 그래서
    확인이 한 건도 없는 갈래는 count=0에 나머지가 NULL이라 **나누는 자리가 아예 안 생긴다.**
    """
    seconds = _elapsed_seconds(later)
    return (
        func.count(later).label(f"{prefix}_count"),
        func.avg(seconds).label(f"{prefix}_avg"),
        func.percentile_cont(0.5).within_group(seconds).label(f"{prefix}_median"),
    )


def _round_seconds(value) -> float | None:
    """소수 둘째 자리 반올림. NULL은 그대로 NULL이다(0으로 안 채운다)."""
    return None if value is None else round(float(value), 2)


def _response_summary(row) -> AlertResponseSummary:
    return AlertResponseSummary(
        total=row.total,
        ack=StatsDurationSummary(
            count=row.ack_count,
            avg_seconds=_round_seconds(row.ack_avg),
            median_seconds=_round_seconds(row.ack_median),
        ),
        resolve=StatsDurationSummary(
            count=row.resolve_count,
            avg_seconds=_round_seconds(row.resolve_avg),
            median_seconds=_round_seconds(row.resolve_median),
        ),
    )


def _alert_response_stmt(
    *,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    by_type: bool,
    types: tuple[str, ...] | None = None,
):
    """대응 통계 질의 하나. `by_type`이면 종류로 묶고, 아니면 구간 전체 한 줄이다.

    전체 줄을 종류별 줄에서 합치지 않는 이유는 중앙값이다 — 평균·건수와 달리 중앙값은
    부분집합 값에서 다시 만들 수 없다. 그래서 같은 조건으로 질의를 한 번 더 돈다.

    ⛔ **`types`를 안 주면 안 가린다 — 그것이 기존 거동이다**(2026-08-07). 이 질의를
    **두 화면이 같이 쓴다.** 교대 결산은 무단 통과만 세야 하고, AI 탭 대응 통계는 "요원이
    알림에 얼마나 빨리 응답하나"라 종류를 안 가리는 것이 맞다. **여기서 기본값을 좁히면
    안 물어본 쪽 숫자가 조용히 바뀐다.**
    """
    columns = [
        func.count().label("total"),
        *_duration_columns("ack", Alert.acked_at),
        *_duration_columns("resolve", Alert.resolved_at),
    ]
    if by_type:
        columns.insert(0, Alert.type.label("type"))

    stmt = select(*columns).where(
        Alert.created_at >= start_utc, Alert.created_at < end_utc
    )
    if types:
        stmt = stmt.where(Alert.type.in_(types))
    if by_type:
        stmt = stmt.group_by(Alert.type)
    return apply_stats_filter(stmt, time_column=Alert.created_at)


async def _remaining_alerts(
    session: AsyncSession,
    *,
    start_utc: dt.datetime,
    end_utc: dt.datetime,
    types: tuple[str, ...] | None,
) -> int:
    """이 구간에서 아직 확인 안 된 경고 수.

    ⭐ **왜 여기서 세나**(2026-08-07 · 화면 58차). 교대 결산이 "남음"에 스냅샷의
    `active_alerts`를 쓰고 있었는데 그 값은 **종류도 기간도 안 가린다** — 종 아이콘이
    "요원이 확인할 알림 전부"를 세는 자리라 그쪽은 그게 맞다.

    ⛔ **그래서 그 함수를 고치는 대신 여기서 따로 센다.** 한 값을 두 화면이 다른 뜻으로
    쓰고 있었고, 공유 함수를 좁히면 종 아이콘 숫자가 조용히 같이 줄어든다.
    """
    stmt = (
        select(func.count())
        .select_from(Alert)
        .where(
            Alert.created_at >= start_utc,
            Alert.created_at < end_utc,
            Alert.ack.is_(False),
        )
    )
    if types:
        stmt = stmt.where(Alert.type.in_(types))
    return (
        await session.execute(apply_stats_filter(stmt, time_column=Alert.created_at))
    ).scalar_one()


async def alert_response_stats(
    session: AsyncSession, *, days: int, types: tuple[str, ...] | None = None
) -> AlertResponseStatsOut:
    """경고 대응 통계 한 벌. 구간은 미태깅 집계와 같은 `_resolve_range`로 잡는다.

    구간에 경고가 없어도 200이다 — 묶지 않은 질의는 빈 집합에도 한 줄을 돌려주므로
    overall.total=0에 평균·중앙값이 null인 응답이 나오고, by_type만 빈 배열이다.

    ⛔ **`types`를 안 주면 안 가린다.** 이 창구를 두 화면이 같이 쓰고 서로 다른 값을
    기대한다 — `_alert_response_stmt` 머리 참고.
    """
    start_date, start_utc, end_utc = await _resolve_range(session, days)

    overall_row = (
        await session.execute(
            _alert_response_stmt(
                start_utc=start_utc, end_utc=end_utc, by_type=False, types=types
            )
        )
    ).one()
    type_rows = (
        await session.execute(
            _alert_response_stmt(
                start_utc=start_utc, end_utc=end_utc, by_type=True, types=types
            )
        )
    ).all()
    remaining = await _remaining_alerts(
        session, start_utc=start_utc, end_utc=end_utc, types=types
    )

    # 건수 많은 순, 같으면 종류 이름 오름차순. 동점 순서를 못박아야 같은 자료에 같은 응답이 난다.
    by_type = [
        AlertResponseByType(type=row.type, **_response_summary(row).model_dump())
        for row in sorted(type_rows, key=lambda r: (-r.total, r.type))
    ]

    return AlertResponseStatsOut(
        range=StatsRange(
            days=days,
            start_date=start_date,
            end_date=start_date + dt.timedelta(days=days - 1),
            timezone=STATS_TIMEZONE,
        ),
        overall=_response_summary(overall_row),
        by_type=by_type,
        # ⚠ 스냅샷 `active_alerts`와 **다른 값이다** — 이쪽은 구간과 종류를 가린다.
        remaining=remaining,
        # 무엇으로 좁혔는지 응답에 남긴다. 화면이 "전 종류"와 "무단 통과만"을 같은 칸에
        # 그리면 요원이 두 수를 견줄 때 무엇끼리 견주는지 알 수 없다.
        types=list(types) if types else None,
    )


# ── 시간 버킷 집계 (프론트 요구 N12) ───────────────────────────────────────
# 이벤트 탭 추이 차트 두 칩이 읽는 자리다. 버킷팅을 서버에서 하는 이유는 오늘 집계와 같다 —
# 화면이 `/api/events`·`/api/alerts` 목록을 받아 직접 자르면 조회 상한(500건)에 걸려 바쁜
# 시간대가 실제보다 낮게 그려진다.
#
# ⚠ 버킷 경계 규칙(계약) — 두 창구 다 **시작 이상 끝 미만**이고, 이 저장소 다른 집계와 같다.
#   - 15분 칸은 **KST 벽시계 15분 눈금**(:00·:15·:30·:45)에 맞춘다. 한국은 DST가 없고
#     오프셋이 +09:00 정시라 그 눈금이 UTC 15분 눈금과 같은 자리다 — 그래서 이 격자만은
#     시간대 변환 없이 epoch 나눗셈으로 잡는다(변환이 없으니 파이썬·DB가 갈릴 자리도 없다).
#     창의 끝은 "지금"이 든 칸의 다음 눈금이라, 진행 중인 칸이 마지막 자리에 들어온다.
#   - 24시간 칸은 **KST 로컬 날짜 경계**(00:00 KST)다. 고정 24시간을 지금부터 거꾸로 세면
#     칸이 자정에 안 맞아 "어제"가 두 칸에 걸친다. 이쪽 변환은 다른 집계와 똑같이 전부
#     Postgres `timezone()`이 맡는다(모듈 docstring의 ⚠ 참고).


def _bucket_window(hours: int) -> tuple[dt.datetime, dt.datetime, int, int]:
    """15분 격자에 맞춘 (시작, 끝, 시작 epoch, 칸 수).

    끝은 "지금"이 든 칸의 다음 눈금이다. 지금을 그대로 끝으로 쓰면 마지막 칸이 15분이 아닌
    토막이 돼서, 진행 중인 칸이 늘 앞 칸보다 낮게 그려진다(차트가 항상 우하향으로 보인다).

    기준 시각은 `_now_utc()`라 시험이 못박을 수 있다(미태깅 집계와 같은 수법).
    """
    now_epoch = int(_now_utc().timestamp())
    end_epoch = (now_epoch // GATE_PASS_BUCKET_SECONDS + 1) * GATE_PASS_BUCKET_SECONDS
    count = hours * 3600 // GATE_PASS_BUCKET_SECONDS
    start_epoch = end_epoch - count * GATE_PASS_BUCKET_SECONDS
    return (
        dt.datetime.fromtimestamp(start_epoch, dt.timezone.utc),
        dt.datetime.fromtimestamp(end_epoch, dt.timezone.utc),
        start_epoch,
        count,
    )


async def gate_pass_buckets(
    session: AsyncSession, *, hours: int
) -> GatePassBucketsOut:
    """게이트 통과를 15분 칸으로 갈라 판정별로 센다. 빈 칸도 0으로 채워 돌려준다.

    시각 축은 `observed_at`(기기 관측)이다 — 다른 집계와 같은 이유로 서버 수신 시각을 안 쓴다.
    구간 필터는 변환 안 한 원본 칸에 그대로 걸고, 칸 번호만 식으로 만든다(왼쪽을 함수로
    감싸면 인덱스를 못 탄다).
    """
    start_utc, end_utc, start_epoch, count = _bucket_window(hours)

    # 칸 번호 = (관측 epoch − 시작 epoch) ÷ 900을 내림. 시작이 이미 눈금에 맞아서 0부터 센다.
    bucket_no = cast(
        func.floor(
            (func.extract("epoch", GatePassEvent.observed_at) - start_epoch)
            / GATE_PASS_BUCKET_SECONDS
        ),
        Integer,
    )
    stmt = (
        select(bucket_no.label("b"), GatePassEvent.verdict, func.count().label("n"))
        .where(
            GatePassEvent.observed_at >= start_utc,
            GatePassEvent.observed_at < end_utc,
        )
        .group_by(bucket_no, GatePassEvent.verdict)
    )
    stmt = apply_stats_filter(
        stmt,
        time_column=GatePassEvent.observed_at,
        device_column=GatePassEvent.device_id,
    )

    per_bucket: dict[int, list[tuple[str | None, int]]] = defaultdict(list)
    for row in (await session.execute(stmt)).all():
        per_bucket[row.b].append((row.verdict, row.n))

    buckets = [
        GatePassBucketPoint(
            start=start_utc + dt.timedelta(seconds=i * GATE_PASS_BUCKET_SECONDS),
            counts=_gate_pass_counts(per_bucket.get(i, ())),
        )
        for i in range(count)
    ]

    return GatePassBucketsOut(
        range=StatsBucketRange(
            start=start_utc,
            end=end_utc,
            bucket_seconds=GATE_PASS_BUCKET_SECONDS,
            bucket_count=count,
            timezone=STATS_TIMEZONE,
        ),
        total=sum(b.counts.total for b in buckets),
        buckets=buckets,
    )


# 심각도 칸 이름은 응답 필드 이름과 같아야 한다. 여기 없는 값(새 심각도·아직 null)은 other로 모은다.
# 값 셋은 경고를 만드는 자리에서 그대로 가져왔다 — state_machine.py("high")·robot_channel.py
# ("warning"·"info")·dispatch.py("info").
ALERT_SEVERITIES = ("info", "warning", "high")


def _severity_counts(rows) -> AlertSeverityCounts:
    """(심각도, 건수) 짝들을 응답 칸으로 접는다. 사전 밖 값과 null은 other로 모은다."""
    known = {s: 0 for s in ALERT_SEVERITIES}
    other = 0
    for severity, count in rows:
        if severity in known:
            known[severity] += count
        else:
            other += count

    return AlertSeverityCounts(
        total=sum(known.values()) + other,
        info=known["info"],
        warning=known["warning"],
        high=known["high"],
        other=other,
    )


async def alert_severity_buckets(
    session: AsyncSession, *, days: int
) -> AlertBucketsOut:
    """경고를 24시간(KST 하루) 칸으로 갈라 심각도별로 센다. 빈 날도 0으로 채운다.

    구간과 하루 경계는 미태깅 집계와 같은 `_resolve_range`를 쓴다 — 자르는 자리를 한 곳에
    모아야 이벤트 탭 두 칩이 서로 다른 "하루"를 그리지 않는다. 시각 축은 `created_at`이다
    (경고엔 기기 관측 시각이 따로 없다).
    """
    start_date, start_utc, end_utc = await _resolve_range(session, days)

    tz = cast(literal(STATS_TIMEZONE), Text)  # timezone() 오버로드를 text 쪽으로 못박는다
    local_date = cast(func.timezone(tz, Alert.created_at), Date)
    stmt = (
        select(local_date.label("d"), Alert.severity, func.count().label("n"))
        .where(Alert.created_at >= start_utc, Alert.created_at < end_utc)
        .group_by(local_date, Alert.severity)
    )
    stmt = apply_stats_filter(stmt, time_column=Alert.created_at)

    per_date: dict[dt.date, list[tuple[str | None, int]]] = defaultdict(list)
    for row in (await session.execute(stmt)).all():
        per_date[row.d].append((row.severity, row.n))

    dates = [start_date + dt.timedelta(days=i) for i in range(days)]
    buckets = [
        AlertBucketPoint(date=d, counts=_severity_counts(per_date.get(d, ())))
        for d in dates
    ]

    return AlertBucketsOut(
        range=StatsRange(
            days=days,
            start_date=start_date,
            end_date=dates[-1],
            timezone=STATS_TIMEZONE,
        ),
        total=sum(b.counts.total for b in buckets),
        buckets=buckets,
    )
