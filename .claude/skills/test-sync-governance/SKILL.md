---
name: "test-sync-governance"
description: "迭代功能（新增/修改/删除行为、改契约、重构、删模块）时，决定测试该增、该改还是该删，避免欠测、陈测和脆测。用户提到测试要不要补、测试是否过时、测试太多/冗余、删了功能测试怎么办、重构后测试碎了时使用。只做测试增删改的判断工作流，不重复覆盖率门。"
---

# 测试同步治理

> 本技能与 `.codex/skills/test-sync-governance/SKILL.md` 同源镜像，修改需两侧同步。

本技能用于功能迭代时同步治理测试，避免三类债：

- 欠测：新行为没有测试或没有 GAP（由 `tests/quality_policy.json` + `scripts/check_test_quality_policy.py` 守住，本技能不重复）。
- 陈测：测试还在验证已删除或已改变的旧行为（机械信号见 `scripts/check_orphan_tests.py`，语义判断靠本技能）。
- 脆测：测实现细节、重复覆盖，重构时成片碎裂——这是「测试太多」的真实来源。

数量不是治理目标，同步才是。先填同步矩阵，再动测试。

测试资产统一使用 KEEP、PROMOTE、DEMOTE、REFACTOR、MERGE、QUARANTINE、DELETE-CANDIDATE 和 GAP。隔离必须有 owner、issue、到期日与修复方案；删除候选至少需要两类证据，不能只凭运行慢、长期未失败或覆盖率贡献低。

## 测试同步矩阵

对本次改动涉及的每个行为，先判定处置，再落测试：

| 行为变更 | 命中的旧测试 | 处置 | 新增测试 | 深度要求 |
| --- | --- | --- | --- | --- |
| 新增能力 | 无 | — | 必加 | 正常 + 边界 + 失败态 |
| 改契约/接口 | 有 | 改（同步断言/快照） | 视新增分支 | 契约/字段所有权同步 |
| 改内部实现（行为不变） | 有 | 原则不动 | 一般不加 | 若旧测试断言实现细节 → 改为断言行为 |
| 删行为/删模块 | 有 | 删 | — | 删后无悬挂引用（跑孤儿检测） |
| docs 归档/重命名/权威替换 | 有 | 改或删旧文档契约 | 视新权威入口 | `tests/test_documentation_contracts.py` 不再读取旧路径，改断言新权威和归档索引 |

矩阵任一格说不清，先补清楚再写测试。

## 删测判定（识别陈测）

满足任一即应删除或重写，不留「常绿但无意义」的测试：

- 被测行为、分支或模块已删除。
- 被测活跃文档已归档、重命名或被新权威文档替代。
- `scripts/check_orphan_tests.py` 报告该测试 import 了已删除的 `app`/`scripts` 符号或模块。
- 测试只为覆盖率存在，断言的是不再可达的路径。
- 同一行为已有更高层测试完整覆盖，此用例纯属重复。

删测和删功能在同一次改动里完成，不要留到「以后清理」。

## 避免脆测（控制冗余）

- 测行为和契约，不测实现细节（不断言私有方法调用顺序、内部数据结构形状、日志原文）。
- 不为 trivial getter/setter、纯转发、纯常量写独立用例。
- 同一规则只在一处建立权威断言，其他用例引用而非复制。
- 优先在合适层级测（store/服务/契约），不要同一逻辑在多层重复全量断言。

## 深度要求（引用，不重复）

测试深度的硬性要求以 `.codex/guidance/verify.md`（Claude 侧 `.claude/rules/verify.md`）为准：生命周期状态变更加非法转移用例、并发资源加竞态/部分失败用例、外部输入加异常/敌意/越权用例、AI 自动化输出契约加后端所有权字段污染用例。本技能只负责「增删改判断」，深度清单不在此复制。

## 测试选择前置判断

先按改动行为和验收目标选择测试深度，不要在配置、README、docs 或 skill 镜像同步这类低运行时风险改动中默认跑全量；也不要把局部治理通过当成主流程或发版证明。涉及 UI 设计一致性时，测试必须覆盖语义负向断言，例如「会话」抽屉不混入运行设置、「运行设置」抽屉不混入会话历史、旧配置入口不存在，而不是只验证元素出现或宽度达标。

TIA 和 xdist 在满足 `tests/quality_policy.json` 的配对样本与时间窗前只作为 shadow 证据。未知改动必须回退全量；局部选择结果不能替代提交前或发布前的完整回归。

## 收尾验证

改动类型先映射到推荐验证命令，不默认跑全量，也不把局部测试当成发版证明：

| 改动类型 | 推荐验证命令 | 升级条件 |
| --- | --- | --- |
| `.codex` / `.claude` skill、README、docs 容器治理 | `git diff --check`、`scripts/check_docs_governance.py`、`scripts/check_stage_language.py`、`scripts/check_codex_governance.py --mode fail`、相关 skill/governance 单测 | 用户要求完整验证、发版、或改动影响运行时代码 |
| docs 归档、删除、重命名或权威替换 | `tests/test_documentation_contracts.py`、`scripts/check_orphan_tests.py`、`scripts/check_docs_governance.py` | 提交/发版时追加 `make test` |
| runtime/env、Docker、模型凭据边界 | settings/env policy/documentation 相关 pytest；真实 live 改动追加 `make container-live-test` | 影响 Agent job 主流程时跑 `make main-flow-test` |
| 产品主流程、Agent job、formatter、store、API/UI 状态 | `make main-flow-test` + 相关 pytest | 提交/发版时跑 `make test` |
| 四阶段改进治理 UI 设计一致性、抽屉/modal 语义、Playground 动作边界 | `pnpm --dir frontend run verify:design-parity`，必要时追加真实容器 `RUNTIME_UI_BASE` 验收 | 改动真实前端组件时同时跑 `pnpm --dir frontend build` |
| 前端可见行为 | `pnpm --dir frontend build`，必要时浏览器 smoke | 改 OpenAPI/类型时先生成并检查漂移 |
| 仅版本面或发版元数据 | 版本引用检索、Compose config、前端 build 或相关 smoke | 创建 tag 前确认分支/tag 远端校验 |

```bash
# 局部
.venv/bin/python -m pytest -q tests/test_xxx.py::test_xxx
.venv/bin/python scripts/check_orphan_tests.py
# 改了主流程
make main-flow-test
# 校验资产分类与绑定
.venv/bin/python scripts/check_test_quality_policy.py --manifest-only
# 提交/发版
make test
```

- 改动主流程、Agent job、formatter、store 投影、API response 或用户可见 tab 状态时，同步更新 `tests/quality_policy.json` 的 nodeid、owner、capabilities、resource classes 和 lane 绑定。
- 归档或删除 docs 时，同步更新文档契约测试：旧路径只能作为“不存在/已归档”的字面量检查，不能继续 `_read_repo_text` 读取为活跃权威。
- 删除测试后跑 `scripts/check_orphan_tests.py` 与 `scripts/check_codex_governance.py --mode fail`，确认无孤儿引用与回归。
