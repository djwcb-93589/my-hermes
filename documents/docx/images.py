"""本地 PNG/JPEG 安全校验、尺寸计算与 DrawingML 生成。"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from .errors import DocxError
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PIC_NS = "http://schemas.openxmlformats.org/drawingml/2006/picture"

_W_PARAGRAPH = f"{{{_W_NS}}}p"
_W_RUN = f"{{{_W_NS}}}r"
_W_DRAWING = f"{{{_W_NS}}}drawing"
_WP_INLINE = f"{{{_WP_NS}}}inline"
_WP_EXTENT = f"{{{_WP_NS}}}extent"
_WP_EFFECT_EXTENT = f"{{{_WP_NS}}}effectExtent"
_WP_DOC_PROPERTIES = f"{{{_WP_NS}}}docPr"
_WP_NON_VISUAL_PROPERTIES = f"{{{_WP_NS}}}cNvGraphicFramePr"
_A_GRAPHIC_LOCKS = f"{{{_A_NS}}}graphicFrameLocks"
_A_GRAPHIC = f"{{{_A_NS}}}graphic"
_A_GRAPHIC_DATA = f"{{{_A_NS}}}graphicData"
_A_BLIP = f"{{{_A_NS}}}blip"
_A_STRETCH = f"{{{_A_NS}}}stretch"
_A_FILL_RECTANGLE = f"{{{_A_NS}}}fillRect"
_A_TRANSFORM = f"{{{_A_NS}}}xfrm"
_A_OFFSET = f"{{{_A_NS}}}off"
_A_EXTENT = f"{{{_A_NS}}}ext"
_A_PRESET_GEOMETRY = f"{{{_A_NS}}}prstGeom"
_A_ADJUST_VALUE_LIST = f"{{{_A_NS}}}avLst"
_PIC_PICTURE = f"{{{_PIC_NS}}}pic"
_PIC_NON_VISUAL_PROPERTIES = f"{{{_PIC_NS}}}nvPicPr"
_PIC_NON_VISUAL_DRAWING_PROPERTIES = f"{{{_PIC_NS}}}cNvPr"
_PIC_NON_VISUAL_PICTURE_PROPERTIES = f"{{{_PIC_NS}}}cNvPicPr"
_PIC_BLIP_FILL = f"{{{_PIC_NS}}}blipFill"
_PIC_SHAPE_PROPERTIES = f"{{{_PIC_NS}}}spPr"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_START = b"\xff\xd8"
JPEG_END = b"\xff\xd9"
EMU_PER_PIXEL = 9525
MAX_IMAGE_FILE_SIZE = 20 * 1024 * 1024
MAX_IMAGE_DIMENSION = 20_000
MAX_IMAGE_PIXELS = 100_000_000
MAX_LAYOUT_WIDTH_PX = 624
MAX_ALT_TEXT_LENGTH = 1024

_JPEG_SOF_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)
_JPEG_STANDALONE_MARKERS = frozenset({0x01, *range(0xD0, 0xDA)})


@dataclass(frozen=True)
class ValidatedImage:
    source_path: Path
    payload: bytes
    image_format: str
    extension: str
    width_px: int
    height_px: int
    rendered_width_px: int
    rendered_height_px: int
    width_emu: int
    height_emu: int
    alt_text: str | None


def validate_local_image(
    image_path: object,
    *,
    width_px: object,
    height_px: object,
    alt_text: object,
) -> ValidatedImage:
    """一次性读取并验证本地普通文件、真实格式和像素上限。"""

    requested_width = _validate_requested_dimension(width_px, "width_px")
    requested_height = _validate_requested_dimension(height_px, "height_px")
    validated_alt_text = _validate_alt_text(alt_text)
    source_path = _normalize_image_path(image_path)
    suffix = source_path.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg"}:
        raise DocxError(
            "unsupported_image_format",
            "只支持扩展名与真实格式一致的 PNG 或 JPEG 图片。",
        )
    payload = _read_image_bytes(source_path)
    if payload.startswith(PNG_SIGNATURE):
        image_format = "png"
        extension = "png"
        original_width, original_height = _read_png_dimensions(payload)
        if suffix != ".png":
            raise DocxError(
                "unsupported_image_format",
                "图片扩展名与真实 PNG 格式不一致。",
            )
    elif payload.startswith(JPEG_START):
        image_format = "jpeg"
        extension = "jpeg"
        original_width, original_height = _read_jpeg_dimensions(payload)
        if suffix not in {".jpg", ".jpeg"}:
            raise DocxError(
                "unsupported_image_format",
                "图片扩展名与真实 JPEG 格式不一致。",
            )
    else:
        raise DocxError(
            "unsupported_image_format",
            "图片签名不是受支持的 PNG 或 JPEG。",
        )
    _validate_pixel_limits(original_width, original_height)
    rendered_width, rendered_height = _calculate_rendered_dimensions(
        original_width,
        original_height,
        requested_width,
        requested_height,
    )
    _validate_pixel_limits(rendered_width, rendered_height)
    return ValidatedImage(
        source_path=source_path,
        payload=payload,
        image_format=image_format,
        extension=extension,
        width_px=original_width,
        height_px=original_height,
        rendered_width_px=rendered_width,
        rendered_height_px=rendered_height,
        width_emu=rendered_width * EMU_PER_PIXEL,
        height_emu=rendered_height * EMU_PER_PIXEL,
        alt_text=validated_alt_text,
    )


def next_doc_properties_id(
    document_root: ElementTree.Element,
    reserved_ids: set[int] | None = None,
) -> int:
    """扫描整个 document.xml，分配唯一的正整数 wp:docPr/@id。"""

    used: set[int] = set()
    for element in document_root.iter(_WP_DOC_PROPERTIES):
        raw_value = element.attrib.get("id")
        try:
            value = int(raw_value) if raw_value is not None else 0
        except ValueError:
            continue
        if value > 0:
            used.add(value)
    if reserved_ids is not None:
        used.update(reserved_ids)
    candidate = 1
    while candidate in used:
        candidate += 1
    return candidate


def create_inline_image_paragraph(
    *,
    relationship_id: str,
    part_name: str,
    width_emu: int,
    height_emu: int,
    doc_properties_id: int,
    alt_text: str | None,
) -> ElementTree.Element:
    """创建标准 inline DrawingML 图片段落。"""

    paragraph = ElementTree.Element(_W_PARAGRAPH)
    run = ElementTree.SubElement(paragraph, _W_RUN)
    drawing = ElementTree.SubElement(run, _W_DRAWING)
    inline = ElementTree.SubElement(
        drawing,
        _WP_INLINE,
        {"distT": "0", "distB": "0", "distL": "0", "distR": "0"},
    )
    extent_attributes = {"cx": str(width_emu), "cy": str(height_emu)}
    ElementTree.SubElement(inline, _WP_EXTENT, extent_attributes)
    ElementTree.SubElement(
        inline,
        _WP_EFFECT_EXTENT,
        {"l": "0", "t": "0", "r": "0", "b": "0"},
    )
    doc_properties = ElementTree.SubElement(
        inline,
        _WP_DOC_PROPERTIES,
        {
            "id": str(doc_properties_id),
            "name": f"Picture {doc_properties_id}",
        },
    )
    if alt_text is not None:
        doc_properties.set("descr", alt_text)
    frame_properties = ElementTree.SubElement(
        inline,
        _WP_NON_VISUAL_PROPERTIES,
    )
    ElementTree.SubElement(
        frame_properties,
        _A_GRAPHIC_LOCKS,
        {"noChangeAspect": "1"},
    )
    graphic = ElementTree.SubElement(inline, _A_GRAPHIC)
    graphic_data = ElementTree.SubElement(
        graphic,
        _A_GRAPHIC_DATA,
        {"uri": _PIC_NS},
    )
    picture = ElementTree.SubElement(graphic_data, _PIC_PICTURE)
    non_visual = ElementTree.SubElement(
        picture,
        _PIC_NON_VISUAL_PROPERTIES,
    )
    ElementTree.SubElement(
        non_visual,
        _PIC_NON_VISUAL_DRAWING_PROPERTIES,
        {
            "id": "0",
            "name": part_name.rsplit("/", 1)[-1],
        },
    )
    ElementTree.SubElement(
        non_visual,
        _PIC_NON_VISUAL_PICTURE_PROPERTIES,
    )
    blip_fill = ElementTree.SubElement(picture, _PIC_BLIP_FILL)
    ElementTree.SubElement(
        blip_fill,
        _A_BLIP,
        {f"{{{_OFFICE_REL_NS}}}embed": relationship_id},
    )
    stretch = ElementTree.SubElement(blip_fill, _A_STRETCH)
    ElementTree.SubElement(stretch, _A_FILL_RECTANGLE)
    shape_properties = ElementTree.SubElement(
        picture,
        _PIC_SHAPE_PROPERTIES,
    )
    transform = ElementTree.SubElement(shape_properties, _A_TRANSFORM)
    ElementTree.SubElement(transform, _A_OFFSET, {"x": "0", "y": "0"})
    ElementTree.SubElement(transform, _A_EXTENT, extent_attributes)
    geometry = ElementTree.SubElement(
        shape_properties,
        _A_PRESET_GEOMETRY,
        {"prst": "rect"},
    )
    ElementTree.SubElement(geometry, _A_ADJUST_VALUE_LIST)
    return paragraph


_OFFICE_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)


def _normalize_image_path(image_path: object) -> Path:
    if not isinstance(image_path, (str, os.PathLike)):
        raise DocxError("invalid_image", "image_path 必须是本地文件系统路径。")
    try:
        raw_path = os.fspath(image_path)
        if not raw_path:
            raise ValueError("empty path")
        expanded = Path(raw_path).expanduser()
        absolute_path = expanded.absolute()
        existing_components = (absolute_path, *absolute_path.parents)
        if any(
            component.exists() and component.is_symlink()
            for component in existing_components
        ):
            raise DocxError("invalid_image", "image_path 不能是符号链接。")
        return absolute_path.resolve(strict=True)
    except DocxError:
        raise
    except FileNotFoundError as exc:
        raise DocxError("invalid_image", "本地图片不存在。") from exc
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DocxError("invalid_image", "image_path 无法安全规范化。") from exc


def _read_image_bytes(source_path: Path) -> bytes:
    try:
        source_stat = source_path.stat()
        if not stat.S_ISREG(source_stat.st_mode):
            raise DocxError("invalid_image", "image_path 不是普通文件。")
        if source_stat.st_size <= 0:
            raise DocxError("invalid_image", "图片文件为空。")
        if source_stat.st_size > MAX_IMAGE_FILE_SIZE:
            raise DocxError("image_limit_exceeded", "图片文件超过 20 MiB 限制。")
        with source_path.open("rb") as source:
            opened_stat = os.fstat(source.fileno())
            if not stat.S_ISREG(opened_stat.st_mode):
                raise DocxError("invalid_image", "image_path 不是普通文件。")
            payload = source.read(MAX_IMAGE_FILE_SIZE + 1)
    except DocxError:
        raise
    except OSError as exc:
        raise DocxError("invalid_image", "无法读取本地图片。") from exc
    if not payload:
        raise DocxError("invalid_image", "图片文件为空。")
    if len(payload) > MAX_IMAGE_FILE_SIZE:
        raise DocxError("image_limit_exceeded", "图片文件超过 20 MiB 限制。")
    return payload


def _read_png_dimensions(payload: bytes) -> tuple[int, int]:
    if (
        len(payload) < 33
        or payload[8:12] != b"\x00\x00\x00\r"
        or payload[12:16] != b"IHDR"
        or len(payload) < 12
        or payload[-12:-8] != b"\x00\x00\x00\x00"
        or payload[-8:-4] != b"IEND"
    ):
        raise DocxError("invalid_image", "PNG 缺少合法的 IHDR。")
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    if width <= 0 or height <= 0:
        raise DocxError("invalid_image", "PNG 像素尺寸无效。")
    return width, height


def _read_jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 4 or not payload.endswith(JPEG_END):
        raise DocxError("invalid_image", "JPEG 缺少合法的结束标记。")
    offset = 2
    while offset < len(payload) - 2:
        if payload[offset] != 0xFF:
            raise DocxError("invalid_image", "JPEG marker 结构无效。")
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            break
        marker = payload[offset]
        offset += 1
        if marker == 0xD9:
            break
        if marker in _JPEG_STANDALONE_MARKERS:
            continue
        if offset + 2 > len(payload):
            break
        segment_length = int.from_bytes(payload[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(payload):
            raise DocxError("invalid_image", "JPEG segment 长度无效。")
        if marker in _JPEG_SOF_MARKERS:
            if segment_length < 7:
                raise DocxError("invalid_image", "JPEG SOF 结构无效。")
            height = int.from_bytes(payload[offset + 3 : offset + 5], "big")
            width = int.from_bytes(payload[offset + 5 : offset + 7], "big")
            if width <= 0 or height <= 0:
                raise DocxError("invalid_image", "JPEG 像素尺寸无效。")
            return width, height
        if marker == 0xDA:
            break
        offset += segment_length
    raise DocxError("invalid_image", "JPEG 缺少受支持的尺寸标记。")


def _calculate_rendered_dimensions(
    original_width: int,
    original_height: int,
    requested_width: int | None,
    requested_height: int | None,
) -> tuple[int, int]:
    if requested_width is None and requested_height is None:
        if original_width <= MAX_LAYOUT_WIDTH_PX:
            return original_width, original_height
        scale = MAX_LAYOUT_WIDTH_PX / original_width
        return MAX_LAYOUT_WIDTH_PX, max(1, round(original_height * scale))
    if requested_width is not None and requested_height is not None:
        return requested_width, requested_height
    if requested_width is not None:
        return requested_width, max(
            1,
            round(original_height * requested_width / original_width),
        )
    assert requested_height is not None
    return (
        max(1, round(original_width * requested_height / original_height)),
        requested_height,
    )


def _validate_requested_dimension(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > MAX_IMAGE_DIMENSION
    ):
        raise DocxError(
            "invalid_edit_operation",
            f"{field_name} 必须是合理范围内的正整数或 null。",
        )
    return value


def _validate_pixel_limits(width: int, height: int) -> None:
    if (
        width > MAX_IMAGE_DIMENSION
        or height > MAX_IMAGE_DIMENSION
        or width * height > MAX_IMAGE_PIXELS
    ):
        raise DocxError("image_limit_exceeded", "图片像素尺寸超过安全限制。")


def _validate_alt_text(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) > MAX_ALT_TEXT_LENGTH
        or "\r" in value
        or not _is_valid_xml_text(value)
    ):
        raise DocxError(
            "invalid_edit_operation",
            "alt_text 必须是受支持且长度受限的 XML 字符串或 null。",
        )
    return value


def _is_valid_xml_text(value: str) -> bool:
    return all(
        codepoint in {0x9, 0xA, 0xD}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
        for codepoint in map(ord, value)
    )
