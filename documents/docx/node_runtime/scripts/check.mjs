try {
  const docx = await import("../vendor/docx.mjs");
  if (
    typeof docx.Document !== "function"
    || typeof docx.Packer?.toBuffer !== "function"
  ) {
    throw new Error("DOCX bundle 导出不完整。");
  }
  process.stdout.write(`${JSON.stringify({ ok: true })}\n`);
} catch {
  process.stderr.write(
    `${JSON.stringify({
      ok: false,
      error_type: "node_dependencies_missing",
      message: "DOCX Node 依赖无法加载。",
    })}\n`,
  );
  process.exitCode = 1;
}
