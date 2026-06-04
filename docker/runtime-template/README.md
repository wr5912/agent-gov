# Runtime Template

本目录保存可复用的 Agent Runtime 初始配置模板，用于从零部署时填充运行态目录。

模板只保存结构、说明和安全默认值。真实环境里的 API key、token、Authorization header、数据库凭据、MCP 地址、IP、端口、URL、邮箱、账号、本机路径和 Claude 本地状态都不能进入模板。

## 使用方式

- 初始化运行态目录：`make runtime-bootstrap`
- 从当前运行态保存模板：`make runtime-template-export`
- 查看模板备份：`make runtime-template-restore-list`
- 恢复模板备份：`make runtime-template-restore BACKUP=<backup-file>`

`runtime-bootstrap` 默认只补齐缺失文件，不覆盖已有本地配置。真实部署值应写入 `docker/.env`、部署环境变量或不提交的本地覆盖文件。

## 占位符

模板中的 `${...}` 是部署占位符，例如 `${MCP_SERVER_URL}`、`${SOC_API_URL}`、`${API_TOKEN}`、`${SERVICE_HOST}`、`${SERVICE_PORT}`。部署时按环境注入，不要把真实值提交回模板。

## 安全规则

保存模板会先进入 staging 目录，执行脱敏和校验，通过后才替换本目录。无法判断是否安全的内容会阻断导出，正式模板保持不变。
