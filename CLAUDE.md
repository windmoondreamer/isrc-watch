# isrc-watch — Claude Code 작업 가이드

반도체 교육·공모전 자동 감시 → Notion 등록 (GitHub Actions 무인 운영).

## "이어서 하자" — 새 기계·새 세션 시작 절차
1. **코드 최신화**: `git pull`
2. **스킬 동기화**: `bash ~/.claude/skills/sync-skills/sync.sh pull`
   - "받음: <이름>" 이 뜨면 다른 기계에서 만든 새 스킬 → 사용자에게 알린다.
   - sync-skills 자체가 없으면(스킬 폴더 비었으면):
     `gh repo clone windmoondreamer/claude-skills ~/.claude/claude-skills-repo && cp -R ~/.claude/claude-skills-repo/*/ ~/.claude/skills/`
3. **기억 로드**: `~/claude-memory` 없으면
   `git clone https://github.com/windmoondreamer/claude-memory ~/claude-memory`
   → 그 안의 `MEMORY.md` + `project_isrc_watch_infra.md` 등을 읽어 맥락 파악.
4. 사용자에게 **현재 상태 + 다음 할 일**을 3줄로 요약 보고.

## 이 세션에서 새 스킬을 만들면
작업 끝에 `bash ~/.claude/skills/sync-skills/sync.sh push` 를 실행해 GitHub에 자동 반영한다.

## 핵심 맥락 (자세한 건 claude-memory)
- **노션 인증 2체계**: 내부통합 `isrc-watch`(REST·GitHub Actions용) vs claude.ai MCP 커넥터(브리핑 클라우드 루틴용). 커넥터는 ~2주마다 만료 → 재연결 필요.
- **실행 캘린더 진짜 DB = `cbf3ebf0330d44dd938e6186cb2bf896`** ("2026 여름 실행 캘린더"). 이름 비슷한 구버전 `7305739e` 아님.
- 로컬 스크립트 실행엔 `NOTION_TOKEN` 필요(평소엔 GitHub Secret으로 클라우드 실행).
- 관련 저장소: `claude-memory`(기억), `claude-skills`(스킬).

## 구조
- `scripts/watch.py` — 6개 소스(ISRC·IDEC·KSIA·KANC·NNFC·건국대게시판) 감시 → 교육·공모전 DB. 신청완료→캘린더 미러, 만료 자동정리.
- `scripts/daily.py` — 아침 루틴(메인 페이지 D-Day·스케줄).
- `scripts/notion.py` — 노션 API 래퍼 + DB ID 상수.
- `.github/workflows/` — watch(교육감시)·daily(아침루틴)·trading(SOXL) cron.
