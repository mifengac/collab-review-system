/* 公共工具：鉴权、请求、状态徽章、布局 */
const API = "";
const TOKEN_KEY = "crs_token";
const USER_KEY = "crs_user";

const STATUS_CLASS = {
  草稿: "badge-draft",
  承办中: "badge-handling",
  A领导审核中: "badge-a-review",
  A领导退回: "badge-a-reject",
  B领导审核中: "badge-b-review",
  B领导退回: "badge-b-reject",
  已定稿: "badge-done",
  已归档: "badge-archive",
  已作废: "badge-cancel",
};

const URGENCY_CLASS = {
  一般: "",
  重要: "badge-important",
  紧急: "badge-urgent",
  特急: "badge-urgent",
};

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

function getUser() {
  try {
    return JSON.parse(localStorage.getItem(USER_KEY) || "null");
  } catch {
    return null;
  }
}

function setAuth(token, user) {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, JSON.stringify(user));
}

function clearAuth() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

function requireAuth() {
  if (!getToken()) {
    location.href = "/login.html";
    return false;
  }
  return true;
}

function logout() {
  clearAuth();
  location.href = "/login.html";
}

function toast(msg, type) {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.className = "toast show" + (type ? " " + type : "");
  el.textContent = msg;
  clearTimeout(el._t);
  el._t = setTimeout(() => {
    el.className = "toast";
  }, 2800);
}

async function api(path, options = {}) {
  const headers = options.headers || {};
  if (!(options.body instanceof FormData)) {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
  }
  const token = getToken();
  if (token) headers["Authorization"] = "Bearer " + token;

  const res = await fetch(API + path, { ...options, headers });
  if (res.status === 401) {
    clearAuth();
    if (!location.pathname.includes("login")) {
      location.href = "/login.html";
    }
    throw new Error("未登录");
  }

  const ct = res.headers.get("content-type") || "";
  let data = null;
  if (ct.includes("application/json")) {
    data = await res.json();
  } else if (res.ok) {
    return res;
  } else {
    throw new Error("请求失败 " + res.status);
  }

  if (!res.ok) {
    const detail = data.detail;
    let msg = "请求失败";
    if (typeof detail === "string") msg = detail;
    else if (Array.isArray(detail)) msg = detail.map((d) => d.msg || d).join("; ");
    throw new Error(msg);
  }
  return data;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return (
    d.getFullYear() +
    "-" +
    p(d.getMonth() + 1) +
    "-" +
    p(d.getDate()) +
    " " +
    p(d.getHours()) +
    ":" +
    p(d.getMinutes())
  );
}

function statusBadge(status) {
  const cls = STATUS_CLASS[status] || "";
  return `<span class="badge ${cls}">${escapeHtml(status || "")}</span>`;
}

function urgencyBadge(u) {
  const cls = URGENCY_CLASS[u] || "";
  return `<span class="badge ${cls}">${escapeHtml(u || "一般")}</span>`;
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function roleLabel(role) {
  const map = {
    admin: "管理员",
    office_clerk: "办公室收文员",
    supervisor: "督办人员",
    handler: "承办人",
    leader_a: "A领导",
    leader_b: "B领导",
    viewer: "只读",
  };
  return map[role] || role;
}

function isGlobalViewer(user) {
  const r = (user || getUser() || {}).role;
  return r === "admin" || r === "office_clerk" || r === "supervisor";
}

function isOfficeOrAdmin(user) {
  const r = (user || getUser() || {}).role;
  return r === "admin" || r === "office_clerk";
}

function isAdminUser(user) {
  return (user || getUser() || {}).role === "admin";
}

function renderShell(active, title) {
  const user = getUser() || {};
  document.body.innerHTML = `
    <div class="app-layout">
      <aside class="sidebar">
        <div class="brand">材料协同办理</div>
        <nav>
          <a href="/index.html" class="${active === "dashboard" ? "active" : ""}">工作台</a>
          <a href="/oa_items.html" class="${active === "oa" ? "active" : ""}">OA 公文池</a>
          <a href="/items.html" class="${active === "items" ? "active" : ""}">事项列表</a>
          <a href="/item_form.html" class="${active === "new" ? "active" : ""}">新建事项</a>
          <a href="/settings.html" class="${active === "settings" ? "active" : ""}">系统设置</a>
        </nav>
        <div class="side-foot">内网办公 · MVP v1.0</div>
      </aside>
      <div class="main-wrap">
        <header class="topbar">
          <div class="page-title">${escapeHtml(title)}</div>
          <div class="user-area">
            <span>${escapeHtml(user.display_name || user.username || "")}（${roleLabel(user.role)}）</span>
            <button type="button" onclick="logout()">退出</button>
          </div>
        </header>
        <main class="content" id="app-content"></main>
      </div>
    </div>
    <div class="toast" id="toast"></div>
  `;
  return document.getElementById("app-content");
}

function fileSize(n) {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
}

function qs(name) {
  return new URLSearchParams(location.search).get(name);
}
