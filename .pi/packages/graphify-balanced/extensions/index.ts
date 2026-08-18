import { createHash } from "node:crypto";
import {
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readSync,
  realpathSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, extname, join, relative, resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export const MAX_GRAPH_BYTES = 10 * 1024 * 1024;
export const MAX_SOURCE_BYTES = 2 * 1024 * 1024;
export const MAX_DIGEST_BYTES = 64 * 1024 * 1024;
export const MAX_DIGEST_FILES = 10_000;
export const LOOKUP_DEBOUNCE_MS = 2_000;
export const GRAPHIFY_VERSION = "0.9.46";

const CODE_EXTENSIONS = new Set([
  ".bash", ".c", ".cc", ".cjs", ".cpp", ".cxx", ".fish", ".go", ".h", ".hh",
  ".hpp", ".java", ".js", ".jsx", ".kt", ".kts", ".mjs", ".php",
  ".py", ".rb", ".rs", ".scala", ".sh", ".swift", ".toml", ".ts", ".tsx",
  ".yaml", ".yml", ".zsh",
]);
const GENERIC_QUERY_TERMS = new Set([
  "architecture", "arquitectura", "component", "components", "componente", "componentes",
  "dependency", "dependencies", "dependencia", "dependencias", "flow", "flujo", "module",
  "modules", "modulo", "modulos", "overview", "resumen", "summary", "system", "sistema",
]);
const MAX_LOOKUP_NODES = 18;
const MAX_LOOKUP_EDGES = 24;
const DIGEST_FILE = ".pi-source-digest.json";
const RELATIONS = new Set([
  "calls", "contains", "defines", "dynamic_import", "extends", "imports", "imports_from",
  "indirect_call", "inherits", "method", "rationale_for", "re_exports", "references", "uses",
]);

type GraphRecord = Record<string, unknown>;
type RefreshState = {
  digest?: string;
  checkedAt: number;
  pending?: Promise<RefreshResult>;
};
type RefreshResult = { ok: boolean; updated: boolean; reason?: "digest" | "graph" | "refresh" };
type LookupNode = { id: string; label: string; path: string; location?: string };
type LookupEdge = { source: string; target: string; relation: string };

const refreshStates = new Map<string, RefreshState>();

function isRecord(value: unknown): value is GraphRecord {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function inside(root: string, path: string): boolean {
  const fromRoot = relative(root, path);
  return fromRoot !== "" && !fromRoot.startsWith("..") && !fromRoot.startsWith("/");
}

function readGraph(cwd: string): GraphRecord | undefined {
  const root = realpathSync(cwd);
  const graphPath = realpathSync(join(root, "graphify-out", "graph.json"));
  if (!inside(root, graphPath)) return;

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
    ) return;

    const graph: unknown = JSON.parse(buffer.subarray(0, bytesRead).toString("utf8"));
    const allowedKeys = new Set([
      "built_at_commit", "directed", "graph", "hyperedges", "links", "multigraph", "nodes",
    ]);
    if (
      !isRecord(graph) ||
      Object.keys(graph).some((key) => !allowedKeys.has(key)) ||
      typeof graph.built_at_commit !== "string" ||
      !/^[0-9a-f]{40}$/i.test(graph.built_at_commit) ||
      typeof graph.directed !== "boolean" ||
      typeof graph.multigraph !== "boolean" ||
      !isRecord(graph.graph) ||
      !Array.isArray(graph.nodes) || !graph.nodes.every(isRecord) ||
      !Array.isArray(graph.links) || !graph.links.every(isRecord) ||
      !Array.isArray(graph.hyperedges) || !graph.hyperedges.every(isRecord)
    ) return;
    return graph;
  } finally {
    closeSync(fd);
  }
}

function relevantSource(path: string): boolean {
  if (!path || path.startsWith(".pi/") || path.startsWith("graphify-out/")) return false;
  return CODE_EXTENSIONS.has(extname(path).toLowerCase());
}

export async function computeSourceDigest(
  pi: Pick<ExtensionAPI, "exec">,
  cwd: string,
  signal?: AbortSignal,
): Promise<string | undefined> {
  const result = await pi.exec("git", ["ls-files", "-co", "--exclude-standard", "-z"], {
    cwd,
    signal,
    timeout: 15_000,
  });
  if (result.code !== 0) return;

  const root = realpathSync(cwd);
  const paths = result.stdout.split("\0").filter(relevantSource).sort();
  if (paths.length > MAX_DIGEST_FILES) return;

  const hash = createHash("sha256");
  let totalBytes = 0;
  for (const path of paths) {
    const absolute = resolve(root, path);
    if (!inside(root, absolute)) return;
    const stat = lstatSync(absolute);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > MAX_SOURCE_BYTES) return;
    totalBytes += stat.size;
    if (totalBytes > MAX_DIGEST_BYTES) return;
    const content = readFileSync(absolute);
    hash.update(String(Buffer.byteLength(path))).update(":").update(path);
    hash.update(String(content.length)).update(":").update(content);
  }
  return hash.digest("hex");
}

function digestPath(cwd: string): string {
  return join(cwd, "graphify-out", DIGEST_FILE);
}

function readSavedDigest(cwd: string): string | undefined {
  try {
    const value: unknown = JSON.parse(readFileSync(digestPath(cwd), "utf8"));
    if (isRecord(value) && typeof value.digest === "string" && /^[0-9a-f]{64}$/.test(value.digest)) {
      return value.digest;
    }
  } catch {
    return;
  }
}

function saveDigest(cwd: string, digest: string): void {
  const path = digestPath(cwd);
  mkdirSync(dirname(path), { recursive: true });
  const temporary = `${path}.${process.pid}.tmp`;
  writeFileSync(temporary, `${JSON.stringify({ version: 1, digest })}\n`, { mode: 0o600 });
  renameSync(temporary, path);
}

export function resolveGraphifyBinary(cwd: string, home = homedir()): string | undefined {
  try {
    const root = realpathSync(join(home, ".local", "share", "uv", "tools", "graphifyy"));
    const binary = realpathSync(join(root, "bin", "graphify"));
    const stat = lstatSync(binary);
    if (!stat.isFile() || !inside(root, binary) || inside(realpathSync(cwd), binary)) return;
    return binary;
  } catch {
    return;
  }
}

type BinaryResolver = (cwd: string) => string | undefined;

async function refreshGraph(
  pi: Pick<ExtensionAPI, "exec">,
  cwd: string,
  signal: AbortSignal | undefined,
  digest: string,
  resolveBinary: BinaryResolver,
): Promise<RefreshResult> {
  const graphifyBinary = resolveBinary(cwd);
  if (!graphifyBinary) return { ok: false, updated: false, reason: "refresh" };

  const isolatedHome = join(cwd, "graphify-out", ".pi-home");
  mkdirSync(isolatedHome, { recursive: true });
  const environment = [
    "-i",
    `HOME=${isolatedHome}`,
    `TMPDIR=${process.env.TMPDIR ?? "/tmp"}`,
    "GRAPHIFY_QUERY_LOG_DISABLE=1",
  ];
  const version = await pi.exec("/usr/bin/env", [...environment, graphifyBinary, "--version"], {
    cwd,
    signal,
    timeout: 15_000,
  });
  if (version.code !== 0 || !new RegExp(`^graphify\\s+${GRAPHIFY_VERSION.replaceAll(".", "\\.")}\\s*$`, "m").test(version.stdout)) {
    return { ok: false, updated: false, reason: "refresh" };
  }
  const updateOnce = () => pi.exec(
    "/usr/bin/env",
    [...environment, graphifyBinary, "update", "."],
    { cwd, signal, timeout: 120_000 },
  );
  const firstUpdate = await updateOnce();
  if (firstUpdate.code !== 0) return { ok: false, updated: false, reason: "refresh" };

  let stableDigest = await computeSourceDigest(pi, cwd, signal);
  if (!stableDigest) return { ok: false, updated: false, reason: "digest" };
  if (stableDigest !== digest) {
    const secondUpdate = await updateOnce();
    if (secondUpdate.code !== 0) return { ok: false, updated: false, reason: "refresh" };
    const afterSecondUpdate = await computeSourceDigest(pi, cwd, signal);
    if (!afterSecondUpdate || afterSecondUpdate !== stableDigest) {
      return { ok: false, updated: false, reason: "digest" };
    }
    stableDigest = afterSecondUpdate;
  }

  saveDigest(cwd, stableDigest);
  return { ok: true, updated: true };
}

async function ensureFresh(
  pi: Pick<ExtensionAPI, "exec">,
  cwd: string,
  resolveBinary: BinaryResolver,
  signal?: AbortSignal,
): Promise<RefreshResult> {
  const state = refreshStates.get(cwd) ?? { checkedAt: 0 };
  refreshStates.set(cwd, state);
  if (state.pending) return state.pending;
  if (Date.now() - state.checkedAt < LOOKUP_DEBOUNCE_MS && state.digest) {
    return { ok: true, updated: false };
  }

  state.pending = (async () => {
    const digest = await computeSourceDigest(pi, cwd, signal);
    state.checkedAt = Date.now();
    if (!digest) return { ok: false, updated: false, reason: "digest" } as RefreshResult;

    const saved = state.digest ?? readSavedDigest(cwd);
    let graphExists = false;
    try { graphExists = Boolean(readGraph(cwd)); } catch { graphExists = false; }
    if (saved === digest && graphExists) {
      state.digest = digest;
      return { ok: true, updated: false } as RefreshResult;
    }

    const refreshed = await refreshGraph(pi, cwd, signal, digest, resolveBinary);
    if (refreshed.ok) state.digest = (await computeSourceDigest(pi, cwd, signal)) ?? digest;
    return refreshed;
  })();

  try {
    return await state.pending;
  } finally {
    state.pending = undefined;
  }
}

function normalized(value: string): string {
  return value.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
}

function safeId(value: unknown): string {
  return typeof value === "string" && /^[A-Za-z0-9_.$:/@+-]{1,200}$/.test(value) ? value : "";
}

function safeLabel(value: unknown): string {
  return typeof value === "string" && /^\.?[A-Za-z_$][A-Za-z0-9_$.]*(?:\(\))?$/.test(value)
    ? value
    : "";
}

function safePath(value: unknown): string {
  if (
    typeof value !== "string" ||
    value.length > 240 ||
    value.startsWith("/") ||
    !/^[A-Za-z0-9_@+.-]+(?:\/[A-Za-z0-9_@+.-]+)*$/.test(value) ||
    value.split("/").includes("..") ||
    !relevantSource(value)
  ) return "";
  return value;
}

function safeLocation(value: unknown): string {
  return typeof value === "string" && /^L\d+(?:-L\d+)?$/.test(value) ? value : "";
}

function safeRelation(value: unknown): string {
  return typeof value === "string" && RELATIONS.has(value) ? value : "";
}

function lookupGraph(graph: GraphRecord, question: string): { nodes: LookupNode[]; edges: LookupEdge[] } {
  const rawNodes = graph.nodes as GraphRecord[];
  const rawLinks = graph.links as GraphRecord[];
  const tokens = normalized(question).match(/[a-z0-9_.$/@:-]{2,}/g) ?? [];
  const specific = tokens.filter((token) => !GENERIC_QUERY_TERMS.has(token));
  const degree = new Map<string, number>();
  for (const edge of rawLinks) {
    if (typeof edge.source !== "string" || typeof edge.target !== "string") continue;
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  }

  const ranked = rawNodes
    .filter((node) => typeof node.id === "string" && typeof node.label === "string")
    .map((node) => {
      const id = node.id as string;
      const fields = [node.label, node.norm_label, node.source_file, id]
        .filter((value): value is string => typeof value === "string")
        .map(normalized);
      let score = 0;
      for (const token of specific) {
        for (const field of fields) {
          if (field === token) score += 12;
          else if (field.startsWith(token)) score += 6;
          else if (field.includes(token)) score += 2;
        }
      }
      if (specific.length === 0) score = degree.get(id) ?? 0;
      return { node, id, score, degree: degree.get(id) ?? 0 };
    })
    .filter((entry) => entry.score > 0)
    .sort((a, b) => b.score - a.score || b.degree - a.degree)
    .slice(0, 6);

  const selected = new Map(ranked.map((entry) => [entry.id, entry.node]));
  const candidateEdges = rawLinks
    .filter((edge) => typeof edge.source === "string" && typeof edge.target === "string")
    .filter((edge) => selected.has(edge.source as string) || selected.has(edge.target as string))
    .slice(0, MAX_LOOKUP_EDGES);
  const byId = new Map(
    rawNodes.filter((node) => typeof node.id === "string").map((node) => [node.id as string, node]),
  );
  for (const edge of candidateEdges) {
    for (const id of [edge.source as string, edge.target as string]) {
      if (selected.size >= MAX_LOOKUP_NODES) break;
      const node = byId.get(id);
      if (node) selected.set(id, node);
    }
  }

  const nodes = [...selected.values()].map((node) => ({
    id: safeId(node.id),
    label: safeLabel(node.label),
    path: safePath(node.source_file),
    ...(safeLocation(node.source_location) ? { location: safeLocation(node.source_location) } : {}),
  })).filter((node) => node.id && node.label && node.path);
  const allowedIds = new Set(nodes.map((node) => node.id));
  const edges = candidateEdges.map((edge) => ({
    source: safeId(edge.source),
    target: safeId(edge.target),
    relation: safeRelation(edge.relation),
  })).filter((edge) => allowedIds.has(edge.source) && allowedIds.has(edge.target) && edge.relation);
  return { nodes, edges };
}

export function buildGraphifyContext(cwd: string): string | undefined {
  try {
    const graph = readGraph(cwd);
    if (!graph) return;
    const nodes = graph.nodes as GraphRecord[];
    const links = graph.links as GraphRecord[];
    const hyperedges = graph.hyperedges as GraphRecord[];
    return [
      "<graphify-context>",
      `Validated local AST graph: ${nodes.length} nodes, ${links.length} links, ${hyperedges.length} hyperedges.`,
      "For architecture, dependency, caller, path, flow, or impact questions, call graphify_lookup automatically before broad source searches.",
      "If lookup cannot answer, continue immediately with ffgrep/read; never ask the user to run Graphify commands.",
      "</graphify-context>",
    ].join("\n");
  } catch {
    return;
  }
}

export function registerGraphifyExtension(
  pi: ExtensionAPI,
  resolveBinary: BinaryResolver = resolveGraphifyBinary,
) {
  pi.registerTool({
    name: "graphify_lookup",
    label: "Graphify Lookup",
    description: "Automatically refresh the local AST graph when source content changed, then return a bounded one-hop neighborhood of sanitized code identifiers and paths.",
    promptSnippet: "Inspect the local code graph automatically for architecture, dependency, caller, flow, path, or impact questions",
    promptGuidelines: [
      "Use graphify_lookup automatically before broad grep/read exploration for architecture, dependencies, callers, flows, paths, modules, or change impact.",
      "Pass likely code identifiers, filenames, or module terms to graphify_lookup; it is symbol/path lookup, not natural-language explanation.",
      "If graphify_lookup reports no match or fallback, continue immediately with ffgrep/read without asking the user to run Graphify commands.",
    ],
    parameters: {
      type: "object",
      properties: {
        query: {
          type: "string",
          minLength: 1,
          maxLength: 200,
          description: "Code identifier, filename, module, or architecture term inferred from the user's request.",
        },
      },
      required: ["query"],
      additionalProperties: false,
    } as never,
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      const freshness = await ensureFresh(pi, ctx.cwd, resolveBinary, signal);
      if (!freshness.ok) {
        return {
          content: [{ type: "text", text: "Graphify automatic refresh was unavailable. Continue now with ffgrep/read; do not ask the user to run Graphify commands." }],
          details: { fallback: true, reason: freshness.reason },
        };
      }
      try {
        const graph = readGraph(ctx.cwd);
        if (!graph) throw new Error("graph unavailable");
        const result = lookupGraph(graph, params.query);
        if (result.nodes.length === 0) {
          return {
            content: [{ type: "text", text: "No matching graph identifiers were found. Continue now with ffgrep/read; do not ask the user to run Graphify commands." }],
            details: { fallback: true, refreshed: freshness.updated },
          };
        }
        return {
          content: [{ type: "text", text: `<graphify-data>\n${JSON.stringify(result)}\n</graphify-data>` }],
          details: { fallback: false, refreshed: freshness.updated, nodeCount: result.nodes.length, edgeCount: result.edges.length },
        };
      } catch {
        return {
          content: [{ type: "text", text: "Graphify lookup was unavailable. Continue now with ffgrep/read; do not ask the user to run Graphify commands." }],
          details: { fallback: true, reason: "graph" },
        };
      }
    },
  });

  pi.on("before_agent_start", (event, ctx) => {
    const graphContext = buildGraphifyContext(ctx.cwd);
    if (!graphContext) return;
    return { systemPrompt: `${event.systemPrompt}\n\n${graphContext}` };
  });
}

export default registerGraphifyExtension;
