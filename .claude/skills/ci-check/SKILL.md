---
name: ci-check
description: 현재 브랜치/PR의 CI(GitHub Actions) 상태를 확인하고 실패 시 관련 job 로그만 뽑아 요약한다. "CI 상태 확인해줘", "CI 왜 실패했어" 류 요청에 사용.
disable-model-invocation: true
---

# CI 상태 확인

이 저장소의 CI는 두 워크플로우로 구성된다.

- `CI` (`.github/workflows/ci.yml`) — PR/main push마다 실행. job: `backend`(pytest), `frontend`(lint/build)
- `E2E Smoke` (`.github/workflows/e2e-smoke.yml`) — 스케줄 실행. job: `smoke`

## 절차

1. **현재 브랜치의 최신 실행 확인**

   ```bash
   gh run list --branch "$(git branch --show-current)" --limit 5
   ```

   PR이 있다면 대신 PR 기준으로 봐도 된다:

   ```bash
   gh pr checks --watch=false
   ```

2. **실패한 run이 있으면 해당 job 로그만 추출**

   전체 로그는 노이즈가 크므로 실패한 job만 지정한다.

   ```bash
   gh run view <run-id> --log-failed
   ```

   `<run-id>`는 1번 출력의 첫 컬럼(또는 `gh run list --json databaseId,conclusion,name`).

3. **요약해서 보고**

   - 어떤 job이 실패했는지 (`backend` pytest? `frontend` lint/build? `smoke`?)
   - 실패 원인 한 줄(예: 특정 테스트 assertion, 타입 에러, lint 룰)
   - 로컬 재현 명령 제시:
     - backend: `docker compose exec web pytest tests/<파일> -k <테스트명>`
     - frontend: `docker compose exec frontend npm run lint` / `npm run build`

4. **아직 실행 중이면** `gh run watch <run-id>`로 완료를 기다릴지, 그냥 상태만 보고할지 사용자에게 물어본다(장시간 대기는 기본으로 하지 않는다).

## 주의

- 로그 원문을 통째로 붙여넣지 말 것 — 실패 원인과 관련된 부분만 요약.
- CI 실패를 고치려면 이 스킬 밖에서(코드 수정 후) 별도로 진행한다. 이 스킬은 진단 전용.
