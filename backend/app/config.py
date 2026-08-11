"""환경변수 설정. DB 접속·API Key를 코드에 박지 않고 .env / 환경변수로 받는다."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schemas import Destination

# 저장소에 그대로 적혀 있는 개발용 기본키. 공개된 값이라 자물쇠 구실을 못 한다.
# security.secret_is_usable이 이 값을 WS 인증에서 거절하고, test_ws_auth가 아래 필드
# 기본값과 이 상수가 같은지 붙들어 둔다(한쪽만 바뀌면 거절이 조용히 풀린다).
INSECURE_DEFAULT_API_KEY = "dev-local-key"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # asyncpg 관통. 로컬/원격 전환은 이 값 하나로.
    database_url: str = "postgresql+asyncpg://postgres@127.0.0.1:5432/c207"
    # 기기 키. 인입 API·로봇 WS 채널이 쓴다. 배포 때 반드시 덮어쓴다.
    api_key: str = INSECURE_DEFAULT_API_KEY
    # 대시보드(브라우저) WS 채널 전용 키. **api_key와 다른 값**을 넣는다.
    #
    # 카드 S15P11C207-238 원문이 요구하는 분리다 — 브라우저가 기기 키를 들면 개발자 도구에
    # 그대로 보여서, 화면 하나가 새는 순간 인입·명령 창구까지 통째로 열린다. 그래서 대시보드
    # 채널은 이 값만 받고 api_key는 안 받는다.
    #
    # ⚠ 기본이 빈 값이다. WS_REQUIRE_API_KEY_DASHBOARD를 켜 놓고 이 값을 안 넣으면 대시보드
    #   접속이 전부 닫힌다(열어 두는 쪽보다 낫다). 그리고 이 키는 "사람용 인증"이 아니라 그
    #   자리를 메우는 임시 자물쇠다 — 진짜 답은 세션·역할 구분(M-6)이고 팀 결정 자리다.
    dashboard_api_key: str = ""
    # WS 메시지 version 필드 기본값
    ws_protocol_version: str = "1"
    # WebSocket 핸드셰이크에 키를 요구할까. 상위 스위치 하나 + 채널별 스위치 둘이다.
    #
    # ⚠ 셋 다 기본 False다. 켜는 순간 그 채널에 붙는 쪽이 전부 키를 실어야 한다 —
    # 코드 한 줄로 켜고 끄는 값이지만 현장 배선이 같이 움직여야 하는 결정이라, 언제 켤지는
    # 팀이 정한다(아키텍처 M-6 "정식 인증"). 지금 상태에서 익명 클라이언트가 /ws/robot에
    # 붙어 robot_state 프레임 한 장을 보내면 Alert 행 생성·ETA 표본 오염·명령 수신까지
    # 열린다는 게 2026-07-30 검토 실측이다.
    #
    # 채널을 가른 이유는 붙는 쪽 준비 상태가 다르기 때문이다. 로봇 채널은 젯슨·라파이만
    # 붙어서 배선을 우리가 쥐고 있지만, 대시보드 채널은 브라우저가 붙어 헤더를 못 실어
    # 쿼리(로그에 남는다)나 프록시 인증으로 가야 한다. 한 스위치로 묶어 두면 준비가 끝난
    # 로봇 채널까지 화면 사정에 발이 묶인다.
    ws_require_api_key: bool = False
    # 로봇 채널(/ws/robot)만 켜는 스위치. 상위 스위치가 켜져 있으면 이 값과 무관하게 켜진다.
    ws_require_api_key_robot: bool = False
    # 대시보드 채널(/ws/dashboard)만 켜는 스위치. 위와 같은 규칙이다.
    # ⚠ 켜기 전에 DASHBOARD_API_KEY를 넣고 화면 쪽이 그 키를 실을 수 있어야 한다. 지금
    #   화면(frontend/index.html)은 키를 실을 자리가 없어서, 이 스위치만 켜면
    #   화면이 무한 재접속만 돈다. 켜는 순서는 backend/README.md의 롤아웃 절에 있다.
    ws_require_api_key_dashboard: bool = False

    # ── 크레딧 상태머신 튜닝값 (아키텍처 2026-07-23 §03) ──────────────────
    # 크레딧 만료: 태깅 후 이 초가 지나면 무효. 비교 기준은 빔 A 시각(beam_a_ts).
    credit_ttl_sec: float = 3.0
    # 같은 게이트·같은 종류 경고 쿨다운. 게이트에 물건이 놓여 알림이 폭주하는 걸 막는다.
    # 이 값은 이벤트 시각 창과 서버 벽시계 창에 같이 쓰인다 — 둘 다 창 안일 때만 삼킨다.
    alert_cooldown_sec: float = 10.0
    # 미소비 유효 크레딧이 이 수를 넘으면 대시보드에 신뢰도 낮음 플래그를 실어 보낸다.
    low_confidence_credit_threshold: int = 3

    # ── 로봇 outbound WebSocket (아키텍처 2026-07-23 §02) ─────────────────
    # 서버→로봇 하트비트 주기. 계약은 10~20초.
    ws_heartbeat_sec: float = 15.0
    # pong이 이 횟수만큼 연속으로 안 오면 커넥션을 스테일로 보고 닫는다.
    ws_heartbeat_max_miss: int = 2
    # 로봇이 마지막으로 보고한 지 이만큼 지나면 **붙어 있다고 안 센다.**
    #
    # ⛔ **`ws_heartbeat_sec`으로 계산하면 안 된다**(2026-08-07에 실제로 그렇게 했다가 AI
    # 갈래가 잡았다). 둘은 **방향이 반대인 다른 물건**이다.
    #
    #     ws_heartbeat_sec   서버 → 로봇   서버가 얼마나 자주 찌르나
    #     robot_stale_sec    로봇 → 서버   로봇 보고가 얼마나 뜸하면 끊긴 걸로 보나
    #
    # ⭐ **판단 기준은 "로봇이 마지막으로 보고한 지 얼마나 됐나"**이지 "서버가 얼마나 자주
    # 찔렀나"가 아니다. 요원 화면도 그 기준이고, 화면 값이 25초다(`STALE_S =
    # HEARTBEAT_MAX_S(20) + 5`). **여기를 25로 맞춰야 화면·브리핑·챗봇이 같은 말을 한다.**
    #
    # ⚠ 설정을 따로 뺀 까닭 — 하트비트 주기를 조절하는 것과 신선도 문턱을 정하는 것은
    # 다른 결정이다. 묶어 두면 주기를 바꾸는 순간 브리핑 판정이 조용히 따라 움직인다.
    robot_stale_sec: float = 25.0
    # 로봇 명령 유효 시간(ms). 명령 ID·TTL로 재전송·순서 역전을 흡수한다.
    command_ttl_ms: int = 30000
    # 셔틀 도착 신호를 받았을 때 로봇에 지시할 목적지. 팀 확정 대기라 설정값으로 뺀다.
    # 타입이 Destination이라 command.schema.json 허용 밖 값을 넣으면 기동 시점에 터진다.
    shuttle_destination: Destination = Destination.OUTDOOR_TAGGING
    # 셔틀 신호 ↔ 로봇 도착 짝짓기 유효 시간. 이 시간을 넘긴 미짝 신호는 만료로 처리해
    # 뒤에 온 도착이 옛 신호에 잘못 붙지 않게 한다.
    arrival_match_ttl_sec: float = 900.0
    # 같은 값 ARRIVED 재보고를 "새 도착"으로 칠까(기본 꺼짐).
    # 켜면 그 로봇 앞으로 직전 도착 뒤에 새 목적지 명령이 나갔을 때 재출동 도착으로 친다.
    # 명령이 붙어 있는 로봇 전부에게 브로드캐스트되는 한 게이트에 서 있는 로봇도 새 신호마다
    # 명령을 받아, 다음 주기 보고가 가짜 도착이 된다. 그래서 기본은 전이 에지만 본다.
    # 연속 재출동을 제대로 잡는 길은 mission_status enum 확장(월요일 팀 안건)이다.
    arrival_edge_on_recommand: bool = False
    # 로봇별 mission_status 에지 추적 상태를 들고 있는 시간. 마지막 보고가 이 시간을 넘으면
    # 버린다(사라진 로봇의 인메모리 상태가 무한히 쌓이는 걸 막는다). 상태를 버리는 길은 이
    # 청소 하나뿐이다 — 커넥션이 끊겼다고 버리면 재접속 첫 보고가 가짜 도착이 된다.
    # "마지막 보고"는 mission_status가 실린 보고만이 아니라 그 로봇의 모든 상태 보고다 —
    # 주행 중엔 mission_status를 비우고 올려서, 실린 보고만 세면 달리는 로봇의 상태가 걷힌다.
    mission_state_ttl_sec: float = 3600.0

    # ── ETA (역할상세 §5 · 아키텍처 §07 P1) ───────────────────────────────
    # 구간 소요시간 EWMA 계수. 클수록 최근 주행에 민감하다.
    eta_ewma_alpha: float = 0.3
    # 기본 표본 수(최근 주행 몇 건까지 볼까).
    eta_sample_limit: int = 20
    # 이 초를 넘는 구간 기록은 이상치로 버린다(신호만 오고 로봇이 한참 뒤 도착한 경우).
    eta_max_sample_sec: float = 3600.0

    # ── [초안·팀 확정 대기] 안건① 룸 단위 크레딧 ─────────────────────────
    # 켜면 크레딧 소비·회수·쿨다운 범위가 (gate_no, room_id)로 좁아진다.
    # 기본 False라 기존 동작(게이트 단위)이 그대로다. 팀 확정 전까지 켜지 않는다.
    credit_scope_room: bool = False

    # ── [초안·팀 확정 대기] 안건② event_id 안 boot_id ────────────────────
    # 켜면 boot_id 없는 예전 3토막 event_id를 422로 막는다. 기본 False라 예전 형식도
    # 새 형식도 다 받는다. 현장 기기가 전부 새 형식으로 넘어간 뒤에 켠다.
    event_id_require_boot_id: bool = False

    # ── ✅ 확정됨 — mission_status EN_ROUTE ──────────────────────────────
    # ⭐ **2026-08-06에 열었다.** 프론트(38차 §1-1)와 젯슨(회신 §7)이 **같이 요청했다** —
    # 나머지 넷이 전부 "끝났다" 계열이라 출발을 담을 낱말이 없었고, 그래서 도착 표시가
    # 다음 도착까지 안 지워져 화면이 복귀 중에도 태깅 판정을 켜고 있었다.
    #
    # ⚠ **실제 판정은 이 스위치가 아니라 `MissionStatus` enum이 한다**(`jetson_adapter`가
    # enum 밖 값을 거른다). 이 값을 읽는 자리는 초안 모듈 `app/robot/mission_state.py` 하나뿐이라,
    # 여기만 끄고 enum을 늘리면 EN_ROUTE는 그대로 통과한다. 끄는 스위치로 믿지 마라.
    mission_status_en_route_enabled: bool = True

    # ── Mattermost 웹훅 (S15P11C207-82) ──────────────────────────────────
    # 스위치는 URL 하나다 — 비어 있으면 조용히 스킵이 기본이고, 넣으면 그때부터 발사한다.
    # URL만 넣고 아무 일도 안 일어나는 쪽이 배포에서 더 위험한 함정이라 이렇게 잡았다.
    # mattermost_enabled는 URL을 지우지 않고 발사만 멈추는 킬 스위치라 기본이 None(미지정)이다.
    # false를 명시하면 URL이 있어도 안 쏜다.
    mattermost_enabled: bool | None = None
    mattermost_webhook_url: str = ""
    # 신원 확인 보고용 두 번째 웹훅. 미태깅 경고 채널과 다른 채널(상위 보고선)로 나간다.
    # 채널을 아직 안 만들었으니 기본은 빈 문자열 = 꺼짐이고, 비면 조용히 건너뛴다.
    # 미태깅 URL과 같은 값을 넣으면 한 채널에 두 종류가 섞여 붙는다 — 나누려고 만든 값이니
    # 채널을 따로 파서 그 주소를 넣는다.
    mattermost_identify_webhook_url: str = ""

    # ── 관제 보조 LLM (GMS OpenAI 호환 프록시) ────────────────────────────
    # 스위치는 키 하나다 — 비어 있으면 LLM을 아예 안 부르고 규칙 기반 폴백 문장으로 간다.
    # 실키는 .env에만 두고 절대 커밋하지 않는다(.env.example엔 자리만).
    gms_api_key: str = ""
    # 빈 값이면 ai/control_assistant/config.py의 DEFAULT_BASE_URL·DEFAULT_MODEL을 쓴다(기본값 정본 한 곳).
    gms_base_url: str = ""
    gms_model: str = ""
    # 관제 화면이 기다리는 시간이라 짧게 잡는다. 넘기면 폴백이 대신 나간다.
    # ⚠ **위(nginx)보다 확실히 작아야 한다.** `deploy/nginx-c207-api.conf` 의 관제 보조 블록이
    #    `proxy_read_timeout 60s` 다. 둘이 같아지면 백엔드가 59초에 답해도 위에서 먼저 끊어
    #    504 가 나가고 폴백 문장조차 화면에 못 간다.
    # ⭐ **2026-08-08 사용자 확정 — 20초로 굳힌다.** 로컬 LLM 전환 뒤 찬 첫 호출이 13.12초라
    #    (근거 목록을 여섯 건으로 줄여 25.84초에서 내려온 값이다) 8초로는 시연 첫 질문이
    #    규칙 문장으로 떨어진다. 예전 확정값 12초도 13.12초 앞에서는 같은 결과다.
    # ⛔ **기본값 자리가 여기라는 게 중요하다.** 08-07에 `backend.env`에만 20을 넣어 뒀는데
    #    그 파일은 `.gitignore` 라, 새 배포·롤백·시험 환경으로 가면 조용히 8초로 떨어졌다
    #    (08-08 백지검토 ⑨). 화면은 안 깨지고 답만 규칙 문장이 되어 아무도 못 알아챈다.
    gms_timeout_sec: float = 20.0
    gms_max_tokens: int = 400
    # 하루에 LLM 을 몇 번까지 부르나. 넘기면 규칙 문장(폴백)이 나가고 응답 `meta.fallback_reason`
    # 이 `daily_budget` 으로 말한다.
    #
    # ⭐ **2026-08-07 신설.** 그 전에는 `routers/assistant.py` 가 `getattr(settings,
    # "assistant_daily_llm_calls", 300)` 로 읽었는데 **이 이름이 `Settings` 에 아예 없어서**
    # 늘 기본값 300 으로 조용히 떨어졌다. 오타가 아니라 없는 이름이라 로그도 안 남았다
    # (AI 갈래 3차 ③이 잡았다). [[feedback_getattr_missing_name_reads_false]] 계열이다.
    #
    # ⚠ **기본값은 300 그대로 둔다.** GMS 는 호출마다 크레딧을 태우므로 이 숫자가 안전판이다.
    #    로컬 LLM 은 크레딧을 안 쓰니 `.env` 에서 올린다 — 필드를 만드는 것과 값을 바꾸는 것을
    #    갈라야 GMS 로 되돌아갈 때 안 헷갈린다.
    # ⚠ **0 이하로 두면 LLM 을 아예 안 부른다**(`_take_llm_budget` 이 곧장 False 다).
    #    "무제한"이 아니라 "끄기"다.
    assistant_daily_llm_calls: int = 300
    # 일일 요약 캐시가 오늘치를 얼마나 오래 재사용하나(초). 지난 날짜는 자료가 안 바뀌니
    # 이 값과 무관하게 영구 재사용이다.
    #
    # ⚠ 짧으면 LLM 을 자주 부르고 길면 요약이 낡는다. 30분은 "요원이 개요 탭을 다시 열
    # 만한 간격"으로 잡은 값이라 실사용을 보고 조절할 자리다. 0 이하면 캐시를 끈다 —
    # 매번 새로 만들던 예전 거동으로 돌아간다.
    assistant_brief_ttl_sec: int = 1800
    # 기동할 때 관제 보조 프롬프트 캐시를 데울까.
    #
    # ⛔ **시험에서는 꺼야 한다**(`tests/conftest.py`). 켜 두면 케이스마다 lifespan 이 돌면서
    # 데우기 작업이 이벤트 루프에 쌓이고, 루프를 세션으로 공유하는 구조라 **뒤 케이스가
    # 무더기로 무너진다**(2026-08-07에 실제로 밟았다 — 단독으로는 다 통과하는데 전량에서만
    # 905건이 에러였다).
    #
    # ⚠ 실서버는 기동이 한 번뿐이라 그 자리가 안 생긴다.
    assistant_warm_up_on_start: bool = True

    # ── 셔틀 도착 → 출동 대기 창 (셔틀출동 설계 2026-07-31 §2.2) ──────────
    # 셔틀 신호를 받고 목적지 명령을 붙들고 있는 시간. 이 창 안에 요원이 실내·실외·대기 중
    # 하나를 고르면 그대로 나가고, 아무도 안 고르면 그날 기본 목적지로 자동 발사한다.
    #
    # ⚠ 값이 `shuttle_call.SHUTTLE_CALL_COOLDOWN_SEC`(5.0)와 **정확히 같다.** 둘은 전혀
    # 다른 일을 한다 — 쿨다운은 같은 게이트 연타를 429로 삼키는 입구 방어고, 이 값은 이미
    # 받아들인 신호 하나를 붙들고 있는 시간이다. 연타 시험에서 무엇이 막았는지 헷갈리지
    # 않게 로그 머리를 갈라 뒀다([게이트 연타 차단] ↔ [출동 대기 창], 설계 §6.1).
    # 0 이하로 두면 대기 없이 예전처럼 즉시 발사한다(되돌릴 자리).
    shuttle_dispatch_hold_sec: float = 5.0
    # 출동 창구(/api/dispatch/*)에 X-API-Key를 요구할까. 기본 False다 — 켜는 순간 관제
    # 화면도 키를 실어야 붙어서 현장 배선이 같이 움직인다(ws_require_api_key와 같은 결).
    # 끈 채로 공개 도메인에 두면 아무나 로봇을 보내거나 세울 수 있다는 게 2026-07-31 검증
    # 지적이고, 진짜 경계(프록시 인증·세션 쿠키)를 어디에 둘지는 팀·사용자 결정 자리다.
    dispatch_require_api_key: bool = False
    # 즉시 이동·복귀를 이 초 안에 다시 부르면 429(Retry-After 포함). 인증이 없는 창구라
    # 이게 유일한 유량 제한이다. ⚠ 긴급정지 창구는 여기서 빠진다 — 세우기도 풀기도 막히면
    # 안 된다. 0 이하면 제한을 끈다.
    dispatch_command_min_interval_sec: float = 1.0

    # ── 날씨 조회 (셔틀출동 설계 2026-07-31 §5) ───────────────────────────
    # 키가 필요 없는 wttr.in. 기상청 키는 8/2 이후에나 신청할 수 있어서 1차는 여기로 간다.
    # 갈아 끼울 자리는 app/weather.py의 fetch_current_weather 하나다.
    weather_api_url: str = "https://wttr.in/Gwangju?format=j1"
    # 같은 운영자가 둔 대체 도메인. 1차가 죽으면 여기로 한 번 더 간다. 제작자 README가
    # 스크립트 용도엔 wttr.is를 직접 권한다(조사_백엔드5과제_웹검증_2026-07-31 §T2,
    # 같은 j1 본문을 주는 건 2026-07-31 실측). 비우면 사슬 없이 1차 하나만 탄다.
    weather_api_fallback_url: str = "https://wttr.is/Gwangju?format=j1"
    # 화면이 기다리는 시간이라 짧게 잡는다. 넘기면 마지막 성공값으로 폴백한다.
    weather_timeout_sec: float = 4.0

    # ── 기상청 단기예보 (2026-08-03 키 발급·실측) ─────────────────────────
    # ⭐ 인증키는 **디코딩 판(88자, `==`로 끝나는 것)**을 넣는다. 포털이 인코딩 판(92자,
    # `%3D%3D`)도 같이 주는데, 그걸 params dict로 넘기면 httpx·requests가 퍼센트를 한 번
    # 더 인코딩해 `%253D%253D`로 나가고 SERVICE_KEY_IS_NOT_REGISTERED_ERROR가 떨어진다
    # (2026-08-03 실측 재현). weather.py가 인코딩 판을 받아도 한 번 풀어 주지만, 애초에
    # 디코딩 판을 넣는 게 맞다.
    #
    # ⚠ 이 값이 비어 있으면 기상청 경로를 아예 안 탄다 — 위 wttr 사슬만 돈다. 그래서
    #   .env에 키를 안 넣은 기기·시험에서는 거동이 예전과 같다.
    # ⚠ pydantic-settings는 .env를 자기 안으로만 읽고 os.environ에 안 흘린다. 그래서
    #   이 필드가 **여기 있어야** .env의 KMA_SERVICE_KEY가 먹는다.
    kma_service_key: str = ""
    # 기상청 격자 좌표. **5번 게이트(광주 광산구 하남산단6번로 133) 기준 57·75다.**
    #
    # ⚠ 2026-08-04 정정 — 08-03에는 58·74였는데 그건 광주 시청(서구 치평동) 칸이다.
    # 격자가 5km 칸이라 같은 광주 안에서도 갈린다. 사용자가 5번 게이트 자리를 알려 줘서
    # 격자 변환(Lambert Conformal Conic 표준식)으로 다시 재니 57·75였고, 하남산단 일대를
    # 위경도 0.005도 간격으로 훑어도 전부 같은 칸이라 경계에 안 걸린다.
    #
    # 두 칸이 실제로 값이 다르다 — 같은 시각 실황이 시청 28.5도·습도 75%, 게이트 27.1도·
    # 습도 98%였다. 비·눈 판정은 잘 안 갈리겠지만 화면에 기온을 띄우므로 맞춘다.
    kma_nx: int = 57
    kma_ny: int = 75
    # 오퍼레이션 앞까지의 주소. 뒤에 /getUltraSrtNcst·/getUltraSrtFcst가 붙는다.
    kma_base_url: str = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
    # 1시간 강수량(mm)이 이 값 이상이면 HEAVY_RAIN. 실내·실외 판정은 안 갈리고 화면 칩
    # 문구만 갈린다 — 비가 오면 어느 쪽이든 실내다.
    kma_heavy_rain_mm: float = 3.0
    # 주기 조회 간격(초). 5분이면 두 오퍼레이션 합쳐 하루 576건이라 한도(오퍼레이션마다
    # 1만 건)의 6%다. 0 이하면 주기 조회를 끄고 예전처럼 부를 때만 갱신한다.
    weather_poll_interval_sec: float = 300.0
    # 기상청이 죽었을 때 위 wttr 사슬로 물러설까. 끄면 마지막 성공값에 stale 표시만 단다.
    weather_wttr_fallback_enabled: bool = True
    # ⛔ 날씨가 기본 목적지를 자동으로 바꿀까. **시연 기간에는 꺼 둔다**(2026-08-03 사용자 확정).
    #
    # 끄면 날씨 조회·화면 갱신은 그대로 돌고 **목적지만 안 건드린다.** 칩은 실제 날씨를
    # 계속 보여주고, 발표에서 "날씨를 보고 판단한다"는 설명도 그대로 할 수 있다.
    #
    # 왜 껐나 — 실내 태깅 도착 태그가 시연 배치에 없다(시연 범위 밖이라 안 만들었다).
    # 그 상태에서 비가 오면 서버는 기본값을 실내로 넘기는데 젯슨은 그 명령을 외부 경로로
    # 태운다. 화면은 "못 나간다"인데 로봇은 나가는 어긋남이 발표 중에 보인다.
    # 게다가 전환이 한 방향 걸쇠라(비가 그쳐도 안 되돌림) 아침 소나기 한 번이 그날을
    # 통째로 잠근다. 광주 격자가 5km 칸이라 시연장에 안 와도 잡힐 수 있다.
    #
    # ⚠ 시연이 끝나고 실내 지점이 생기면 켠다. 켤 때 같이 볼 것 — 되돌리는 길(비가 그치면
    # 어떻게 할까)과 요원이 손으로 되돌릴 자리다.
    weather_auto_default_enabled: bool = False
    # 시간대별 예보(GET /api/weather/forecast)를 몇 초 동안 다시 안 부를까. 예보는 1시간
    # 단위라 짧게 잡을 이유가 없다. 0이면 매 요청이 바깥을 탄다.
    #
    # ⭐ 600 → 3600(2026-08-05 프론트 17차). **기상청 발표는 3시간마다**이고 캐시 열쇠에
    #   발표 슬롯이 들어 있어서, 새 발표가 나오면 이 값과 상관없이 저절로 무효가 된다.
    #   그래서 낡은 값을 보여 줄 위험은 열쇠가 이미 막고, 600은 **자료가 그대로인 동안에도
    #   10분마다 바깥을 다시 타는** 값일 뿐이었다. 미스가 "열에 한 번꼴"에서 "발표마다
    #   한 번"으로 줄어 화면이 기다리는 시간도 그만큼 줄어든다.
    weather_forecast_cache_sec: float = 3600.0
    # 예보를 며칠치 줄까. 1이면 오늘만이다. 5일치까지 오지만 모레 이후는 관제 업무와
    # 관계가 없어 둘로 잘랐다(2026-08-03 사용자 확정).
    weather_forecast_days: int = 2

    # ── 실시간 카메라 프록시 (2026-08-03, S15P11C207-398) ─────────────────
    # 젯슨 미리보기(MJPEG) 주소. 검출 노드가 카메라를 점유해서 그 노드가 내주는 자리를
    # 그대로 받아 넘긴다. ⚠ 발표장 망이 바뀌면 여기만 고친다.
    camera_stream_url: str = "http://192.168.0.13:8090/stream"
    # 동시 접속 상한. 넘치면 503이다.
    #
    # ⚠ **예전 주석("젯슨이 매 프레임 JPEG 인코딩을 하므로 여럿이 붙으면 주행 중 태그 검출이
    # 밀린다")은 틀렸다.** 2026-08-05에 젯슨 실물을 열어 확인했다 —
    # `robot_perception/tag_localizer_cv.py`가 프레임을 **한 번 인코딩해 `node._jpeg`에 담고**,
    # 각 클라이언트 핸들러는 그 값을 **읽어서 쓰기만** 한다. 그래서 **보는 사람이 늘어도 젯슨
    # CPU 부담은 안 늘고**, 늘어나는 것은 대역폭과 소켓 쓰기뿐이다.
    #
    # ⭐ 그 사실을 확인하고 **3에서 6으로 올렸다**(2026-08-05). 발표장 셈이 근거다 —
    # 관제 화면 두 대 + 심사위원 폰 하나 + 팀원 확인 하나 = 넷이고 여유 둘을 뒀다.
    # 프론트가 재접속을 "그림이 멈췄을 때만"으로 고쳐서 화면 하나가 자리 하나만 쓴다
    # (예전에는 15초마다 다시 물어 순간 두 자리를 먹었다).
    #
    # ⚠ 카운터가 프로세스 안에 있어 워커를 늘리면 그만큼 곱해진다(지금 Dockerfile이
    # `--workers 1`이다). `camera.py`의 `_note_worker_assumption`이 그 전제를 스스로 본다.
    camera_max_viewers: int = 6
    # 상류 연결 타임아웃. "젯슨이 꺼져 있다"를 빨리 알아채는 값이라 짧게 잡는다.
    camera_connect_timeout_sec: float = 3.0
    # 프레임 읽기 타임아웃. 젯슨 프레임 간격이 0.01초라 5초면 넉넉하다.
    camera_read_timeout_sec: float = 5.0
    # 스트림 한 벌의 총 수명 상한(초). 넘으면 끊고 자리를 되돌린다.
    # ⚠ **필드가 여기 없으면 환경변수로 못 바꾼다** — `_setting`이 `Settings`를 먼저 보고
    # 없으면 코드 상수(`camera.DEFAULT_MAX_STREAM_SEC`)로 떨어지기 때문이다. 발표장에서 값을
    # 바꿔야 할 때 이미지를 다시 굽는 일이 없게 여기 둔다. 0 이하면 상한을 끈다.
    camera_max_stream_sec: float = 900.0
    # ⭐ 밀린 프레임을 버리고 **최신 한 장만** 흘린다(실시간 우선).
    # 2026-08-05 프론트 실측 — 상류가 초당 20.2장인데 브라우저 `<img>`는 7~8장만 그린다.
    # 매초 12장씩 밀려 5초면 60장 넘게 뒤처지고, 사용자가 "영상이 5초쯤 늦다"고 본 자리다.
    # 버퍼링이 아니라 **브라우저 디코더 큐**가 밀리는 것이라 서버가 덜 보내야 풀린다.
    # 끄면 예전대로 조각을 받는 대로 흘린다(되돌릴 자리). 자세한 근거는 routers/camera.py 머리.
    camera_realtime_drop: bool = True

    # ── 로그인 (로그인 설계 확정본 2026-08-01 §1·§6.2) ────────────────────
    # ⭐ 제1 불변 — 이 값이 false인 동안 **창구 거동이 글자 그대로 지금과 같다.**
    # 역할 게이트(require_agent 등)는 전부 통과시키고, 기존 키 게이트(require_api_key·
    # WS 키·dispatch 키)도 옛 경로 그대로 산다. 로그인 창구 셋(/api/auth/*)만 이 값과
    # 무관하게 늘 살아 있어서, 켜기 전에 계정을 심고 실측할 수 있다(§6.2 3단계).
    #
    # ⚠ 딱 하나 예외가 있다. 교차 오리진 상태변경을 막는 Origin 가드(§1.2,
    # app/security.py OriginGuardMiddleware)는 **이 플래그 밖에서 항상 돈다** — 켜고 끄는
    # 스위치가 따로 없다. 그래서 false 구간에서도 "브라우저가 교차 오리진에서 보낸 쓰기"만은
    # 403이다. Origin 헤더가 없는 갈래(젯슨 인입·curl·젠킨스)와 같은 오리진 쓰기는 그대로다.
    #
    # 켜는 순서가 뒤집히면 아무도 못 들어가는 잠금이 난다 — 계정 심기(2단계)를 건너뛰고
    # 켜지 않는다. 롤백은 이 값 하나만 되돌리면 된다.
    auth_require_login: bool = False
    # 세션 쿠키에 Secure를 붙일까. 배포는 443+certbot이라 기본이 True고, http로 도는
    # 로컬 개발·시험만 false로 끈다. ⚠ true인 채 http로 띄우면 브라우저가 쿠키를 아예
    # 저장하지 않아 로그인이 매번 튕긴 것처럼 보인다.
    session_cookie_secure: bool = True
    # 절대 만료(초). 발급 시각 + 이 값이고 **어떤 활동으로도 안 늘어난다**(§1.4).
    session_absolute_ttl_sec: float = 43200.0   # 12시간
    # 유휴 만료(초). last_seen_at + 이 값. 활동하면 밀린다.
    session_idle_ttl_sec: float = 7200.0        # 2시간
    # last_seen_at 갱신 최소 간격(초). 화면이 주기로 조회를 때리므로 매 요청 UPDATE는
    # 안 한다 — 이 시간이 지났을 때만 쓴다(쓰기 폭주 방어).
    session_touch_min_interval_sec: float = 60.0
    # 만료된 세션 행을 몇 일 뒤에 지울까. 지우는 자리는 로그인 창구 하나다(크론 없음).
    session_purge_after_days: float = 7.0
    # 한 계정이 동시에 들고 있을 수 있는 살아 있는 세션 수. 로그인이 이 수를 넘기면 가장
    # 오래된 세션부터 무효화한다(2026-08-02 백지 검토 F21). 로그인은 세션을 쌓기만 하고
    # 낱장으로 끊을 창구가 없어서, 상한이 없으면 옛 토큰이 절대 만료(12시간)까지 그대로
    # 산다. 값 5는 요원 한 명이 관제 화면·노트북·폰까지 겹쳐 써도 안 걸리는 선이다.
    # 0 이하면 상한을 끈다(되돌릴 자리).
    session_max_per_user: int = 5
    # 로그인 실패 잠금 — 계정당 이 횟수만큼 연속 실패하면 아래 초만큼 잠근다(§8 결정 3
    # 확정: 5회·60초로 시작하고 시연 리허설에서 걸리적거리면 그때 조정).
    login_fail_max_attempts: int = 5
    login_fail_lock_sec: float = 60.0
    # 교차 오리진 상태변경을 막는 보강 한 겹(§1.2)이 **추가로** 받아 줄 오리진. 콤마로 여럿.
    # 비워 두는 게 기본이고 그때는 자기 오리진 + 로컬 오리진만 통과한다 — 배포는 SPA와 API가
    # 같은 오리진이라(nginx 실측) 아무것도 안 적어도 그대로 돈다.
    # ⚠ 로컬 개발(정적 서버를 몇 번 포트로 띄우든)도 여기에 안 적어도 된다. localhost·127.0.0.1은
    # 포트·스킴과 무관하게 기본 허용이다(근거는 app/security.py origin_allowed).
    # 그래서 이 값이 필요한 건 로컬도 자기 오리진도 아닌 자리에서 화면을 띄울 때뿐이다.
    # 예: AUTH_ALLOWED_ORIGINS=https://c207.example
    # ⚠ Origin 헤더가 아예 없는 요청(젯슨 인입·curl·젠킨스)은 이 값과 무관하게 통과한다.
    auth_allowed_origins: str = ""

    @property
    def ws_auth_required_robot(self) -> bool:
        """로봇 채널 핸드셰이크에 키가 필요한가. 상위 스위치 OR 채널 스위치."""
        return self.ws_require_api_key or self.ws_require_api_key_robot

    @property
    def ws_auth_required_dashboard(self) -> bool:
        """대시보드 채널 핸드셰이크에 키가 필요한가. 상위 스위치 OR 채널 스위치."""
        return self.ws_require_api_key or self.ws_require_api_key_dashboard

    @property
    def mattermost_active(self) -> bool:
        """지금 웹훅을 쏘는 상태인가. URL이 있고 명시적으로 꺼두지 않았을 때만."""
        return bool(self.mattermost_webhook_url) and self.mattermost_enabled is not False

    @property
    def mattermost_identify_active(self) -> bool:
        """신원 보고 웹훅을 쏘는 상태인가.

        mattermost_enabled를 같이 본다 — 그 값은 매터모스트 발사 전체를 멈추는 킬 스위치라,
        false로 꺼둔 배포에서 신원 카드만 몰래 나가면 킬 스위치가 거짓말이 된다.
        """
        return (
            bool(self.mattermost_identify_webhook_url)
            and self.mattermost_enabled is not False
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
