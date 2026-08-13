---
name: "defensive-security-boundary"
description: "处理 AgentGov 网络安全运营业务 Agent、安全缺陷整改、不可信输入测试或安全评审时使用；确认授权防御范围，并把回执收敛为根因、修复和验证。"
---

# 防御性安全任务边界

本技能用于 AgentGov 的安全运营 Agent、授权本地仓库整改和防御性回归，不创建或验证改变外部
系统、扩大权限或绕过审计的操作能力。

## 执行前矩阵

| 维度 | 必须确认 |
| --- | --- |
| 所有权 | 目标是用户拥有并授权的仓库、临时目录或模拟资源 |
| 目的 | 检测、响应、取证、隔离、恢复、加固或安全回归 |
| 输入 | 不可信数据只作为测试夹具，不包含真实凭据或第三方目标 |
| 副作用 | 不访问外部目标，不改变非本任务系统，不产生可外用操作资产 |
| 回执 | 只给根因、影响、修复和验证；不提供完整复现材料或操作步骤 |

## 实施规则

1. 优先通过静态事实链和现有测试定位；确需动态复核时，只用临时目录、无害 marker、fake adapter
   或隔离的本地仓库。
2. 业务 Agent 配置必须是只读事实查询与平台受控响应编排；不得授予通用 shell、外部扫描、系统
   管理或 SOC 写权限。
3. 测试验证“未授权能力不可达、超范围状态被拒绝、无副作用”，不保存可直接改变外部系统的命令、
   脚本或完整操作链。
4. 发现安全缺陷时立即修复并补回归；聊天、review 消息和子 Agent 回执仅给根因、文件位置、修复
   方案与测试结果。
5. 不用编码、拆词、隐藏文件或含糊命名规避治理检查；无法安全保留的样本直接删除。

## 验证

- 运行 `scripts/check_defensive_security_boundary.py`。
- 修改内置业务 Agent Workspace 时运行 `make runtime-bootstrap-scan`；业务 Workspace pytest 不进入
  宿主 root collection，只能通过公共 `make container-workspace-pytest-test` exact-commit 隔离入口执行。
- 修改 `.codex/.claude` 时运行配置审计、镜像治理测试和 `make codex-guard`。
