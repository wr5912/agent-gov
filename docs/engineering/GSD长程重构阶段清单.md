# GSD 长程重构阶段清单

本清单用于规划和执行跨 records、store、API、OpenAPI、前端类型、UI 或持久化数据的长程整改阶段。它配合 `.planning/METHODOLOGY.md` 使用。

## 适用触发

- Pydantic 化、`JsonObject` / `dict[...]` 边界治理。
- store 投影、response schema、OpenAPI 或前端生成类型收口。
- 涉及 `${HOME}/volume-agent-runtime` 默认数据、`docker/volume` 历史数据、SQLite payload、agent job 快照或旧 API 契约。
- 用户要求“继续收口直到完全做完”“完成以上所有建议”“提高代码质量”。

## Phase SPEC 必填

- [ ] 写明本阶段要删除、迁移或统一的旧设计。
- [ ] 写明不做什么，避免阶段膨胀。
- [ ] 写明公开契约是否允许变化：API path、OpenAPI schema、前端生成类型、配置/env、Docker 路径、持久化数据。
- [ ] 写明真实数据样本来源，尤其是 `${HOME}/volume-agent-runtime/data`、迁移前 `docker/volume/data` 与历史 SQLite。
- [ ] 写明本阶段要新增或收紧哪些治理硬门。

## Phase PLAN 必填

- [ ] 边界表：records、store、response schema、OpenAPI、frontend types、UI、docs、tests。
- [ ] 单一真相来源：每个实体只能有一个主模型或明确派生关系。
- [ ] 开放 JSON 清单：只允许真实开放边界，例如 `raw_output_json`、`metadata`、`request_json`。
- [ ] 删除/迁移/保留清单：保留项必须有期限或清理条件。
- [ ] 验证矩阵：治理、后端、OpenAPI、前端类型、前端 build、浏览器 smoke、真实历史数据。
- [ ] 回滚或失败处理：涉及 DB + 文件系统、外部通知或 agent job 时必须说明部分失败如何处理。

## Execute 必查

- [ ] 不让投影 record 继承持久化 row record，除非生命周期和字段约束完全一致。
- [ ] 不新增公共 store 方法返回裸 `JsonObject`。
- [ ] 不新增旧目录中的中性类型，例如把通用 JSON 类型放回 `records`。
- [ ] 不新增前端手写字段覆盖真实 OpenAPI schema。
- [ ] 不在事务块内执行不可回滚副作用。
- [ ] 不用“兼容旧数据”掩盖 schema 双轨；兼容只能作为迁移边界，并写清退出条件。

## Verify 必跑或说明

| 验证 | 默认命令或动作 |
| --- | --- |
| 治理硬门 | `.venv/bin/python scripts/check_codex_governance.py --mode fail` |
| 后端全量 | `make test` |
| OpenAPI 导出 | `.venv/bin/python scripts/export_openapi.py` |
| 前端类型 | `pnpm --dir frontend generate:api-types` |
| 前端构建 | `pnpm --dir frontend build` |
| 浏览器 smoke | `RUNTIME_UI_BASE=... RUNTIME_API_BASE=... pnpm --dir frontend verify:feedback-browser` |
| 真实历史数据 | 使用 `${HOME}/volume-agent-runtime/data` 或迁移前 `docker/volume/data` 中现有数据访问列表、详情和关键投影 |

如果某项没有运行，阶段总结必须写明原因、剩余风险和下次恢复路径。不能用“应该没问题”替代。

## End-of-Phase Audit

阶段完成前必须回答：

- [ ] 是否还有新增或扩大的 `JsonObject` / `dict[...]` 边界？
- [ ] 是否还有同一实体多套模型靠人工同步？
- [ ] 是否有旧 facade、shim、旧路径或不可达 UI 仍参与主流程？
- [ ] 是否有 OpenAPI 或前端生成类型未刷新？
- [ ] 是否用真实历史数据验证过投影和 UI？
- [ ] 是否把新发现的重复问题转成测试、治理硬门或本清单条目？

## Release Gate

只有在 Verify 和 End-of-Phase Audit 都完成后，才能进入提交、tag、推送。创建新版本时必须确认：

- [ ] 版本文件同步。
- [ ] tag 不存在本地或远端冲突。
- [ ] 分支和 tag 推送后用远端引用校验。
