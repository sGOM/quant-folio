---
name: commit-pr-merge
description: 작업 변경사항을 작은 단위 커밋으로 나눠 브랜치에 쌓고, PR을 생성한 뒤 admin으로 머지까지 끝내는 형상관리 워크플로우. "커밋하고 PR 만들어 머지", "PR 올려줘", "머지까지 해줘" 류 요청에 사용. pr-manager 에이전트의 표준 절차.
---

# 작은 단위 커밋 → PR 생성 → 머지

QuantFolio의 표준 형상관리 흐름. 한 덩어리 커밋 대신 **논리적으로 작은 커밋 여러 개**로
쌓고, 하나의 PR로 묶어 리뷰 가능하게 만든 뒤, 소유자 승인 정책에 따라 admin으로 머지한다.

> 원칙(커밋 메시지 한국어, main 직접 커밋 금지, `--no-verify` 금지, 파괴적 작업 승인 필요)은
> pr-manager 에이전트 정의를 따른다. 이 스킬은 그 원칙 위에서 **순서와 커밋 분할 방법**을 규정한다.

## 절차

### 1. 상태 파악

```bash
git status
git diff --stat
git log --oneline -10
```

변경 파일 목록과 최근 커밋 스타일(제목 컨벤션·언어)을 확인한다. 이번 작업과 무관한
파일(빌드 산출물 `frontend/tsconfig.tsbuildinfo`, untracked 잡파일, 다른 기능의 미완성
변경)을 식별해 **커밋에서 제외할 목록**을 미리 정한다.

### 2. 브랜치 생성 (main이면)

현재 `main`이면 목적이 드러나는 브랜치를 먼저 만든다. 이미 작업 브랜치면 그대로 쓴다.

```bash
git rev-parse --abbrev-ref HEAD          # 현재 브랜치 확인
git switch -c feat/<주제>                # main일 때만
```

접두어: `feat/` `fix/` `refactor/` `docs/` `chore/` `test/`.

### 3. 작은 단위로 나눠 순차 커밋

변경을 **관련 있는 것끼리 묶은 최소 단위**로 쪼갠다. 각 커밋은 하나의 의도만 담고,
그 자체로 빌드/설명이 성립해야 한다. 좋은 분할 기준:

- 계층/역할별: 모델·마이그레이션 / 서비스 로직 / 라우트 / 프론트 / 테스트 / 문서
- 성격별: 리팩터링(동작 불변)과 기능 변경을 **같은 커밋에 섞지 않는다**
- 공유 유틸 추가와 그 사용처 변경은 나눌 수 있으면 나눈다(먼저 유틸, 다음 사용처)

각 단위를 명시적으로 스테이징해 커밋한다(파일 단위로 지정 — `git add .` 지양):

```bash
git add path/a.py path/b.py
git commit -m "refactor: 잔고 정규화를 Balance.positions_normalized로 통합" \
  -m "- reconcile._balance_map이 공유 메서드를 재사용하도록 변경
- 미사용 InvalidOperation import 정리" \
  -m "Co-Authored-By: Claude <모델명> <noreply@anthropic.com>"

git add path/route.py
git commit -m "feat: 실시간 포지션을 실계좌 잔고로 조회" \
  -m "- /positions가 브로커 잔고 우선, 실패 시 로컬 DB 폴백" \
  -m "Co-Authored-By: Claude <모델명> <noreply@anthropic.com>"
```

주의:
- **인터랙티브 스테이징(`git add -p`, `git add -i`)은 이 환경에서 동작하지 않는다.**
  분할은 파일 단위로 한다. 한 파일 안의 변경을 더 쪼개야 하면 사용자에게 알리고 파일 단위로 합친다.
- Bash에서 여러 줄 메시지는 `-m`을 반복한다(PowerShell heredoc `@'...'@`를 Bash에 쓰지 말 것).
- 제목: `feat|fix|refactor|docs|chore|test: <한국어 요약>` (50자 내외). 마지막 트레일러 필수.
- 트레일러의 `<모델명>`은 **지금 실행 중인 모델 이름**을 쓴다(예: `Claude Sonnet 5`, `Claude Opus 5`). 버전을 하드코딩하지 말 것 — 모델이 바뀌면 이력이 거짓이 된다.
- 무엇을 어느 커밋에 넣고 뺐는지 사용자에게 보고한다.

### 4. 푸시

```bash
git push -u origin HEAD
```

### 5. PR 생성

```bash
gh pr create --base main --head <branch> --title "<대표 커밋 제목>" --body "$(cat <<'EOF'
## 요약
<1~3줄로 이 PR이 하는 일>

## 변경사항
- <핵심 변경 불릿 — 커밋 단위와 대응>

## 테스트
- <검증 방법: docker compose exec web pytest 등, 또는 미검증 시 그대로 명시>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- 제목은 대표 커밋 제목과 일치시킨다.
- 테스트를 돌리지 않았으면 "미검증"이라고 정직하게 적는다.

### 6. 머지 (admin bypass)

이 저장소는 `Restrict-Merge` 룰셋으로 모든 머지가 `BLOCKED`으로 표시되며, 소유자가
admin bypass를 **상시 정책으로 승인**했다. 따라서 룰셋 차단을 만나도 멈춰 되묻지 말고
`--admin`으로 머지한다.

```bash
gh pr merge <번호> --merge --admin --delete-branch
```

머지 후:

```bash
git switch main && git pull --ff-only
```

**PR 번호·머지 커밋 해시**를 사용자에게 보고한다.

## 중단 지점 (사용자 지시로만 진행)

기본은 커밋→PR→머지를 한 번에 끝낸다. 단, 사용자가 "PR까지만" / "커밋만"이라고 하면
해당 단계에서 멈춘다. 반대로 요청 없이 머지를 임의로 실행하지는 않되, 이 스킬이
호출됐다는 것 자체가 "머지까지" 위임으로 본다(요청 문구가 커밋·PR로 한정되면 그에 맞춘다).

## 하지 않는 것

- 코드·설정 **로직 수정** (형상관리 외 변경은 담당 에이전트에 위임).
- `git push --force`, `git reset --hard`, `--delete-branch` 외 브랜치 삭제 등 파괴적 작업은
  사용자 명시 승인 없이 하지 않는다(머지 시 `--delete-branch`는 이 흐름의 정상 정리 단계).
- 훅 우회(`--no-verify`)·서명 우회.
