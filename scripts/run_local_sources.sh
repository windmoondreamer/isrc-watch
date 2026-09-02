#!/bin/bash
# 깃허브 러너(미국 IP)에서 막히는 소스를 한국 IP인 이 맥에서 보충 실행한다.
#
# 막히는 이유는 코드가 아니라 접속 위치다 (2026-09-02 실측):
#   기업뉴스룸  news.skhynix.co.kr  → 해외 IP에 403 (wp-json·RSS·sitemap 전부)
#   성균관대    ice.skku.edu        → 해외 IP에 무응답 (curl로도 실패)
# 나머지 소스는 Actions가 매일 07:20에 돌리므로 여기서 다시 받지 않는다.
#
# 노션 토큰은 이 파일에 적지 말고 keychain에 둔다. 최초 1회:
#   security add-generic-password -a "$USER" -s isrc-watch-notion -w 'ntn_...'

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

NOTION_TOKEN=$(security find-generic-password -a "$USER" -s isrc-watch-notion -w 2>/dev/null)
if [ -z "$NOTION_TOKEN" ]; then
  echo "[$(date '+%F %T')] 노션 토큰을 keychain에서 못 찾았다 (서비스명 isrc-watch-notion)" >&2
  exit 1
fi
export NOTION_TOKEN

echo "[$(date '+%F %T')] 보충 실행 시작"
/usr/bin/python3 scripts/watch.py --only=기업뉴스룸,성균관대 "$@"
echo "[$(date '+%F %T')] 보충 실행 끝 (rc=$?)"
