"""仅展示受限 PluginContext 的最小 Hook 注册方式。"""

from hermes.hooks import Allow


def _observe(context):
    """观察型 Hook 不读取或修改业务对象。"""
    return None


def _allow(context):
    """示例控制 Hook 只返回显式 Allow，不改变请求内容。"""
    return Allow()


def register(ctx):
    """同步注册函数可同时用于 CLI 和 Gateway。"""
    ctx.register_hook(
        "post_tool_call",
        _observe,
        hook_id="audit_tool_result",
    )
    ctx.register_hook(
        "pre_llm_call",
        _allow,
        hook_id="audit_llm_allow",
    )
