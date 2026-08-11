# C207 관제 백엔드 이미지.
#
# ⚠ 빌드 컨텍스트는 backend/가 아니라 **리포 루트**다.
#    app/ai_bridge.py가 리포 루트를 sys.path에 꽂고 최상위 `ai` 패키지를 임포트하기 때문에
#    이미지 안에도 backend/와 ai/가 형제로 나란히 있어야 한다.
#    (backend/만 컨텍스트로 잡으면 관제 보조 라우터가 ImportError로 죽는다.)
#
#    docker build -f backend/Dockerfile -t c207-backend:latest .
#
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app/backend

# requirements를 먼저 복사해야 소스만 바뀐 재빌드에서 설치 레이어가 캐시로 재사용된다.
# 소스와 같은 레이어에 두면 코드 한 줄 고칠 때마다 의존성을 통째로 다시 받는다.
#
# ⚠ requirements.txt는 운영 런타임만이다. 시험 도구는 requirements-dev.txt로 갈라 놨고
#   여기서 설치하지 않는다 — 운영 이미지에 pytest가 있으면
#   `docker exec c207-backend pytest` 한 줄로 운영 DB 스키마가 drop된다.
#
# ⚠ 이 한 줄이 무는 판은 requirements.txt에 전부 `==`로 박혀 있다 — 간접 의존성까지다.
#   그 핀을 풀면 같은 커밋이 날마다 다른 이미지로 빌드된다. 2026-08-05 빌드 #74가
#   그렇게 죽었고, 그때까지 판을 붙잡고 있던 것은 이 레이어의 캐시뿐이었다.
#   캐시가 언제 식는지는 아무도 선언하지 않는다.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt

# ai/를 backend/보다 먼저 복사한다 — 백엔드 코드보다 덜 바뀌어서 캐시가 더 오래 산다.
COPY ai/ /app/ai/
COPY backend/ /app/backend/

# 시험 코드도 이미지에서 뺀다. requirements에서 pytest를 뺀 것만으로는 부족하다 —
# 컨테이너 안에서 pip install pytest 한 번이면 시험이 다시 돌 수 있고, 그 시험의
# 세션 픽스처가 drop_all을 부른다. tests/가 없으면 그 경로가 아예 안 생긴다.
# (.dockerignore에 넣지 않고 여기서 지우는 이유 — .dockerignore는 리포 루트 공용 파일이라
#  이 이미지 사정으로 남의 빌드 규칙을 바꾸지 않는다.)
#
# 가짜 자료 주입 도구 둘도 같이 뺀다. 운영 이미지에 남아 있으면
# `docker exec c207-backend python tools/dummy_robot.py` 한 줄로 라이브 DB와 관제 화면에
# 없는 로봇·없는 미태깅 이벤트가 들어간다 — 시연 중이면 화면이 그대로 오염된다.
# ⚠ seed_users.py는 남긴다. 로그인 롤아웃 2단계(계정 심기)가 이 이미지 안에서 이 파일을
#   부른다(backend/.env.example의 AUTH_REQUIRE_LOGIN 절차). 지우면 계정을 못 심는다.
RUN rm -rf /app/backend/tests /app/backend/pytest.ini \
           /app/backend/tools/dummy_robot.py /app/backend/tools/dummy_publisher.py

# 빌드 게이트. 운영 의존성에 pytest가 다시 새어 들어오면 여기서 빌드가 깨진다.
# 조용히 새어 들어오는 쪽이 훨씬 나쁘다. (heredoc은 BuildKit 전용이라 한 줄로 적는다.)
RUN python -c "import importlib.util as u, sys; leaked=[m for m in ('pytest','_pytest') if u.find_spec(m)]; sys.exit('운영 이미지에 시험 도구가 들어왔다: %s' % leaked) if leaked else print('운영 이미지 시험 도구 없음 확인')"

# ── 비-root 실행 ────────────────────────────────────────────────────────────
# 기본값 그대로면 uvicorn이 컨테이너 안에서 root로 돈다. 앱에 원격 코드 실행이 하나
# 열리면 그게 곧 컨테이너 root라, 마운트된 자리·커널 인터페이스까지 손이 닿는다.
# 이 앱은 8000(1024 위)만 열고 파일을 한 자리도 안 쓰므로 root일 이유가 없다.
#
# ⚠ `user: "10001:10001"`처럼 compose에서 숫자만 주지 않고 여기서 계정을 만든다.
#   /etc/passwd에 없는 uid로 돌면 `expanduser("~")`·`getpass.getuser()`가 KeyError로
#   터지는 라이브러리가 있다. 계정을 만들어 두면 그 갈래가 통째로 안 생긴다.
# ⚠ 파일은 전부 root 소유 그대로 두고 chown하지 않는다. 읽기 권한(644/755)은 이미 있고,
#   앱이 자기 코드를 못 고치는 편이 오히려 안전하다(compose의 read_only와 같은 방향).
# ⚠ 이 USER는 배포가 부르는 일회용 alembic 컨테이너에도 그대로 먹는다
#   (deploy/jenkins-deploy.sh의 `docker run --entrypoint alembic`). 마이그레이션도
#   파일을 안 쓰므로 문제없다 — 2026-08-02 실물 검증에서 upgrade head가 그대로 통과했다.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser
USER appuser

# 컨테이너 안에서만 여는 포트. 바깥 노출은 compose가 127.0.0.1로 묶는다.
EXPOSE 8000

# 워커 1개 고정. 커넥션 목록·크레딧 쿨다운·미션 에지 추적이 전부 인메모리라
# 워커를 늘리면 브로드캐스트와 판정이 프로세스별로 갈라진다(backend/README.md).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
