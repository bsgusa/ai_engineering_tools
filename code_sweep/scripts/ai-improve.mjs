#!/usr/bin/env node
/**
 * ai-improve.mjs — the LLM pass of the code_sweep engine.
 *
 * Two modes:
 *   scan (default)  Autonomous. Reads the repo within a hard byte budget, asks a
 *                   staff-engineer model for ranked findings and upstream
 *                   pattern opportunities, writes $SWEEP_OUT/ai-findings.md.
 *                   NEVER writes to the repository.
 *   build (--brief) Only runs when a human supplied a brief (workflow_dispatch
 *                   input). The model may rewrite a small allowlist of files;
 *                   every change is validated with sweep.sh (install+lint+typecheck
 *                   +test) before `changed=1` is emitted. On validation failure the
 *                   changes are reverted and status=rejected_validation.
 *
 * Safety rails (deliberate, do not loosen without an owner decision):
 *   - exactly ≤1 gateway call per run, hard output-token cap, byte budget
 *   - blocked write paths: .github/**, lockfiles, package.json, .env*, migrations
 *   - no dependency can be added by the model (lockfile mismatch would fail CI;
 *     findings say "needs dep X" instead)
 *   - upstream suggestions must cite a real source and may not paste copyleft code
 *   - status is ALWAYS written to $SWEEP_OUT/status.txt and GITHUB_OUTPUT —
 *     silent failure is the failure mode this engine exists to remove
 *
 * Exit codes: 0 = handled outcome (findings | applied | rejected | no_key | dry_run)
 *             1 = provider/API error (surfaced, job marked degraded)
 *             2 = usage error (bad invocation)
 *             3 = engine bug (unexpected exception)
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync, unlinkSync, statSync, appendFileSync } from "node:fs";
import { execSync, spawnSync } from "node:child_process";
import { dirname, join, resolve, relative, isAbsolute, sep } from "node:path";
import { fileURLToPath } from "node:url";

const ENGINE_DIR = dirname(dirname(fileURLToPath(import.meta.url))); // …/code_sweep
const CONFIG = JSON.parse(readFileSync(join(ENGINE_DIR, "config.json"), "utf8"));
const B = CONFIG.budget;

const OUT = process.env.SWEEP_OUT || "./sweep";
mkdirSync(OUT, { recursive: true });

const args = process.argv.slice(2);
function argOf(name) {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] !== undefined ? args[i + 1] : null;
}
const BRIEF = argOf("--brief");
const DRY_RUN = args.includes("--dry-run");
const MODE = BRIEF ? "build" : "scan";

const PRICES = {
  // USD per 1M tokens (input, output) — estimates only, shown for honesty
  "openai/gpt-4o-mini": [0.15, 0.6],
  "openai/gpt-5-mini": [0.25, 2.0],
  "google/gemini-2.5-flash": [0.3, 2.5],
};
const MODEL = process.env.AI_IMPROVE_MODEL || B.model;

// ---------------------------------------------------------------- status ----
function setStatus(status, detail = "") {
  writeFileSync(join(OUT, "status.txt"), `${status}${detail ? " — " + detail : ""}\n`);
  const ghOut = process.env.GITHUB_OUTPUT;
  if (ghOut) {
    appendFileSyncSafe(ghOut, `status=${status}\nchanged=${status === "applied_validated" ? 1 : 0}\n`);
  }
}
function appendFileSyncSafe(file, data) {
  try {
    appendFileSync(file, data);
  } catch {
    writeFileSync(file, data, { flag: "a" });
  }
}
function fail(code, msg) {
  console.error(`[ai-improve] ${msg}`);
  const md = join(OUT, "ai-findings.md");
  writeFileSync(md, `# ai-improve could not run\n\n${msg}\n`);
  setStatus(code === 1 ? "api_error" : "engine_error", msg.slice(0, 200));
  process.exit(code);
}

const runUrl =
  process.env.GITHUB_SERVER_URL && process.env.GITHUB_REPOSITORY && process.env.GITHUB_RUN_ID
    ? `${process.env.GITHUB_SERVER_URL}/${process.env.GITHUB_REPOSITORY}/actions/runs/${process.env.GITHUB_RUN_ID}`
    : "local run";

// ------------------------------------------------------- context builder ----
const SRC_EXT = /\.(js|jsx|ts|tsx|mjs|cjs|py|go|rs|java|rb|php|css|scss|html|vue|svelte|sh|md|ya?ml|json)$/i;
const SKIP_PATH = /(^|\/)(node_modules|dist|build|\.next|coverage|vendor|public|assets|static|Archive|.*_discarded.*|migrations)(\/|$)|\.min\.|package-lock\.json$|pnpm-lock\.yaml$|yarn\.lock$|\.(png|jpe?g|gif|svg|ico|woff2?|ttf|pdf|zip|mp4)$/i;
// any hidden directory (.bolt, .agents, .github, .cursor …) — vendored/generated, not product code
const HIDDEN_DIR = /(^|\/)\.[^\/]+\//;
const MD_OK = /(^|\/)(README|ROADMAP|AGENTS|CLAUDE|CONTRIBUTING|CHANGELOG|SECURITY|ACTION_PLAN)\.md$/i;
const CORE_DIR = /(^|\/)(src|app|lib|components|api|server|scripts|ops)(\/|$)/i;

function sh(cmd) {
  return execSync(cmd, { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 }).trim();
}

function collectFiles() {
  const tracked = sh("git ls-files").split("\n").filter(Boolean);
  const scored = [];
  for (const p of tracked) {
    if (SKIP_PATH.test(p)) continue;
    if (HIDDEN_DIR.test(p)) continue;
    const ext = p.slice(p.lastIndexOf("."));
    const isMd = /\.md$/i.test(p);
    if (!SRC_EXT.test(p)) continue;
    if (isMd && !MD_OK.test(p)) continue;
    let size;
    try {
      size = statSync(p).size;
    } catch {
      continue;
    }
    if (size > B.max_file_chars * 4) continue;
    let recency = 0;
    try {
      recency = Number(sh(`git log -1 --format=%ct -- "${p}"`)) || 0;
    } catch {
      recency = 0;
    }
    // product code outranks stray root files, regardless of commit date
    if (CORE_DIR.test(p)) recency += 90 * 86400;
    scored.push({ p, size, recency, ext });
  }
  // brief mode: boost paths whose words appear in the brief
  if (BRIEF) {
    const briefWords = new Set(BRIEF.toLowerCase().split(/\W+/).filter((w) => w.length > 3));
    for (const f of scored) {
      const hay = f.p.toLowerCase();
      for (const w of briefWords) if (hay.includes(w)) { f.boost = 5 * 86400 * 100; break; }
    }
  }
  scored.sort((a, b) => (b.recency + (b.boost || 0)) - (a.recency + (a.boost || 0)));
  const picked = [];
  let budget = B.source_budget_chars;
  for (const f of scored) {
    if (picked.length >= B.max_files || budget <= 0) break;
    let content;
    try {
      content = readFileSync(f.p, "utf8");
    } catch {
      continue;
    }
    let excerpt = content;
    if (excerpt.length > B.max_file_chars) {
      excerpt = excerpt.slice(0, B.max_file_chars) + "\n… [truncated by sweep budget]";
    }
    budget -= excerpt.length;
    picked.push({ path: f.p, content: excerpt, truncated: excerpt !== content });
  }
  return { picked, totalTracked: tracked.length };
}

function repoMeta() {
  const pkg = existsSync("package.json")
    ? JSON.parse(readFileSync("package.json", "utf8"))
    : {};
  const sweepResults = existsSync(join(OUT, "results.txt"))
    ? readFileSync(join(OUT, "results.txt"), "utf8")
    : "(staff-sweep results not available this run)";
  return {
    name: process.env.GITHUB_REPOSITORY || pkg.name || "unknown-repo",
    scripts: Object.keys(pkg.scripts || {}),
    deps: Object.keys(pkg.dependencies || {}).slice(0, 40),
    devDeps: Object.keys(pkg.devDependencies || {}).slice(0, 40),
    sweepResults,
  };
}

const SYSTEM = `You are a principal engineer running a scheduled review of a production repository owned by a two-LLC US software business. Money depends on these repos: they take card payments and serve live customers.

Rules — non-negotiable:
- Return ONE JSON object only. No prose outside it. No markdown fences.
- Schema: {"summary": string, "findings": [{"severity":"critical|high|medium|low","file":string,"issue":string,"fix":string,"evidence":string}], "opportunities": [{"title":string,"source":string,"why":string,"how":string,"license":string}], "changes": [{"file":string,"content":string,"rationale":string}]}
- findings: concrete defects or risks you can evidence from the code shown. Do not invent issues to fill quota. If the code is sound, return fewer findings.
- opportunities: improvements borrowed from well-known open-source projects or official documentation (cite the real repo or doc URL in "source"). Recommend REIMPLEMENTATION of the pattern. Never paste code from GPL/AGPL/SSPL projects into "changes". Set "license" to the upstream license, or "pattern-only" if you are only describing it.
- changes: EMPTY ARRAY unless a human brief was provided below. Max ${B.max_changes_per_run} files, full new file contents, no dependency changes, no CI/secrets/env/migrations edits.
- Never output credentials, tokens, or personal data even if you see them — flag their presence in "issue" only.
- No fabricated claims, metrics, certifications, customers, or testimonials anywhere in the output (this portfolio rejects them in CI).
- Prefer a small, provably-correct improvement over a speculative rewrite. Confidence belongs in "evidence".`;

function buildUserPrompt(meta, picked) {
  const parts = [];
  parts.push(`Repository: ${meta.name}`);
  parts.push(`Declared npm scripts: ${meta.scripts.join(", ") || "(none)"}`);
  if (meta.deps.length) parts.push(`Dependencies: ${meta.deps.join(", ")}`);
  if (meta.devDeps.length) parts.push(`Dev dependencies: ${meta.devDeps.join(", ")}`);
  parts.push(`\nLatest staff-sweep results:\n${meta.sweepResults}`);
  parts.push(`\nFiles reviewed this run (${picked.length} of ${meta.totalTracked} tracked, byte-budgeted):\n${picked.map((f) => f.path).join("\n")}`);
  if (MODE === "build") {
    parts.push(
      `\nHUMAN BRIEF (this is an explicit work order — produce "changes" for it):\n${BRIEF}\n\n` +
        `If the brief needs new npm dependencies, do NOT emit changes; explain the requirement in findings instead. ` +
        `If the brief is too large for one pass, do the smallest coherent slice and say what remains.`
    );
  } else {
    parts.push(
      `\nAutonomous scan: no work order. Emit ranked findings and upstream pattern opportunities. ` +
        `"changes" must be []. Do not propose feature work — propose what a staff engineer would fix or improve before the next release.`
    );
  }
  for (const f of picked) {
    parts.push(`\n----- file: ${f.path}${f.truncated ? " (truncated)" : ""} -----\n${f.content}`);
  }
  return parts.join("\n");
}

// ------------------------------------------------------------ gateway ------
async function callGateway(userPrompt) {
  const key = process.env.AI_GATEWAY_API_KEY;
  if (!key) return { noKey: true };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 120_000);
  const started = Date.now();
  try {
    const res = await fetch("https://ai-gateway.vercel.sh/v1/chat/completions", {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${key}` },
      body: JSON.stringify({
        model: MODEL,
        temperature: 0.2,
        max_tokens: B.max_output_tokens,
        messages: [
          { role: "system", content: SYSTEM },
          { role: "user", content: userPrompt },
        ],
      }),
      signal: ctrl.signal,
    });
    const text = await res.text();
    if (!res.ok) {
      let msg = text.slice(0, 500);
      try {
        const j = JSON.parse(text);
        msg = j?.error?.message || j?.message || msg;
      } catch { /* raw */ }
      return { error: `gateway ${res.status}: ${msg}` };
    }
    const data = JSON.parse(text);
    const usage = data.usage || {};
    return {
      content: data.choices?.[0]?.message?.content || "",
      usage,
      ms: Date.now() - started,
    };
  } catch (e) {
    return { error: `gateway request failed: ${e?.message || e}` };
  } finally {
    clearTimeout(timer);
  }
}

function parseJson(content) {
  let c = content.trim();
  const fence = c.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fence) c = fence[1].trim();
  const start = c.indexOf("{");
  const end = c.lastIndexOf("}");
  if (start < 0 || end <= start) throw new Error("no JSON object in model output");
  return JSON.parse(c.slice(start, end + 1));
}

// ------------------------------------------------------------ findings -----
function renderFindings(parsed, meta, usage, statusLine) {
  const cost = estCost(usage);
  const md = [];
  md.push(`# ai-improve findings — ${new Date().toISOString().slice(0, 16)}Z`);
  md.push("");
  md.push(`- **Mode:** ${MODE}${MODE === "build" ? ` (brief: "${BRIEF.slice(0, 120)}")` : ""}`);
  md.push(`- **Model:** ${MODEL} · **tokens:** in ${usage?.prompt_tokens ?? "?"} / out ${usage?.completion_tokens ?? "?"}${cost ? ` · **est cost:** $${cost}` : ""}`);
  md.push(`- **Run:** ${runUrl}`);
  md.push(`- **Status:** ${statusLine}`);
  md.push("");
  if (parsed.summary) {
    md.push(`> ${parsed.summary}`);
    md.push("");
  }
  if (parsed.findings?.length) {
    md.push(`## Findings (${parsed.findings.length})`);
    md.push("");
    md.push("| sev | file | issue | fix |");
    md.push("|---|---|---|---|");
    for (const f of parsed.findings.slice(0, 30)) {
      md.push(`| ${f.severity} | \`${String(f.file || "").slice(0, 60)}\` | ${esc(f.issue)} | ${esc(f.fix)} |`);
    }
    md.push("");
    md.push("<details><summary>evidence</summary>\n");
    for (const f of parsed.findings.slice(0, 30)) {
      md.push(`- **${String(f.file || "?")}** — ${esc(f.evidence || "—")}`);
    }
    md.push("\n</details>\n");
  }
  if (parsed.opportunities?.length) {
    md.push(`## Upstream pattern opportunities (${parsed.opportunities.length})`);
    md.push("");
    for (const o of parsed.opportunities.slice(0, 15)) {
      md.push(`### ${o.title}`);
      md.push(`- **Source:** ${o.source} · **license:** ${o.license || "pattern-only"}`);
      md.push(`- **Why:** ${esc(o.why)}`);
      md.push(`- **How:** ${esc(o.how)}`);
      md.push("");
    }
  }
  return md.join("\n");
}
function esc(s) {
  return String(s ?? "").replace(/\|/g, "\\|").replace(/\n/g, " ").slice(0, 400);
}
function estCost(usage) {
  const [i, o] = PRICES[MODEL] || [];
  if (!i || !usage?.prompt_tokens) return null;
  const c = (usage.prompt_tokens / 1e6) * i + (usage.completion_tokens / 1e6) * o;
  return c.toFixed(6);
}

// ------------------------------------------------------------- apply -------
const BLOCKED = [
  /^\.github\//,
  /(^|\/)(package\.json|package-lock\.json|pnpm-lock\.yaml|yarn\.lock)$/,
  /(^|\/)\.env(\..*)?$/,
  /(^|\/)supabase\/migrations\//,
  /(^|\/)node_modules\//,
];
function safePath(p) {
  if (!p || isAbsolute(p) || p.includes("..")) return null;
  const norm = p.replace(/^\.\//, "");
  if (BLOCKED.some((re) => re.test(norm))) return null;
  return norm;
}

function applyChanges(changes) {
  const applied = [];
  const rejected = [];
  const preCounts = {};
  for (const ch of changes.slice(0, B.max_changes_per_run)) {
    const p = safePath(ch.file);
    if (!p) { rejected.push(`${ch.file} (blocked path)`); continue; }
    if (typeof ch.content !== "string" || ch.content.length === 0) { rejected.push(`${ch.file} (empty)`); continue; }
    if (ch.content.length > 80_000) { rejected.push(`${ch.file} (too large)`); continue; }
    if (existsSync(p)) {
      try { preCounts[p] = readFileSync(p, "utf8").split("\n").length; } catch { /* ignore */ }
    }
    mkdirSync(dirname(resolve(p)), { recursive: true });
    writeFileSync(p, ch.content.endsWith("\n") ? ch.content : ch.content + "\n");
    applied.push(p);
  }
  return { applied, rejected, preCounts };
}

/**
 * Live-test lesson (2026-11-15): on an "add a doc comment" brief, a small model
 * handed back a gutted file — 68 lines → 4 — and lint+tests still passed green,
 * because a file of comments is valid JavaScript. Green checks prove the repo
 * still builds; they do NOT prove the model followed the brief. This guard
 * rejects gross file shrinkage unless the human brief explicitly asked for
 * deletion/rewrite.
 */
function assessDestructive(applied, preCounts) {
  const wantsDeletion = /delet|remov|rewrit|shr\w*|simplif|strip|reduc|condens|gut/i.test(BRIEF || "");
  const hits = [];
  for (const f of applied) {
    const before = preCounts[f];
    if (before == null || before < 20) continue; // new or tiny files: no baseline
    let after;
    try { after = readFileSync(f, "utf8").split("\n").length; } catch { continue; }
    if (after < before * 0.35) {
      hits.push(`\`${f}\`: ${before} → ${after} lines${wantsDeletion ? " (brief allowed deletion — review closely)" : ""}`);
    }
  }
  return hits;
}

function revertChanges(files) {
  for (const f of files) {
    try {
      const tracked = sh(`git ls-files --error-unmatch "${f}" 2>/dev/null || true`);
      if (tracked === f) sh(`git checkout -- "${f}"`);
      else unlinkSync(f);
    } catch (e) {
      console.error(`[ai-improve] revert failed for ${f}: ${e.message}`);
    }
  }
}

function runValidation() {
  const res = spawnSync("bash", [join(ENGINE_DIR, "scripts", "sweep.sh")], {
    env: { ...process.env, SWEEP_OUT: join(OUT, "validation") },
    encoding: "utf8",
    timeout: 20 * 60 * 1000,
  });
  return { ok: res.status === 0, log: (res.stdout || "") + (res.stderr || "") };
}

function writePrBody(parsed, applied, meta) {
  const md = [];
  md.push(`## Draft prepared by ai-improve`);
  md.push("");
  md.push(`**Brief (human-supplied):** ${BRIEF}`);
  md.push("");
  md.push(`**Model:** ${MODEL} · **run:** ${runUrl}`);
  md.push("");
  md.push(`**Files changed:** ${applied.map((f) => `\`${f}\``).join(", ")}`);
  md.push("");
  if (parsed.summary) md.push(`**Model rationale:** ${parsed.summary}\n`);
  md.push(`**Validation:** install + declared checks rerun against this change — see the run for logs.`);
  md.push("");
  md.push(`### Review checklist for the human merger`);
  md.push(`- [ ] Change does what the brief asked, nothing more`);
  md.push(`- [ ] No fabricated claims, credentials, or personal data introduced`);
  md.push(`- [ ] Tests/claims checks above are green`);
  md.push(`- [ ] Anything from an upstream project is pattern-reimplemented or license-compatible`);
  md.push("");
  md.push(`*This PR was opened by a scheduled agent. It never merges itself. If the model misunderstood the brief, close the PR and re-run the workflow with a sharper brief.*`);
  return md.join("\n");
}

// ---------------------------------------------------------------- main -----
(async function main() {
  try {
    const meta = repoMeta();
    const { picked, totalTracked } = collectFiles();
    if (!picked.length) fail(3, "no reviewable source files found (byte budget / filters)");
    meta.totalTracked = totalTracked;

    const userPrompt = buildUserPrompt(meta, picked);
    console.error(`[ai-improve] mode=${MODE} files=${picked.length}/${totalTracked} prompt_chars=${userPrompt.length}`);

    if (DRY_RUN) {
      writeFileSync(join(OUT, "prompt.txt"), userPrompt);
      setStatus("dry_run", `${picked.length} files, ${userPrompt.length} chars`);
      console.error(`[ai-improve] dry-run: prompt written to ${OUT}/prompt.txt`);
      process.exit(0);
    }

    const res = await callGateway(userPrompt);
    if (res.noKey) {
      const md = `# ai-improve skipped\n\nAI_GATEWAY_API_KEY is not set for this repository — no model ran.\n`;
      writeFileSync(join(OUT, "ai-findings.md"), md);
      setStatus("no_key", "AI_GATEWAY_API_KEY missing");
      process.exit(0);
    }
    if (res.error) {
      writeFileSync(join(OUT, "ai-findings.md"), `# ai-improve degraded\n\n${res.error}\n`);
      setStatus("api_error", res.error.slice(0, 200));
      process.exit(1);
    }

    let parsed;
    try {
      parsed = parseJson(res.content);
    } catch (e) {
      writeFileSync(join(OUT, "ai-findings.md"), `# ai-improve: unparsable model output\n\n\`\`\`\n${res.content.slice(0, 4000)}\n\`\`\`\n`);
      setStatus("api_error", `unparsable output: ${e.message}`);
      process.exit(1);
    }

    if (MODE === "scan") {
      const md = renderFindings(parsed, meta, res.usage, "scan complete — no repository files were written");
      writeFileSync(join(OUT, "ai-findings.md"), md);
      setStatus("findings", `${parsed.findings?.length || 0} findings, ${parsed.opportunities?.length || 0} opportunities`);
      console.error(`[ai-improve] usage in=${res.usage?.prompt_tokens} out=${res.usage?.completion_tokens} est=$${estCost(res.usage) ?? "?"} ms=${res.ms}`);
      process.exit(0);
    }

    // build mode
    const changes = Array.isArray(parsed.changes) ? parsed.changes : [];
    if (!changes.length) {
      const md = renderFindings(parsed, meta, res.usage, "brief received but model emitted no changes — findings only");
      writeFileSync(join(OUT, "ai-findings.md"), md);
      writeFileSync(join(OUT, "pr-body.md"), "");
      setStatus("no_changes", "model returned no file changes");
      process.exit(0);
    }

    const { applied, rejected, preCounts } = applyChanges(changes);
    if (!applied.length) {
      const md = renderFindings(parsed, meta, res.usage, `changes rejected at path guard: ${rejected.join(", ")}`);
      writeFileSync(join(OUT, "ai-findings.md"), md);
      setStatus("rejected_paths", rejected.join(", "));
      process.exit(0);
    }

    // gross-shrinkage guard (see assessDestructive) — revert before wasting a validation run
    const destructive = assessDestructive(applied, preCounts);
    if (destructive.length && !/delet|remov|rewrit|shr\w*|simplif|strip|reduc|condens|gut/i.test(BRIEF || "")) {
      revertChanges(applied);
      const md =
        renderFindings(parsed, meta, res.usage, `changes REVERTED — destructive shrinkage: ${destructive.join("; ")}`) +
        `\n\n## Guard detail\n\nThe model returned files that would **delete most of existing code** without the brief asking for deletion:\n\n${destructive.map((d) => `- ${d}`).join("\n")}\n\nRe-run with a sharper brief, or reword it to explicitly ask for a rewrite.\n`;
      writeFileSync(join(OUT, "ai-findings.md"), md);
      setStatus("rejected_destructive", destructive.join("; "));
      console.error(`[ai-improve] destructive guard: ${destructive.join("; ")}`);
      process.exit(0);
    }

    const val = runValidation();
    if (val.ok) {
      const md = renderFindings(parsed, meta, res.usage, `changes applied and validated: ${applied.join(", ")}`);
      writeFileSync(join(OUT, "ai-findings.md"), md);
      writeFileSync(join(OUT, "pr-body.md"), writePrBody(parsed, applied, meta));
      setStatus("applied_validated", applied.join(", "));
      console.error(`[ai-improve] validated: ${applied.join(", ")}`);
      process.exit(0);
    }

    // validation failed → revert and report honestly
    revertChanges(applied);
    const tail = val.log.split("\n").slice(-60).join("\n");
    const md =
      renderFindings(parsed, meta, res.usage, `changes REVERTED — validation failed (${applied.join(", ")})`) +
      `\n\n## Validation failure (reverted; repo left clean)\n\n\`\`\`\n${tail}\n\`\`\`\n`;
    writeFileSync(join(OUT, "ai-findings.md"), md);
    setStatus("rejected_validation", applied.join(", "));
    console.error(`[ai-improve] validation failed, reverted: ${applied.join(", ")}`);
    process.exit(0);
  } catch (e) {
    fail(3, `unexpected engine error: ${e?.stack || e}`);
  }
})();
