import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { isAdminUser, useAuth } from "../store";
import type { ProjectMemory, ProjectNote } from "../types";
import ModelConfigTab, { loadModelConfig, type LlmProviderRow, type ModelProfileRow } from "../components/ModelConfigTab";
import AppearanceTab from "../components/AppearanceTab";
import SkillsTab from "../components/SkillsTab";
import SystemLogs from "../components/SystemLogs";
import ProfileTab from "../components/ProfileTab";
import AssistantTab from "../components/AssistantTab";
import StorageTab from "../components/StorageTab";
import SearchSelect from "../components/SearchSelect";

const ROLE_NAMES: Record<string, string> = {
  main: "助手",
};

/** 推理强度档位的显示名（与后端 REASONING_EFFORTS 对齐；空=端点默认） */
const EFFORT_LABELS: Record<string, string> = {
  none: "None", minimal: "Minimal", low: "Low", medium: "Medium", high: "High",
};

/** 后端时间是 UTC 裸串（`2026-10-30 06:06:12+00:00`）。直接 slice 会既带 +00:00 又差 8 小时，
 *  这里按本地时区格式化成「年月日 时分」；旧的无时区串按 UTC 归一化。 */
function localStamp(iso: string): string {
  const s = String(iso || "").replace(" ", "T");
  if (!s) return "";
  const d = new Date(/[zZ+]|-\d{2}:?\d{2}$/.test(s) ? s : s + "Z");
  if (isNaN(d.getTime())) return String(iso).slice(0, 16).replace("T", " ");
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

type Agent = {
  key: string;
  system_prompt: string;
  model_profile_key: string | null;
  params_json: Record<string, any>;
  tools: string[];
  is_default: boolean;
  /** 项目作用域下：true = 该角色在本项目没有覆盖行，展示的是团队默认/出厂值 */
  inherited?: boolean;
  prompt_inherited?: boolean;
  model_inherited?: boolean;
  project_id?: string | null;
};
type Profile = ModelProfileRow;
export type ConfigTab = "profile" | "assistant" | "appearance" | "skills" | "agents" | "profiles" | "memory" | "mcp" | "storage" | "logs";

/** 「个人设置」是弹窗（点头像进入）；「设置」是独立菜单页（顶栏进入，仅管理员），
 *  左侧分类 + 右侧内容，和对话/团队/用量一样占满内容区。 */
export default function Config({ onClose, initialTab, mode = "personal", page = false }: {
  onClose?: () => void;
  initialTab?: ConfigTab;
  mode?: "personal" | "team";
  /** true = 作为菜单页渲染（不覆盖、不居中、不随 Esc 关闭） */
  page?: boolean;
}) {
  const currentTeamId = useAuth((s) => s.currentTeamId);
  const isAdmin = useAuth((s) => isAdminUser(s.user));
  const effectiveMode = mode === "team" && isAdmin ? "team" : "personal";
  const [tab, setTab] = useState<ConfigTab>(
    initialTab || (effectiveMode === "team" ? "profiles" : "profile"));
  const [agents, setAgents] = useState<Agent[]>([]);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [llmProviders, setLlmProviders] = useState<LlmProviderRow[]>([]);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [msg, setMsg] = useState("");
  // 作用范围："" = 团队默认；非空 = 该项目专属（提示词 / 参数 / 对话模型都跟随它）
  // 带上 model_profile_key：项目作用域的「对话模型」就是项目绑定，直接在这里改。
  const [projects, setProjects] = useState<{ id: string; name: string; model_profile_key?: string | null }[]>([]);
  const [scopeProject, setScopeProject] = useState("");

  function reloadProjects() {
    if (!currentTeamId || !isAdmin) return Promise.resolve();
    return api.projects(currentTeamId).then((ps: any[]) => {
      setProjects((ps || []).map((p) => ({
        id: p.id, name: p.name, model_profile_key: p.model_profile_key || null,
      })));
    }).catch(() => {});
  }
  useEffect(() => { void reloadProjects(); }, [currentTeamId, isAdmin]); // eslint-disable-line react-hooks/exhaustive-deps

  function refresh() {
    if (!isAdmin) return;
    api.agents(scopeProject).then((r) => setAgents(r.agents)).catch(() => {});
    loadModelConfig(scopeProject).then((r) => {
      setProfiles(r.profiles);
      setLlmProviders(r.providers);
    }).catch(() => {});
  }
  useEffect(refresh, [currentTeamId, isAdmin, scopeProject]);

  useEffect(() => {
    if (!onClose || page) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, page]);

  async function saveAgent(a: Agent) {
    setMsg("");
    try {
      const body: any = { system_prompt: draft, project_id: scopeProject || null };
      // 项目里模型仍是继承态时不要把它固化成覆盖（否则团队换模型本项目不跟）
      if (!scopeProject || !a.model_inherited) body.model_profile_key = a.model_profile_key ?? "";
      await api.updateAgent(a.key, body);
      setEditing(null);
      refresh();
      setMsg("已保存");
    } catch (e: any) {
      setMsg(e?.message || "保存失败");
    }
  }

  async function bindModel(a: Agent, key: string) {
    // 只传要改的字段：带上整段 system_prompt 会把「继承团队默认」变成一份显式拷贝
    await api.updateAgent(a.key, {
      model_profile_key: key,
      project_id: scopeProject || null,
    });
    refresh();
  }

  /** 在「对话模型」下拉里直接选「供应商 · 模型」时调用：先确保该模型有对应档位
   *  （同供应商+同模型已建档位就复用，否则后端自动建档位），再绑到 main。
   *  这样「加完供应商 + 拉取模型」就能直接在这里选模型对话，不必再手工建档位——
   *  以前下拉只列已有档位，新加的供应商模型一个都不出现，看着像「无法配置模型」。 */
  async function bindProviderModel(a: Agent, providerId: string, model: string) {
    setMsg("");
    try {
      const r: any = await api.autoModelProfile(providerId, model, scopeProject);
      const key = r?.key || "";
      if (!key) { setMsg("未能创建模型档位"); return; }
      await api.updateAgent(a.key, {
        model_profile_key: key,
        project_id: scopeProject || null,
      });
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "选择模型失败");
    }
  }

  /** 项目作用域：对话模型写到项目的唯一绑定（projects.model_profile_key），
   *  运行时优先级最高。以前这项只在「团队 → 项目与对话模型 → 配置」里能改，
   *  这里只读展示，导致管理员不知道该信哪边；现在统一在这里设置。 */
  async function bindProjectModel(key: string) {
    if (!scopeProject) return;
    setMsg("");
    try {
      await api.setProjectModel(scopeProject, key || null);
      await reloadProjects();
      refresh();
      setMsg(key ? "已固定该项目的对话模型" : "已取消项目固定模型，回退到助手默认档位");
    } catch (e: any) {
      setMsg(e?.message || "保存项目模型失败");
    }
  }

  async function bindProjectProviderModel(providerId: string, model: string) {
    setMsg("");
    try {
      const r: any = await api.autoModelProfile(providerId, model, scopeProject);
      const key = r?.key || "";
      if (!key) { setMsg("未能创建模型档位"); return; }
      await bindProjectModel(key);
    } catch (e: any) {
      setMsg(e?.message || "选择模型失败");
    }
  }

  /** 默认推理强度（params_json.reasoning_effort）：输入框右下角切换「推理等级」时也写这里。
   *  只传 params_json，避免顺手覆盖 system_prompt / 打上「自定义提示词」标记。 */
  async function bindEffort(a: Agent, key: string) {
    await api.updateAgent(a.key, {
      params_json: { reasoning_effort: key },
      project_id: scopeProject || null,
    });
    refresh();
  }

  /** 恢复默认：团队作用域 = 删除团队行回到出厂词；项目作用域 = 删除项目覆盖、
   *  重新继承团队默认（团队行不受影响）。 */
  async function resetAgentScope(a: Agent) {
    if (scopeProject) {
      await api.dropAgentOverride(a.key, scopeProject);
    } else {
      await api.resetAgent(a.key);
    }
    refresh();
  }

  const ico = (d: ReactNode) => (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
      {d}
    </svg>
  );
  const CATS: { v: typeof tab; label: string; icon: ReactNode }[] = [
    { v: "profile", label: "个人资料", icon: ico(<><circle cx="12" cy="8" r="3.6" /><path d="M4.5 20a7.5 7.5 0 0 1 15 0" /></>) },
    { v: "assistant", label: "助手行为", icon: ico(<><path d="M12 3a5 5 0 0 1 5 5v1a2 2 0 0 1 2 2v1a2 2 0 0 1-2 2h-1v2H8v-2H7a2 2 0 0 1-2-2v-1a2 2 0 0 1 2-2V8a5 5 0 0 1 5-5z" /><path d="M9.5 9h.01M14.5 9h.01M9 13.5h6" /></>) },
    { v: "appearance", label: "外观", icon: ico(<><circle cx="12" cy="12" r="9" /><path d="M8 14s1.4 2 4 2 4-2 4-2" /><path d="M9 9h.01M15 9h.01" /></>) },
    { v: "skills", label: "技能", icon: ico(<><circle cx="12" cy="12" r="4" /><path d="M12 3v3M12 18v3M3 12h3M18 12h3" /></>) },
    { v: "agents", label: "智能体", icon: ico(<><rect x="4" y="7" width="16" height="12" rx="2" /><path d="M12 3v4M9 12h.01M15 12h.01M9.5 16h5" /></>) },
    { v: "profiles", label: "模型配置", icon: ico(<><path d="M4 8h9M19 8h1M4 16h3M13 16h7" /><circle cx="16" cy="8" r="2.2" /><circle cx="10" cy="16" r="2.2" /></>) },
    { v: "mcp", label: "MCP 服务器", icon: ico(<><rect x="3" y="4" width="18" height="7" rx="2" /><rect x="3" y="13" width="18" height="7" rx="2" /><path d="M7 7.5h.01M7 16.5h.01" /></>) },
    { v: "memory", label: "项目记忆", icon: ico(<><path d="M4 5a2 2 0 0 1 2-2h12v18H6a2 2 0 0 1-2-2z" /><path d="M8 3v18M12 8h4M12 12h4" /></>) },
    { v: "storage", label: "存储空间", icon: ico(<><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5" /><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></>) },
    { v: "logs", label: "系统日志", icon: ico(<><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 8h10M7 12h10M7 16h6" /></>) },
  ];
  // 个人设置只有个人资料与外观；团队设置才是模型/智能体/技能等管理项（仅管理员）。
  // 非管理员误入团队设置时自动降级为个人设置，避免看到管理入口。
  const PERSONAL_TABS = CATS.filter((c) => c.v === "profile" || c.v === "appearance");
  const TEAM_TABS = CATS.filter((c) => c.v !== "profile" && c.v !== "appearance");
  const visibleCats = effectiveMode === "team" ? TEAM_TABS : PERSONAL_TABS;
  useEffect(() => {
    if (!visibleCats.some((c) => c.v === tab)) setTab(visibleCats[0]?.v || "profile");
  }, [effectiveMode, tab]); // eslint-disable-line react-hooks/exhaustive-deps

  // 项目作用域的「对话模型」= projects.model_profile_key（运行时优先级最高），
  // 就在下面这条下拉里直接改，团队页不再提供第二个入口。
  const scopeProjectKey = projects.find((p) => p.id === scopeProject)?.model_profile_key || "";
  const scopeProjectBound = scopeProjectKey
    ? profiles.find((p) => p.key === scopeProjectKey) || null
    : null;

  const panel = (      <div
        className={`bg-white flex flex-col overflow-hidden ${
          page
            ? "h-full w-full rounded-lg border border-slate-200"
            : `h-[86vh] rounded-lg shadow-xl ${
                effectiveMode === "team" ? "w-[min(1180px,94vw)]" : "w-[min(860px,94vw)]"
              }`
        }`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="shrink-0 flex items-center gap-2 px-4 h-12 border-b border-slate-200 bg-slate-50">
          <span className="font-semibold text-slate-700">
            {effectiveMode === "team" ? "设置" : "个人设置"}
          </span>
          <span className="text-[11px] text-slate-400">
            {effectiveMode === "team" ? "团队级配置，仅管理员可改" : "头像 / 名称 / 密码 / 外观"}
          </span>
          {!page && (
            <button
              className="ml-auto p-1 rounded hover:bg-slate-200 text-slate-500"
              onClick={onClose}
              title="关闭设置（Esc）"
              aria-label="关闭设置"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                strokeWidth="2" strokeLinecap="round">
                <path d="M18 6 6 18M6 6l12 12" />
              </svg>
            </button>
          )}
        </div>

        <div className="flex-1 flex overflow-hidden">
          <nav className="w-44 shrink-0 border-r border-slate-200 bg-slate-50 p-2 space-y-1 overflow-auto">
            {visibleCats.map((c) => (
              <button
                key={c.v}
                onClick={() => setTab(c.v)}
                className={`w-full flex items-center gap-2 px-2.5 py-2 rounded text-sm text-left ${
                  tab === c.v
                    ? "bg-brand-50 text-brand-700 font-medium"
                    : "text-slate-600 hover:bg-slate-200"
                }`}
              >
                {c.icon}
                <span>{c.label}</span>
              </button>
            ))}
          </nav>

          <div className="flex-1 overflow-auto p-5">
      {tab === "agents" && (
        <div className="mb-3 flex items-center gap-2 flex-wrap bg-white border border-slate-200 rounded p-2.5">
          <span className="text-xs text-slate-500">作用范围</span>
          <SearchSelect
            className="border rounded px-2 py-1 text-xs w-[240px]"
            value={scopeProject}
            title="选择配置作用范围：团队默认对所有项目生效；选中某个项目后，这里的提示词/参数/对话模型只对该项目生效（未单独设置的项自动继承团队默认）"
            searchPlaceholder="搜索项目…"
            options={[
              { value: "", label: "团队默认（所有项目）" },
              ...projects.map((p) => ({ value: p.id, label: `项目：${p.name}`, keywords: p.name })),
            ]}
            onChange={(v) => { setScopeProject(v); setEditing(null); setMsg(""); }}
          />
          <span className="text-[11px] text-slate-400 flex-1 min-w-[240px] leading-relaxed">
            {scopeProject
              ? "仅影响所选项目：提示词、参数、对话模型都只对这个项目生效；没单独设置的项自动继承团队默认。"
              : "团队级默认配置，所有未单独配置的项目都按这里执行。"}
          </span>
        </div>
      )}

      {msg && <div className="text-xs text-green-700 mb-2">{msg}</div>}

      {tab === "profile" && <ProfileTab />}

      {tab === "assistant" && <AssistantTab />}

      {tab === "appearance" && <AppearanceTab />}

      {tab === "skills" && <SkillsTab />}

      {tab === "memory" && <MemoryTab />}

      {tab === "storage" && <StorageTab />}

      {tab === "mcp" && <McpTab />}

      {tab === "logs" && <SystemLogs open onClose={() => {}} embedded />}

      {tab === "agents" && (
        <div className="space-y-3">
          {agents.map((a) => {
            // 项目作用域：对话模型写项目绑定（运行时同口径）；角色绑定只在项目未固定时兜底。
            const projectBound = scopeProject ? scopeProjectBound : null;
            const roleBound = profiles.find((p) => p.key === a.model_profile_key
              && ((p.project_id || "") === scopeProject)) ||
              profiles.find((p) => p.key === a.model_profile_key) || null;
            const bound = projectBound || roleBound;
            // 对话档位才能当"大脑"：不过滤的话助手能绑上一个出图档位，运行时报模型不存在
            const llmProfiles = profiles.filter((p) => (p.kind || "llm") === "llm");
            // 「对话模型」下拉直接列出「供应商 → 模型目录」，选中即自动建档位并绑定。
            // 以前只列已有档位(llmProfiles)，新加的供应商就算拉到了模型也一个都不出现，
            // 用户以为「配了模型却选不了」。已建档位的模型显示为「已就绪」。
            const modelRows: { pid: string; plabel: string; model: string; key: string }[] = [];
            const seenModel = new Set<string>();
            for (const pv of llmProviders) {
              if (!pv.enabled) continue;
              for (const m of Array.isArray(pv.models) ? pv.models : []) {
                const name = typeof m === "string" ? m : m?.name || "";
                if (!name) continue;
                const sig = pv.id + "\u0000" + name;
                if (seenModel.has(sig)) continue;
                seenModel.add(sig);
                const exist = llmProfiles.find((p) => p.provider_id === pv.id
                  && p.model === name && ((p.project_id || "") === scopeProject)) ||
                  llmProfiles.find((p) => p.provider_id === pv.id && p.model === name);
                modelRows.push({ pid: pv.id, plabel: pv.label, model: name, key: exist?.key || "" });
              }
            }
            return (
            <div key={a.key} className="bg-white rounded border border-slate-200 p-4">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-medium">{ROLE_NAMES[a.key] || a.key}</span>
                <span className="text-[11px] mono text-slate-400">{a.key}</span>
                {a.is_default && <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500">默认</span>}
                {scopeProject && a.inherited && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-sky-50 text-sky-700"
                    title="该项目没有单独配置这个角色，当前展示并执行的是团队默认配置">
                    继承团队默认
                  </span>
                )}
                {scopeProject && !a.inherited && a.prompt_inherited && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500"
                    title="该项目只覆盖了参数，提示词仍跟随团队默认">提示词继承</span>
                )}
                {/* 对话模型：团队作用域 = 角色默认档位；项目作用域 = 项目固定档位
                    （运行时优先级最高）。两个作用域都在这一个下拉里改。 */}
                <div className="ml-auto flex items-center gap-2 text-sm flex-wrap">
                  {(() => {
                    const modelValue = scopeProject ? scopeProjectKey : (a.model_profile_key ?? "");
                    const pick = (key: string) =>
                      (scopeProject ? bindProjectModel(key) : bindModel(a, key));
                    const pickProvider = (pid: string, model: string) =>
                      (scopeProject ? bindProjectProviderModel(pid, model)
                        : bindProviderModel(a, pid, model));
                    return (
                      <label className="text-xs text-slate-500 flex items-center gap-1"
                        title={scopeProject
                          ? "项目固定对话模型：绑定后这个项目里所有对话都用它，运行时以它为准；不固定 = 用团队默认作用域里的助手默认档位"
                          : "团队默认对话模型：未单独绑定模型的项目用它；不绑则该项目没有可用模型"}>
                        对话模型
                        <SearchSelect
                          className="border rounded px-2 py-1 text-xs w-[210px]"
                          value={modelValue}
                          title="列的是各供应商已拉取的模型：选中会自动建档位并绑定"
                          searchPlaceholder="搜索模型…"
                          options={[
                            { value: "", label: scopeProject ? "不固定（用助手默认档位）" : "不绑定（未就绪）" },
                            ...modelRows.map((r) => ({
                              value: r.key || `pm:${r.pid}:${r.model}`,
                              label: `${r.model}${r.key ? "（已就绪）" : ""}`,
                              keywords: `${r.model} ${r.plabel || ""}`,
                              group: r.plabel || "供应商",
                            })),
                            ...(modelValue && bound && !modelRows.some((r) => r.key === modelValue)
                              ? [{
                                  value: modelValue,
                                  label: `${bound.label}${bound.model ? ` · ${bound.model}` : ""}${bound.provider_id ? "（供应商已停用）" : ""}`,
                                }]
                              : []),
                          ]}
                          onChange={(v) => {
                            if (!v) { pick(""); return; }
                            // 已有档位（含历史档位）→ 直接绑
                            if (llmProfiles.some((p) => p.key === v)) { pick(v); return; }
                            // 供应商模型目录里的模型 → 自动建档位再绑
                            if (v.startsWith("pm:")) {
                              const rest = v.slice(3);
                              const i = rest.indexOf(":");
                              pickProvider(rest.slice(0, i), rest.slice(i + 1));
                              return;
                            }
                            pick(v);
                          }}
                        />
                      </label>
                    );
                  })()}
                  {scopeProject && (
                    <span className={`text-[11px] ${projectBound ? "text-emerald-700" : (roleBound && !scopeProjectKey) ? "text-slate-400" : "text-rose-600"}`}>
                      {projectBound
                        ? "项目固定（所有角色）"
                        : scopeProjectKey
                          ? "项目绑定的档位已不存在，请重选"
                          : (roleBound ? "未固定，用助手默认档位" : "未固定，且助手默认档位未绑 → 无法对话")}
                    </span>
                  )}
                  {modelRows.length === 0 && (
                    <span className="text-[11px] text-amber-600">
                      还没有可用模型：先去「模型配置」加供应商并拉取模型
                    </span>
                  )}
                  <label className="text-xs text-slate-500 flex items-center gap-1"
                    title="默认推理强度：输入框右下角切换「推理等级」会写回这里；不指定=走模型端点默认">
                    推理等级
                    <SearchSelect
                      className="border rounded px-2 py-1 text-xs w-[112px]"
                      value={a.params_json?.reasoning_effort ?? ""}
                      title="默认推理强度。输入框右下角切换「推理等级」时也写这里；不指定=走端点默认"
                      searchPlaceholder="搜索…"
                      options={[
                        { value: "", label: "Default" },
                        { value: "none", label: "None" },
                        { value: "minimal", label: "Minimal" },
                        { value: "low", label: "Low" },
                        { value: "medium", label: "Medium" },
                        { value: "high", label: "High" },
                      ]}
                      onChange={(v) => bindEffort(a, v)}
                    />
                  </label>
                  <button
                    className="text-xs px-2 py-1 rounded border border-slate-200 hover:bg-slate-50"
                    onClick={() => { setEditing(editing === a.key ? null : a.key); setDraft(a.system_prompt); }}
                  >
                    {editing === a.key ? "收起" : "编辑 Prompt"}
                  </button>
                  {(scopeProject ? !a.inherited : !a.is_default) && (
                    <button
                      className="text-xs px-2 py-1 rounded border border-slate-200 hover:bg-slate-50 text-red-600"
                      title={scopeProject ? "删除该项目的覆盖，恢复继承团队默认" : "删除团队默认配置，恢复出厂提示词"}
                      onClick={() => resetAgentScope(a)}
                    >
                      {scopeProject ? "恢复继承" : "重置"}
                    </button>
                  )}
                </div>
              </div>
              <div className="mt-2 flex flex-wrap gap-1">
                {a.tools.map((t) => (
                  <span key={t} className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 mono">{t}</span>
                ))}
              </div>
              {/* 三个绑定各自跑在哪个模型/哪个站点上：绑错或供应商停用要能一眼看出来 */}
              <div className="mt-2 text-[11px] text-slate-500 flex flex-wrap items-center gap-x-4 gap-y-1">
                <span>
                  会话：{bound ? (
                    <>
                      <span className="mono text-slate-700">{bound.model || "（未选模型）"}</span>
                      <span className="text-slate-400"> · </span>
                      <span>{bound.provider_label
                        ? `供应商 ${bound.provider_label}`
                        : bound.base_url ? `内联地址 ${bound.base_url}` : "供应商未绑（档位不可用）"}</span>
                      {!bound.provider_id && !bound.base_url && (
                        <span className="ml-1 text-[10px] px-1.5 py-0.5 rounded bg-amber-50 text-amber-700">
                          没有密钥来源，该档位不可用
                        </span>
                      )}
                    </>
                  ) : (
                    <span className="text-rose-600">未绑定（没有可用模型）</span>
                  )}
                  <span className="text-slate-400"> · </span>
                  <span title="默认推理强度（输入框右下角切换会写回这里）">
                    推理：{a.params_json?.reasoning_effort
                      ? <span className="mono text-slate-700">{EFFORT_LABELS[a.params_json.reasoning_effort] || a.params_json.reasoning_effort}</span>
                      : <span className="text-slate-400">端点默认</span>}
                  </span>
                </span>
              </div>
              {editing === a.key && (
                <div className="mt-3">
                  <textarea
                    className="w-full h-40 border rounded p-2 text-xs mono"
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                  />
                  <div className="mt-2 flex gap-2">
                    <button className="ga-btn ga-btn-primary ga-btn-sm" onClick={() => saveAgent(a)}>保存</button>
                    <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={() => setEditing(null)}>取消</button>
                  </div>
                </div>
              )}
            </div>
            );
          })}
        </div>
      )}

      {tab === "profiles" && (
        <ModelConfigTab profiles={profiles} providers={llmProviders} refresh={refresh} />
      )}
          </div>
        </div>
      </div>
  );

  if (page) return <div className="h-full bg-slate-100 p-4">{panel}</div>;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
      onClick={onClose}
    >
      {panel}
    </div>
  );
}

type McpServer = {
  id: string; name: string; transport: string;
  command: string; args: string[]; env: Record<string, string>;
  url: string; enabled: boolean;
};

function McpTab() {
  const currentTeamId = useAuth((s) => s.currentTeamId);
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const [projectId, setProjectId] = useState("");
  const [items, setItems] = useState<McpServer[]>([]);
  const [msg, setMsg] = useState("");
  const [form, setForm] = useState<McpServer>({
    id: "", name: "", transport: "stdio", command: "", args: [],
    env: {}, url: "", enabled: true,
  });
  const [argsText, setArgsText] = useState("");
  const [envText, setEnvText] = useState("");

  useEffect(() => {
    if (!currentTeamId) return;
    api.projects(currentTeamId).then((ps) => {
      const list = ps.map((p: any) => ({ id: p.id, name: p.name }));
      setProjects(list);
      // 进页面默认选中第一个项目，省掉每次手动选
      setProjectId((cur) => cur || list[0]?.id || "");
    }).catch(() => {});
  }, [currentTeamId]);

  function refresh() {
    if (!projectId) return;
    api.mcpServers(projectId).then(setItems).catch(() => {});
  }
  useEffect(refresh, [projectId]);

  function resetForm() {
    setForm({ id: "", name: "", transport: "stdio", command: "", args: [], env: {}, url: "", enabled: true });
    setArgsText(""); setEnvText("");
  }

  async function save() {
    setMsg("");
    let args: string[] = [];
    let env: Record<string, string> = {};
    try {
      if (argsText.trim()) args = JSON.parse(argsText);
      if (envText.trim()) env = JSON.parse(envText);
    } catch {
      setMsg("args / env 必须是合法 JSON");
      return;
    }
    const body = { ...form, args, env };
    try {
      if (form.id) {
        await api.updateMcpServer(projectId, form.id, body);
        setMsg("已更新（下次 run 生效）");
      } else {
        await api.createMcpServer(projectId, body);
        setMsg("已添加（下次 run 生效）");
      }
      resetForm();
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "保存失败");
    }
  }

  function editOne(m: McpServer) {
    setForm({ ...m });
    setArgsText(JSON.stringify(m.args || []));
    setEnvText(JSON.stringify(m.env || {}, null, 0));
  }

  return (
    <div>
      <div className="bg-white rounded border border-slate-200 p-4 mb-4 text-sm">
        <div className="flex items-center gap-2 mb-3">
          <SearchSelect className="border rounded px-2 py-1 text-sm w-[180px]" value={projectId}
            title="选择项目" searchPlaceholder="搜索项目…"
            options={[{ value: "", label: "选择项目" }, ...projects.map((p) => ({ value: p.id, label: p.name, keywords: p.name }))]}
            onChange={(v) => { setProjectId(v); resetForm(); }} />
          <span className="text-[11px] text-slate-400">
            项目级 MCP 服务器：工具以 mcp_名称_工具名 注入各角色。改配置下次 run 生效。
          </span>
        </div>
        {projectId && (
          <div className="grid grid-cols-2 gap-2">
            <input autoComplete="off" className="border rounded px-2 py-1 text-xs" placeholder="名称（如 fs、browser）"
              value={form.name} disabled={!!form.id}
              onChange={(e) => setForm({ ...form, name: e.target.value })} />
            <SearchSelect className="border rounded px-2 py-1 text-xs bg-white w-full" rootClassName="w-full"
              value={form.transport}
              onChange={(v) => setForm({ ...form, transport: v })}
              title="传输方式" searchPlaceholder="搜索传输方式…" panelMinWidth={200}
              options={[
                { value: "stdio", label: "stdio（本地进程）" },
                { value: "http", label: "http（远程 URL）" },
              ]} />
            {form.transport === "stdio" ? (
              <>
                <input autoComplete="off" className="border rounded px-2 py-1 text-xs col-span-2"
                  placeholder='command（如 npx / python）'
                  value={form.command}
                  onChange={(e) => setForm({ ...form, command: e.target.value })} />
                <textarea className="border rounded px-2 py-1 text-xs col-span-2 mono"
                  placeholder='args JSON 数组，如 ["-y", "@modelcontextprotocol/server-fs", "./"]'
                  value={argsText} onChange={(e) => setArgsText(e.target.value)} />
                <textarea className="border rounded px-2 py-1 text-xs col-span-2 mono"
                  placeholder='env JSON 对象，如 {"API_KEY": "xxx"}'
                  value={envText} onChange={(e) => setEnvText(e.target.value)} />
              </>
            ) : (
              <input autoComplete="off" className="border rounded px-2 py-1 text-xs col-span-2" placeholder="MCP 端点 URL"
                value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
            )}
            <label className="flex items-center gap-1.5 text-xs text-slate-600">
              <input autoComplete="off" type="checkbox" checked={form.enabled}
                onChange={(e) => setForm({ ...form, enabled: e.target.checked })} />
              启用
            </label>
            <div className="flex gap-2 justify-end">
              {form.id && (
                <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={resetForm}>取消编辑</button>
              )}
              <button className="ga-btn ga-btn-primary ga-btn-sm" onClick={save}>
                {form.id ? "保存修改" : "添加"}
              </button>
            </div>
          </div>
        )}
        {msg && <div className="text-xs text-green-700 mt-2">{msg}</div>}
      </div>

      {!projectId ? (
        <div className="text-sm text-slate-400">请先选择一个项目。</div>
      ) : items.length === 0 ? (
        <div className="text-sm text-slate-400">
          该项目暂无 MCP 服务器；添加后智能体可在 run 中调用其工具（如文件系统、浏览器、搜索等）。
        </div>
      ) : (
        <div className="space-y-1.5">
          {items.map((m) => (
            <div key={m.id} className="bg-white rounded border border-slate-200 p-3 flex items-start gap-2 text-sm">
              <span className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 mt-0.5 ${
                m.enabled ? "bg-green-100 text-green-700" : "bg-slate-100 text-slate-500"
              }`}>
                {m.enabled ? "启用" : "停用"}
              </span>
              <div className="flex-1 min-w-0">
                <div className="font-medium">{m.name}
                  <span className="text-[11px] text-slate-400 ml-2 mono">{m.transport}</span>
                </div>
                <div className="text-[11px] text-slate-500 mono truncate">
                  {m.transport === "http" ? m.url : `${m.command} ${(m.args || []).join(" ")}`}
                </div>
              </div>
              <button className="ga-btn ga-btn-secondary ga-btn-sm shrink-0"
                onClick={() => editOne(m)}>编辑</button>
              <button className="ga-btn ga-btn-danger ga-btn-sm shrink-0"
                onClick={async () => {
                  if (!confirm(`删除 MCP 服务器 ${m.name}？`)) return;
                  await api.deleteMcpServer(projectId, m.id);
                  refresh();
                }}>删除</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function MemoryCard({ m, onDelete }: { m: ProjectMemory; onDelete: () => void }) {
  const [open, setOpen] = useState(false);
  const tags = (m.tags || "").split(",").map((t) => t.trim()).filter(Boolean);
  const title = (m.title || "").trim() || m.content.split("\n")[0].slice(0, 24);
  const stamp = localStamp(m.updated_at || m.created_at || "");
  return (
    <div className="bg-white rounded-lg border border-slate-200 p-3">
      <div className="flex items-start gap-2">
        <span className={`text-[10px] px-1.5 py-0.5 rounded shrink-0 mt-0.5 ${
          m.kind === "manual" ? "bg-sky-100 text-sky-700" : "bg-slate-100 text-slate-500"
        }`}>
          {m.kind === "manual" ? "手动" : "自动"}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="font-medium text-slate-800 text-sm">{title}</span>
            {tags.map((t) => (
              <span key={t} className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">{t}</span>
            ))}
          </div>
          <div className={`mt-1 text-[12.5px] leading-relaxed text-slate-600 whitespace-pre-wrap break-words ${open ? "" : "line-clamp-3"}`}>
            {m.content}
          </div>
          <div className="mt-1 flex items-center gap-2 flex-wrap text-[11px] text-slate-400">
            <span>{stamp}</span>
            {m.source_run_id && <span>· 来源 run {m.source_run_id.slice(0, 8)}</span>}
            <button className="text-slate-500 hover:text-slate-700 underline underline-offset-2"
              onClick={() => setOpen((v) => !v)}>
              {open ? "收起" : "展开正文"}
            </button>
          </div>
        </div>
        <button className="text-xs text-red-600 shrink-0" onClick={onDelete}>删除</button>
      </div>
    </div>
  );
}

function MemoryTab() {
  const currentTeamId = useAuth((s) => s.currentTeamId);
  const [projects, setProjects] = useState<{ id: string; name: string }[]>([]);
  const [projectId, setProjectId] = useState("");
  const [items, setItems] = useState<ProjectMemory[]>([]);
  const [notes, setNotes] = useState<ProjectNote[]>([]);
  const [draft, setDraft] = useState("");
  const [msg, setMsg] = useState("");
  const [merging, setMerging] = useState(false);

  useEffect(() => {
    if (!currentTeamId) return;
    api.projects(currentTeamId).then((ps) => {
      const list = ps.map((p: any) => ({ id: p.id, name: p.name }));
      setProjects(list);
      // 进页面默认选中第一个项目，省掉每次手动选
      setProjectId((cur) => cur || list[0]?.id || "");
    }).catch(() => {});
  }, [currentTeamId]);

  function refresh() {
    if (!projectId) return;
    api.memories(projectId).then(setItems).catch(() => {});
    api.projectNotes(projectId).then((r: any) => setNotes(r || [])).catch(() => setNotes([]));
  }
  useEffect(refresh, [projectId]);

  async function add() {
    if (!draft.trim()) return;
    setMsg("");
    try {
      await api.addMemory(projectId, draft.trim());
      setDraft("");
      setMsg("已添加记忆");
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "添加失败");
    }
  }

  async function merge() {
    if (!projectId || merging) return;
    setMsg("");
    setMerging(true);
    try {
      const r: any = await api.mergeMemories(projectId);
      setMsg(r.used_ai
        ? `融合完成：${r.before} 条 → ${r.after} 条（模型已合并全部记忆）`
        : `融合完成：${r.before} 条 → ${r.after} 条（模型不可用，已按本地规则合并为一份）`);
      refresh();
    } catch (e: any) {
      setMsg(e?.message || "融合失败");
    } finally {
      setMerging(false);
    }
  }

  const sortedItems = [...items].sort((a, b) => {
    const am = a.kind === "manual" ? 0 : 1;
    const bm = b.kind === "manual" ? 0 : 1;
    if (am !== bm) return am - bm;
    return (b.updated_at || b.created_at).localeCompare(a.updated_at || a.created_at);
  });

  return (
    <div>
      <div className="bg-white rounded border border-slate-200 p-4 mb-4 text-sm">
        <div className="flex items-center gap-2 mb-2 flex-wrap">
          <SearchSelect
            className="border rounded px-2 py-1 text-sm w-[180px]"
            value={projectId}
            title="选择项目"
            searchPlaceholder="搜索项目…"
            options={[{ value: "", label: "选择项目" }, ...projects.map((p) => ({ value: p.id, label: p.name, keywords: p.name }))]}
            onChange={setProjectId}
          />
          <span className="text-[11px] text-slate-400 flex-1 min-w-[260px] leading-relaxed">
            每轮 run 收尾由模型把本次结果与历史对话的记忆融合成结构化条目（重复合并、冲突以新为准，上限 12 条）；
            对话时只注入标题索引，需要细节由助手用 memory_read 按需读取。手动约定不参与改写。
          </span>
          <button
            className="ga-btn ga-btn-secondary ga-btn-sm shrink-0"
            disabled={!projectId || merging}
            onClick={merge}
            title="立即让模型融合一次：合并跨对话的重复记忆。不想要这步就等 run 收尾自动融合"
          >
            {merging ? "融合中…" : "AI 融合"}
          </button>
        </div>
        {projectId && (
          <div className="flex gap-2">
            <input autoComplete="off"
              className="flex-1 border rounded px-2 py-1 text-xs"
              placeholder="手动补充一条项目记忆，如：美术风格为像素风、配色以深蓝为主"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && add()}
            />
            <button className="ga-btn ga-btn-primary ga-btn-sm" onClick={add}>
              添加
            </button>
          </div>
        )}
        {msg && <div className="text-xs text-green-700 mt-2">{msg}</div>}
      </div>

      {projectId && notes.length > 0 && (
        <div className="mb-4">
          <div className="text-xs text-slate-500 mb-1.5">
            改动经验（run 收尾判断「值得记」才写，落在工作区 .trace/notes/ 的分类 md；后续 run 注入的是条目标题索引，智能体按标题自己去读正文）。
            每类满 40 条后按「已失效最先淘汰 → 其余丢最旧 → 钉住的永不淘汰」处理；「标失效」是留痕，不是删除。
          </div>
          <div className="space-y-1.5">
            {notes.map((n) => (
              <div key={`${n.category}:${n.key}`}
                   className={`bg-white rounded border p-3 flex items-start gap-2 text-sm ${
                     n.stale ? "border-slate-200 opacity-60" : "border-emerald-100"}`}>
                <span className="text-[10px] px-1.5 py-0.5 rounded shrink-0 mt-0.5 bg-emerald-50 text-emerald-700">
                  {n.category_cn}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-slate-700 break-words">
                    {n.title}
                    {n.keep && (
                      <span className="ml-1.5 text-[10px] px-1 py-0.5 rounded bg-amber-50 text-amber-700 align-middle">
                        钉住
                      </span>
                    )}
                    {n.stale && (
                      <span className="ml-1.5 text-[10px] px-1 py-0.5 rounded bg-slate-100 text-slate-500 align-middle">
                        已失效
                      </span>
                    )}
                  </div>
                  <div className="whitespace-pre-wrap break-words text-[12.5px] text-slate-600 mt-0.5">{n.detail}</div>
                  {n.files.length > 0 && (
                    <div className="text-[11px] text-slate-400 mt-1 break-words">涉及：{n.files.join("、")}</div>
                  )}
                  <div className="text-[11px] text-slate-400 mt-0.5">
                    {n.when}{n.run_id && ` · run ${n.run_id.slice(0, 8)}`}
                  </div>
                </div>
                <div className="flex flex-col items-end gap-1 shrink-0">
                  <button
                    className="text-xs text-amber-700"
                    title={n.keep
                      ? "取消钉住：不再豁免淘汰"
                      : "钉住：每类满了 40 条时优先淘汰最旧的，钉住的永不淘汰"}
                    onClick={async () => {
                      await api.setProjectNoteFlag(projectId, n.category, n.key, { keep: !n.keep });
                      refresh();
                    }}
                  >
                    {n.keep ? "取消钉住" : "钉住"}
                  </button>
                  <button
                    className="text-xs text-slate-500"
                    title={n.stale
                      ? "恢复：重新参与注入与正常淘汰"
                      : "标失效：不再注入上下文、优先淘汰（留痕，不删除）"}
                    onClick={async () => {
                      await api.setProjectNoteFlag(projectId, n.category, n.key, { stale: !n.stale });
                      refresh();
                    }}
                  >
                    {n.stale ? "恢复" : "标失效"}
                  </button>
                  <button
                    className="text-xs text-red-600"
                    onClick={async () => {
                      if (!confirm("删除这条改动经验？")) return;
                      await api.deleteProjectNote(projectId, n.category, n.key);
                      refresh();
                    }}
                  >
                    删除
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {!projectId ? (
        <div className="text-sm text-slate-400">请先选择一个项目。</div>
      ) : items.length === 0 ? (
        <div className="text-sm text-slate-400">该项目暂无记忆；完成一次需求后要点会自动沉淀到这里。</div>
      ) : (
        <div>
          <div className="text-xs text-slate-500 mb-1.5">
            结构化记忆 · 共 {items.length} 条（自动 {items.filter((m) => m.kind !== "manual").length}
            {" / "}手动 {items.filter((m) => m.kind === "manual").length}）
          </div>
          <div className="space-y-1.5">
            {sortedItems.map((m) => (
              <MemoryCard
                key={m.id}
                m={m}
                onDelete={async () => {
                  if (!confirm("删除这条记忆？")) return;
                  await api.deleteMemory(projectId, m.id);
                  refresh();
                }}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
