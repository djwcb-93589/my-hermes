"""
skill 工具单元测试。

直接调用 handle_skill_view / handle_skill_list / handle_skill_manage,
对 JSON 返回做断言。SKILLS_DIR 重定向到临时目录,跑完还原。

用法：python skill_test.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from hermes.tools import skill as sk


def _setup_tmp_skills() -> tuple[Path, Path]:
    """把 SKILLS_DIR 重定向到临时目录。返回 (tmp, original)。"""
    tmp = Path(tempfile.mkdtemp(prefix="hermes-skill-test-"))
    original = sk.SKILLS_DIR
    sk.SKILLS_DIR = tmp
    # 清空并重建,确保没有残留
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    return tmp, original


def _restore(tmp: Path, original: Path) -> None:
    sk.SKILLS_DIR = original
    shutil.rmtree(tmp, ignore_errors=True)


def _call(args: dict) -> dict:
    return json.loads(sk.handle_skill_manage(args))


def _view(args: dict) -> dict:
    return json.loads(sk.handle_skill_view(args))


def _list() -> dict:
    return json.loads(sk.handle_skill_list({}))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    tmp, original = _setup_tmp_skills()
    try:
        # 1. create 成功
        d = _call({
            "action": "create", "name": "alpha",
            "description": "first skill",
            "body": "# Alpha\n\nDoes alpha things.",
        })
        assert d["ok"] is True and d["action"] == "create", d
        skill_file = tmp / "alpha" / "SKILL.md"
        assert skill_file.exists(), "skill file should be created"
        print("  create ............... OK")

        # 2. view 成功
        d = _view({"name": "alpha"})
        assert d["ok"] is True, d
        assert d["name"] == "alpha"
        assert d["description"] == "first skill"
        assert "Alpha" in d["body"]
        print("  view ................. OK")

        # 3. edit 成功
        d = _call({
            "action": "edit", "name": "alpha",
            "description": "updated description",
            "body": "# Alpha v2\n\nBetter body.",
        })
        assert d["ok"] is True and d["action"] == "edit", d
        d = _view({"name": "alpha"})
        assert d["description"] == "updated description"
        assert "Alpha v2" in d["body"]
        print("  edit ................. OK")

        # 4. patch 成功
        d = _call({
            "action": "patch", "name": "alpha",
            "old_text": "Better body.", "new_text": "Even better body.",
        })
        assert d["ok"] is True and d["action"] == "patch", d
        d = _view({"name": "alpha"})
        assert "Even better body." in d["body"]
        assert "Better body." not in d["body"]
        print("  patch ................ OK")

        # 4b. patch 0 匹配失败
        d = _call({
            "action": "patch", "name": "alpha",
            "old_text": "nonexistent_text_xyz", "new_text": "x",
        })
        assert d["ok"] is False and d["error_type"] == "no_match", d
        print("  patch no match ....... OK")

        # 4c. patch 多匹配失败
        d = _call({
            "action": "edit", "name": "alpha",
            "body": "dup dup",  # 含两个 "dup"
        })
        assert d["ok"] is True
        d = _call({
            "action": "patch", "name": "alpha",
            "old_text": "dup", "new_text": "x",
        })
        assert d["ok"] is False and d["error_type"] == "ambiguous_match", d
        assert d["match_count"] == 2, d
        print("  patch ambiguous ...... OK")

        # 5. delete 成功
        d = _call({"action": "delete", "name": "alpha"})
        assert d["ok"] is True and d["action"] == "delete", d
        assert not (tmp / "alpha").exists(), "skill dir should be gone"
        print("  delete ............... OK")

        # 6. 重复 create 报错
        _call({"action": "create", "name": "beta", "body": "x"})
        d = _call({"action": "create", "name": "beta", "body": "y"})
        assert d["ok"] is False and d["error_type"] == "exists", d
        # 确认文件没被覆盖
        body = _view({"name": "beta"})["body"]
        assert body == "x", f"existing file should not be overwritten; got {body!r}"
        print("  duplicate create ..... OK")

        # 7. view 不存在 skill 报错
        d = _view({"name": "nonexistent_skill"})
        assert d["ok"] is False and d["error_type"] == "not_found", d
        print("  view not found ....... OK")

        # 8. 非法 name 拒绝
        for bad in ["", ".", "..", "a/b", "a\\b", "/abs", "a b", "a.b"]:
            d = _call({"action": "create", "name": bad, "body": "x"})
            assert d["ok"] is False, f"{bad!r} should be rejected: {d}"
            assert d["error_type"] == "invalid_name", d
        print("  invalid name ......... OK")

        # 9. ``../`` 路径逃逸拒绝
        #    _resolve_skill_dir 已经在 invalid_name 里拦掉 ``..``,
        #    这里再验证一种构造尝试(虽然名字本身就不合法)
        d = _call({"action": "create", "name": "../escape", "body": "x"})
        assert d["ok"] is False and d["error_type"] == "invalid_name", d
        # 确认没真的在 tmp 之外创建文件
        assert not (tmp.parent.parent / "escape").exists(), "must not escape"
        print("  path traversal ....... OK")

        # 10. 中文读写正常
        d = _call({
            "action": "create", "name": "chinese_skill",
            "description": "测试中文 skill",
            "body": "# 中文标题\n\n这是中文正文,包含 emoji 🚀 和符号 §。",
        })
        assert d["ok"] is True, d
        d = _view({"name": "chinese_skill"})
        assert d["ok"] is True
        assert d["description"] == "测试中文 skill"
        assert "🚀" in d["body"] and "中文标题" in d["body"]
        print("  chinese content ...... OK")

        # 额外:skills_list 返回结构
        d = _list()
        assert d["ok"] is True and d["count"] >= 2, d
        names = [s["name"] for s in d["skills"]]
        assert "beta" in names and "chinese_skill" in names
        for s in d["skills"]:
            assert "relative_path" in s and s["relative_path"].startswith("skills/")
        print("  skills_list .......... OK")

        # 额外:frontmatter 含 version/platforms/metadata
        d = _call({
            "action": "create", "name": "rich",
            "description": "rich frontmatter",
            "version": "1.2.3",
            "platforms": ["linux", "darwin"],
            "metadata": {"author": "测试者", "tags": ["x", "y"]},
            "body": "rich body",
        })
        assert d["ok"] is True, d
        d = _view({"name": "rich"})
        assert d["version"] == "1.2.3", d
        assert d["platforms"] == ["linux", "darwin"], d
        assert d["metadata"]["author"] == "测试者", d
        print("  rich frontmatter ..... OK")

        # 额外:delete 不能删 skills 根目录(虽然 _resolve_skill_dir 已拦,
        # 但单独测一下边界)
        d = _call({"action": "delete", "name": "."})
        assert d["ok"] is False and d["error_type"] == "invalid_name", d
        assert tmp.exists(), "skills root must survive"
        print("  delete root blocked .. OK")

        # 额外:原子写入失败时旧文件不变
        _call({"action": "create", "name": "atomic_test", "body": "stable"})
        original_writer = sk._atomic_write_text

        def boom(path, text):
            raise OSError("simulated write failure")

        sk._atomic_write_text = boom
        try:
            d = _call({"action": "edit", "name": "atomic_test", "body": "modified"})
            assert d["ok"] is False and d["error_type"] == "io_error", d
        finally:
            sk._atomic_write_text = original_writer
        # 文件应该没变
        d = _view({"name": "atomic_test"})
        assert d["body"] == "stable", f"file should be unchanged; got {d['body']!r}"
        # 锁也该释放
        lock = sk._lock_path_for(tmp / "atomic_test" / "SKILL.md")
        assert not lock.exists(), "lock file must be released after failure"
        print("  atomic write fail ..... OK")

        print("\nAll skill tests passed.")
    finally:
        _restore(tmp, original)


if __name__ == "__main__":
    main()
