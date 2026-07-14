# isrc-watch

서울대 ISRC(반도체공동연구소) 교육 공고를 매일 감시해 노션 「교육·강의」 DB에 자동 등록.

## 왜 GitHub Actions인가

클로드 클라우드 루틴(매일 07:06 KST)은 아웃바운드 네트워크가 프록시로 전면 차단돼 있어
(구글조차 CONNECT 403) 교육 사이트를 직접 읽지 못한다. WebSearch로만 교차확인하다 보니
검색엔진에 색인 안 된 공고를 놓쳤다 — 2026-07 서울대 연합교육 실습이 실제로 며칠간 누락됨.

GitHub Actions 러너는 네트워크 제약이 없으므로 ISRC 일정 JSON을 직접 조회할 수 있다.
클로드 루틴은 그대로 두고(스케줄 리셋·D-Day·브리핑 담당), 여기서는 "공고 누락 방지"만 책임진다.

## 동작

1. `https://isrc.snu.ac.kr/edu/school/info/state/json` POST 조회 (전체 교육 일정)
2. 접수종료일이 오늘 이후인 과정만 추출 (EVENT_SKEY 중복 제거)
3. 노션 DB에서 `주요 일정` 텍스트의 `[ISRC:<skey>]` 마커로 기등록분 식별
4. 미등록 과정만 행 추가 — 상태(신청중/예정)·신청마감·주요 일정 자동 채움
5. 실행 결과는 Actions Summary에 표시

과정명이 아니라 EVENT_SKEY로 중복 판정하므로, 회차명이 바뀌어도 중복 등록되지 않는다.
`(일반)`이 붙은 과정은 재직자 트랙이라 메모에 자격 경고를 남긴다.

## 셋업 (최초 1회)

### 1. 노션 통합 생성 + DB 연결
1. https://www.notion.so/my-integrations → **New integration**
   - 이름: `isrc-watch`, 타입: Internal
   - Capabilities: Read content + Insert content 체크
2. **Internal Integration Secret** 복사 (`ntn_...`)
3. 노션에서 「교육·강의」 DB 페이지 열기 → 우측 상단 `···` → **Connections** → `isrc-watch` 추가
   (이걸 안 하면 API가 DB를 못 본다)

### 2. GitHub Secrets 등록
리포지토리 → Settings → Secrets and variables → Actions → New repository secret

| 이름 | 값 |
|---|---|
| `NOTION_TOKEN` | 위에서 복사한 `ntn_...` |
| `NOTION_DB_ID` | `3244fcb7249c4098aa5a7f81a572c8cb` |

### 3. 동작 확인
Actions 탭 → "반도체 교육 공고 감시" → **Run workflow** 수동 실행 → Summary 확인.

## 로컬 테스트

```bash
export NOTION_TOKEN='ntn_...'
export NOTION_DB_ID='3244fcb7249c4098aa5a7f81a572c8cb'
python3 scripts/watch.py
```

## 스케줄

매일 22:20 UTC = **07:20 KST**. 클로드 아침 루틴(07:06 KST) 직후라, 루틴이 갱신한 DB 위에
신규 건만 얹는 순서가 된다.
