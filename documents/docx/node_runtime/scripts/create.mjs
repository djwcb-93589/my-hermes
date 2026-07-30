import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";

import {
  AlignmentType,
  Document,
  HeadingLevel,
  Packer,
  PageBreak,
  Paragraph,
  Table,
  TableCell,
  TableRow,
  TextRun,
  UnderlineType,
} from "docx";


const ALIGNMENTS = Object.freeze({
  left: AlignmentType.LEFT,
  center: AlignmentType.CENTER,
  right: AlignmentType.RIGHT,
  justify: AlignmentType.JUSTIFIED,
});

const HEADING_LEVELS = Object.freeze({
  1: HeadingLevel.HEADING_1,
  2: HeadingLevel.HEADING_2,
  3: HeadingLevel.HEADING_3,
  4: HeadingLevel.HEADING_4,
  5: HeadingLevel.HEADING_5,
  6: HeadingLevel.HEADING_6,
});


class SpecError extends Error {
  constructor(errorType, message, options = undefined) {
    super(message, options);
    this.errorType = errorType;
  }
}


function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}


function assertKeys(value, allowedKeys, errorType) {
  for (const key of Object.keys(value)) {
    if (!allowedKeys.has(key)) {
      throw new SpecError(errorType, "规格包含不支持的字段。");
    }
  }
}


function validateRun(run) {
  if (!isObject(run)) {
    throw new SpecError("invalid_block", "TextRun 必须是 object。");
  }
  assertKeys(
    run,
    new Set(["text", "bold", "italic", "underline"]),
    "invalid_block",
  );
  if (typeof run.text !== "string") {
    throw new SpecError("invalid_block", "TextRun.text 必须是字符串。");
  }
  for (const key of ["bold", "italic", "underline"]) {
    if (typeof run[key] !== "boolean") {
      throw new SpecError("invalid_block", `TextRun.${key} 必须是布尔值。`);
    }
  }
}


function validateBlock(block) {
  if (!isObject(block) || typeof block.type !== "string") {
    throw new SpecError("invalid_block", "内容块必须包含有效 type。");
  }

  if (block.type === "paragraph") {
    assertKeys(
      block,
      new Set(["type", "runs", "style", "alignment"]),
      "invalid_block",
    );
    if (!Array.isArray(block.runs)) {
      throw new SpecError("invalid_block", "Paragraph.runs 必须是数组。");
    }
    block.runs.forEach(validateRun);
    if (block.style !== null && typeof block.style !== "string") {
      throw new SpecError("invalid_block", "Paragraph.style 无效。");
    }
    if (
      block.alignment !== null
      && !Object.prototype.hasOwnProperty.call(ALIGNMENTS, block.alignment)
    ) {
      throw new SpecError("invalid_block", "Paragraph.alignment 无效。");
    }
    return;
  }

  if (block.type === "heading") {
    assertKeys(block, new Set(["type", "text", "level"]), "invalid_block");
    if (typeof block.text !== "string") {
      throw new SpecError("invalid_block", "Heading.text 必须是字符串。");
    }
    if (!Number.isInteger(block.level) || !(block.level in HEADING_LEVELS)) {
      throw new SpecError("invalid_block", "Heading.level 必须在 1 到 6 之间。");
    }
    return;
  }

  if (block.type === "table") {
    assertKeys(block, new Set(["type", "rows", "header_row"]), "invalid_block");
    if (!Array.isArray(block.rows) || block.rows.length === 0) {
      throw new SpecError("invalid_block", "Table.rows 必须是非空数组。");
    }
    if (typeof block.header_row !== "boolean") {
      throw new SpecError("invalid_block", "Table.header_row 必须是布尔值。");
    }
    const columnCount = Array.isArray(block.rows[0]) ? block.rows[0].length : 0;
    if (columnCount === 0) {
      throw new SpecError("invalid_block", "Table 行必须是非空数组。");
    }
    for (const row of block.rows) {
      if (
        !Array.isArray(row)
        || row.length !== columnCount
        || row.some((cell) => typeof cell !== "string")
      ) {
        throw new SpecError("invalid_block", "Table 行列结构无效。");
      }
    }
    return;
  }

  if (block.type === "page_break") {
    assertKeys(block, new Set(["type"]), "invalid_block");
    return;
  }

  throw new SpecError("invalid_block", "内容块 type 不受支持。");
}


function validateSpecification(specification) {
  if (!isObject(specification)) {
    throw new SpecError("invalid_request", "规格顶层必须是 object。");
  }
  assertKeys(
    specification,
    new Set(["title", "creator", "blocks"]),
    "invalid_request",
  );
  if (!Array.isArray(specification.blocks)) {
    throw new SpecError("invalid_request", "spec.blocks 必须是数组。");
  }
  for (const key of ["title", "creator"]) {
    if (specification[key] !== null && typeof specification[key] !== "string") {
      throw new SpecError("invalid_request", `spec.${key} 必须是字符串或 null。`);
    }
  }
  specification.blocks.forEach(validateBlock);
}


function createParagraph(block) {
  const options = {
    children: block.runs.map(
      (run) => new TextRun({
        text: run.text,
        bold: run.bold,
        italics: run.italic,
        underline: run.underline ? { type: UnderlineType.SINGLE } : undefined,
      }),
    ),
  };
  if (block.style !== null) {
    options.style = block.style;
  }
  if (block.alignment !== null) {
    options.alignment = ALIGNMENTS[block.alignment];
  }
  return new Paragraph(options);
}


function createHeading(block) {
  return new Paragraph({
    heading: HEADING_LEVELS[block.level],
    children: [new TextRun({ text: block.text })],
  });
}


function createTable(block) {
  return new Table({
    rows: block.rows.map(
      (row, rowIndex) => {
        const isHeaderRow = block.header_row && rowIndex === 0;
        const options = {
          children: row.map(
            (cell) => new TableCell({
              children: [
                new Paragraph({
                  children: [
                    new TextRun({
                      text: cell,
                      bold: isHeaderRow,
                    }),
                  ],
                }),
              ],
            }),
          ),
        };
        if (isHeaderRow) {
          options.tableHeader = true;
        }
        return new TableRow(options);
      },
    ),
  });
}


function createBlock(block) {
  if (block.type === "paragraph") {
    return createParagraph(block);
  }
  if (block.type === "heading") {
    return createHeading(block);
  }
  if (block.type === "table") {
    return createTable(block);
  }
  return new Paragraph({ children: [new PageBreak()] });
}


async function main() {
  const [specPath, outputPath, ...extraArguments] = process.argv.slice(2);
  if (
    extraArguments.length !== 0
    || typeof specPath !== "string"
    || typeof outputPath !== "string"
    || !path.isAbsolute(specPath)
    || !path.isAbsolute(outputPath)
  ) {
    throw new SpecError(
      "invalid_request",
      "必须提供绝对 spec 路径和绝对输出路径。",
    );
  }

  let specification;
  try {
    specification = JSON.parse(await readFile(specPath, "utf8"));
  } catch (error) {
    throw new SpecError("invalid_request", "无法读取有效的 JSON 规格。", { cause: error });
  }
  validateSpecification(specification);

  const documentOptions = {
    sections: [
      {
        children: specification.blocks.map(createBlock),
      },
    ],
  };
  if (specification.title !== null) {
    documentOptions.title = specification.title;
  }
  if (specification.creator !== null) {
    documentOptions.creator = specification.creator;
  }

  const buffer = await Packer.toBuffer(new Document(documentOptions));
  await writeFile(outputPath, buffer, { flag: "w" });
  process.stdout.write(
    `${JSON.stringify({
      ok: true,
      block_count: specification.blocks.length,
    })}\n`,
  );
}


main().catch((error) => {
  const errorType = error instanceof SpecError
    ? error.errorType
    : "node_execution_failed";
  process.stderr.write(
    `${JSON.stringify({
      ok: false,
      error_type: errorType,
      message: error instanceof SpecError
        ? error.message
        : "DOCX 创建进程执行失败。",
    })}\n`,
  );
  process.exitCode = 1;
});
