"""안내 방송 TTS 후보 실측 스크립트 (Edge-TTS vs Piper).

목적: 같은 문장 3개를 Edge-TTS 한국어 보이스 3종 + Piper 한국어 보이스로
합성해 지연시간·파일 크기를 실측하고, 결과 파일은 사람이 직접 들어볼 자리에 저장한다.

사용법 (스크래치 venv에서 실행 — edge-tts, piper-tts 별도 설치 필요):
    python measure_tts.py --out-dir <샘플 저장 경로> \
        --piper-model <ko_KR-kss-medium.onnx 경로>

Piper 모델 경로를 안 주면 Piper 구간은 건너뛰고 그 사실을 리포트에 남긴다.
edge-tts, piper 패키지는 리포에 안 실어 — 각자 venv에 pip install로 받는다.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# 안내 문구 3종 — README.md 템플릿과 동일한 본문이어야 함 (실측용 = 실제 후보 문구)
SENTENCES: dict[str, str] = {
    "tagging_ok": "정상 출입이 확인되었습니다. 안전하게 이동해 주세요.",
    "untagged_warning": "출입 태그가 확인되지 않았습니다. 근무자에게 문의해 주세요.",
    "classroom_arrival": "로봇이 강의실 앞에 도착했습니다. 잠시만 기다려 주세요.",
}

EDGE_VOICES: list[str] = [
    "ko-KR-SunHiNeural",       # 여성
    "ko-KR-InJoonNeural",      # 남성
    "ko-KR-HyunsuMultilingualNeural",  # 남성, 다국어
]

PIPER_VOICE_NAME = "ko_KR-kss-medium"


@dataclass
class MeasureRow:
    engine: str
    voice: str
    sentence_id: str
    ok: bool
    latency_sec: float | None
    file_bytes: int | None
    out_path: str | None
    note: str


async def synth_edge(voice: str, sentence_id: str, text: str, out_dir: Path) -> MeasureRow:
    import edge_tts

    out_path = out_dir / f"edge_{voice}_{sentence_id}.mp3"
    t0 = time.perf_counter()
    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(out_path))
        latency = time.perf_counter() - t0
        size = out_path.stat().st_size
        return MeasureRow("edge-tts", voice, sentence_id, True, latency, size, str(out_path), "")
    except Exception as e:  # noqa: BLE001 - 실측 스크립트는 실패도 기록하고 계속 진행
        latency = time.perf_counter() - t0
        return MeasureRow("edge-tts", voice, sentence_id, False, latency, None, None, f"{type(e).__name__}: {e}")


def synth_piper(model_path: Path, sentence_id: str, text: str, out_dir: Path) -> MeasureRow:
    out_path = out_dir / f"piper_{PIPER_VOICE_NAME}_{sentence_id}.wav"
    t0 = time.perf_counter()
    # Windows에서 stdin 파이프로 한글을 넘기면 콘솔 코드페이지 탓에 espeak-ng
    # phonemizer가 UnicodeEncodeError를 내는 함정이 있다 (2026-07-28 실측).
    # PYTHONUTF8=1로 자식 프로세스 인코딩을 강제해야 정상 합성된다.
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "piper", "--model", str(model_path), "--output_file", str(out_path)],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=60,
            env=env,
        )
        latency = time.perf_counter() - t0
        if proc.returncode != 0 or not out_path.exists():
            note = proc.stderr.decode("utf-8", errors="replace")[-500:]
            return MeasureRow("piper", PIPER_VOICE_NAME, sentence_id, False, latency, None, None, note)
        size = out_path.stat().st_size
        return MeasureRow("piper", PIPER_VOICE_NAME, sentence_id, True, latency, size, str(out_path), "")
    except Exception as e:  # noqa: BLE001
        latency = time.perf_counter() - t0
        return MeasureRow("piper", PIPER_VOICE_NAME, sentence_id, False, latency, None, None, f"{type(e).__name__}: {e}")


async def run(out_dir: Path, piper_model: Path | None) -> list[MeasureRow]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[MeasureRow] = []

    for voice in EDGE_VOICES:
        for sentence_id, text in SENTENCES.items():
            rows.append(await synth_edge(voice, sentence_id, text, out_dir))

    if piper_model is not None:
        if not piper_model.exists():
            rows.append(MeasureRow("piper", PIPER_VOICE_NAME, "-", False, None, None, None,
                                    f"모델 파일 없음: {piper_model}"))
        else:
            for sentence_id, text in SENTENCES.items():
                rows.append(synth_piper(piper_model, sentence_id, text, out_dir))
    else:
        rows.append(MeasureRow("piper", PIPER_VOICE_NAME, "-", False, None, None, None,
                                "Piper 한국어 보이스는 ko_KR-kss-medium 1종뿐 (rhasspy/piper-voices 실측, 2026-07-28). "
                                "--piper-model 인자로 .onnx 경로를 안 주면 건너뜀."))
    return rows


def write_report(rows: list[MeasureRow], out_dir: Path) -> None:
    csv_path = out_dir / "measure_report.csv"
    json_path = out_dir / "measure_report.json"

    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))

    with json_path.open("w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)

    print(f"\n리포트 저장: {csv_path}")
    print(f"리포트 저장: {json_path}\n")
    print(f"{'engine':<10}{'voice':<32}{'sentence':<20}{'ok':<6}{'latency(s)':<12}{'bytes':<10}")
    for row in rows:
        latency_s = f"{row.latency_sec:.2f}" if row.latency_sec is not None else "-"
        size_s = str(row.file_bytes) if row.file_bytes is not None else "-"
        print(f"{row.engine:<10}{row.voice:<32}{row.sentence_id:<20}{str(row.ok):<6}{latency_s:<12}{size_s:<10}")
        if row.note:
            print(f"    note: {row.note}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True, type=Path, help="샘플 wav/mp3·리포트 저장 경로")
    parser.add_argument("--piper-model", type=Path, default=None,
                         help="ko_KR-kss-medium.onnx 경로 (안 주면 Piper 구간 건너뜀)")
    args = parser.parse_args()

    rows = asyncio.run(run(args.out_dir, args.piper_model))
    write_report(rows, args.out_dir)


if __name__ == "__main__":
    main()
