/**
 * NaumiAgent analysis tools for the pi coding agent.
 *
 * Registers deterministic static-scan tools (chaos / scale / state) backed
 * by NaumiAgent's Python scanners. The scans never call a model — they
 * return hard evidence, and pi's own model does the reasoning on top.
 *
 * Load with:  pi -e pi_extensions/naumi-analysis.js
 * or persist via engine.pi.extra_args in the NaumiAgent config.
 *
 * The Python interpreter defaults to $NAUMI_PYTHON (set automatically by
 * NaumiAgent when it spawns pi) and falls back to `python3`.
 */

import { spawn } from "node:child_process";

const SCAN_TIMEOUT_MS = 120_000;

const MODES = [
  {
    tool: "naumi_chaos",
    mode: "chaos",
    description:
      "NaumiAgent 灾难演练静态扫描：对目标源码做确定性检查（裸 except、无超时/重试的外部调用、" +
      "硬编码地址、错误处理覆盖率），返回带行号证据的报告。不含模型推演，结论可复现。",
  },
  {
    tool: "naumi_scale",
    mode: "scale",
    description:
      "NaumiAgent 并发扩展静态扫描：检查目标源码在给定 QPS 下的同步阻塞点、串行瓶颈、" +
      "连接池/缓存缺失等确定性证据。默认按 1000 QPS 评估，可通过 qps 参数指定。",
  },
  {
    tool: "naumi_state",
    mode: "state",
    description:
      "NaumiAgent 云原生状态审查静态扫描：检查模块级可变全局状态、无持久化的内存缓存、" +
      "单实例假设等在分布式/重启场景下会失效的确定性证据。",
  },
];

function pythonExecutable() {
  return process.env.NAUMI_PYTHON || "python3";
}

function runScan(mode, params, signal) {
  const target = String(params.target || "").trim();
  const args = ["-m", "naumi_agent.analysis_scan", "--mode", mode, "--target", target];
  if (mode === "scale" && params.qps !== undefined) {
    args.push("--qps", String(Math.max(1, Math.floor(Number(params.qps) || 1000))));
  }
  return new Promise((resolve) => {
    if (!target) {
      resolve({ isError: true, content: [{ type: "text", text: "缺少 target 参数。" }] });
      return;
    }
    const child = spawn(pythonExecutable(), args, {
      cwd: process.cwd(),
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => child.kill("SIGKILL"), SCAN_TIMEOUT_MS);
    const onAbort = () => child.kill("SIGKILL");
    signal?.addEventListener?.("abort", onAbort);
    child.stdout.on("data", (chunk) => (stdout += chunk));
    child.stderr.on("data", (chunk) => (stderr += chunk));
    child.on("error", (error) => {
      clearTimeout(timer);
      signal?.removeEventListener?.("abort", onAbort);
      resolve({
        isError: true,
        content: [
          {
            type: "text",
            text:
              `无法启动 Python 扫描器（${pythonExecutable()}）：${error.message}。` +
              "请设置 NAUMI_PYTHON 指向已安装 naumi_agent 的解释器。",
          },
        ],
      });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      signal?.removeEventListener?.("abort", onAbort);
      if (code === 0) {
        resolve({ content: [{ type: "text", text: stdout }] });
        return;
      }
      const detail = (stderr || stdout || "").trim().slice(0, 800);
      resolve({
        isError: true,
        content: [
          {
            type: "text",
            text: `扫描失败（退出码 ${code}）：${detail || "无输出"}`,
          },
        ],
      });
    });
  });
}

export default function registerNaumiAnalysis(api) {
  for (const definition of MODES) {
    const parameters = {
      type: "object",
      properties: {
        target: {
          type: "string",
          description: "要扫描的文件或目录路径（相对当前工作区或绝对路径）",
        },
      },
      required: ["target"],
    };
    if (definition.mode === "scale") {
      parameters.properties.qps = {
        type: "integer",
        description: "目标 QPS（默认 1000）",
        minimum: 1,
      };
    }
    api.registerTool({
      name: definition.tool,
      description: definition.description,
      parameters,
      execute: async (toolCallId, params, signal) =>
        runScan(definition.mode, params || {}, signal),
    });
  }
}
