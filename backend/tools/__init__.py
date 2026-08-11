"""운영·시험용 도구 묶음.

⛔ **이 파일을 지우지 마라.** 빈 파일처럼 보이지만 있어야 하는 이유가 있다.

`__init__.py`가 없으면 이 폴더는 파이썬이 말하는 **네임스페이스 패키지**가 된다. 그때 파이썬은
`sys.path`를 끝까지 훑으면서 `tools`라는 이름의 **정식 패키지**(`__init__.py`가 있는 것)를 먼저
찾고, 하나라도 있으면 그것을 쓴다. 네임스페이스는 정식이 하나도 없을 때만 조합된다.

**2026-08-05에 그 자리가 실제로 터졌다.** `mako`가 1.3.12에서 1.4.0으로 올라가면서 최상위
`tools` 패키지를 site-packages에 설치하기 시작했고, `app/routers/admin.py`의
`from tools.purge_test_data import PURGE_ORDER`가 mako 쪽을 물어 `ModuleNotFoundError`로 죽었다.
컨테이너가 뜨자마자 죽어 실서버가 내려갔다(젠킨스 빌드 #74).

같은 이미지에서 대조한 실측이다.

    __init__.py 없음 → ModuleNotFoundError: No module named 'tools.purge_test_data'
    __init__.py 있음 → /app/backend/tools/purge_test_data.py

이 파일이 있으면 우리 폴더도 정식 패키지가 되고, `sys.path` 첫 자리가 작업 디렉터리
(`/app/backend`)라 site-packages보다 먼저 잡힌다.
"""
