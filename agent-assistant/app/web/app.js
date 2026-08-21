/* Multi-Agent 软件开发助手 · 控制台逻辑(原生 JS,无外部依赖) */
"use strict";

const $ = (id) => document.getElementById(id);
const TOKEN_KEY = "mvd.controlToken";
const THEME_KEY = "mvd.theme";

/* ---------------- 主题切换 ---------------- */

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function applyTheme(theme, persist = true) {
  document.documentElement.dataset.theme = theme;
  if (persist) {
    try { localStorage.setItem(THEME_KEY, theme); } catch (_) { /* 忽略存储失败 */ }
  }
  const btn = $("themeToggleBtn");
  if (btn) btn.textContent = theme === "light" ? "🌙 深色" : "☀️ 浅色";
}

function toggleTheme() {
  applyTheme(currentTheme() === "light" ? "dark" : "light");
}

/* 测量顶部 header 实际高度, 供详情页吸顶操作栏计算偏移 */
function syncHeaderHeight() {
  try {
    const headerEl = document.querySelector("header");
    if (!headerEl || !headerEl.offsetHeight) return;
    document.documentElement.style.setProperty(
      "--header-h",
      `${headerEl.offsetHeight}px`
    );
  } catch (_) { /* 忽略 */ }
}

const STATUS_LABEL = {
  queued: "排队中",
  running: "执行中",
  waiting_approval: "等待审批",
  completed: "已完成",
  needs_human: "需人工介入",
  failed: "失败",
  cancelled: "已取消",
  rejected: "已拒绝",
};

const NODE_LABEL = {
  api: "API",
  worker: "Worker",
  manager: "Manager",
  developer: "Developer",
  tester: "Tester",
  policy_gate: "Policy Gate",
  reviewer: "Reviewer",
  validate_request: "校验请求",
  prepare_workspace: "准备副本",
  wait_for_plan_approval: "等待审批",
  finalize: "收尾",
  runtime_error: "运行时错误",
};

let state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  view: "login",
  runId: null,
  eventCursor: 0,
  pollTimer: null,
  packs: [],
};

/* ---------------- 基础工具 ---------------- */

function esc(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return esc(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function statusBadge(status) {
  const label = STATUS_LABEL[status] || status;
  return `<span class="badge st-${esc(status)}">${esc(label)}</span>`;
}

function nodeClass(node) {
  const n = (node || "").replace(/^n_/, "");
  if (["api", "worker", "manager", "developer", "tester", "policy_gate", "reviewer"].includes(n)) {
    return "n-" + n;
  }
  return "";
}

function truncate(text, len) {
  text = String(text || "");
  return text.length > len ? text.slice(0, len) + "…" : text;
}

/* ---------------- API ---------------- */

async function api(path, options = {}) {
  const headers = { Authorization: `Bearer ${state.token}`, ...(options.headers || {}) };
  if (options.body !== undefined && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(path, { ...options, headers });
  if (resp.status === 401) {
    logout("控制令牌无效或已过期,请重新连接");
    throw new Error("unauthorized");
  }
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      if (data && data.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch (_) { /* 非 JSON 响应 */ }
    throw new Error(detail);
  }
  const ct = resp.headers.get("Content-Type") || "";
  return ct.includes("json") ? resp.json() : resp.text();
}

async function downloadArtifact(artifactId, filename) {
  const resp = await fetch(`/api/v1/artifacts/${encodeURIComponent(artifactId)}`, {
    headers: { Authorization: `Bearer ${state.token}` },
  });
  if (resp.status === 401) {
    logout("控制令牌无效或已过期,请重新连接");
    throw new Error("unauthorized");
  }
  if (!resp.ok) throw new Error(`制品下载失败: HTTP ${resp.status}`);
  const url = URL.createObjectURL(await resp.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename || "artifact";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---------------- 视图切换 ---------------- */

function showView(name) {
  state.view = name;
  ["loginView", "listView", "createView", "detailView"].forEach((id) => $(id).classList.toggle("hidden", id !== name));
  $("providerBadge").classList.toggle("hidden", !state.token);
  $("logoutBtn").classList.toggle("hidden", !state.token);
  window.scrollTo(0, 0);
}

function logout(message) {
  stopPoll();
  state.token = "";
  localStorage.removeItem(TOKEN_KEY);
  $("tokenInput").value = "";
  $("providerBadge").textContent = "";
  if (message) $("loginError").textContent = message;
  showView("loginView");
}

/* ---------------- 登录 ---------------- */

async function doLogin() {
  const token = $("tokenInput").value.trim();
  if (token.length < 32) {
    $("loginError").textContent = "令牌长度不足(至少 32 字符)";
    return;
  }
  state.token = token;
  try {
    await api("/api/v1/acceptance-packs");
    localStorage.setItem(TOKEN_KEY, token);
    $("loginError").classList.add("hidden");
    $("providerBadge").textContent = "已连接 127.0.0.1:8080";
    await openList();
  } catch (err) {
    if (err.message !== "unauthorized") {
      $("loginError").textContent = `连接失败: ${err.message}`;
      state.token = "";
    }
  }
}

/* ---------------- 任务列表 ---------------- */

async function openList() {
  stopPoll();
  showView("listView");
  await refreshList();
  state.pollTimer = setInterval(refreshList, 5000);
}

async function refreshList() {
  const wrap = $("runTableWrap");
  try {
    const runs = await api("/api/v1/runs?limit=50");
    if (state.view !== "listView") return;
    if (!runs.length) {
      wrap.innerHTML = `<p class="empty">还没有运行任务,点击右上角「新建任务」开始第一次多智能体流水线。</p>`;
      return;
    }
    const rows = runs
      .map((r) => {
        const active = ["queued", "running", "waiting_approval"].includes(r.status);
        return `<tr class="clickable" data-run="${esc(r.run_id)}">
          <td>${statusBadge(r.status)}</td>
          <td class="req-cell" title="${esc(r.requirement)}">${esc(truncate(r.requirement, 90))}</td>
          <td class="mono">${esc(r.acceptance_pack_id || "—")}</td>
          <td class="mono">${esc(r.provider_mode)}</td>
          <td class="ts">${fmtTime(r.created_at)}</td>
        </tr>`;
      })
      .join("");
    wrap.innerHTML = `<table>
      <thead><tr><th>状态</th><th>需求</th><th>验收任务</th><th>模型</th><th>创建时间</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
    wrap.querySelectorAll("tr.clickable").forEach((tr) =>
      tr.addEventListener("click", () => openDetail(tr.dataset.run))
    );
    if (runs.some((r) => ["queued", "running", "waiting_approval"].includes(r.status))) {
      // 有活跃任务时保持 5s 轮询
    }
  } catch (err) {
    if (err.message !== "unauthorized" && state.view === "listView") {
      wrap.innerHTML = `<p class="error">加载失败: ${esc(err.message)}</p>`;
    }
  }
}

/* ---------------- 创建任务 ---------------- */

async function openCreate() {
  stopPoll();
  showView("createView");
  const proj = $("createProject");
  proj.innerHTML = `<option value="student-management-backend">student-management-backend</option>`;
  try {
    state.packs = await api("/api/v1/acceptance-packs");
    const ready = state.packs.filter((p) => p.status === "ready");
    $("createPack").innerHTML =
      `<option value="">(不使用验收包 — 仅 mock 模式可用)</option>` +
      ready
        .map(
          (p) =>
            `<option value="${esc(p.pack_id)}" data-spec="${esc(p.task_spec || "")}">${esc(p.pack_id)}</option>`
        )
        .join("");
    $("createPack").addEventListener("change", () => {
      const opt = $("createPack").selectedOptions[0];
      $("packHint").textContent = opt && opt.dataset.spec ? opt.dataset.spec : "";
    });
    if (ready.length) $("packHint").textContent = ready[0].task_spec || "";
  } catch (err) {
    if (err.message !== "unauthorized") $("packHint").textContent = "加载验收任务失败: " + err.message;
  }
}

async function createRun() {
  const requirement = $("createRequirement").value.trim();
  const acceptance_pack_id = $("createPack").value || null;
  $("createError").classList.add("hidden");
  if (requirement.length < 8) {
    $("createError").textContent = "需求描述至少 8 个字符";
    $("createError").classList.remove("hidden");
    return;
  }
  if (!acceptance_pack_id) {
    $("createError").textContent = "真实 LLM 模式必须选择一个验收任务(acceptance pack)";
    $("createError").classList.remove("hidden");
    return;
  }
  const btn = $("createRunBtn");
  btn.disabled = true;
  btn.textContent = "创建中…";
  try {
    const run = await api("/api/v1/runs", {
      method: "POST",
      body: JSON.stringify({ project_id: $("createProject").value, requirement, acceptance_pack_id }),
    });
    await openDetail(run.run_id);
  } catch (err) {
    if (err.message !== "unauthorized") {
      $("createError").textContent = `创建失败: ${err.message}`;
      $("createError").classList.remove("hidden");
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "创建并进入队列";
  }
}

/* ---------------- 运行详情 ---------------- */

let detailData = null; // 最近一次 run 详情,用于审批/取消按钮
let detailEvents = [];

async function openDetail(runId) {
  stopPoll();
  state.runId = runId;
  state.eventCursor = 0;
  detailData = null;
  detailEvents = [];
  showView("detailView");
  $("detailView").innerHTML = `<div class="card"><span class="spin"></span>正在加载运行详情…</div>`;
  await refreshDetail();
  state.pollTimer = setInterval(refreshDetail, 3000);
}

async function refreshDetail() {
  if (!state.runId || state.view !== "detailView") return;
  const runId = state.runId;
  try {
    const [run, events, artifacts] = await Promise.all([
      api(`/api/v1/runs/${runId}`),
      api(`/api/v1/runs/${runId}/events?after_event_id=${state.eventCursor}`),
      api(`/api/v1/runs/${runId}/artifacts`),
    ]);
    let publication = null;
    if (run.status === "completed") {
      try { publication = await api(`/api/v1/runs/${runId}/publication`); } catch (_) { publication = null; }
    }
    if (events.length) {
      detailEvents.push(...events);
      state.eventCursor = Math.max(...events.map((e) => e.event_id));
    }
    detailData = run;
    renderDetail(run, detailEvents, artifacts, publication);
    const done = ["completed", "failed", "cancelled", "needs_human", "rejected"].includes(run.status);
    if (done) stopPoll();
  } catch (err) {
    if (err.message === "unauthorized") return;
    if (state.view === "detailView") {
      $("detailView").innerHTML = `<div class="card"><p class="error">加载失败: ${esc(err.message)}</p></div>`;
    }
  }
}

async function fetchJsonArtifact(artifact) {
  return api(`/api/v1/artifacts/${artifact.artifact_id}`);
}

async function renderDetail(run, events, artifacts, publication) {
  const active = ["queued", "running", "waiting_approval"].includes(run.status);
  const plan = run.plan;
  const approvalPending = run.status === "waiting_approval" && plan;

  // 尝试读取 report.json(测试/评审结果)
  let report = null;
  const reportArtifact = artifacts.find((a) => a.kind === "report.json");
  if (reportArtifact) {
    try { report = JSON.parse(await fetchJsonArtifact(reportArtifact)); } catch (_) { report = null; }
  }

  // 测试日志制品
  const baselineArt = artifacts.find((a) => a.kind === "baseline_test.stdout.txt");
  const acceptArt = artifacts.find((a) => a.kind === "acceptance_test.stdout.txt");
  const patchArt = artifacts.find((a) => a.kind === "change.patch");

  // Diff
  let diffText = "";
  try { diffText = await api(`/api/v1/runs/${run.run_id}/diff`); } catch (_) { diffText = ""; }

  const html = `
    <div class="detail-head">
      <button class="ghost" id="detailBackBtn">← 返回列表</button>
      <h2>${esc(run.run_id)}</h2>
      ${statusBadge(run.status)}
    </div>
    <div class="detail-meta">
      <span>项目: <b>${esc(run.project_id)}</b></span>
      <span>验收包: <b>${esc(run.acceptance_pack_id || "—")}</b></span>
      <span>模型: <b>${esc(run.provider_mode)}</b></span>
      <span>节点: <b>${esc(NODE_LABEL[run.current_node] || run.current_node || "—")}</b></span>
      <span>创建: <b>${fmtTime(run.created_at)}</b></span>
      ${run.ended_at ? `<span>结束: <b>${fmtTime(run.ended_at)}</b></span>` : ""}
      ${run.terminal_reason ? `<span>原因: <b style="color:var(--red)">${esc(run.terminal_reason)}</b></span>` : ""}
    </div>

    <div class="section-title">
      <span>需求</span>
      <span class="sub mono">${esc(run.run_id)}</span>
    </div>
    <div class="card"><p>${esc(run.requirement)}</p></div>

    <div class="section-title"><span>流程时间线</span></div>
    <div class="card">
      ${events.length ? renderTimeline(events) : '<p class="muted">暂无事件。</p>'}
    </div>

    <div class="section-title"><span>Manager 方案</span></div>
    <div class="card plan-card ${approvalPending ? "pending" : ""}">
      ${
        plan
          ? renderPlan(plan, run, approvalPending)
          : '<p class="muted">方案尚未生成(waiting_approval 之前无方案)。</p>'
      }
    </div>

    <div class="section-title"><span>测试结果</span></div>
    <div class="card">
      ${renderTests(report, baselineArt, acceptArt)}
    </div>

    <div class="section-title">
      <span>代码 Diff</span>
      ${patchArt ? artifactDownloadButton(patchArt, "下载 change.patch") : ""}
    </div>
    <div class="card">${diffText ? renderDiff(diffText) : '<p class="muted">暂无可用 diff。</p>'}</div>

    <div class="section-title"><span>Reviewer 评审</span></div>
    <div class="card">${renderReview(report)}</div>

    ${run.status === "completed" ? `
      <div class="section-title"><span>发布到真实项目</span></div>
      <div class="card publication-card">${renderPublication(publication)}</div>
    ` : ""}

    <div class="section-title"><span>LLM 调用审计</span></div>
    <div class="card"><div id="llmCallsWrap"><span class="spin"></span>加载中…</div></div>

    <div class="section-title"><span>制品</span></div>
    <div class="card">
      ${artifacts.length
        ? `<table><thead><tr><th>类型</th><th>大小</th><th>创建时间</th><th></th></tr></thead><tbody>
          ${artifacts
            .map(
              (a) => `<tr>
                <td class="mono">${esc(a.kind)}</td>
                <td class="ts">${a.size_bytes} B</td>
                <td class="ts">${fmtTime(a.created_at)}</td>
                <td>${artifactDownloadButton(a, "下载")}</td>
              </tr>`
            )
            .join("")}
        </tbody></table>`
        : '<p class="muted">暂无制品。</p>'}
    </div>

    ${active ? `<div class="card" style="text-align:right"><button id="cancelBtn" class="danger">取消任务</button></div>` : ""}
  `;

  $("detailView").innerHTML = html;

  $("detailBackBtn").addEventListener("click", openList);
  if (approvalPending) {
    $("approveBtn").addEventListener("click", () => submitApproval(run, "approve"));
    $("rejectBtn").addEventListener("click", () => submitApproval(run, "reject"));
  }
  const cancelBtn = $("cancelBtn");
  if (cancelBtn) cancelBtn.addEventListener("click", () => cancelRun(run.run_id));
  document.querySelectorAll(".artifact-download").forEach((button) =>
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await downloadArtifact(button.dataset.artifact, button.dataset.filename);
      } catch (err) {
        if (err.message !== "unauthorized") alert(err.message);
      } finally {
        button.disabled = false;
      }
    })
  );
  const publishCheck = $("publishConfirmCheck");
  const publishBtn = $("publishBtn");
  if (publishCheck && publishBtn && publication) {
    publishCheck.addEventListener("change", () => {
      publishBtn.disabled = !publishCheck.checked;
    });
    publishBtn.addEventListener("click", () => submitPublication(run, publication));
  }

  // LLM 调用异步加载
  loadLlmCalls(run.run_id);
}

function artifactDownloadButton(artifact, label) {
  const name = String(artifact.kind || "artifact").replace(/[^A-Za-z0-9._-]/g, "_");
  return `<button type="button" class="download artifact-download" data-artifact="${esc(artifact.artifact_id)}" data-filename="${esc(name)}">${esc(label)}</button>`;
}

function renderPublication(publication) {
  if (!publication) {
    return '<p class="muted">发布预检暂不可用。代码仍只保存在隔离工作副本中。</p>';
  }
  if (publication.status === "published") {
    return `<p class="review-approve">✓ 已发布到独立 Git 分支并重新通过 Baseline + Acceptance 验证</p>
      <div class="hash-box">目标: ${esc(publication.source_path)}<br>
      仓库根: ${esc(publication.git_repository_root || "—")}<br>
      项目子路径: ${esc(publication.git_project_subpath || "(仓库根)")}<br>
      Git 远程: ${esc(publication.git_remote_name || "—")} · ${esc(publication.git_remote_url || "—")}<br>
      Git 分支: ${esc(publication.git_target_branch || "旧版直接发布")}<br>
      Git commit: ${esc(publication.git_commit || "—")}<br>
      Git base: ${esc(publication.git_base_commit || "—")}<br>
      diff_sha256: ${esc(publication.diff_sha256)}</div>`;
  }
  const files = (publication.changed_files || []).map((f) => `<li class="mono">${esc(f)}</li>`).join("");
  return `
    <p><b>当前 completed 仅表示隔离工作副本通过评审，不代表已写入真实项目。</b></p>
    <p class="muted" style="margin-top:6px">目标路径: <span class="mono">${esc(publication.source_path)}</span></p>
    <p class="muted" style="margin-top:6px">Git 仓库根: <span class="mono">${esc(publication.git_repository_root || "—")}</span>；项目子路径: <span class="mono">${esc(publication.git_project_subpath || "(仓库根)")}</span></p>
    ${files ? `<ul class="publish-files">${files}</ul>` : ""}
    <div class="hash-box">
      project_profile_hash: ${esc(publication.project_profile_hash)}<br>
      source_manifest_hash: ${esc(publication.source_manifest_hash)}<br>
      current_source_manifest_hash: ${esc(publication.current_source_manifest_hash || "—")}<br>
      diff_sha256: ${esc(publication.diff_sha256 || "—")}<br>
      Git 远程: ${esc(publication.git_remote_name || "—")} · ${esc(publication.git_remote_url || "—")}<br>
      Git 配置基线: ${esc(publication.git_base_branch || "—")}<br>
      Git 当前分支: ${esc(publication.git_current_branch || "—")}<br>
      Git 基线 commit: ${esc(publication.git_base_commit || "—")}<br>
      Git 发布分支: ${esc(publication.git_target_branch || "—")}
    </div>
    ${publication.eligible
      ? `<label class="publish-confirm"><input id="publishConfirmCheck" type="checkbox"> 我已检查 Diff 和测试结果，同意创建独立 Git 分支、写入上述文件、复测并提交。</label>
         <button id="publishBtn" class="danger" disabled>创建分支并安全发布</button>`
      : `<p class="error">当前不可发布: ${esc(publication.reason || publication.status)}</p>`}
  `;
}

function renderTimeline(events) {
  return `<div class="timeline">${events
    .map(
      (e) => `<div class="tl-item ${nodeClass(e.node)}">
        <div class="tl-node">${esc(e.node)} · ${esc(e.event_type)}</div>
        <div class="tl-summary">${esc(e.summary)}</div>
        <div class="tl-time">${fmtTime(e.created_at)}${e.artifact_id ? ` · artifact: <span class="mono">${esc(e.artifact_id.slice(0, 8))}…</span>` : ""}</div>
      </div>`
    )
    .join("")}</div>`;
}

function renderPlan(plan, run, pending) {
  const criteria = (plan.acceptance_criteria || [])
    .map((c) => `<li>${esc(c)}</li>`)
    .join("");
  const files = (plan.files_to_inspect || []).map((f) => `<span class="mono" style="margin-right:10px;color:var(--cyan)">${esc(f)}</span>`).join("");
  const steps = (plan.steps || [])
    .map((s) => `<li><b class="mono">${esc(s.id)}</b> — ${esc(s.description)}${s.depends_on && s.depends_on.length ? ` <span class="muted">(依赖: ${esc(s.depends_on.join(", "))})</span>` : ""}</li>`)
    .join("");
  return `
    <div class="plan-summary">${esc(plan.summary || "(无摘要)")}</div>
    <div class="plan-meta">
      <span>风险: <b>${esc(plan.risk_level || "—")}</b></span>
      <span>测试画像: <b>${esc(plan.test_profile || "—")}</b></span>
      <span>修改范围: <b class="mono">${esc((plan.allowed_change_globs || []).join(", "))}</b></span>
    </div>
    ${criteria ? `<div class="section-title" style="margin-top:12px"><span>验收标准</span></div><ul class="plan-criteria">${criteria}</ul>` : ""}
    ${files ? `<div class="section-title" style="margin-top:12px"><span>拟检查文件</span></div><div>${files}</div>` : ""}
    ${steps ? `<div class="section-title" style="margin-top:12px"><span>执行步骤</span></div><ul class="plan-criteria">${steps}</ul>` : ""}
    <div class="hash-box">
      plan_hash: ${esc(run.plan_hash || "—")}<br>
      project_profile_hash: ${esc(run.project_profile_hash || "—")}<br>
      source_manifest_hash: ${esc(run.source_manifest_hash || "—")}<br>
      context_bundle_hash: ${esc(run.context_bundle_hash || "—")}<br>
      acceptance_pack_hash: ${esc(run.acceptance_pack_hash || "—")}
    </div>
    ${
      pending
        ? `<div class="approval-actions">
            <button id="approveBtn" class="primary">✓ 批准方案</button>
            <button id="rejectBtn" class="danger">✗ 拒绝方案</button>
          </div>`
        : ""
    }
  `;
}

function renderTests(report, baselineArt, acceptArt) {
  if (!report) {
    const links = [];
    if (baselineArt) links.push(artifactDownloadButton(baselineArt, "baseline 日志"));
    if (acceptArt) links.push(artifactDownloadButton(acceptArt, "acceptance 日志"));
    return links.length ? `<p class="muted">测试结果尚未汇总。${links.join(" · ")}</p>` : '<p class="muted">尚无测试运行。</p>';
  }
  const base = report.baseline_test_result || {};
  const acc = report.acceptance_test_result || {};
  const cards = (title, r) => `
    <div class="result-card ${r.exit_code === 0 ? "ok" : "bad"}">
      <div class="k">${title}</div>
      <div class="v">${r.passed ?? "—"} / ${(r.passed ?? 0) + (r.failed ?? 0)} 通过</div>
      <div class="k" style="margin-top:4px">exit ${r.exit_code} · ${Math.round((r.duration_ms || 0) / 1000)}s</div>
    </div>`;
  return `
    <div class="result-grid">
      ${cards("Baseline 回归测试", base)}
      ${cards("Acceptance 黑盒验收", acc)}
    </div>
    <div style="margin-top:12px">
      ${baselineArt ? artifactDownloadButton(baselineArt, "下载 baseline 完整日志") : ""}
      ${acceptArt ? ` · ${artifactDownloadButton(acceptArt, "下载 acceptance 完整日志")}` : ""}
    </div>`;
}

function renderDiff(text) {
  const lines = text.split("\n").map((line) => {
    let cls = "meta-line";
    if (line.startsWith("+++") || line.startsWith("---")) cls = "meta-line";
    else if (line.startsWith("@@")) cls = "hunk";
    else if (line.startsWith("+")) cls = "add";
    else if (line.startsWith("-")) cls = "del";
    return `<span class="${cls}">${esc(line) || " "}</span>`;
  });
  return `<pre class="diff">${lines.join("\n")}</pre>`;
}

function renderReview(report) {
  if (!report || !report.review) {
    return '<p class="muted">Reviewer 尚未评审。</p>';
  }
  const rev = report.review;
  const decision = rev.decision === "approve" ? "approve · 批准" : "changes_requested · 要求修改";
  const coverage = (rev.requirement_coverage || [])
    .map(
      (c) =>
        `<li class="${c.status === "met" ? "met" : "miss"}">${c.status === "met" ? "✓" : "✗"} ${esc(c.criterion)} <span class="muted">[${esc(c.status)}]</span></li>`
    )
    .join("");
  return `
    <p class="${rev.decision === "approve" ? "review-approve" : "review-change"}">${esc(decision)}</p>
    <p style="margin-top:8px">${esc(rev.summary || "")}</p>
    ${coverage ? `<ul class="coverage">${coverage}</ul>` : ""}
    ${rev.findings && rev.findings.length ? `<div class="findings" style="margin-top:8px">发现: ${esc(rev.findings.join("; "))}</div>` : ""}
    ${rev.residual_risks && rev.residual_risks.length ? `<div class="muted" style="margin-top:8px">残余风险: ${esc(rev.residual_risks.join("; "))}</div>` : ""}
  `;
}

async function loadLlmCalls(runId) {
  const wrap = $("llmCallsWrap");
  if (!wrap) return;
  try {
    const calls = await api(`/api/v1/runs/${runId}/llm-calls`);
    if (!wrap) return;
    if (!calls.length) {
      wrap.innerHTML = '<p class="muted">暂无 LLM 调用记录(mock 模式或尚未调用)。</p>';
      return;
    }
    wrap.innerHTML = `<table class="llm-table">
      <thead><tr><th>角色</th><th>模型</th><th>实际模型</th><th>状态</th><th>输入 Tokens</th><th>输出 Tokens</th><th>时间</th></tr></thead>
      <tbody>${calls
        .map(
          (c) => `<tr>
            <td>${esc(c.role)}</td>
            <td>${esc(c.configured_model)}</td>
            <td>${esc(c.actual_model || "—")}</td>
            <td class="${c.status === "succeeded" ? "llm-ok" : "llm-err"}">${esc(c.status)}</td>
            <td>${c.input_tokens ?? "—"}</td>
            <td>${c.output_tokens ?? "—"}</td>
            <td class="ts">${fmtTime(c.created_at)}</td>
          </tr>`
        )
        .join("")}
      </tbody></table>`;
  } catch (_) {
    if (wrap) wrap.innerHTML = '<p class="muted">LLM 调用记录加载失败。</p>';
  }
}

/* ---------------- 审批 / 取消 ---------------- */

async function submitApproval(run, decision) {
  const btn = decision === "approve" ? $("approveBtn") : $("rejectBtn");
  btn.disabled = true;
  btn.textContent = "提交中…";
  const payload = {
    decision,
    plan_hash: run.plan_hash,
    project_profile_hash: run.project_profile_hash,
    source_manifest_hash: run.source_manifest_hash,
    context_bundle_hash: run.context_bundle_hash,
    acceptance_pack_hash: run.acceptance_pack_hash,
    idempotency_key: `console-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    comment: decision === "approve" ? "控制台批准" : "控制台拒绝",
  };
  try {
    await api(`/api/v1/runs/${run.run_id}/approval`, { method: "POST", body: JSON.stringify(payload) });
    refreshDetail();
  } catch (err) {
    if (err.message !== "unauthorized") alert(`审批提交失败: ${err.message}`);
    btn.disabled = false;
    btn.textContent = decision === "approve" ? "✓ 批准方案" : "✗ 拒绝方案";
  }
}

async function cancelRun(runId) {
  if (!confirm("确认取消该任务?已开始的 Docker 容器将被停止。")) return;
  try {
    await api(`/api/v1/runs/${runId}/cancel`, {
      method: "POST",
      body: JSON.stringify({ reason: "控制台取消" }),
    });
    refreshDetail();
  } catch (err) {
    if (err.message !== "unauthorized") alert(`取消失败: ${err.message}`);
  }
}

async function submitPublication(run, publication) {
  if (!confirm(`最后确认：将在 ${publication.git_remote_name} (${publication.git_remote_url}) 对应仓库的 ${publication.git_target_branch} 分支发布 ${publication.changed_files.length} 个文件\n仓库根：${publication.git_repository_root}\n项目：${publication.git_project_subpath || "(仓库根)"}\n\n本步骤只创建本地 commit，不会自动推送远程；测试失败会恢复原分支。是否继续？`)) return;
  const btn = $("publishBtn");
  btn.disabled = true;
  btn.textContent = "发布并复验中…";
  try {
    await api(`/api/v1/runs/${run.run_id}/publication`, {
      method: "POST",
      body: JSON.stringify({
        confirmation: "publish",
        project_profile_hash: publication.project_profile_hash,
        source_manifest_hash: publication.source_manifest_hash,
        diff_sha256: publication.diff_sha256,
        git_original_branch: publication.git_current_branch,
        git_base_commit: publication.git_base_commit,
        git_target_branch: publication.git_target_branch,
        git_remote_name: publication.git_remote_name,
        git_remote_url: publication.git_remote_url,
        idempotency_key: `console-publish-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        comment: "控制台二次确认发布",
      }),
    });
    await refreshDetail();
  } catch (err) {
    if (err.message !== "unauthorized") alert(`发布失败: ${err.message}`);
    btn.disabled = false;
    btn.textContent = "创建分支并安全发布";
  }
}

/* ---------------- 轮询 ---------------- */

function stopPoll() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

/* ---------------- 初始化 ---------------- */

function init() {
  $("themeToggleBtn").addEventListener("click", toggleTheme);
  applyTheme(currentTheme(), false); // 主题已由 head 内联脚本应用, 这里只同步按钮文案
  syncHeaderHeight();
  try { window.addEventListener("resize", syncHeaderHeight); } catch (_) { /* 忽略 */ }
  $("loginBtn").addEventListener("click", doLogin);
  $("tokenInput").addEventListener("keydown", (e) => e.key === "Enter" && doLogin());
  $("logoutBtn").addEventListener("click", () => logout());
  $("refreshBtn").addEventListener("click", refreshList);
  $("newRunBtn").addEventListener("click", openCreate);
  $("createBackBtn").addEventListener("click", openList);
  $("createRunBtn").addEventListener("click", createRun);

  if (state.token) {
    showView("listView");
    $("providerBadge").textContent = "已连接 127.0.0.1:8080";
    openList().catch(() => {});
  } else {
    showView("loginView");
  }
}

document.addEventListener("DOMContentLoaded", init);
