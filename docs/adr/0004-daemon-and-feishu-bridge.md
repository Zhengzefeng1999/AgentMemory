# ADR-0004: 轻量飞书桥与本地 Daemon 合流

- 日期：2026-08-11
- 状态：已接受

## 背景

性能实测：`add` 全流程 1306ms，其中 SQLite 首次 `commit()` 占 1127ms（Windows 上叠加文件系统/杀软实时扫描）。v1 的 CLI 每次调用都 spawn 独立 Python 进程，导致 vibe coding 中"运行完指令后 memory 调用停顿 ~1.3s"。

同时，"随时随地能记"的移动入口需求（借鉴 cc-connect 的 IM 写入模式）需要一个常驻服务承载飞书 Webhook 回调。

## 决策

引入**本地守护进程（daemon）**：常驻 HTTP 服务（默认 :8123），一个进程三用：

1. **快速写入**：SKILL 指令从"spawn CLI"改为调本地 HTTP（~10ms，进程内 SQLite 首次 commit 只付一次）。
2. **PRELOAD 生成**：定时重建预热索引。
3. **飞书桥**：`bridge_feishu.py` 作为可选组件（`--with-bridge`），复用 daemon 进程接收飞书消息（记:/查:）。

CLI 保留作离线兜底；daemon 不启动时一切功能照常。

## 备选方案

- 纯 CLI（现状）：每次 spawn 付 1.1s 首次提交成本。
- 异步 fire-and-forget：进程被杀丢数据；PRELOAD/飞书桥仍需另起服务。
- 复用 cc-connect：重量级依赖（把整个 agent 桥到 IM），与现有 pi-web+Feishu 集成重复建设，为"记/查"两条命令引入整条依赖链。

## 后果

- 热路径写入从 ~1.3s 降至 ~10ms。
- 一个常驻进程 = 写入 + 预热 + 飞书桥，架构更简单。
- HTTP 接口对任意 agent（pi/Claude Code/CodeBuddy）是通用入口。
- 风险：daemon 需管理生命周期（提供 daemon 启动/停止命令）；桥为可选组件，不部署不影响核心。
