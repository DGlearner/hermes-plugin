# 企业微信自建应用接入

员工入口：`https://zsk.freshpixxp.com/account/personal-token?tab=wecom`。
回调 URL：`https://zsk.freshpixxp.com/wecom/callback`。

当前实现使用 Hermes 原生 `wecom_callback` 自建应用适配器。企业微信智能机器人
长连接是另一个平台，本次没有配置或启用它。应用配置完成前，生成绑定码返回
“企业微信应用尚未配置”，不能把页面上线视为真实通道已接通。

## 管理员配置

1. 创建企业微信自建应用，将需要使用的员工加入应用可见范围。
2. 在 Hermes 的安全配置流程中配置 `WECOM_CALLBACK_CORP_ID`、
   `WECOM_CALLBACK_AGENT_ID`、`WECOM_CALLBACK_CORP_SECRET`、
   `WECOM_CALLBACK_TOKEN`、`WECOM_CALLBACK_ENCODING_AES_KEY`，并启用
   `wecom_callback` 平台。Secret、Token、AESKey 不进入网页、聊天或 Git。
3. 根配置加入 `wecom_binding: {enabled: true, corp_id: <实际 CorpID>, relay_url: "http://100.97.84.9:8790"}`。
   这是非凭据配置；CorpID 必须与原生适配器配置一致。目前只支持一个绑定企业。
4. 原生回调监听保持 `127.0.0.1:8645/wecom/callback`。ECS 将受限大小的
   加密请求经已有私有 Provisioner 认证通道转发到本地，无需新增 Mac 公网端口。
5. 重启 Hermes，在企业微信后台保存上述公网回调 URL，使用相同 Token/AESKey
   完成 URL 验证。企业可信 IP 配置为 ECS 固定出口 `8.163.39.85`，不再使用 Mac
   所在网络的出口 IP。没有完成该步骤前不得宣称主动提醒已验证。

## 员工绑定

登录知识库帐号设置，进入企业微信绑定，生成并复制 `绑定 <一次性码>`。
在企业微信工作台的自建应用中发送，再返回帐号设置页面完成绑定。
绑定码有效期 5 分钟，只保存与 CorpID 绑定的哈希。必须由验签解密后的
应用私聊消息确认，且页面轮询必须属于生成码的登录员工。

企业微信成员身份以 `CorpID:UserID` 保存；禁止一个成员绑定两个员工。
已有员工 Profile 复用自身 PAT，新员工只在企业微信确认后签发 PAT。
绑定码不进入模型、普通会话日志或工具调用。解绑先删除路由，随后撤销配对授权。
ECS 后台清理停用员工的企业微信绑定；微信解绑时保留仍被企业微信使用的 PAT。

绑定成功自动同步该 Profile 的巡检目标，与微信/TG 共用独立的已读记录和调度。
支持 `已读 R编号` 单项确认、`已读`/`全部已读` 全部确认。
企业微信巡检通过自建应用主动消息 API 发送，不启动第二个回调监听器。
使用 Markdown 消息，按 4000 UTF-8 字节预算打包完整事项。单篇日报超长时，
按段落或完整 Unicode 字符分段，保留全部正文；每段含相同的已读编号和部分序号，
标题、编号、已读提示均计入字节预算。任一段投递结果不确定时，整批该通道仍记为不确定。
同一员工同时绑定其他通道时，沿用现有共用分页机制，各通道收到相同的日报分段；
未绑定企业微信的员工仍使用原有格式。任务、文件审核事项保持整项打包，单事项超过预算时
仍可能被企业微信拒绝；当前没有大事项附件降级。

## ECS 发布

生产候选及发布文件在 `/opt/releases/wecom-binding-20260929`，基于上线前镜像
`rag-mcp-app:daily-reports-7e3b2d0`，仅覆盖绑定服务、页面和路由三个文件。
同时更新 `/app/src` 和实际导入的 site-packages，避免仅更新未使用源码。

正式服务使用以下命令（不读取或编辑 `.env` 内容）：

```sh
cd /opt/xxp-rag-mcp
docker compose -f compose.production.yaml -f compose.wecom-binding.yaml up -d --no-deps --no-build app
```

回滚可保留全部状态，仅指定先前镜像替换 app：

```sh
RAG_MCP_APP_IMAGE=rag-mcp-app:daily-reports-7e3b2d0 docker compose -f compose.production.yaml up -d --no-deps --no-build app
```

本次没有数据库迁移。源码集成已同步到本地 `xxp-rag-mcp`，不要用其其他
尚未发布的工作区改动覆盖本次生产基线。Mac 使用挂载的 `hermes-plugin`，
不要覆盖为 `xxp-rag-mcp/integrations/hermes` 中较旧的副本。

## 固定 ECS 出口（2026-09-29）

运行镜像升级为 `rag-mcp-app:wecom-egress-20260929`，候选源码、测试和原 Compose
备份位于 `/opt/releases/wecom-egress-20260929`。现有 `compose.wecom-binding.yaml`
继续作为上线入口，同时仅在 Tailscale 地址 `100.97.84.9:8790` 发布应用端口。

Mac 的 `wecom_binding.relay_url` 指向该私网 origin。`wecom_egress.py` 将原生
适配器的令牌刷新和文本回复，以及巡检的 Markdown 发送，全部导向两个受限接口：
`/internal/hermes/wecom-api/gettoken`、`/internal/hermes/wecom-api/send`。
不允许任意 URL、其他企业微信 API、群发或 `@all`。中继失败不回退到 Mac 直连。
Mac→ECS 与 ECS→企业微信的客户端均忽略 HTTP_PROXY 等环境变量。

认证复用已有私有 Provisioner 的服务凭据，应用 Secret/令牌只在 Tailscale 加密
传输及进程内使用，不写入新文件、日志或发布包。企业微信上游 URL 的日志会脱敏。
公网 Caddy 带转发头的请求被拒绝，另外校验请求来源和服务认证。
Docker 当前把私网映射端口的来源转换为网关 `172.20.0.1`，因此 Compose 明确
配置 `HERMES_WECOM_RELAY_DOCKER_GATEWAY=172.20.0.1`。仅信任这个单一地址，
不放开 Docker 子网；其他容器地址被拒绝。Docker 网络变更后需重新核对该值。

应用表单再次加载会保留 `relay_url`。模板、员工绑定、已读记录无需重新创建。
ECS 公网 IP 保持不变时，Mac 更换网络不需要修改企业微信可信 IP；Mac 仍需在线，
且能经 Tailscale 连通 ECS。ECS 公网地址变更则需要更新可信 IP。

回滚出口版本时，先停用企业微信发送以免回到不期望的 Mac 出口；恢复
`rollback-compose.yaml` 中的原镜像和上一个版本的 Mac 插件，再显式决定出口策略。
