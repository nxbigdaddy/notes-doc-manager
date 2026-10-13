# 贡献指南

感谢参与本项目。

## 提交规范

本项目使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：

```text
<type>(<scope>): <subject>

<body>
```

常用 type：`feat` 新功能、`fix` 修复、`docs` 文档、`style` 格式、`refactor` 重构、`test` 测试、`chore` 构建与杂项。

示例：

```text
feat(api): 支持批量查询接口
fix(login): 修复令牌过期后未刷新
docs: 补充部署说明
```

## 分支与流程

1. 从 `main` 创建分支：`feat/xxx`、`fix/xxx`。
2. 提交前本地自测，确保不引入密钥、真实数据与内网地址。
3. 发起 Pull Request，说明变更动机、影响范围与验证方式。

## 禁止提交

`*.env`、私钥与证书、数据库导出与备份、真实业务数据、客户信息、内网地址与账号口令。
