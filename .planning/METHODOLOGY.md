# GSD Methodology

本文件是本仓库的 GSD 项目级方法论入口。后续长程重构、阶段规划和恢复工作时，应优先读取本文，并结合 `docs/engineering/长程重构质量闭环.md` 与 `docs/engineering/GSD长程重构阶段清单.md`。

## Boundary-First Refactor Lens

**Diagnoses:** 重构一开始没有分清 records、store、response schema、OpenAPI、frontend types、UI 和持久化数据边界，导致局部实现通过但跨层漂移。

**Recommends:** 在 PLAN 之前先列边界表。每个边界必须有单一真相来源、允许开放 JSON 的字段清单、删除/迁移/保留策略和验证方式。

**Apply when:** 阶段涉及 Pydantic 化、`JsonObject`、store 投影、API schema、前端类型生成、历史 SQLite payload 或 Docker volume 数据。

## Projection-vs-Persistence Lens

**Diagnoses:** 把持久化 row record、运行时投影 record 和 API response 当成同一种模型，导致生命周期约束误伤历史快照，或 response schema 被迫放宽。

**Recommends:** 持久化 record 只表达数据库 row 的完整不变量；投影 record 表达可嵌入、可能历史不完整的快照；response schema 表达公开 API 契约。三者可以共享 base 或字段类型，但不能因为字段相似就默认继承。

**Apply when:** 批次、任务、agent job、eval run、regression plan、external governance 等对象被嵌入到其他 payload 中。

## Real-Data Verification Lens

**Diagnoses:** 临时测试数据通过，但真实 `${HOME}/volume-agent-gov/data` 或迁移前 `docker/volume/data` 中的历史数据、旧 job 快照或旧 payload 触发 API 500 或前端请求失败。

**Recommends:** 涉及持久化投影或历史 schema 的阶段，必须用真实数据验证列表、详情和 UI。浏览器 smoke 要记录 console error、failed request 和 4xx/5xx API 响应计数。

**Apply when:** 修改 `from_row()`、`to_payload()`、store list/detail、response model、OpenAPI、前端工作台或任何读取历史 payload 的路径。

## Hard-Gate Conversion Lens

**Diagnoses:** 问题只写进总结或口头规则，下一阶段仍可能重复。

**Recommends:** 阶段结束时分类沉淀：静态可识别的进治理脚本，行为可验证的进测试或 smoke，只能人工判断的进 `.planning/METHODOLOGY.md`、`.codex/guidance` 或工程 checklist。

**Apply when:** 同一类问题出现第二次，或发现治理硬门未覆盖但风险可复发。

## Release-After-Verify Lens

**Diagnoses:** 提交、tag 或推送早于真实验证，导致发布后才发现生成物、前端页面或历史数据问题。

**Recommends:** release 只在 Verify 和 End-of-Phase Audit 完成后执行。提交前确认生成物、ignored runtime artifacts、版本文件、tag 冲突和远端引用。

**Apply when:** 用户要求提交、推送、创建新版本，或阶段已经接近收尾。
