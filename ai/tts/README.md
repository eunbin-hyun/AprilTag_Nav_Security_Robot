# 안내 방송 TTS — 후보 실측 & 템플릿

정본 결론(`docs/싸큐리티_AI활용_조사원문_2026-07-16.md`, `싸큐리티_준비패키지_2026-07-16.md`): **Edge-TTS(온라인 기본) + Piper(오프라인 폴백)**, **gTTS는 제외**. 이 문서는 그 결론을 실측으로 확인하고, 안내 문구 템플릿과 서버 트리거 지점을 정리한다.

실측 스크립트: `measure_tts.py`. 실측 샘플 음성(리포에는 안 실음, 직접 들어볼 실물): `L:\sonseuk\SSAFY\공통PJT_보안로봇\worklog\tts_samples_0728\`

## 1. 실측 결과 (2026-07-28, 게이밍PC, 문장 3개 × 보이스 4종 = 12개 합성)

| 엔진 | 보이스 | 성별 | 평균 지연시간 | 평균 파일 크기 | 형식 |
|---|---|---|---|---|---|
| Edge-TTS | ko-KR-SunHiNeural | 여 | 0.45초 | 34.0KB | mp3 |
| Edge-TTS | ko-KR-InJoonNeural | 남 | 0.49초 | 38.9KB | mp3 |
| Edge-TTS | ko-KR-HyunsuMultilingualNeural | 남(다국어) | 0.44초 | 34.8KB | mp3 |
| Piper | ko_KR-kss-medium | 여 | 1.37초 | 194.1KB | wav(비압축) |

- Edge-TTS 한국어 보이스는 딱 3종뿐이야(마이크로소프트 공식 목록 실측). 그 이상 고를 옵션이 없어.
- **Piper 한국어 보이스는 `ko_KR-kss-medium` 1종뿐이야.** `rhasspy/piper-voices`(HuggingFace 공식 저장소) 실측 확인 — ko/ko_KR 밑에 kss/medium 하나만 있고 다른 화자·품질 옵션이 없어. "아예 없다"는 아니고 "선택지가 하나"라는 게 정확한 사실이야.
- Piper 지연시간(1.37초)은 매 호출마다 `python -m piper` 서브프로세스를 새로 띄우고 onnx 모델을 처음부터 로드하는 비용이 섞여 있어서, Edge-TTS(네트워크 API 호출)보다 3배 느리게 나온 거야. 실제 서버에 붙일 땐 모델을 상주 프로세스에 한 번만 로드해두면 이 오버헤드는 사라져 — 지금 수치는 "콜드 스타트 상한"으로 보면 돼.
- Piper 파일이 5~6배 큰 건 wav 비압축이라 그래. mp3로 인코딩하면 Edge-TTS급으로 줄어.
- ⚠ Windows 함정 — Piper stdin으로 한글 텍스트를 넘기면 콘솔 코드페이지 탓에 espeak-ng phonemizer가 `UnicodeEncodeError`를 낸다. 자식 프로세스 환경변수에 `PYTHONUTF8=1`을 강제해야 정상 합성돼(스크립트에 반영 완료).
- 음질 판정(자연스러움·발음 정확도)은 숫자로 못 재. 샘플 12개 직접 들어보고 판단해줘.

## 2. 안내 문구 템플릿 (제품 문구 — 합니다체)

실측에 쓴 문장과 동일해(스크립트 `SENTENCES` 상수와 동기화).

| 상황 | 문구 |
|---|---|
| 태깅 확인 (정상 통과) | "정상 출입이 확인되었습니다. 안전하게 이동해 주세요." |
| 미태깅 경고 | "출입 태그가 확인되지 않았습니다. 근무자에게 문의해 주세요." |
| 강의실 도착 안내 | "로봇이 강의실 앞에 도착했습니다. 잠시만 기다려 주세요." |

문구는 LLM으로 실시간 생성하지 않는다 — 정본 결론("안내 문구 생성에 LLM 쓰는 건 하지 마, 템플릿이 낫고 심사에서도 역풍 맞을 자리")을 그대로 따른다.

## 3. 서버 트리거 지점 제안

`backend/app/models.py`, `robot_channel.py`, `credit/state_machine.py` 실측 기준.

| 트리거 이벤트 | 조건 | 재생 템플릿 |
|---|---|---|
| `GatePassEvent.verdict == "normal"` | 빔 통과 완료 + 태깅 대조 정상 | 태깅 확인 |
| `Alert.type == "untagged"` (`ALERT_TYPE_UNTAGGED`, `state_machine.py:62`) | 미태깅 판정, 게이트당 쿨다운 있음 | 미태깅 경고 |
| `Alert.type == "robot_arrival"` (`ROBOT_ARRIVAL_ALERT_TYPE`, `robot_channel.py:74`) | 셔틀 로봇이 강의실 앞 도착 | 강의실 도착 안내 |

정본 데이터 흐름 문서("TTS는 로봇 로컬 재생 — 서버 왕복 지연 회피")를 따르면, 서버는 이벤트 발생 시 "재생할 템플릿 ID"만 로봇에 내려주고, 실제 TTS 재생은 로봇이 로컬에서 한다. 템플릿 문구가 고정 3종이라 **매 이벤트마다 실시간 합성할 필요가 없어** — 배포 시점에 3개 파일을 한 번만 미리 합성해서 로봇에 심어두고, 이벤트가 오면 파일 재생만 하면 돼. 이렇게 하면 Edge-TTS의 온라인 의존(와이파이 필요)도, Piper의 콜드스타트 지연도 런타임에서는 안 걸린다 — 둘 다 "배포 시 합성 도구"로만 쓰이고 로봇은 완성된 오디오 파일만 재생.

## 4. ElevenLabs 폴백 경로 (실호출 안 함 — 경로·비용만)

Edge-TTS/Piper 품질이 심사·데모 기준에 부족하면 ElevenLabs로 성우를 씌우는 게 최종 폴백이다(사용자 구독 보유).

- 경로: ElevenLabs 콘솔이나 MCP(`generate_audio`, `create_voice`)로 문구 3종을 1회성 오프라인 합성 → wav/mp3 파일을 위 2번 방식과 똑같이 로봇에 미리 심어두는 캐시 방식. 실시간 API 호출 아님 — 3문장 고정이라 한 번만 만들면 끝.
- 비용(2026-07 시세, API 요금제 기준): Multilingual v2는 글자당 1 credit, Flash 모델은 0.5~1 credit. API Pro $99/월(credit 100 포함) 구간이면 문구 3종(총 100자 안팎) 몇 번 우려먹는 수준은 사실상 무료 오차범위. 구독 초과분 종량은 1000자당 $0.06~$0.12 선.
- 결정 필요: Edge-TTS/Piper 샘플을 들어보고 품질이 충분하면 ElevenLabs는 안 써도 됨. 부족하면 이 경로로 3문장만 1회 합성.

Sources: [Flexprice — ElevenLabs 가격 분해](https://flexprice.io/blog/elevenlabs-pricing-breakdown), [TextToLab — ElevenLabs API 요율](https://texttolab.com/blog/elevenlabs-pricing)

## 5. 실측 재현 방법

```bash
# 스크래치 venv (리포 밖)에 설치
pip install edge-tts piper-tts

# Piper 한국어 보이스 다운로드 (rhasspy/piper-voices 공식 저장소)
# https://huggingface.co/rhasspy/piper-voices/resolve/main/ko/ko_KR/kss/medium/ko_KR-kss-medium.onnx
# https://huggingface.co/rhasspy/piper-voices/resolve/main/ko/ko_KR/kss/medium/ko_KR-kss-medium.onnx.json

python measure_tts.py --out-dir <샘플 저장 경로> --piper-model <ko_KR-kss-medium.onnx 경로>
```

`--piper-model`을 안 주면 Piper 구간은 건너뛰고 리포트에 그 사실을 남긴다.
