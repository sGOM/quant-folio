// PreToolUse(Edit|Write) 훅: secrets/*.txt, .env* 파일 편집/작성을 차단한다.
// KIS/KRX/OpenDART 등 실거래 자격증명이 평문 파일로 저장되므로 실수로 인한 노출을 막기 위함.
let data = "";
process.stdin.on("data", (chunk) => (data += chunk));
process.stdin.on("end", () => {
  try {
    const input = JSON.parse(data);
    const filePath = (input.tool_input && input.tool_input.file_path) || "";
    const normalized = filePath.replace(/\\/g, "/");
    const isSecretFile = /(^|\/)secrets\/[^/]+\.txt$/.test(normalized);
    const isEnvFile =
      /(^|\/)\.env(\.[^/]*)?$/.test(normalized) &&
      !/\.env\.example$/.test(normalized);

    if (isSecretFile || isEnvFile) {
      console.log(
        JSON.stringify({
          hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "deny",
            permissionDecisionReason:
              "시크릿/환경변수 파일 편집 차단됨: " +
              filePath +
              " (시크릿은 secrets/*.txt로 관리되며 직접 편집 대상이 아닙니다. 필요하면 secrets/README.md를 참고하세요.)",
          },
        })
      );
    }
  } catch (e) {
    // 파싱 실패 시 안전하게 통과(차단하지 않음)
  }
});
