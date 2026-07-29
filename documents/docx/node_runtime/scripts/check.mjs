try {
  await import("docx");
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
