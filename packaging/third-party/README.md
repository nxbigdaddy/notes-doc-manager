# 第三方发行清单

`catalog.json` 只批准复制已检查的**特定哈希许可原文**，不是法律审核或整个发行包的分发许可结论。版本变化、文件缺失或哈希变化会停止自动复制，并列为待核对。

运行示例（现有发行目录只读）：

```powershell
.venv\Scripts\python.exe scripts\release_inventory.py `
  --project-root <workspace>\notes `
  --release-dir <workspace>\notes\dist\0.8.0\Notes `
  --output-dir <workspace>\notes\.artifacts\license-inventory-0.8
```

输出目录必须不存在，并且不能覆盖或位于发行包、虚拟环境、私有工具目录中。脚本不联网、不执行发行包、不修改运行时；读取本地声明、原文及二进制哈希。

构建流程可显式传 `--allow-package-output`，仅允许写入**没有 `BUILD-INFO.json` 的新包**下不存在的 `third-party/`。默认不会开启此权限，任何已经验证的归档包仍拒绝写入。

输出：

- `release-inventory.json`：组件、许可证证据层级、原文/元数据来源及哈希、全部 `.pyd/.dll/.exe`、可证明的同哈希来源、待办。
- `THIRD-PARTY-NOTICES.txt`：人类可读的技术清单及明确边界。
- `licenses/`：目录中批准且哈希吻合的本地原文副本，字节不变。

发行流程应在**新构建目录**上生成清单，再整体复制隔离验证；或先生成外部目录、由发行脚本将输出复制到新包的 `third-party/`。不要改写已经验证/归档的 `0.8.0`。将 JSON 哈希及未解决事项写入新构建报告。不要将“找到原文”“来源哈希匹配”或清单生成成功当成许可义务全部满足。
