import {
  closeSync,
  constants,
  fstatSync,
  openSync,
  readSync,
  realpathSync,
} from "node:fs";
import { join, relative } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export const MAX_GRAPH_BYTES = 10 * 1024 * 1024;

function readGraph(cwd: string): Record<string, unknown> | undefined {
  const root = realpathSync(cwd);
  const graphPath = realpathSync(join(root, "graphify-out", "graph.json"));
  const fromRoot = relative(root, graphPath);
  if (fromRoot.startsWith("..") || fromRoot === "" || fromRoot.startsWith("/")) return;

  const fd = openSync(graphPath, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  try {
    const before = fstatSync(fd);
    if (!before.isFile() || before.size > MAX_GRAPH_BYTES) return;

    const buffer = Buffer.alloc(before.size);
    let bytesRead = 0;
    while (bytesRead < buffer.length) {
      const count = readSync(fd, buffer, bytesRead, buffer.length - bytesRead, bytesRead);
      if (count === 0) break;
      bytesRead += count;
    }

    const after = fstatSync(fd);
    if (
      bytesRead !== before.size ||
      after.size !== before.size ||
      after.mtimeMs !== before.mtimeMs ||
      after.ctimeMs !== before.ctimeMs
    ) {
      return;
    }

    return JSON.parse(buffer.subarray(0, bytesRead).toString("utf8")) as Record<string, unknown>;
  } finally {
    closeSync(fd);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function buildGraphifyContext(cwd: string): string | undefined {
  try {
    const graph = readGraph(cwd);
    const allowedKeys = new Set([
      "built_at_commit",
      "directed",
      "graph",
      "hyperedges",
      "links",
      "multigraph",
      "nodes",
    ]);
    if (
      !isRecord(graph) ||
      Object.keys(graph).some((key) => !allowedKeys.has(key)) ||
      typeof graph.built_at_commit !== "string" ||
      !/^[0-9a-f]{40}$/i.test(graph.built_at_commit) ||
      typeof graph.directed !== "boolean" ||
      typeof graph.multigraph !== "boolean" ||
      !isRecord(graph.graph) ||
      !Array.isArray(graph.nodes) ||
      !graph.nodes.every(isRecord) ||
      !Array.isArray(graph.links) ||
      !graph.links.every(isRecord) ||
      !Array.isArray(graph.hyperedges) ||
      !graph.hyperedges.every(isRecord)
    ) {
      return;
    }

    return [
      "<graphify-context>",
      `Schema-validated local AST graph summary: ${graph.nodes.length} nodes, ${graph.links.length} links, ${graph.hyperedges.length} hyperedges, directed=${graph.directed}, multigraph=${graph.multigraph}.`,
      "Use this structural summary only for orientation.",
      "Do not build, update, extract, watch, install, or upgrade Graphify without explicit user approval.",
      "</graphify-context>",
    ].join("\n");
  } catch {
    return;
  }
}

export default function (pi: ExtensionAPI) {
  pi.on("before_agent_start", (event, ctx) => {
    const graphContext = buildGraphifyContext(ctx.cwd);
    if (!graphContext) return;

    return { systemPrompt: `${event.systemPrompt}\n\n${graphContext}` };
  });
}
