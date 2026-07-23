"""
accessibility tree 格式化。

Playwright 1.61 移除了 ``page.accessibility.snapshot()``,这里改走 CDP
的 ``Accessibility.getFullAXTree``,它返回扁平的 nodes 数组::

    {
        "nodes": [
            {
                "nodeId": "0",
                "role": {"type": "role", "value": "rootWebArea"},
                "name": {"value": "GitHub"},
                "childIds": ["1", "2"],
                "properties": [
                    {"name": "level", "value": {"value": 1}},
                    {"name": "focused", "value": {"value": true}}
                ],
                "value": {"value": "current input"},   # 输入框当前值(可选)
                "checked": {"value": "mixed"},         # 复选框状态(可选)
            },
            ...
        ]
    }

本模块把它转成给 LLM 看的文本快照,格式参考 s17 文档::

    rootWebArea "GitHub"
      link "Sign in" [ref=e1]
      search "Search GitHub" [ref=e2]
      heading "Let's build from here" [level=1]

规则:
- 可解析的交互元素(role 命中 ``INTERACTIVE_ROLES`` 且带有
  ``backendDOMNodeId``)才分配 ``[ref=eN]``;
  heading / text / paragraph 等纯展示元素不给 ref,但 heading 带 ``[level=N]``。
- ref 编号在每次 ``format_snapshot`` 调用内从 1 开始递增,跨调用不复用--
  DOM 变了旧 ref 即失效,调用方必须重新拿快照。
- 深度用 2 空格缩进表示。
"""

from __future__ import annotations

from typing import Any


# 命中这些 role 且能解析回 DOM 的元素可交互,给 ref。
# 来源:s17 文档示例 + Playwright accessibility role 常用集合。
# 不含 heading / text / paragraph / list / banner / contentinfo 等纯展示角色。
INTERACTIVE_ROLES: frozenset[str] = frozenset({
    "link",
    "button",
    "textbox",
    "searchbox",
    "search",
    "checkbox",
    "radio",
    "combobox",
    "listbox",
    "option",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "tab",
    "slider",
    "spinbutton",
    "switch",
    "togglebutton",
    "treeitem",
    "gridcell",
    "menu",
    "menubar",
    "tablist",
    "tree",
    "grid",
})


def _role_of(node: dict) -> str:
    """从 CDP 节点提取 role 字符串;缺失时返回 "unknown"。"""
    role = node.get("role")
    if isinstance(role, dict):
        return str(role.get("value", "unknown"))
    if isinstance(role, str):
        return role
    return "unknown"


def _name_of(node: dict) -> str:
    """从 CDP 节点提取 name 字符串;缺失返回空串。"""
    name = node.get("name")
    if isinstance(name, dict):
        return str(name.get("value", ""))
    if isinstance(name, str):
        return name
    return ""


def _property(node: dict, key: str) -> Any:
    """从节点 properties 数组里查指定属性值;缺失返回 None。

    CDP 把 level / focusable / checked 等都放 properties 数组里,
    每项形如 ``{"name": "level", "value": {"value": 1}}``。
    """
    for prop in node.get("properties", []) or []:
        if not isinstance(prop, dict):
            continue
        if prop.get("name") == key:
            val = prop.get("value")
            if isinstance(val, dict):
                return val.get("value")
            return val
    return None


def _value_of(node: dict) -> Any:
    """节点的当前值(输入框文本等)。CDP 直接放在顶层 "value" 字段。"""
    val = node.get("value")
    if isinstance(val, dict):
        return val.get("value")
    return val


def _checked_of(node: dict) -> str | None:
    """复选框/单选状态:"true"/"false"/"mixed"。"""
    # CDP 把 checked 放顶层,也可能放 properties 里。
    checked = node.get("checked")
    if isinstance(checked, dict):
        return str(checked.get("value"))
    if isinstance(checked, str):
        return checked
    return _property(node, "checked")  # 可能返回 bool,下面统一转字符串


def _quote(name: str) -> str:
    """把 name 转成带引号的字符串;空 name 省略引号部分。"""
    text = name.strip()
    if not text:
        return ""
    # 转义换行,避免快照里出现跨行节点破坏缩进语义。
    text = text.replace("\n", " ").replace("\r", "")
    return f'"{text}"'


def _format_node(
    node: dict,
    nodes_by_id: dict[str, dict],
    depth: int,
    ref_counter: list[int],
    visited: set[str],
) -> list[str]:
    """递归格式化单棵子树,返回若干行文本。

    ``nodes_by_id`` 把扁平节点列表按 nodeId 索引,便于按 childIds 取子节点。
    ``visited`` 防御性避免循环引用导致无限递归。
    """
    node_id = str(node.get("nodeId", ""))
    if node_id in visited:
        return []
    visited.add(node_id)

    role = _role_of(node)
    name = _quote(_name_of(node))
    parts: list[str] = [role]
    if name:
        parts.append(name)

    extras: list[str] = []
    # ref 不只是展示编号，后续操作还需要用 backendDOMNodeId 找回元素。
    # 没有该 ID 的 AX 节点保留文本展示，但不发放无法执行的 ref。
    if role in INTERACTIVE_ROLES and node.get("backendDOMNodeId") is not None:
        ref_counter[0] += 1
        extras.append(f"ref=e{ref_counter[0]}")
    elif role == "heading":
        level = _property(node, "level")
        if level is not None:
            extras.append(f"level={level}")

    value = _value_of(node)
    if value is not None and str(value) != "":
        extras.append(f"value={str(value)!r}")

    checked = _checked_of(node)
    if checked is True:
        checked = "true"
    elif checked is False:
        checked = "false"
    if checked in ("true", "false", "mixed"):
        extras.append(f"checked={checked}")

    if extras:
        parts.append(f"[{', '.join(extras)}]")

    indent = "  " * depth
    lines: list[str] = [f"{indent}{' '.join(parts)}"]

    for child_id in node.get("childIds", []) or []:
        child = nodes_by_id.get(str(child_id))
        if child is None:
            continue
        lines.extend(
            _format_node(child, nodes_by_id, depth + 1, ref_counter, visited)
        )
    return lines


def format_snapshot(cdp_result: dict | None) -> str:
    """把 CDP ``Accessibility.getFullAXTree`` 结果转成文本。

    ``cdp_result`` 为 ``None`` 或不含 ``nodes`` 时返回空串。
    每次调用 ref 都从 e1 重新计数。

    入口是 nodes 里第一个 role 为 ``rootWebArea`` 的节点(找不到时退回
    nodeId=="0" 的节点,再找不到就退回 nodes[0])。
    """
    if not cdp_result or not cdp_result.get("nodes"):
        return ""

    nodes = cdp_result["nodes"]
    nodes_by_id: dict[str, dict] = {
        str(n.get("nodeId", "")): n for n in nodes if isinstance(n, dict)
    }

    # 选根节点:优先 rootWebArea,其次 nodeId=="0",最后第一个节点。
    root = None
    for n in nodes:
        if _role_of(n) == "rootWebArea":
            root = n
            break
    if root is None:
        root = nodes_by_id.get("0") or (nodes[0] if nodes else None)
    if root is None:
        return ""

    ref_counter = [0]
    lines = _format_node(root, nodes_by_id, depth=0, ref_counter=ref_counter, visited=set())
    return "\n".join(lines)
