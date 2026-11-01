# 安全与威胁模型

这是一个**本机 owner 专用**的空间。默认假设：宿主和局域网可信，公网不可信。

## 1. 信任边界

| 层 | 机制 |
|---|---|
| 浏览器入口 | nginx Basic Auth + 控制台 owner Cookie（HMAC 签名，12 小时） |
| 控制台 | 只监听 `127.0.0.1:19991`；CSRF/Origin/Host 校验 |
| 任务入口 | `task_authority.py` 只接受本机 owner 任务，外部内容标记 `data_only` |
| 模型 | `runtime-policy.mjs` 白名单；四档权限；预算/暂停/设备互斥/观察门 |
| 桌面 | Bearer token + `HER_ALLOWED_IP`；固定动作 API，无宿主目录 |
| 手机 | Bearer token + iptables 只允许空间宿主；固定动作 API，无任意 shell |
| QQ 委托 | `data/ext.key` Bearer；只读/高风险限制；不经过 owner Cookie |

## 2. 密钥与文件

| 文件 | 生成方式 | 权限 |
|---|---|---|
| `config.json` | 从 `config.example.json` 复制 | 600 |
| `data/ext.key` | 首次启动 `secrets.token_urlsafe(32)` | 600 |
| `data/bridge.key` | 首次启动 | 600 |
| `data/owner-http.key` | 首次启动 | 600 |
| `data/observer-http.key` | 首次启动 | 600 |
| `data/her-agent.token` | 手动写入，与桌面容器一致 | 600 |
| `data/android.token` | 手动写入，与手机服务一致 | 600 |
| `data/schedule.json` | 控制台保存 | 600（含 SMTP 授权码） |
| `deploy/webui/htpasswd` | `htpasswd -c` | 600 |
| `deploy/webui/nginx.conf` | `render-nginx.sh` 生成 | 600（含代理签名） |

## 3. 网络

- 只开必需端口：`18081`、`18082` 在局域网；`19991` 只在宿主 loopback；`19990` 只允许空间宿主 IP；`19992` 只允许空间宿主 IP。
- 不要把 Docker 端口改成 `0.0.0.0` 公网映射。
- 需要公网访问：前置 VPN 或带 TLS 的反向代理；不要直接暴露 noVNC。
- 手机宿主的 `iptables` 规则会 DROP 其他来源；如果使用 ufw/firewalld，确认规则顺序。

## 4. 权限档

- `read-only`：最安全，只读空间工具。
- `workspace-write`：默认推荐，工作区写 + ask。
- `auto`：薄审查，只在不可恢复删除、凭据外发、操作他人设备、不可逆付款时拦。
- `danger-full-access`：完全权限，不分类、不询问、不拦截。**只在你完全信任模型和所有输入时使用。**

## 5. 已知风险

- 模型可能被网页/文件/聊天中的内容诱导；`AUTHORITY_RULES.md` 和任务信封降低风险，但不能消除。
- 桌面 `/exec` 是固定 argv 白名单，不是完整沙箱；容器逃逸风险由 Docker/内核承担。
- 手机 ADB 等于设备控制权；只允许空间宿主 IP，token 不要泄露。
- QQ 群里的普通成员可能要求查看设备；技能 prompt 限制为只读，但模型仍可能误判，建议只在可信群使用。
- `data/` 里有完整对话和记忆，备份时按敏感数据对待。

## 6. 上线检查表

- [ ] `config.json` 不在 Git，权限 600
- [ ] `data/` 不在 Git，权限 600
- [ ] `ext.key` / `bridge.key` / `owner-http.key` / `observer-http.key` 已生成
- [ ] `her-agent.token` 与桌面容器一致
- [ ] `android.token` 与手机服务一致
- [ ] nginx `htpasswd` 强密码
- [ ] `DSH_PROXY_SECRET` 与 nginx 头一致
- [ ] 19991/19990/19992 没有公网映射
- [ ] 手机 iptables 只放行空间宿主
- [ ] 控制台首次登录后改 `owner_hold`、人格、模型
- [ ] 备份 `data/` 和 `config.json`
