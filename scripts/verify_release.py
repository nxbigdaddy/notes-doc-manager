"""Verify a copied frozen distribution without project/venv import paths.

This does not substitute for a clean Windows 11 machine. No user documents or
real editors are opened; the application's self-tests create isolated fixtures.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from notes import __version__

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_inventory import generate_inventory, verify_inventory  # noqa: E402


def write_user_guide(package: Path, version: str):
    """The standalone package must not link to absent repository-only documents."""
    (package / "README.md").write_text(
        f"""# Notes {version} · 本地文档管理器（开发预览版）

## 启动

双击本目录中的 **Notes.exe**。复制到其他位置时，请复制整个 Notes 文件夹，
不要只复制 exe。若旧版本窗口仍在运行，先正常关闭后再启动新版。

## 首次使用

1. 添加需要管理的本地文件夹，不会搬动文件，也不会自动扫描全盘。
2. 在设置中选择可信的 Notepad3.exe。双击文档会请求外部编辑器打开。
3. 单击只读预览；通过文件名、正文、标签、收藏和最近打开查找文档。
4. 缺失文件可以手动重新定位到同一库内的新路径，只改变管理关联。
   吸收无人工信息的自动登记占位需要额外确认；有人为信息的目标不合并。
5. 应用运行、身份基线已建立且收到完整新鲜事件时，可自动跟踪同库直接单文件
   改名/移动，保留标签收藏；程序本身不移动文件。跨库、关闭期间、目录移动、
   链式冲突、硬链接或事件不完整时不自动归并，保留缺失记录供手动确认。
6. 管理数据库损坏时，启动恢复界面可选择JSON或自动SQLite快照。
   明确确认后才替换管理信息；故障原件保留，不修改正文。恢复中断时请使用本版
   再次启动，不要先用旧版本或其他工具改写管理数据库。
7. Ctrl+P打开快速检索小窗，按文件名查找、上下选择、Enter打开；空白输入查看
   最近打开，最多显示20项，不继承主列表筛选。Esc关闭并取消尚未执行的打开请求。
   工具菜单可选启用Ctrl+Alt+N全局快捷键，默认关闭；冲突不会抢占他人注册。
   仅在程序运行时有效，不自动启动或驻留，退出释放，备份恢复不自动激活。
8. Ctrl+N新建时可选空白、会议纪要、每日记录、问题排查模板，并查看只读预览。
   日期按实际创建时间生成，不使用旧预览；扩展名尊重文档库规则。
   同名文件不覆盖；创建后写入失败保留文件并提示，不自动重试、删除或打开编辑器。
   模板只创建新文件，不改变已有正文，不执行脚本或读取外部模板。
9. 工具菜单可保存、编辑、删除智能列表，组合搜索词、文档库/文件夹、多标签、
   收藏和最近打开等条件；左侧导航直接访问。失效引用不会静默移除或扩大范围。
   删除列表不删除文档。管理备份包含列表；恢复旧备份会用空列表替换当前列表。
   首次成功保存前自动保护备份，数据库随后升级为schema2，请使用0.8.0或更新的
   兼容版本；0.7及更老版本拒绝打开，新format2备份也不能交给旧版恢复。
10. 普通JSON恢复绑定已检查的备份和当前管理信息，确认后两者改变会拒绝操作，
    必须重新检查确认，不会自动重试。若管理信息已经恢复、仅索引或后续刷新失败，
    会明确提示已恢复，此时请处理索引或刷新，不要重复恢复管理信息。
11. 索引短暂被占用时，本次扫描会提示未完成；占用解除后点击刷新重试即可。
    健康索引不会因一次锁竞争被禁用，新内容完成写入前仍计为待处理。

## 数据与备份

- 默认管理信息保存于 `%LOCALAPPDATA%\\Notes`，不是本程序目录。
- 文档正文仍是普通文件；本程序不提供物理文件移动或删除功能。
- 元数据自动备份按 UTC 自然日、有变化才生成，保留最近 7 份自建自动快照。
  恢复前保护快照不自动轮换；界面可查看状态或提前备份。
- **元数据备份不包含正文**。正文仍需用户自己的文件备份方案。
- 正文搜索缓存包含可读文本，不是加密存储；程序不自动上传内容。
- 升级搜索缓存格式后首次启动会重新建立索引，标签、收藏等管理信息保留。
  旧版本缓存不自动删除，以避免旧可执行文件使用不兼容结构。

## 使用边界

管理器只读取已保存内容，不读取 Notepad3 中未保存的文字。
预览默认最多 256 KiB；超过 5 MiB 的文件默认仅参加文件名搜索。
中文一两个字的正文查询走字面回退，请留意覆盖范围、取消和未完成提示。

这是开发预览版。Windows 11 干净机器、签名及完整真实编辑器交互尚未正式验收。
开发机源码冒烟曾出现一次wxbase原生退出；已修复确证的窗口定时器清理遗漏，
但原始异常的唯一根因尚未证明，不能视为原生崩溃问题全部解决。
本次构建版本、源码哈希和实际验证边界记录在同目录 **BUILD-INFO.json**。
本版包含项目核验的SQLite修复版本；请使用完整新包，不替换为旧DLL或旧版程序。
依赖原文和原生文件证据位于 **third-party**，其中明确列出尚未覆盖的许可核验项；
技术清单不等于法律审核、完整SBOM或商业分发批准。
""",
        encoding="utf-8",
    )


def main():
    root = Path(__file__).resolve().parents[1]
    package = root / "dist" / __version__ / "Notes"
    artifacts = root / ".artifacts"
    artifacts.mkdir(exist_ok=True)
    records = artifacts / "release-records" / __version__
    records.mkdir(parents=True, exist_ok=True)
    inventory_directory = package / "third-party"
    if not inventory_directory.exists():
        generate_inventory(root, package, inventory_directory, allow_package_output=True)
    inventory = verify_inventory(package)
    inventory_path = inventory_directory / "release-inventory.json"
    inventory_digest = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
    previous_manifest = package / "BUILD-INFO.json"
    if previous_manifest.exists():
        previous = json.loads(previous_manifest.read_text(encoding="utf-8"))
        recorded = previous.get("third_party", {}).get("inventory_sha256")
        assert recorded == inventory_digest, "Previously verified third-party evidence changed"
    check = artifacts / "release-check" / __version__ / uuid.uuid4().hex
    check.mkdir(parents=True, exist_ok=False)
    # A fresh target every run prevents old DLLs/modules masking an incomplete package.
    # Owned project artifacts only; no deletion or changes to installed software.
    copied = check / "Notes"
    shutil.copytree(package, copied)
    verify_inventory(copied)
    env = dict(os.environ)
    for key in list(env):
        if key.upper().startswith("PYTHON") or key.upper() in ("VIRTUAL_ENV", "_OLD_VIRTUAL_PATH"):
            env.pop(key, None)
    windows = Path(env.get("SystemRoot", r"C:\Windows"))
    env["PATH"] = os.pathsep.join((str(windows / "System32"), str(windows)))
    reports = {}
    for name, flag in (
        ("selftest", "--self-test"), ("gui-smoke", "--gui-smoke"),
        ("quick-smoke", "--quick-smoke"),
    ):
        report = records / f"frozen-isolated-{name}.json"
        result = subprocess.run(
            [str(copied / "Notes.exe"), flag, "--report", str(report)],
            cwd=check,
            env=env,
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if result.returncode:
            raise RuntimeError(f"Frozen {name} failed with exit code {result.returncode}")
        data = json.loads(report.read_text(encoding="utf-8"))
        assert data["frozen"] and data["fts5_trigram"] and data["wx_stc"]
        assert data["sqlite_wal_reset_fixed"], "Do not publish an affected SQLite WAL runtime"
        assert data["application"] == __version__, "The executable version is stale"
        provenance = data["build_provenance"]
        assert provenance["version"] == __version__
        current = {
            str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root / "src").rglob("*.py"))
        }
        assert provenance["source_sha256"] == current, "Source changed since this binary was built"
        sqlite_dlls = list(copied.rglob("sqlite3.dll"))
        assert len(sqlite_dlls) == 1, "Unexpected SQLite runtime distribution"
        assert hashlib.sha256(sqlite_dlls[0].read_bytes()).hexdigest() == (
            provenance["private_runtime"]["sqlite-runtime.json"]["dll_sha256"]
        ), "Frozen SQLite DLL differs from the pinned official artifact"
        if name == "selftest":
            assert all(data["self_test"].values())
        else:
            assert data["gui_smoke"]
            if name == "quick-smoke":
                assert data["quick_smoke"]
        reports[name] = data
        print(f"PASS copied distribution: {name}", flush=True)

    manifest = {
        "application": f"Notes {__version__} development preview",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "host": sys.platform,
        "verification_boundary": (
            "Existing Windows 10 host, copied package, isolated working directory and PATH. "
            "Not a clean Windows 11 machine, not a signing/license audit."
        ),
        "reports": reports,
        "executable_sha256": hashlib.sha256((package / "Notes.exe").read_bytes()).hexdigest(),
        "source_sha256": {
            str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((root / "src").rglob("*.py"))
        },
        "locks_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.glob("requirements-*.lock.txt")
        },
        "bundled_license_file_inventory": [
            str(path.relative_to(package)).replace("\\", "/")
            for path in package.rglob("*")
            if path.is_file() and ("licen" in path.name.lower() or "copying" in path.name.lower())
        ],
        "third_party": {
            "inventory_path": "third-party/release-inventory.json",
            "inventory_sha256": inventory_digest,
            "summary": inventory["summary"],
            "pending": inventory["pending"],
            "boundary": "Technical evidence only; not a completed legal/licensing audit or SBOM.",
        },
    }
    (package / "BUILD-INFO.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (artifacts / "release-verification.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (records / "release-verification.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_user_guide(package, __version__)
    print("Build manifest and copied-package verification recorded.")


if __name__ == "__main__":
    main()
