import { useAuth } from "./store";

const BASE = "/api";
const TOKEN_KEY = "ga_token";

class ApiError extends Error {
  status: number;
  constructor(message: string, status = 0) {
    super(message);
    this.status = status;
  }
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* 隐私模式：本次会话内仍可用（内存里的 store.token 才是事实源） */
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

/** 二进制 / 下载类请求用的头（<img> 带不了自定义头，那些地方走 blob 或签名 URL）。 */
function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const teamId = useAuth.getState().currentTeamId;
  const headers: Record<string, string> = { ...extra };
  if (teamId) headers["X-Team-Id"] = teamId;
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

interface RequestOpts extends RequestInit {
  /** 登录接口 401 是「密码错误」，不该触发全局退出 */
  handle401?: boolean;
}

async function request<T = any>(path: string, opts: RequestOpts = {}): Promise<T> {
  const { handle401 = true, ...init } = opts;
  const teamId = useAuth.getState().currentTeamId;
  const headers = new Headers(init.headers);
  if (teamId) headers.set("X-Team-Id", teamId);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(BASE + path, { ...init, headers });
  if (!res.ok) {
    let msg = `${res.status}`;
    let detail: any = null;
    try {
      const j = await res.json();
      detail = j?.detail;
      msg = detail?.message || detail || msg;
    } catch {
      /* ignore */
    }
    if (detail?.code === "password_change_required") {
      useAuth.getState().setMustChangePassword(true);
      window.dispatchEvent(new Event("ga-password-required"));
    } else if (res.status === 401 && handle401 && getToken()) {
      useAuth.getState().clearAuth();
      window.dispatchEvent(new Event("ga-unauthorized"));
    }
    throw new ApiError(String(msg), res.status);
  }
  if (res.status === 204) return null as T;
  const ct = res.headers.get("content-type") || "";
  return (ct.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export const api = {
  // ---- 登录 / 个人资料 ----
  login: (username: string, password: string) =>
    request<any>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
      handle401: false,
    }),
  logout: () => request("/auth/logout", { method: "POST", handle401: false }),
  changePassword: (oldPassword: string, newPassword: string) =>
    request("/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),
  me: () => request("/auth/me"),
  updateMe: (body: { display_name?: string; avatar?: string | null }) =>
    request<{ ok: boolean; user: any }>("/auth/me", { method: "PATCH", body: JSON.stringify(body) }),
  uploadAvatar: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<{ ok: boolean; avatar: string }>("/auth/me/avatar", { method: "POST", body: fd });
  },

  // ---- 团队管理（仅管理员）----
  teamMembers: () => request("/team/members"),
  createMember: (body: {
    username: string; display_name?: string; role: string;
    password?: string; project_ids?: string[];
  }) => request("/team/members", { method: "POST", body: JSON.stringify(body) }),
  updateMember: (id: string, body: { display_name?: string; role?: string; is_active?: boolean }) =>
    request(`/team/members/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  resetMemberPassword: (id: string) =>
    request(`/team/members/${id}/reset-password`, { method: "POST" }),
  assignMemberProjects: (id: string, project_ids: string[]) =>
    request(`/team/members/${id}/projects`, { method: "PUT", body: JSON.stringify({ project_ids }) }),
  teamProjects: () => request("/team/projects"),
  setProjectModel: (projectId: string, key: string | null) =>
    request(`/team/projects/${projectId}/model`, {
      method: "PUT", body: JSON.stringify({ model_profile_key: key }),
    }),
  /** 项目代码仓库：Git（地址/分支/令牌）或 SVN（地址/账号/密码） */
  setProjectVcs: (projectId: string, body: {
    vcs_type: "none" | "git" | "svn";
    vcs_remote?: string; vcs_branch?: string; vcs_username?: string;
    vcs_secret?: string | null;
  }) => request(`/team/projects/${projectId}/vcs`, {
    method: "PUT", body: JSON.stringify(body),
  }),
  /** 项目级并发上限：同时执行的 run 最多几个（0 = 不限） */
  setProjectConcurrency: (projectId: string, maxConcurrency: number) =>
    request(`/team/projects/${projectId}/concurrency`, {
      method: "PUT", body: JSON.stringify({ max_concurrency: maxConcurrency }),
    }),
  /** 按当前配置立刻同步一次（验证地址/令牌是否可用，也会真的拉取） */
  testProjectVcs: (projectId: string) =>
    request(`/team/projects/${projectId}/vcs/test`, { method: "POST" }),
  teamModelProfiles: () => request("/team/model-profiles"),

  // ---- teams / projects ----
  teams: () => request("/teams"),
  projects: (teamId: string, archived = false) =>
    request(`/teams/${teamId}/projects?archived=${archived}`),
  archiveProject: (id: string) =>
    request(`/projects/${id}/archive`, { method: "POST" }),
  restoreProject: (id: string) =>
    request(`/projects/${id}/restore`, { method: "POST" }),
  project: (id: string) => request(`/projects/${id}`),
  /** 在本机文件管理器里打开项目工作区目录（仅 lite 模式） */
  revealProject: (id: string) =>
    request(`/projects/${id}/reveal`, { method: "POST" }),
  createProject: (teamId: string, body: any) =>
    request(`/teams/${teamId}/projects`, { method: "POST", body: JSON.stringify(body) }),
  /** 目录浏览：新建工作区时选文件夹（lite 模式可浏览本机任意目录） */
  fsDirs: (path = "") =>
    request(`/fs/dirs${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  /** 在指定目录下新建空文件夹（内置目录浏览器用；迁移数据要求目标是空目录） */
  fsMkdir: (parent: string, name: string) =>
    request<{ path: string }>("/fs/mkdir", {
      method: "POST",
      body: JSON.stringify({ parent, name }),
    }),

  // ---- sessions / runs ----
  sessions: (projectId: string, archived = false) =>
    request(`/projects/${projectId}/sessions?archived=${archived}`),
  updateSession: (id: string, patch: { status?: "active" | "archived" }) =>
    request(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  deleteSession: (id: string) => request(`/sessions/${id}`, { method: "DELETE" }),
  /** 会话消息。带 limit 时返回 {messages, has_more, oldest}（最近 limit 条 / 更早一页）；
   *  不带 limit 时返回全部消息数组（老行为）。before/before_id=(上页最老一条的 created_at, id)
   *  复合游标 → 取它之前的一页（同一秒并列的多条不会被漏掉）。 */
  messages: (sessionId: string, opts?: { limit?: number; before?: string; before_id?: string }) => {
    const q = new URLSearchParams();
    if (opts?.limit) q.set("limit", String(opts.limit));
    if (opts?.before) q.set("before", opts.before);
    if (opts?.before_id) q.set("before_id", opts.before_id);
    const qs = q.toString();
    return request(`/sessions/${sessionId}/messages${qs ? `?${qs}` : ""}`);
  },
  /** 该会话自己的 run 列表（新→旧）：不受「项目最近 50 条」窗口限制，
   *  老会话也能认出上一次执行是成功/失败，避免聊天页看起来一片空白 */
  sessionRuns: (sessionId: string) => request(`/sessions/${sessionId}/runs`),
  /** 会话级用量：总 token / 缓存命中率 / 生成速度（**只算主对话**，不含子agent） */
  sessionUsage: (sessionId: string) => request(`/sessions/${sessionId}/usage`),
  /** 本会话跑过的所有子agent 明细（运行面板「子agent」标签用；与主对话分开统计） */
  sessionSubagents: (sessionId: string) => request(`/sessions/${sessionId}/subagents`),
  /** 上下文统计：请求构成占比、逐轮占用趋势、工具调用分布（runId 为空=整个会话） */
  contextStats: (sessionId: string, runId?: string | null) =>
    request(`/sessions/${sessionId}/context-stats${runId ? `?run_id=${runId}` : ""}`),
  /** 本次运行的逐轮「上下文请求」清单（不含正文，点开某轮再取全文） */
  runTranscripts: (runId: string) => request(`/runs/${runId}/transcripts`),
  /** 单轮往返现场：发给模型什么 + 模型返回了什么 */
  transcriptDetail: (id: string) => request(`/llm-transcripts/${id}`),
  chat: (projectId: string, body: any, idempotencyKey: string) =>
    request(`/projects/${projectId}/chat`, {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  // 聊天附件：先传到项目工作区 .trace/uploads/，chat 时按返回的 path 引用
  uploadProjectFile: (projectId: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request(`/projects/${projectId}/uploads`, { method: "POST", body: fd });
  },
  runs: (projectId: string) => request(`/projects/${projectId}/runs`),
  runDetail: (id: string) => request(`/runs/${id}`),
  cancelRun: (id: string) => request(`/runs/${id}/cancel`, { method: "POST" }),
  // 单独停掉一个子agent（主agent 与其它子agent 继续跑，互不影响）
  cancelTask: (runId: string, taskId: string) =>
    request(`/runs/${runId}/tasks/${taskId}/cancel`, { method: "POST" }),
  // 插话：并进正在执行的那一轮（不取消任务、不新开 run）。图和附件与聊天发送同一套格式。
  // clientMsgId 是幂等键：连点/重试同一条插话时后端只落一条消息。
  // 模型由管理员按项目绑定（projects.model_profile_key），对话里不允许切换。
  steerRun: (
    id: string,
    content: string,
    images: string[] = [],
    files: { path: string; name: string; size: number }[] = [],
    clientMsgId = "",
  ) => request(`/runs/${id}/steer`, {
    method: "POST",
    body: JSON.stringify({ content, images, files, client_msg_id: clientMsgId }),
  }),

  // ---- events / artifacts ----
  /** run 的事件流。taskId 非空时只回这个子agent 自己的事件（弹窗看对话详情用） */
  events: (runId: string, since: number, taskId?: string | null) =>
    request(`/runs/${runId}/events?since=${since}${taskId ? `&task_id=${taskId}` : ""}`),
  /** 计划机制：读取本轮待办清单（组长 update_plan 写入） */
  plan: (runId: string) => request(`/runs/${runId}/plan`),
  /** 提问机制：会话内的问题（待回答 + 已回答记录） */
  questions: (sessionId: string) => request(`/sessions/${sessionId}/questions`),
  /** 提问机制：作答（option_index 0-based，-1=跳过；也可只给 text） */
  answerQuestion: (id: string, body: { option_index?: number | null; text?: string | null }) =>
    request(`/questions/${id}/answer`, { method: "POST", body: JSON.stringify(body) }),
  runDiff: (runId: string) => request(`/runs/${runId}/diff`),
  /** 本次 run 改过哪些文件 + 前后差异（工具层写前快照，不依赖 git） */
  runChanges: (runId: string) => request(`/runs/${runId}/changes`),
  /** 看本次改动里的单个文件：mode=text 当前内容（预览走它），mode=orig 改动前快照 */
  runFileText: (runId: string, path: string, mode: "text" | "orig" = "text") =>
    request(`/runs/${runId}/file?mode=${mode}&path=${encodeURIComponent(path)}`),
  /** 同一文件的原样地址（图片 / HTML / PDF 预览用；HTML 里的相对路径会一起加载） */
  runFileRawUrl: (runId: string, path: string) =>
    `${BASE}/runs/${runId}/raw/${path.split("/").map(encodeURIComponent).join("/")}`,
  /** 在系统文件管理器里定位工作区中的该文件（Windows explorer /select） */
  runReveal: (runId: string, path: string) =>
    request(`/runs/${runId}/reveal`, {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  /** run 过程图稳定地址（工具落图另存到 DATA_DIR/run_shots，收尾清工作区也不丢）。
   *  前端统一走 fetchBlobUrl 取字节再转 blob: URL 喂给 <img>（<img> 带不了自定义头）。 */
  runShotUrl: (ref: string, thumb = 0) => {
    const path = ref.startsWith("run-shot/") ? ref : `run-shot/${ref}`;
    return `${BASE}/run-shots?ref=${encodeURIComponent(path)}${thumb ? `&thumb=${thumb}` : ""}`;
  },
  /** 取二进制并转 blob: URL（取图用；失败抛错由调用方降级）。 */
  fetchBlobUrl: async (url: string): Promise<string> => {
    const res = await fetch(url, { headers: authHeaders() });
    if (!res.ok) throw new ApiError(`${res.status}`, res.status);
    return URL.createObjectURL(await res.blob());
  },
  /** 从中断处继续上次任务（不是把上一条消息重发一次）。
   *  模型跟项目绑定，继续时由后端按 projects.model_profile_key 解析。 */
  continueRun: (runId: string, note?: string) =>
    request(`/runs/${runId}/continue`, {
      method: "POST",
      body: JSON.stringify({ note: note || null }),
    }),
  /** 审阅不通过 → 回滚（paths 为空表示全部回滚） */
  revertRun: (runId: string, paths?: string[]) =>
    request(`/runs/${runId}/revert`, {
      method: "POST",
      body: JSON.stringify({ paths: paths && paths.length ? paths : null }),
    }),
  addComment: (runId: string, body: { body: string; file: string; line: number }) =>
    request(`/runs/${runId}/comments`, { method: "POST", body: JSON.stringify(body) }),
  resolveComment: (id: string) => request(`/comments/${id}/resolve`, { method: "POST" }),
  /** 产物的通用列表接口（按 run / 项目 / 会话过滤）。 */
  artifacts: (params: {
    run_id?: string; project_id?: string; asset_kind?: string; session_id?: string;
    placeholder?: boolean; limit?: number; before?: string; before_id?: string;
  }) => {
    const q = new URLSearchParams();
    if (params.run_id) q.set("run_id", params.run_id);
    if (params.project_id) q.set("project_id", params.project_id);
    if (params.asset_kind && params.asset_kind !== "all") q.set("asset_kind", params.asset_kind);
    if (params.session_id && params.session_id !== "all") q.set("session_id", params.session_id);
    if (params.placeholder) q.set("placeholder", "true");
    if (params.limit) q.set("limit", String(params.limit));
    if (params.before) q.set("before", params.before);
    if (params.before_id) q.set("before_id", params.before_id);
    return request(`/artifacts?${q.toString()}`);
  },
  artifactFile: (artifactId: string, v?: string | number) =>
    `${BASE}/artifacts/${artifactId}/file?rv=2${v ? `&v=${v}` : ""}`,
  /** 素材原图另存到本机：带鉴权头 fetch → blob → 触发浏览器另存，
   *  文件名沿用素材路径的最后一段（直接用 <a download> 拿到的是接口地址和空名字，扩展名也不对）。 */
  downloadArtifact: async (a: { id: string; rel_path?: string; size?: number | null }): Promise<void> => {
    const res = await fetch(
      `${BASE}/artifacts/${a.id}/file?rv=2${a.size ? `&v=${a.size}` : ""}`,
      { headers: authHeaders() });
    if (!res.ok) throw new ApiError(`下载失败 ${res.status}`, res.status);
    const name = (a.rel_path || "").split("/").pop() || a.id;
    const objUrl = URL.createObjectURL(await res.blob());
    const el = document.createElement("a");
    el.href = objUrl;
    el.download = name;
    document.body.appendChild(el);
    el.click();
    el.remove();
    window.setTimeout(() => URL.revokeObjectURL(objUrl), 2000);
  },
  /** 素材原图转 dataURL（复制到剪贴板用：剪贴板不认相对路径，网页里又要能直接渲染出图） */
  artifactDataUrl: async (a: { id: string; size?: number | null }): Promise<string> => {
    const res = await fetch(
      `${BASE}/artifacts/${a.id}/file?rv=2${a.size ? `&v=${a.size}` : ""}`,
      { headers: authHeaders() });
    if (!res.ok) throw new ApiError(`${res.status}`, res.status);
    const blob = await res.blob();
    return new Promise<string>((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(String(r.result || ""));
      r.onerror = () => reject(new Error("读取图片失败"));
      r.readAsDataURL(blob);
    });
  },
  /** 素材缩略图（长边 w）：列表/网格/消息附图都用它，点开大图才加载 artifactFile 原图。
   *  v 参数=内容版本（如重画后的 mtime），变了就破缓存。 */
  artifactThumb: (artifactId: string, w = 320, v?: string | number) =>
    `${BASE}/artifacts/${artifactId}/thumb?w=${w}&rv=2${v ? `&v=${v}` : ""}`,
  // ---- 项目脚本（工作区根目录 run.bat|start.bat / stop.bat）----
  projectScripts: (projectId: string) =>
    request<{ run_script: string | null; stop_script: string | null }>(`/projects/${projectId}/scripts`),
  runProjectScript: (projectId: string, action: "run" | "stop") =>
    request<{ started: boolean; script: string; pid: number }>(`/projects/${projectId}/scripts/${action}`, { method: "POST" }),
  projectFileText: (projectId: string, path: string) =>
    request(`/projects/${projectId}/file?mode=text&path=${encodeURIComponent(path)}`),
  projectFileRaw: (projectId: string, path: string) =>
    `${BASE}/projects/${projectId}/file?mode=raw&path=${encodeURIComponent(path)}`,

  // ---- 消息通道（钉钉 / 企业微信 / 自定义 Webhook，按项目配置）----
  // 绑定已改为「直接填群机器人 Webhook」，不再有扫码 / code 接口。
  channels: (projectId?: string) =>
    request(`/channels${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  channelBind: (channel: string, body: any) =>
    request(`/channels/${channel}/bind`, { method: "POST", body: JSON.stringify(body) }),
  unlinkChannel: (id: string) =>
    request(`/channels/${id}/unlink`, { method: "POST" }),
  patchChannel: (id: string, body: any) =>
    request(`/channels/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  testChannel: (id: string) =>
    request(`/channels/${id}/test`, { method: "POST" }),
  channelMessages: (limit = 30, projectId?: string) =>
    request(`/channels/messages?limit=${limit}${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`),
  saveChannelAppConfig: (body: any) =>
    request("/channels/app-config", { method: "POST", body: JSON.stringify(body) }),

  // ---- 项目记忆 ----
  memories: (projectId: string) => request(`/projects/${projectId}/memory`),
  addMemory: (projectId: string, content: string) =>
    request(`/projects/${projectId}/memory`, { method: "POST", body: JSON.stringify({ content }) }),
  deleteMemory: (projectId: string, id: string) =>
    request(`/projects/${projectId}/memory/${id}`, { method: "DELETE" }),
  /** 立即让模型融合一次项目记忆（跨对话去重合并） */
  mergeMemories: (projectId: string) =>
    request(`/projects/${projectId}/memory/consolidate`, { method: "POST" }),
  // run 收尾判断「值得记」后沉淀的改动经验（工作区 .agent/notes/*.md）
  projectNotes: (projectId: string) => request(`/projects/${projectId}/notes`),
  deleteProjectNote: (projectId: string, category: string, key: string) =>
    request(`/projects/${projectId}/notes/${category}/${key}`, { method: "DELETE" }),
  /** 钉住（keep=true，永不淘汰）/ 标失效（stale=true，不再注入、优先淘汰）；null=不动该标记 */
  setProjectNoteFlag: (projectId: string, category: string, key: string,
                       body: { keep?: boolean | null; stale?: boolean | null }) =>
    request(`/projects/${projectId}/notes/${category}/${key}/flag`,
            { method: "POST", body: JSON.stringify(body) }),

  // ---- mcp servers ----
  mcpServers: (projectId: string) => request(`/projects/${projectId}/mcp`),
  createMcpServer: (projectId: string, body: any) =>
    request(`/projects/${projectId}/mcp`, { method: "POST", body: JSON.stringify(body) }),
  updateMcpServer: (projectId: string, id: string, body: any) =>
    request(`/projects/${projectId}/mcp/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteMcpServer: (projectId: string, id: string) =>
    request(`/projects/${projectId}/mcp/${id}`, { method: "DELETE" }),

  // ---- config ----
  agents: (projectId = "") => request(`/agents${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  updateAgent: (key: string, body: any) =>
    request(`/agents/${key}`, { method: "PUT", body: JSON.stringify(body) }),
  resetAgent: (key: string, projectId = "") =>
    request(`/agents/${key}/reset${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`,
      { method: "POST" }),
  dropAgentOverride: (key: string, projectId: string) =>
    request(`/agents/${key}/override?project_id=${encodeURIComponent(projectId)}`, { method: "DELETE" }),
  modelProfiles: (projectId = "") =>
    request(`/model-profiles${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`),
  /** 选「供应商 → 模型」时复用/创建对话档位，返回 { key, created } */
  autoModelProfile: (providerId: string, model: string, projectId = "") =>
    request("/model-profiles/auto", {
      method: "POST",
      body: JSON.stringify({ provider_id: providerId, model, project_id: projectId || null }),
    }),
  createModelProfile: (body: any) =>
    request("/model-profiles", { method: "POST", body: JSON.stringify(body) }),
  updateModelProfile: (id: string, body: any) =>
    request(`/model-profiles/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteModelProfile: (id: string) =>
    request(`/model-profiles/${id}`, { method: "DELETE" }),
  // ---- LLM 供应商（base_url / key / UA 只在这里配一次，档位选供应商 + 模型）----
  llmProviders: () => request("/llm-providers"),
  createLlmProvider: (body: any) =>
    request("/llm-providers", { method: "POST", body: JSON.stringify(body) }),
  updateLlmProvider: (id: string, body: any) =>
    request(`/llm-providers/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteLlmProvider: (id: string) =>
    request(`/llm-providers/${id}`, { method: "DELETE" }),
  /** 拉模型列表：给了 providerId 就用供应商存着的地址/密钥，并回填到供应商缓存 */
  llmModels: (arg: { providerId?: string; baseUrl?: string; apiKey?: string; protocol?: string }) =>
    request("/llm/models", {
      method: "POST",
      body: JSON.stringify({
        provider_id: arg.providerId || null,
        base_url: arg.baseUrl || "",
        api_key: arg.apiKey || "",
        protocol: arg.protocol || "",
      }),
    }),
  /** 一键测速：对模型列表逐个发短请求，返回每个模型的 tok/s */
  llmSpeedTest: (arg: { providerId?: string; baseUrl?: string; apiKey?: string; protocol?: string; models: string[] }) =>
    request("/llm/speed-test", {
      method: "POST",
      body: JSON.stringify({
        provider_id: arg.providerId || null,
        base_url: arg.baseUrl || "",
        api_key: arg.apiKey || "",
        protocol: arg.protocol || "",
        models: arg.models,
      }),
    }),
  driverSpecs: () => request("/providers/drivers"),
  providers: () => request("/providers"),
  createProvider: (body: any) =>
    request("/providers", { method: "POST", body: JSON.stringify(body) }),
  deleteProvider: (id: string) => request(`/providers/${id}`, { method: "DELETE" }),

  // ---- 用量 / 审计（按当前工作区统计）----
  usage: (days = 30, agent?: string, model?: string,
          projectId?: string, userId?: string) =>
    request(`/usage?days=${days}${agent ? `&agent=${encodeURIComponent(agent)}` : ""}`
      + `${model ? `&model=${encodeURIComponent(model)}` : ""}`
      + `${projectId ? `&project_id=${encodeURIComponent(projectId)}` : ""}`
      + `${userId ? `&user_id=${encodeURIComponent(userId)}` : ""}`),
  audit: (limit = 50, q?: string) =>
    request(`/audit?limit=${limit}${q ? `&q=${encodeURIComponent(q)}` : ""}`),

  // ---- 系统日志（顶栏「系统日志」弹窗）----
  /** 尾部轮询：只取 seq > since 的新行，前端自己拼接 */
  systemLogs: (params: { since?: number; limit?: number; level?: string; q?: string }) => {
    const q = new URLSearchParams();
    q.set("since", String(params.since ?? 0));
    if (params.limit) q.set("limit", String(params.limit));
    if (params.level) q.set("level", params.level);
    if (params.q) q.set("q", params.q);
    return request(`/system/logs?${q.toString()}`);
  },
  /** app.log 末尾若干行（进程刚重启、内存缓冲还空时翻旧账） */
  systemLogFileTail: (tail = 300) => request(`/system/logs/file?tail=${tail}`),
  systemInfo: () => request("/system/info"),
  /** LLM 就绪自检：这一轮到底会不会走真实大模型、用的哪个模型/地址、还差什么 */
  doctorLlm: () => request("/doctor/llm"),
  /** 本机 CPU / 内存 / GPU 瞬时状态（服务端 4s 缓存，前端 5s 一轮就够新） */
  systemMetrics: () => request("/system/metrics"),

  // ---- 技能系统（渐进式披露：清单只带名称+描述，正文按需加载） ----
  skills: (projectId?: string) =>
    request(`/skills${projectId ? `?project_id=${projectId}` : ""}`),
  skillContent: (name: string, projectId?: string) =>
    request(`/skills/${name}/content${projectId ? `?project_id=${projectId}` : ""}`),
  addSkillRoot: (path: string) =>
    request("/skills/roots", { method: "POST", body: JSON.stringify({ path }) }),
  removeSkillRoot: (path: string) =>
    request("/skills/roots", { method: "DELETE", body: JSON.stringify({ path }) }),
  saveGlobalSkill: (body: { name: string; description: string; content: string }) =>
    request("/skills/global", { method: "POST", body: JSON.stringify(body) }),
  deleteGlobalSkill: (name: string) =>
    request(`/skills/global/${name}`, { method: "DELETE" }),

  // ---- 外观偏好 / 壁纸（上传 / Wallpaper Engine） ----
  mePrefs: () => request("/me/prefs"),
  putPrefs: (prefs: Record<string, any>) =>
    request("/me/prefs", { method: "PUT", body: JSON.stringify({ prefs }) }),

  // ---- 助手行为：默认权限作用域 + 指导项 ----
  assistantPolicy: () => request("/assistant-policy"),
  saveAssistantPolicy: (body: { guidance?: any[] }) =>
    request("/assistant-policy", { method: "PUT", body: JSON.stringify(body) }),
  uploadWallpaper: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<{ url: string; kind: "img" | "video"; size: number }>(
      "/me/wallpaper", { method: "POST", body: fd },
    );
  },
  weWallpapers: () => request("/wallpapers/engines"),
  setWeRoot: (path: string) =>
    request("/wallpapers/engines/root", { method: "PUT", body: JSON.stringify({ path }) }),
  /** WE 壁纸文件要鉴权头，浏览器标签带不上 → fetch→blob（见底部 blobUrl） */
  weFileBlob: (path: string) => blobUrl(`/wallpapers/file?path=${encodeURIComponent(path)}`),

  // ---- 数据存储位置（设置 / 存储空间）----
  storageInfo: () => request("/system/storage"),
  storageMigrate: (targetDir: string) =>
    request("/system/storage/migrate", {
      method: "POST",
      body: JSON.stringify({ target_dir: targetDir }),
    }),
  /** 迁移进度（轮询用）：phase = idle|scan|copy|db|done|error */
  storageProgress: () => request("/system/storage/migrate/progress"),
  /** 弹系统原生「选择文件夹」窗口（服务所在机器上）；501 = 弹不出来，前端回退内置浏览器 */
  storagePickDir: (title?: string) =>
    request<{ ok: boolean; path: string; cancelled: boolean }>("/system/storage/pick-dir", {
      method: "POST",
      body: JSON.stringify({ title: title || null }),
    }),

  // ---- 会话导出（zip：conversation.md + session.json）----
  /** 带鉴权头 fetch → blob → 触发浏览器另存；文件名优先取后端 Content-Disposition */
  exportSession: async (sessionId: string, title: string): Promise<void> => {
    const res = await fetch(`${BASE}/sessions/${sessionId}/export`, { headers: authHeaders() });
    // 未知路由会被 SPA 兜底接住返回 200 HTML——下载下来就是个假 zip，必须拦
    const ct = res.headers.get("content-type") || "";
    if (!res.ok || !ct.includes("application/zip")) {
      throw new ApiError(!res.ok
        ? `导出失败 ${res.status}`
        : "导出接口不可用（后端重启后生效）");
    }
    const blob = await res.blob();
    let name = `会话-${(title || sessionId).slice(0, 30)}.zip`;
    const m = (res.headers.get("content-disposition") || "")
      .match(/filename\*=UTF-8''([^;]+)/);
    if (m) name = decodeURIComponent(m[1]);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  },
};

export { ApiError };

/**
 * 取二进制文件并转成 objectURL（图片/音频都走这里）。
 * 为什么不用 <img src> / <audio src> 直接指后端：这些接口要鉴权头，
 * 浏览器原生标签带不上 Authorization，只能先 fetch 再给 blob URL。
 */
async function blobUrl(path: string): Promise<string> {
  const res = await fetch(BASE + path, { headers: authHeaders() });
  if (!res.ok) throw new ApiError(`加载文件失败 ${res.status}`);
  return URL.createObjectURL(await res.blob());
}
