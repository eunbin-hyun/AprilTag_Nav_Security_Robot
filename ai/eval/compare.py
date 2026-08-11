"""두 판을 짝지어 비교한다. 양자화·temperature·프롬프트 개정 전후에 쓴다.

⭐ **짝지은 차이로 잰다.** 같은 케이스를 두 설정으로 돌렸으니, 독립 신뢰구간 둘을 겹쳐
보는 것보다 **케이스별 차이의 평균**이 훨씬 좁고 옳다(Miller 2024 §4.2).

⚠ 두 판이 **같은 골든셋**이어야 한다. 케이스가 다르면 짝을 못 짓는다 — 그때는 겹치는
케이스만 쓰고 몇 벌을 버렸는지 적는다.

    python -m ai.eval.compare results/…-q4.json results/…-q6.json
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from pathlib import Path


def load(p: str) -> dict:
    d = json.loads(Path(p).read_text(encoding="utf-8"))
    return {r["id"]: r for r in d["results"]}, d.get("model", "?")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    a, a_model = load(sys.argv[1])
    b, b_model = load(sys.argv[2])
    name_a = Path(sys.argv[1]).stem
    name_b = Path(sys.argv[2]).stem

    shared = sorted(set(a) & set(b))
    dropped = (set(a) ^ set(b))
    if dropped:
        # ⚠ 조용히 버리지 마라. 몇 벌이 빠졌는지 안 적으면 "전수 비교"로 읽힌다.
        print(f"⚠ 한쪽에만 있는 케이스 {len(dropped)}벌은 뺐다: {sorted(dropped)[:5]}")
    print(f"짝지은 케이스 {len(shared)}벌\n")

    print(f"{'축':12} {name_a[:14]:>14} {name_b[:14]:>14} {'차이':>12}")
    print("-" * 56)

    rows = []
    for key, label, pct in (("pass_rate", "통과율", True),
                            ("recall", "재현율", True),
                            ("precision", "정밀도", True),
                            ("f1", "F1", True),
                            ("elapsed_med", "지연(초)", False)):
        # ⭐ 케이스별 차이를 먼저 내고 그 평균을 본다. 두 평균의 차가 아니다.
        diffs = [b[i][key] - a[i][key] for i in shared]
        mean_d = statistics.mean(diffs)
        se = statistics.stdev(diffs) / math.sqrt(len(diffs)) if len(diffs) > 1 else 0.0
        av, bv = statistics.mean(a[i][key] for i in shared), statistics.mean(b[i][key] for i in shared)
        if pct:
            band = f" ± {1.96 * se:.1%}p" if se else ""
            print(f"{label:12} {av:>13.1%} {bv:>14.1%} {mean_d:>+11.1%}{band}")
        else:
            band = f" ± {1.96 * se:.2f}" if se else ""
            print(f"{label:12} {av:>13.2f} {bv:>14.2f} {mean_d:>+11.2f}{band}")
        rows.append((label, mean_d, se))

    # ⚠ 차이가 오차 안이면 "좋아졌다"고 말하면 안 된다.
    print()
    for label, d, se in rows:
        if se == 0:
            continue
        if abs(d) < 1.96 * se:
            print(f"⚠ {label} 차이는 오차 안이다 — 좋아졌다고 말할 근거가 없다")

    print("\n케이스별로 갈린 자리")
    n = 0
    for i in shared:
        d = b[i]["pass_rate"] - a[i]["pass_rate"]
        if abs(d) > 0.01:
            mark = "↑" if d > 0 else "↓"
            print(f"  {mark} {i:26} {a[i]['pass_rate']:.0%} → {b[i]['pass_rate']:.0%}")
            n += 1
    if not n:
        print("  (없다 — 통과율이 케이스마다 같다)")

    print(f"\n{name_a} = {a_model}  ·  {name_b} = {b_model}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
