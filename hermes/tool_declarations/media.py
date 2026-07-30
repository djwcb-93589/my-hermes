"""Media Toolset 的轻量声明。"""

from hermes.tool_declarations.contracts import ToolDeclaration


MAX_MEDIA_FILES = 20


TOOL_DECLARATIONS = (
    ToolDeclaration(
        name="media_analyze",
        toolset="media",
        schema={
            "name": "media_analyze",
            "description": (
                "Analyze 1 to 20 local media files with the configured Doubao "
                "multimodal model. Supported formats: PNG, JPEG, WEBP, MP3, "
                "WAV, AAC, and M4A. Every path must be relative to the current "
                "session cwd and pass the shared filesystem policy. Use one "
                "call for a single item or several related images/audio files. "
                "This sends media to an external model service and may incur "
                "cost. Do not automatically repeat a request after timeout, "
                "network failure, or an unknown result."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "paths": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_MEDIA_FILES,
                        "items": {"type": "string"},
                        "description": "Media paths relative to the current session cwd.",
                    },
                    "prompt": {
                        "type": "string",
                        "minLength": 1,
                        "description": "What to extract, describe, compare, or summarize.",
                    },
                    "media_type": {
                        "type": "string",
                        "enum": ["auto", "image", "audio"],
                        "default": "auto",
                        "description": "Restrict all inputs to image or audio, or detect automatically.",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Optional model request timeout in milliseconds.",
                    },
                },
                "required": ["paths", "prompt"],
            },
        },
        execution_environments=("cli", "gateway"),
        default_enabled_environments=("cli",),
        unattended_allowed=False,
        approval_mode="interactive_or_remote",
        risk_level="high",
        retry_safe=False,
        unknown_on_crash=True,
    ),
)


__all__ = ["MAX_MEDIA_FILES", "TOOL_DECLARATIONS"]
