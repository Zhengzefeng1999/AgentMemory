#!/usr/bin/env node
// AgentMemory 飞书桥启动器 —— 精确复刻 pi-feishu-lark startDaemon 的 spawn 方式
// 关键：stdin 保持 pipe（daemon 模式依赖）、PI_FEISHU_DAEMON=1、PI_PACKAGES=""
const { spawn } = require("node:child_process");
const { openSync } = require("node:fs");

const nodeExe = process.execPath;
const cliPath = "C:/Users/Lenovo/AppData/Roaming/npm/node_modules/@earendil-works/pi-coding-agent/dist/cli.js";
const extPath = "C:/Users/Lenovo/.pi/agent/npm/node_modules/pi-feishu-lark/.pi/extensions/feishu/index.ts";
const logPath = "C:/Users/Lenovo/.pi/agent/feishu/daemon.log";

const logFd = openSync(logPath, "a");
const env = { ...process.env, PI_FEISHU_DAEMON: "1", PI_PACKAGES: "" };
const args = [
  "--mode", "rpc",
  "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files", "--no-builtin-tools",
  "-e", extPath,
];

console.log("[launcher] spawning feishu daemon:", cliPath, args.join(" "));
const child = spawn(nodeExe, [cliPath, ...args], {
  cwd: process.cwd(),
  env,
  stdio: ["pipe", logFd, logFd],  // stdin pipe 保持打开（关键）
  windowsHide: true,
});
child.on("error", (err) => console.error("[launcher] spawn error:", err.message));
child.on("exit", (code, sig) => {
  console.log(`[launcher] daemon exited: code=${code} sig=${sig}`);
  process.exit(0);
});
child.unref();
console.log("[launcher] daemon pid:", child.pid);
// 保持进程存活：stdin pipe 必须保持打开，否则 rpc daemon 收到 EOF 退出
console.log("[launcher] keeping stdin open (daemon alive)...");
setInterval(() => {}, 1 << 30);
