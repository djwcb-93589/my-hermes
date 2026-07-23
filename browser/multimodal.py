"""火山方舟多模态分析服务。

本模块不依赖 Playwright，也不认识浏览器快照或 artifact 注册表。调用方先
完成路径授权和文件存在性检查，再把已验证的媒体描述交给这里；这里仅负责
格式、大小、Responses API 请求和模型响应的安全收敛。
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx


_DEFAULT_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
_DEFAULT_TIMEOUT_MS = 60_000
_DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_DEFAULT_MAX_TOTAL_BYTES = 40 * 1024 * 1024
_DEFAULT_MAX_MEDIA_FILES = 20

_IMAGE_FORMATS: dict[str, tuple[str, str]] = {
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".webp": ("image", "image/webp"),
}
# 只允许当前 Responses API 明确采用 data URL 传入的常见音频格式；不做本地
# 转码，也不把其他容器假定为模型可理解的音频。
_AUDIO_FORMATS: dict[str, tuple[str, str]] = {
    ".mp3": ("audio", "audio/mpeg"),
    ".wav": ("audio", "audio/wav"),
    ".aac": ("audio", "audio/aac"),
    ".m4a": ("audio", "audio/m4a"),
}


class MultimodalError(Exception):
    """对外可识别的多模态失败，不携带请求体或媒体内容。"""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message


@dataclass(frozen=True)
class MediaSource:
    """已经通过会话路径检查的媒体文件及其安全元数据。"""

    path: Path
    source_type: str
    artifact_id: str | None
    filename: str


@dataclass(frozen=True)
class ValidatedMedia:
    """可发送给模型的媒体描述；文件内容只在构造请求时临时读取。"""

    source: MediaSource
    media_type: str
    mime_type: str
    size_bytes: int

    def public_payload(self) -> dict[str, Any]:
        """返回调用方可见的元数据，绝不包含本地路径或文件内容。"""
        return {
            "source_type": self.source.source_type,
            "artifact_id": self.source.artifact_id,
            "filename": self.source.filename,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class MultimodalAnalysis:
    """屏蔽供应商原始响应后的稳定分析结果。"""

    analysis: str
    model: str
    provider: str
    request_id: str | None
    usage: dict[str, int]
    media: list[ValidatedMedia]


def _positive_int(value: int | None, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MultimodalError("multimodal_not_configured", f"{name} 必须是正整数")
    return value


def _environment_positive_int(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise MultimodalError("multimodal_not_configured", f"{name} 必须是正整数") from exc
    return _positive_int(value, name=name)


class DoubaoMultimodalProvider:
    """通过火山方舟 Responses API 调用配置好的 Doubao 多模态模型。"""

    provider_name = "doubao_ark"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        request_timeout_ms: int | None = None,
        max_image_bytes: int | None = None,
        max_audio_bytes: int | None = None,
        max_total_bytes: int | None = None,
        max_media_files: int | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.getenv("ARK_API_KEY")
        self._base_url = (base_url if base_url is not None else os.getenv("ARK_BASE_URL", _DEFAULT_ARK_BASE_URL)).rstrip("/")
        self._model = model if model is not None else os.getenv("DOUBAO_MULTIMODAL_MODEL")
        self._request_timeout_ms = _positive_int(
            request_timeout_ms
            if request_timeout_ms is not None
            else _environment_positive_int("DOUBAO_MULTIMODAL_TIMEOUT_MS", _DEFAULT_TIMEOUT_MS),
            name="request_timeout_ms",
        )
        self._max_image_bytes = _positive_int(
            max_image_bytes
            if max_image_bytes is not None
            else _environment_positive_int("DOUBAO_MAX_IMAGE_BYTES", _DEFAULT_MAX_IMAGE_BYTES),
            name="max_image_bytes",
        )
        self._max_audio_bytes = _positive_int(
            max_audio_bytes
            if max_audio_bytes is not None
            else _environment_positive_int("DOUBAO_MAX_AUDIO_BYTES", _DEFAULT_MAX_AUDIO_BYTES),
            name="max_audio_bytes",
        )
        self._max_total_bytes = _positive_int(
            max_total_bytes
            if max_total_bytes is not None
            else _environment_positive_int("DOUBAO_MAX_TOTAL_MEDIA_BYTES", _DEFAULT_MAX_TOTAL_BYTES),
            name="max_total_bytes",
        )
        self._max_media_files = _positive_int(
            max_media_files
            if max_media_files is not None
            else _environment_positive_int(
                "DOUBAO_MAX_MEDIA_FILES", _DEFAULT_MAX_MEDIA_FILES
            ),
            name="max_media_files",
        )

    def _require_configuration(self) -> None:
        if not isinstance(self._api_key, str) or not self._api_key.strip():
            raise MultimodalError("multimodal_not_configured", "未配置 ARK_API_KEY")
        if not isinstance(self._model, str) or not self._model.strip():
            raise MultimodalError("multimodal_not_configured", "未配置 DOUBAO_MULTIMODAL_MODEL")
        if not self._base_url.startswith(("https://", "http://")):
            raise MultimodalError("multimodal_not_configured", "ARK_BASE_URL 必须是 HTTP(S) 地址")

    def configuration(self) -> dict[str, str]:
        """返回已验证的安全模型标识，不暴露密钥或服务地址。"""
        self._require_configuration()
        return {
            "provider": self.provider_name,
            "model": self._model.strip(),
        }

    def validate_media(
        self,
        sources: Iterable[MediaSource],
        *,
        expected_type: str | None = None,
    ) -> list[ValidatedMedia]:
        """按文件名扩展名、真实文件大小和总大小验证媒体输入。"""
        try:
            source_list = list(sources)
        except TypeError as exc:
            raise MultimodalError("invalid_media_path", "媒体来源必须是可迭代集合") from exc
        if not source_list:
            raise MultimodalError("invalid_media_path", "至少需要一个媒体文件")
        if len(source_list) > self._max_media_files:
            raise MultimodalError(
                "too_many_media_files",
                f"单次最多允许 {self._max_media_files} 个媒体文件",
            )
        validated: list[ValidatedMedia] = []
        total_size = 0
        for source in source_list:
            suffix = source.path.suffix.lower()
            image_info = _IMAGE_FORMATS.get(suffix)
            audio_info = _AUDIO_FORMATS.get(suffix)
            if image_info is not None:
                media_type, mime_type = image_info
                limit = self._max_image_bytes
            elif audio_info is not None:
                media_type, mime_type = audio_info
                limit = self._max_audio_bytes
            else:
                raise MultimodalError(
                    "unsupported_media_type",
                    f"不支持的媒体格式: {source.path.suffix or '无扩展名'}",
                )
            if expected_type is not None and media_type != expected_type:
                raise MultimodalError(
                    "unsupported_media_type",
                    f"此接口只接受{expected_type}，但 {source.filename} 是 {media_type}",
                )
            try:
                size_bytes = source.path.stat().st_size
            except FileNotFoundError as exc:
                raise MultimodalError("media_not_found", f"媒体文件不存在: {source.filename}") from exc
            except OSError as exc:
                raise MultimodalError("invalid_media_path", f"无法读取媒体文件信息: {source.filename}") from exc
            if size_bytes <= 0:
                raise MultimodalError(
                    "invalid_media_path", f"媒体文件不能为空: {source.filename}"
                )
            if size_bytes >= limit:
                raise MultimodalError(
                    "media_too_large",
                    f"{source.filename} 超出 {media_type} 单文件大小限制",
                )
            total_size += size_bytes
            if total_size > self._max_total_bytes:
                raise MultimodalError("media_too_large", "媒体总大小超出请求限制")
            validated.append(
                ValidatedMedia(source, media_type, mime_type, size_bytes)
            )
        return validated

    def analyze(
        self,
        media: list[ValidatedMedia],
        prompt: str,
        *,
        timeout_ms: int | None = None,
    ) -> MultimodalAnalysis:
        """执行一次不可自动重试的模型请求，并仅提取最终文本输出。"""
        self._require_configuration()
        if not isinstance(prompt, str) or not prompt.strip():
            raise MultimodalError("model_request_failed", "prompt 必须是非空字符串")
        request_timeout = self._request_timeout_ms if timeout_ms is None else _positive_int(
            timeout_ms, name="timeout_ms"
        )
        payload = self._build_request(media, prompt)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=request_timeout / 1000.0) as client:
                response = client.post(f"{self._base_url}/responses", headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise MultimodalError("model_timeout", "多模态模型请求超时") from exc
        except httpx.RequestError as exc:
            raise MultimodalError("model_request_failed", f"多模态模型请求失败: {exc.__class__.__name__}") from exc
        finally:
            # 请求体包含临时 Base64 字符串；离开本次调用后不在 provider 中缓存它。
            payload = None
            headers = None
        request_id = response.headers.get("x-request-id") or response.headers.get("x-tt-logid")
        if response.status_code in {401, 403}:
            raise MultimodalError("model_auth_failed", "火山方舟鉴权失败")
        if response.status_code == 429:
            raise MultimodalError("model_rate_limited", "火山方舟请求频率受限")
        if response.is_error:
            raise MultimodalError("model_request_failed", f"火山方舟请求失败: HTTP {response.status_code}")
        try:
            response_payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise MultimodalError("invalid_model_response", "模型返回的不是有效 JSON") from exc
        if not isinstance(response_payload, dict):
            raise MultimodalError("invalid_model_response", "模型返回结构无效")
        self._validate_response_status(response_payload)
        analysis = self._extract_output_text(response_payload)
        response_model = response_payload.get("model")
        model = response_model if isinstance(response_model, str) and response_model else self._model
        response_request_id = response_payload.get("id")
        if request_id is None and isinstance(response_request_id, str):
            request_id = response_request_id
        return MultimodalAnalysis(
            analysis=analysis,
            model=model,
            provider=self.provider_name,
            request_id=request_id,
            usage=self._safe_usage(response_payload.get("usage")),
            media=media,
        )

    def _build_request(self, media: list[ValidatedMedia], prompt: str) -> dict[str, Any]:
        """在此处短暂读取媒体并构造 Responses API 的输入内容。"""
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for item in media:
            try:
                if item.source.path.is_symlink() or not item.source.path.is_file():
                    raise MultimodalError(
                        "invalid_media_path", f"媒体文件不再是普通文件: {item.source.filename}"
                    )
                raw_data = item.source.path.read_bytes()
            except FileNotFoundError as exc:
                raise MultimodalError("media_not_found", f"媒体文件不存在: {item.source.filename}") from exc
            except OSError as exc:
                raise MultimodalError("invalid_media_path", f"无法读取媒体文件: {item.source.filename}") from exc
            if not raw_data:
                raise MultimodalError(
                    "invalid_media_path", f"媒体文件不能为空: {item.source.filename}"
                )
            if len(raw_data) != item.size_bytes:
                raise MultimodalError("media_too_large", f"媒体文件在读取时发生变化: {item.source.filename}")
            encoded = base64.b64encode(raw_data).decode("ascii")
            # 不把 raw_data 或 encoded 保存为实例字段，函数返回后由 Python 回收。
            if item.media_type == "image":
                content.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{item.mime_type};base64,{encoded}",
                    }
                )
            else:
                content.append(
                    {
                        "type": "input_audio",
                        "audio_url": f"data:{item.mime_type};base64,{encoded}",
                    }
                )
        return {
            "model": self._model,
            "input": [{"role": "user", "content": content}],
            "store": False,
        }

    @staticmethod
    def _validate_response_status(payload: dict[str, Any]) -> None:
        """拒绝失败或不完整响应，绝不把其中的部分文字当作分析结果。"""
        error = payload.get("error")
        if (isinstance(error, str) and error.strip()) or (
            not isinstance(error, str) and bool(error)
        ):
            raise MultimodalError("model_request_failed", "多模态模型返回了错误状态")
        status = payload.get("status")
        if status is None:
            return
        if not isinstance(status, str):
            raise MultimodalError("invalid_model_response", "模型响应状态格式无效")
        if status == "completed":
            return
        if status == "incomplete":
            raise MultimodalError("model_request_failed", "多模态模型输出不完整")
        if status == "failed":
            raise MultimodalError("model_request_failed", "多模态模型执行失败")
        raise MultimodalError("model_request_failed", f"多模态模型未完成: {status}")

    @staticmethod
    def _extract_output_text(payload: dict[str, Any]) -> str:
        """只提取 output_text，显式忽略 reasoning_content 等隐藏推理字段。"""
        texts: list[str] = []
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "output_text"
                        and isinstance(part.get("text"), str)
                    ):
                        texts.append(part["text"])
        result = "\n".join(text for text in texts if text)
        if not result:
            output_text = payload.get("output_text")
            result = output_text if isinstance(output_text, str) else ""
        if not result:
            raise MultimodalError("invalid_model_response", "模型响应中没有最终分析文本")
        return result

    @staticmethod
    def _safe_usage(raw_usage: Any) -> dict[str, int]:
        """只保留常见的整数用量字段，不回传供应商原始响应。"""
        if not isinstance(raw_usage, dict):
            return {}
        usage: dict[str, int] = {}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = raw_usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                usage[key] = value
        return usage


class MultimodalAnalyzer:
    """对 BrowserSession 暴露供应商无关的分析入口。"""

    def __init__(self, provider: DoubaoMultimodalProvider | None = None) -> None:
        self._provider = provider or DoubaoMultimodalProvider()

    def configuration(self) -> dict[str, str]:
        """提供当前实际会使用的供应商和模型，供上层建立审批绑定。"""
        return self._provider.configuration()

    def analyze(
        self,
        sources: Iterable[MediaSource],
        prompt: str,
        *,
        timeout_ms: int | None = None,
        expected_type: str | None = None,
    ) -> MultimodalAnalysis:
        media = self._provider.validate_media(sources, expected_type=expected_type)
        # 请求构造需要配置的模型名，不能让占位文本离开 provider。
        analysis = self._provider.analyze(media, prompt, timeout_ms=timeout_ms)
        return analysis
