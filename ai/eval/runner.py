"""관제 보조 품질 평가 하네스.

⭐ **사람 라벨이 0이다.** DB 집계가 그대로 정답이라 채점이 전부 자동이다.

무엇을 재나 (축 여섯) —
  숫자 정밀도   답에 있는데 사실에 없는 수가 없나 (지어냄)
  숫자 재현율   사실에 있는 핵심 수를 답이 빠뜨리지 않았나
  용어          화면 표기 정본대로 썼나 (금지 낱말이 없나)
  거절          도메인 밖만 거절하고 관제 질문은 답했나
  형식          길이·문단·한국어 순도
  지연          몇 초 걸리나

⚠ **같은 케이스를 여러 번 돌린다**(기본 3회). 한 번만 재면 운 좋은 판이 점수가 된다.
흔들림이 큰 케이스는 그 자체가 결함이라 따로 표시한다.

⛔ **스텁을 안 쓴다.** 실 llama-server를 부른다. `backend/tests/`의 assistant 시험 넷은
전부 스텁이라 모델이 무슨 문장을 쓰는지 한 줄도 안 잰다 — 이 하네스가 그 자리를 메운다.

돌리는 법 (GPU 서버나 EC2 컨테이너 안에서) —
    python -m ai.eval.runner                     기본 3회
    python -m ai.eval.runner --repeat 5          다섯 번
    python -m ai.eval.runner --kind 로봇         한 층만
    python -m ai.eval.runner --tag q6            결과 파일에 이름표
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import math
import re
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent          # 저장소 뿌리
sys.path.insert(0, str(ROOT))

from ai.control_assistant import guards, pipeline          # noqa: E402
from ai.control_assistant.config import AssistantConfig    # noqa: E402

GOLDEN = HERE / "golden.json"
OUT_DIR = HERE / "results"

# 사실에 담긴 시각을 케이스마다 다시 계산한다. 골든셋에 절대 시각을 박으면 하루만 지나도
# "13시간 전"이 "37시간 전"이 되어 기대값이 낡는다.
_PLACEHOLDERS = {
    "__NOW__": lambda: dt.datetime.now(dt.timezone.utc).isoformat(),
    "__13H_AGO__": lambda: (dt.datetime.now(dt.timezone.utc)
                            - dt.timedelta(hours=13)).isoformat(),
}

# ⛔ 거절 문구가 **하나가 아니다**(2026-08-07). 도메인 밖 질문은 "관제 시스템에
# 대해서만 답변합니다"로 거절하지만, **로봇 제어 요청은 다른 말로 거절한다** —
# "제어나 출동 지시는 이 창구에서 처리하지 않습니다" 꼴이다.
#
# 문구 하나만 찾다가 **옳은 거절을 실패로 잡았다.** 모델이 제어를 거절하면서 요원이
# 직접 할 자리까지 안내한 답이었는데 판정이 못 알아봤다.
#
# ⚠ 넓히되 아무 부정문이나 걸리지 않게 한다. 케이스에 `refuse_words` 를 두면 그 목록으로
# 판정하고, 없으면 아래 기본값을 쓴다.
_REFUSAL_DEFAULT = (
    "관제 시스템에 대해서만",
    "처리하지 않습니다",
    "수행할 수 없습니다",
    "대신하지 않습니다",
    "제어 명령은",
    "직접 내릴 수 없",
    "내릴 수 없습니다",
    # ⚠ 판단 대행 거절은 또 다른 말투다. "대신하지 않습니다"만 찾다가 실제로 거절한
    # 답을 "답했다"로 잘못 셌다(2026-08-07 거절 경계 실측).
    "대신 판단하지 않",
    "수행하지 않습니다",
    # ⛔ 개인 지목 거절 말투를 오늘 밤에 새로 만들었는데(prompts `_CHAT_CONTEXT`) 여기에
    # 안 넣어서 `chat-막을것-개인정보` 가 통째로 실패했다. 거절은 제대로 했는데 하네스가
    # 못 알아본 것이다.
    #
    # ⭐ 이 말은 "안 하겠다"만 뜻한다 — 자료가 없어 못 답할 때는 안 쓴다. 그 잣대를
    # 지키는 낱말만 여기 넣는다("알려 드릴 수 없"류를 넣었다가 가짜 거절을 만들었다).
    #
    # ⚠ **프롬프트에서 거절 말투를 바꾸면 여기도 같이 고쳐야 한다.** 두 자리가 갈리면
    # 하네스가 멀쩡한 답을 실패로 세고, 그 숫자를 보고 멀쩡한 코드를 고치게 된다.
    "개인 정보를 다루지 않",
)

# ⛔⛔ **여기 넣으면 안 되는 말들**(2026-08-07 — 넣었다가 가짜 거절을 만들어 되돌렸다).
#
#   "알려 드릴 수 없" · "답변할 수 없" · "하지 않습니다"
#
# 이 말들은 **자료가 없어서 못 답할 때**도 쓴다. 실제로 이런 답이 거절로 잘못 잡혔다 —
#
#   "출입 명부의 등록 인원 수는 조회 자료에 포함되어 있지 않습니다. … 알려 드릴 수 없습니다"
#
# 이건 거절이 아니라 **자료 없음을 밝힌 정상 답**이다. 목록을 넓힐수록 이런 자리가 는다.
#
# ⭐ 남길 기준 — **그 말이 "안 하겠다"만 뜻하고 "못 한다"로는 안 쓰이는가.**
# "관제 시스템에 대해서만"·"제어 명령은"·"대신 판단하지 않"이 그렇다.
_NUM = re.compile(r"\d[\d,]*")


def _resolve(node):
    """자리표시자를 지금 시각으로 바꾼다."""
    if isinstance(node, dict):
        return {k: _resolve(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v) for v in node]
    if isinstance(node, str) and node in _PLACEHOLDERS:
        return _PLACEHOLDERS[node]()
    return node


def _has_number(text: str, want: str) -> bool:
    """답에 그 수가 있나. 천 단위 쉼표를 붙여 적어도 인정한다.

    ⛔ **예전에는 부분문자열 대조라 숫자 재현율이 통째로 헐거웠다**(2026-08-09 백지검토).
    `want in plain` 이면 기대 "15" 가 답의 "153건" 에 걸리고, 기대 "2" 가 사실 날짜
    "2026-08-05" 에 걸린다. 골든셋을 전수 집계해 보니 **숫자축 26벌 중 12벌**이
    한자리수·상호 부분문자열이라 사실상 무조건 통과였다.

    ⭐ 그래서 **수 경계**로 대조한다. 앞뒤에 다른 숫자가 붙으면 다른 수다.
    쉼표는 먼저 걷어내므로(1,234 → 1234) 경계 검사에는 `\\d` 만 보면 된다.

    ⚠ 남는 구멍 하나 — 한 자리 기대값은 **날짜 "8월"·"5일" 같은 자리에 여전히 걸린다.**
    수 경계로는 못 가르는 자리라, 새 벌을 쓸 때 두 자리 이상 값을 고르는 것이 정본이다
    (`golden.json` 머리말 ①·⑦).
    """
    plain = text.replace(",", "")
    w = want.replace(",", "")
    return bool(re.search(rf"(?<!\d){re.escape(w)}(?!\d)", plain))


def score_one(case: dict, text: str, elapsed: float, generated_by: str,
              fallback_reason: str | None) -> dict:
    """한 번 돌린 결과를 채점한다."""
    facts = case["_facts"]
    words = case.get("refuse_words") or _REFUSAL_DEFAULT
    refused = any(w in text for w in words)

    # ── 거절 ──
    want_refuse = bool(case.get("refuse"))
    refuse_ok = (refused == want_refuse)

    # ── 숫자 재현율 — 사실에 있는 핵심 수를 빠뜨리지 않았나 ──
    #    ⚠ 이 축이 없으면 두루뭉술하게 답해도 만점이 된다.
    want_nums = case.get("expect_numbers") or []
    missed = [n for n in want_nums if not _has_number(text, n)]
    recall = 1.0 if not want_nums else (len(want_nums) - len(missed)) / len(want_nums)

    # ── 숫자 정밀도 — 없는 수를 지어냈나 ──
    # ⛔ **`check` 가 아니라 `ungrounded_numbers` 를 직접 부르면 계약 숫자 예외가 안 걸린다**
    # (2026-08-07). 프롬프트에 넣은 설비 번호(5·2)를 모델이 적으면 하네스만 "지어냈다"고
    # 잡아, 실서비스는 통과하는 답이 여기서 실패로 셌다. 실서비스와 같은 잣대를 쓴다.
    invented = ([n for n in guards.ungrounded_numbers(text, facts)
                 if n not in guards.CONTRACT_NUMBERS] if not want_refuse else [])
    total_nums = len([m for m in _NUM.finditer(text)])
    precision = 1.0 if not total_nums else max(0.0, 1 - len(invented) / total_nums)

    # ── 용어 ──
    want_words = case.get("expect_words") or []
    missing_w = [w for w in want_words if w not in text]
    bad_words = [w for w in (case.get("forbid_words") or []) if w in text]
    any_of = case.get("expect_any")
    any_ok = True if not any_of else any(w in text for w in any_of)
    term_ok = not missing_w and not bad_words and any_ok

    # ── 형식 ──
    cap = case.get("max_chars")
    len_ok = True if not cap else len(text) <= cap
    min_p = case.get("min_paragraphs")
    paras = len([p for p in text.split("\n\n") if p.strip()])
    para_ok = True if not min_p else paras >= min_p
    korean_ok = guards.looks_korean(text) and not guards.has_hanja(text)

    return {
        "refuse_ok": refuse_ok,
        "recall": recall, "missed": missed,
        "precision": precision, "invented": invented,
        "term_ok": term_ok, "missing_words": missing_w, "bad_words": bad_words,
        "len_ok": len_ok, "chars": len(text),
        "para_ok": para_ok, "paragraphs": paras,
        "korean_ok": korean_ok,
        "generated_by": generated_by, "fallback_reason": fallback_reason,
        "elapsed": round(elapsed, 2),
        "text": text,
    }


def _passed(s: dict) -> bool:
    """이 판이 통과인가. 하나라도 어긋나면 실패다.

    ⛔⛔ **폴백 문장이 통과로 잡히던 구멍을 막는다**(2026-08-07 반대 심문이 잡음).
    예전에는 `generated_by` 를 안 봤다. 기대 낱말이 비어 있는 케이스는 **LLM 이 죽어도
    규칙 문장이 조건을 우연히 만족해 통과로 셌다** — 점수가 부풀고, LLM 품질을 재는
    하네스가 정작 LLM 을 안 탄 판을 세는 셈이었다.

    ⚠ 폴백 자체가 결함은 아니다. 다만 **이 하네스는 LLM 이 쓴 문장을 재는 자리**다.
    폴백으로 떨어진 판은 사유와 함께 실패로 세고, 그 사유가 결함을 가리킨다.
    """
    if s.get("generated_by") not in (None, "llm"):
        return False
    return (s["refuse_ok"] and s["recall"] == 1.0 and not s["invented"]
            and s["term_ok"] and s["len_ok"] and s["para_ok"] and s["korean_ok"])


async def run_case(cfg: AssistantConfig, case: dict, repeat: int) -> dict:
    # ⛔ **자리표시자를 여기서 다시 푼다.** 케이스를 다 만들어 두고 순서대로 돌면, 30벌째가
    # 시작될 때 `__NOW__` 는 이미 몇 분 전이 되어 신선도 문턱(25초)을 넘긴다. 그러면
    # "방금 보고했다"로 만든 케이스가 "끊김"으로 판정돼 **가짜 실패**가 난다
    # (2026-08-07 실측 — chat-로봇-붙어있음 이 "마지막 보고가 1분 전"으로 떨어졌다).
    facts = _resolve(case["facts"])
    case["_facts"] = facts
    fn = case["fn"]
    runs = []
    for _ in range(repeat):
        pipeline.reset_breaker()      # ⚠ 앞 케이스가 남긴 차단 상태가 다음을 가짜로 떨어뜨린다
        t0 = time.perf_counter()
        if fn == "daily_summary":
            r = await pipeline.daily_summary(cfg, facts)
        elif fn == "alert_brief":
            r = await pipeline.alert_brief(cfg, facts)
        else:
            # ⛔ **실서비스는 최근 대화를 붙여 부르는데 하네스는 안 붙이고 있었다**
            # (2026-08-07 반대 심문이 잡음). `routers/assistant.py` 가 `_recent_history` 로
            # 최근 네 메시지를 넘긴다. 그 경로가 점수에 한 번도 안 들어가 **단일 턴 점수**였다.
            # ⚠ 실사용은 "안녕" 다음에 진짜 질문이 오는 흐름이다.
            r = await pipeline.chat(cfg, case["question"], facts,
                                    history=case.get("history"))
        el = time.perf_counter() - t0
        runs.append(score_one(case, r.text, el, r.generated_by, r.fallback_reason))

    oks = [_passed(s) for s in runs]
    p = statistics.mean(s["precision"] for s in runs)
    r = statistics.mean(s["recall"] for s in runs)
    return {
        "id": case["id"], "kind": case.get("kind", "?"), "fn": fn,
        "pass_rate": sum(oks) / len(oks),
        "stable": len(set(oks)) == 1,     # ⚠ 판마다 결과가 갈리면 그 자체가 결함이다
        "recall": r,
        "precision": p,
        # ⭐ 정밀도만 보면 **짧게 답해 조작할 수 있다**(SAFE 논문, NeurIPS 2024).
        # 셋을 다 적어야 정직하다.
        "f1": 0.0 if (p + r) == 0 else 2 * p * r / (p + r),
        # ⚠ 숫자 축이 실제로 걸린 케이스인가. 안 걸린 케이스는 재현율이 무조건 1.0이라
        # 전체 평균에 섞으면 숫자 점수가 부푼다.
        "has_numbers": bool(case.get("expect_numbers")),
        "elapsed_med": statistics.median(s["elapsed"] for s in runs),
        # ⛔ **중앙값은 반드시 "따뜻한 판"이다**(2026-08-09 백지검토가 잡음).
        # 같은 케이스를 repeat(기본 3)회 돌리는데 facts·question 이 글자까지 같아서
        # 2·3회차는 프리필이 4토큰이다(client.py:195). 셋의 중앙값은 구조상 캐시 적중
        # 판이라, 재료를 아무리 키워도 이 숫자는 안 나빠진다 — **낙관 쪽으로 굳은 값**이다.
        #
        # ⭐ runs[0] 을 따로 실어 '찬 판 / 따뜻한 판'을 갈라 본다. 점수 계산은 안 건드린다.
        # ⚠ runs[0] 도 완전히 찬 판은 아니다 — 앞 케이스가 같은 시스템 프롬프트를 이미
        # 태워 뒀으면 접두는 살아 있다. "그 케이스의 첫 판"으로만 읽어라.
        "elapsed_first": runs[0]["elapsed"],
        "runs": runs,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--kind", default=None, help="한 층만 돌린다")
    ap.add_argument("--tag", default="", help="결과 파일 이름표 (q4·q6·temp07 …)")
    args = ap.parse_args()

    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = [c for c in doc["cases"] if not args.kind or c.get("kind") == args.kind]
    for c in cases:
        c["_facts"] = _resolve(c["facts"])

    cfg = AssistantConfig.from_env()
    if not cfg.enabled:
        print("⛔ 키가 비어 LLM을 안 부른다. GMS_API_KEY를 넣어라(로컬이면 더미 값).")
        return 2

    print(f"모델 {cfg.model} · {cfg.base_url}")
    print(f"케이스 {len(cases)}벌 × {args.repeat}회 = {len(cases) * args.repeat}호출\n")

    results = []
    for i, c in enumerate(cases, 1):
        r = await run_case(cfg, c, args.repeat)
        results.append(r)
        mark = "PASS" if r["pass_rate"] == 1.0 else ("일부" if r["pass_rate"] else "FAIL")
        wobble = "" if r["stable"] else "  ⚠ 판마다 갈림"
        print(f"[{i:2}/{len(cases)}] {mark:4} {r['pass_rate']:.0%}  {r['id']:26} "
              f"재현 {r['recall']:.0%} 정밀 {r['precision']:.0%} {r['elapsed_med']:.1f}초{wobble}")
        if r["pass_rate"] < 1.0:
            bad = next(s for s in r["runs"] if not _passed(s))
            why = []
            if not bad["refuse_ok"]:
                why.append("거절 어긋남")
            if bad["missed"]:
                why.append(f"빠뜨린 수 {bad['missed']}")
            if bad["invented"]:
                why.append(f"지어낸 수 {bad['invented']}")
            if bad["missing_words"]:
                why.append(f"빠진 낱말 {bad['missing_words']}")
            if bad["bad_words"]:
                why.append(f"금지 낱말 {bad['bad_words']}")
            if not bad["len_ok"]:
                why.append(f"{bad['chars']}자")
            if not bad["para_ok"]:
                why.append(f"문단 {bad['paragraphs']}개")
            if not bad["korean_ok"]:
                why.append("한국어 아님")
            print(f"          → {' · '.join(why)}")
            print(f"          → {bad['text'][:150]}")

    print("\n" + "=" * 70)
    n = len(results)
    per = [r["pass_rate"] for r in results]
    mean = statistics.mean(per)

    # ⛔ **KN 개 판을 통째로 모아 표준오차를 내면 틀린다.** 같은 케이스의 여러 답은 독립이
    # 아니라서 오차막대가 실제보다 좁게 나온다(Miller 2024, arXiv 2411.00640 §3.1).
    # 그래서 **케이스 평균을 먼저 내고** 그 평균들 사이에서 오차를 구한다. n 은 판 수가
    # 아니라 케이스 수다.
    se = statistics.stdev(per) / math.sqrt(n) if n > 1 else 0.0
    if se == 0.0:
        # ⚠ 전 케이스 통과면 표준편차가 0인데 그건 "오차가 없다"가 아니라 정규 근사가
        # 경계에서 깨진 것이다. 삼의 법칙으로 실패율 상한을 적는 편이 정직하다.
        tail = f"  (전 케이스 통과 — 실패율 95% 상한 3/{n} = {3 / n:.1%})"
    else:
        tail = f" ± {1.96 * se:.1%}p (95%)"
    print(f"전체 통과율 {mean:.1%}{tail}")
    print(f"  전판 통과 {sum(1 for r in results if r['pass_rate'] == 1.0)}/{n} 벌"
          f"  ·  흔들린 케이스 {sum(1 for r in results if not r['stable'])}벌")
    # ⚠ 두 숫자를 갈라 적는다. 앞의 것이 실사용 첫 질문에 가깝고 뒤의 것이 연달아 물을 때다.
    # 하나만 적으면 뒤의 값(캐시 적중)이 대표값으로 읽혀 시연 체감보다 낙관이 된다.
    print(f"  지연 — 케이스 첫 판 중앙값 {statistics.median(r['elapsed_first'] for r in results):.1f}초"
          f"  ·  반복 중앙값(캐시 적중) {statistics.median(r['elapsed_med'] for r in results):.1f}초")

    # ⚠ 숫자 축은 **그 축이 걸린 케이스만** 평균한다. 안 걸린 케이스는 재현율이 무조건
    # 1.0이라 섞으면 점수가 부푼다.
    num = [r for r in results if r["has_numbers"]]
    if num:
        print(f"숫자 축 ({len(num)}벌 기준) — 재현율 {statistics.mean(r['recall'] for r in num):.1%}"
              f"  정밀도 {statistics.mean(r['precision'] for r in num):.1%}"
              f"  F1 {statistics.mean(r['f1'] for r in num):.1%}")

    # ⚠ 표본이 적으면 통계로 말하면 안 된다. 15벌 밑은 숫자를 못 믿고 30벌부터 쓸 만하다.
    if n < 15:
        print(f"⛔ 케이스가 {n}벌뿐이라 통계로 말하면 안 된다. 30벌 이상으로 늘려라.")
    elif n < 30:
        print(f"⚠ 케이스 {n}벌은 방향만 본다. 30벌부터 통계가 선다.")

    print("\n층별")
    kinds = sorted({r["kind"] for r in results})
    for k in kinds:
        sub = [r for r in results if r["kind"] == k]
        print(f"  {k:8} {statistics.mean(r['pass_rate'] for r in sub):5.0%}  ({len(sub)}벌)")

    # ⚠ `parents=True` 가 없으면 부모가 없을 때 FileNotFoundError 로 죽는다. 90호출을
    # 8분 걸려 다 돌고 마지막 한 줄에서 결과가 통째로 날아갔다(2026-08-07 실사고).
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%m%d-%H%M")
    name = f"{stamp}{'-' + args.tag if args.tag else ''}.json"
    (OUT_DIR / name).write_text(
        json.dumps({"model": cfg.model, "repeat": args.repeat, "results": results},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n결과: {OUT_DIR / name}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
