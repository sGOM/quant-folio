// PostToolUse(Edit|Write) 훅: backend/** 파일 변경 시 web/engine/worker 재시작 리마인더.
// CLAUDE.md 명시 함정: web/engine/worker는 핫리로드가 없어 재시작을 잊기 쉬움.
let data = "";
process.stdin.on("data", (chunk) => (data += chunk));
process.stdin.on("end", () => {
  try {
    const input = JSON.parse(data);
    const filePath =
      (input.tool_input && input.tool_input.file_path) ||
      (input.tool_response && input.tool_response.filePath) ||
      "";
    const normalized = filePath.replace(/\\/g, "/");

    if (/(^|\/)backend\//.test(normalized)) {
      console.log(
        JSON.stringify({
          systemMessage:
            "backend 코드 변경 감지: web/engine/worker는 핫리로드가 없습니다. " +
            "반영하려면 `docker compose restart web` (또는 engine/worker) 실행하세요.",
        })
      );
    }
  } catch (e) {
    // 파싱 실패 시 조용히 종료
  }
});
