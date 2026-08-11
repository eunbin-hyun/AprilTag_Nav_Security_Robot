"""[초안·팀 확정 대기] 안건② event_id에 boot_id 넣기.

지금 형식은 `{device_id}-{unix_ms}-{seq}`이고 seq는 라파이가 재기동할 때마다 1부터
다시 센다. unix_ms까지 겹치면(NTP 되감김·같은 밀리초 재기동·시각 미동기 부팅)
서로 다른 이벤트가 같은 event_id를 갖는다. 서버는 event_id UNIQUE로 멱등을 걸어서
두 번째 이벤트를 "재시도 중복"으로 보고 조용히 버린다 — 에러도 로그도 안 남는
조용한 유실이라 시연 중에 알아채기 어렵다.

정공법은 부팅마다 새로 뽑는 boot_id를 event_id에 넣는 거다.

형식 선택지 두 가지(회의에서 하나 고르면 이 파일 상수 한 줄만 바꾸면 된다).
- 안 A. `{device_id}-{boot8}-{unix_ms}-{seq}` — boot 자리에 8자리 hex를 그냥 넣는다.
  짧지만 device_id 끝 토막이 우연히 8자리 hex면 파싱이 흔들린다(예: "gate-deadbeef").
- 안 B(여기 기본값). `{device_id}-b{boot8}-{unix_ms}-{seq}` — boot 자리에 `b` 표식을
  붙인다. 한 글자 늘지만 파싱이 결정론이라 device_id에 하이픈이 몇 개 있든 안전하다.

멱등 영향 정리.
- event_id는 여전히 UNIQUE 키 하나뿐이라 서버 멱등 로직은 한 줄도 안 바뀐다.
  길이만 9~10자 늘고 상한 128자 안에 넉넉히 들어간다.
- 바뀌는 건 "가짜 중복"이 사라지는 방향뿐이다. 재기동 뒤 seq가 1로 돌아가도
  boot_id가 달라서 새 행으로 들어간다.
- 대신 퍼블리셔 쪽 계약이 하나 생긴다. boot_id는 프로세스가 살아 있는 동안 고정이어야
  한다. 재시도할 때마다 새로 뽑으면 같은 이벤트가 여러 행이 되어 멱등이 깨진다.
- 예전 3토막 형식은 그대로 받는다(legacy=True). 강제 전환은 플래그로만 연다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 안 B 표식. 안 A로 갈 거면 이 정규식에서 `b` 하나만 빼면 된다.
# 대소문자는 회의에서 확정한다. 초안은 양쪽을 다 받아들이고 소문자로 정규화해서
# 같은 부팅이 대소문자만 다르게 와도 한 값으로 모이게 했다(표식 `b`는 소문자 고정).
BOOT_SEGMENT_RE = re.compile(r"^b([0-9a-fA-F]{8})$")


def _is_ascii_digits(text: str) -> bool:
    """int()가 확실히 먹는 ASCII 숫자만 참.

    str.isdigit()은 위첨자(`²`)·이집트 숫자 같은 유니코드 숫자에도 참이라,
    그대로 int()에 넘기면 ValueError가 터진다. 파서는 예외를 안 던지기로 약속했으니
    ASCII 여부를 같이 본다. 아랍-인도 숫자(`٣`)처럼 int()가 먹는 값도 여기서 걸러
    "형식이 안 맞으면 None"이 흔들리지 않게 했다.
    """
    return text.isascii() and text.isdigit()


@dataclass(frozen=True)
class EventIdParts:
    device_id: str
    boot_id: str | None   # 표식 `b`를 뗀 알맹이 8자리 hex(소문자 정규화). 예전 형식이면 None.
    unix_ms: int
    seq: int

    @property
    def legacy(self) -> bool:
        return self.boot_id is None


def parse_event_id(event_id: str) -> EventIdParts | None:
    """event_id를 토막낸다. 형식이 안 맞으면 None(예외를 안 던진다).

    event_id는 서버 입장에서 그냥 불투명한 멱등 키라, 형식이 낯설다고 인입을 막지
    않는다. 판단은 부르는 쪽에 맡긴다.

    boot_id는 대문자로 와도 받아서 소문자로 정규화해 돌려준다.
    """
    parts = event_id.split("-")
    if len(parts) < 3:
        return None
    if not (_is_ascii_digits(parts[-1]) and _is_ascii_digits(parts[-2])):
        return None
    seq = int(parts[-1])
    unix_ms = int(parts[-2])

    if len(parts) >= 4:
        marked = BOOT_SEGMENT_RE.match(parts[-3])
        if marked:
            return EventIdParts(
                device_id="-".join(parts[:-3]),
                boot_id=marked.group(1).lower(),
                unix_ms=unix_ms,
                seq=seq,
            )
    return EventIdParts(
        device_id="-".join(parts[:-2]), boot_id=None, unix_ms=unix_ms, seq=seq
    )


def format_event_id(device_id: str, boot_id: str | None, unix_ms: int, seq: int) -> str:
    """퍼블리셔가 쓸 형식기. boot_id가 없으면 예전 3토막 그대로 만든다.

    boot_id는 소문자로 정규화해서 붙인다(파서 정규화랑 짝을 맞춘다).
    """
    if boot_id is None:
        return f"{device_id}-{unix_ms}-{seq}"
    return f"{device_id}-b{boot_id.lower()}-{unix_ms}-{seq}"
