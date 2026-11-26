# AstrBot 核心补丁

跑在本地这套 QQ 环境上的 AstrBot 是动了源码的（`~/projects/astrbot`）。上游仓库不能推，
这里存的是把本地改动打回上游基线用的 patch。

- 基线：`v4.29.4-61-gd978ee29`（`AstrBotDevs/AstrBot` master）
- 生成方式：`git format-patch origin/master..HEAD -o docs/patches/astrbot-core`
- 应用顺序：按文件序号从小到大，`git apply` 或 `git am`

```bash
cd /path/to/AstrBot
git checkout d978ee29
git am /path/to/astrbot-plugin-fish/docs/patches/astrbot-core/*.patch
```

## 打的是什么

| 文件 | 改动 |
| --- | --- |
| 0001-fix-log.patch | 控制台日志整行写两遍：handler 里已经有 `logger.info`，结果格式化器又补了一遍，日志翻倍 |
| 0002-fix-aiocqhttp-event.role-ADMIN-QQ-False.patch | aiocqhttp 适配器从不给 `event.role` 赋值，`is_admin()` 恒 False，QQ 群的管理员指令全废。从 `sender.role` 补齐 |
| 0003-fix-10.patch | 插件抛异常时不再把原始 traceback 发到群里（只报插件名 + handler）；空参数指令树截到 10 行 |
| 0004-fix-stats-ADMIN-provider.patch | `/stats` 加 `PermissionType.ADMIN`，与 `/provider` 一致。token 用量属运维信息 |
| 0005-fix-handler.patch | 事件在进入钩子前已被停止时，不再把「停止」记到当前正在跑的 handler 头上 |

## 升级注意

升级 AstrBot 后这几个补丁要重打一遍，尤其是 0002 —— 没有它，QQ 群里所有管理员指令都是失效的，
`/provider`、`/set` 这类命令会直接回「权限不足」。打完重启，用管理员账号发一条管理指令确认。
