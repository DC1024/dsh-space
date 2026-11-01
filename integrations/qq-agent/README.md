# QQ Agent -> 空间委托技能

```bash
./install.sh
```

然后在 QQ Agent 控制台填：

- `baseUrl`: `http://127.0.0.1:19991`
- `token`: 空间 `data/ext.key`
- `persona`: `li`
- `waitSeconds`: 120

给技能授权 `net.outbound`，并在管理员附加规则里覆盖「你没有本地工具」的系统提示。详见 [docs/QQ-AGENT.md](../../docs/QQ-AGENT.md)。
