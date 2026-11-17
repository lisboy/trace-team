import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { ProjectSummary, Session } from "../types";
import SearchSelect from "./SearchSelect";

/** 会话列表里的相对时间：刚刚 / N分钟前 / N小时前 / N天前。 */
function relTime(iso?: string): string {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (!t || Number.isNaN(t)) return "";
  const diff = Date.now() - t;
  if (diff < 60_000) return "刚刚";
  const m = Math.floor(diff / 60_000);
  if (m < 60) return `${m}分钟前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}小时前`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}天前`;
  return new Date(t).toLocaleDateString();
}

function SessionRow({
  s, activeId, archivedMode, onPick, onArchiveToggle, onRemove,
}: {
  s: Session;
  activeId: string | null;
  archivedMode: boolean;
  onPick: (s: Session) => void;
  onArchiveToggle: (s: Session, status: "active" | "archived") => void;
  onRemove: (s: Session) => void;
}) {
  const active = s.id === activeId && !archivedMode;
  return (
    <div className={`group relative rounded ${active ? "bg-brand-50" : "hover:bg-slate-50"}`}>
      <button
        onClick={() => onPick(s)}
        className="w-full text-left text-[12.5px] pl-2 pr-16 py-1.5 flex items-center gap-1.5"
        title={`${s.title || "（无标题）"} · ${relTime(s.created_at)}`}
      >
        {s.running && (
          <svg className="shrink-0 animate-spin text-brand-600" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" aria-label="正在执行">
            <path d="M21 12a9 9 0 1 1-6.22-8.56" />
          </svg>
        )}
        <span className="truncate flex-1">{s.title || "（无标题）"}</span>
        <span className="shrink-0 text-[10px] text-slate-400 group-hover:hidden">{relTime(s.last_active || s.created_at)}</span>
      </button>
      <div className="absolute right-1 top-1/2 -translate-y-1/2 hidden group-hover:flex items-center gap-0.5">
        {archivedMode ? (
          <button
            className="text-[10px] px-1 py-0.5 rounded bg-slate-100 hover:bg-green-100 text-slate-600"
            title="恢复会话"
            onClick={(e) => { e.stopPropagation(); onArchiveToggle(s, "active"); }}
          >
            恢复
          </button>
        ) : (
          <button
            className="text-[10px] px-1 py-0.5 rounded bg-slate-100 hover:bg-amber-100 text-slate-600"
            title="归档会话（可稍后在「已归档」中恢复）"
            onClick={(e) => { e.stopPropagation(); onArchiveToggle(s, "archived"); }}
          >
            归档
          </button>
        )}
        <button
          className="text-[10px] px-1 py-0.5 rounded bg-slate-100 hover:bg-red-100 text-red-600"
          title="删除会话"
          onClick={(e) => { e.stopPropagation(); onRemove(s); }}
        >
          删除
        </button>
      </div>
    </div>
  );
}

/**
 * 左侧工作区（团队版）：顶部「项目下拉」+ 当前项目的会话列表。
 * 开发/需求只看到被管理员分派的项目；切换项目即切换会话上下文。
 */
export default function SessionList({
  teamId,
  projects,
  projectId,
  activeId,
  onPick,
  onPickProject,
  activeIsArchived = false,
}: {
  teamId: string;
  projects: ProjectSummary[];
  projectId: string;
  activeId: string | null;
  onPick: (projectId: string, s: Session) => void;
  onPickProject: (projectId: string) => void;
  /** 兼容旧调用：项目增删统一在「团队」页完成，这里不再主动回调 */
  onProjectsChanged?: () => void;
  activeIsArchived?: boolean;
}) {
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [archivedMode, setArchivedMode] = useState(false);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [msg, setMsg] = useState("");

  // 只拉当前项目：2.5s 静默刷新 + ga-sessions-changed 事件立即刷新，
  // running 转圈和标题变化都能及时亮/灭。
  useEffect(() => {
    if (!teamId || !projectId) {
      setSessions([]);
      return;
    }
    let dead = false;
    let inFlight = false;
    async function tick() {
      if (inFlight) return;
      inFlight = true;
      try {
        const rows = await api.sessions(projectId, archivedMode);
        if (!dead) setSessions(Array.isArray(rows) ? rows : []);
      } catch {
        /* 下一轮轮询重试 */
      } finally {
        inFlight = false;
      }
    }
    tick();
    const t = window.setInterval(tick, 2500);
    const onChanged = () => tick();
    window.addEventListener("ga-sessions-changed", onChanged);
    window.addEventListener("focus", onChanged);
    return () => {
      dead = true;
      window.clearInterval(t);
      window.removeEventListener("ga-sessions-changed", onChanged);
      window.removeEventListener("focus", onChanged);
    };
  }, [projectId, archivedMode, teamId]);

  const q = query.trim().toLowerCase();
  const visible = useMemo(
    () => sessions.filter((s) => !q || (s.title || "（无标题）").toLowerCase().includes(q)),
    [sessions, q],
  );

  async function setStatus(s: Session, status: "active" | "archived") {
    setMsg("");
    try {
      await api.updateSession(s.id, { status });
      window.dispatchEvent(new Event("ga-sessions-changed"));
    } catch (e: any) {
      setMsg(e?.message || "操作失败");
    }
  }

  async function remove(s: Session) {
    if (!confirm(`删除会话「${s.title || "（无标题）"}」？删除后不再显示（消息记录保留）。`)) return;
    setMsg("");
    try {
      await api.deleteSession(s.id);
      if (s.id === activeId) onPick(projectId, { id: "__new__", title: "新会话", created_at: "" });
      else window.dispatchEvent(new Event("ga-sessions-changed"));
    } catch (e: any) {
      setMsg(e?.message || "删除失败");
    }
  }

  function newSession() {
    if (!projectId) return;
    onPick(projectId, { id: "__new__", title: "新会话", created_at: "" });
  }

  return (
    <div className="ga-session-pane w-64 shrink-0 border-r border-slate-200 bg-white flex flex-col">
      <div className="p-2 border-b border-slate-200 space-y-2">
        <button className="ga-btn ga-btn-primary w-full flex items-center justify-center gap-1.5" onClick={newSession} disabled={!projectId}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
            <path d="M12 5v14M5 12h14" />
          </svg>
          新会话
        </button>

        <div className="flex items-center gap-1">
          <SearchSelect
            className="w-full rounded border border-slate-200 bg-white px-2 py-1.5 text-xs"
            rootClassName="flex-1 min-w-0"
            value={projectId}
            title="切换项目（只列出你有权限的项目）"
            searchPlaceholder="搜索项目…"
            panelMinWidth={200}
            onChange={(v) => { setMsg(""); setQuery(""); onPickProject(v); }}
            options={projects.length ? projects.map((p) => ({
              value: p.id, label: p.name, keywords: p.name,
            })) : [{ value: "", label: "暂无项目" }]}
          />
          <button
            className={`h-7 w-7 shrink-0 flex items-center justify-center rounded hover:bg-slate-100 ${searchOpen ? "text-brand-600" : "text-slate-400"}`}
            title="搜索会话"
            aria-label="搜索会话"
            onClick={() => setSearchOpen((v) => !v)}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.5-3.5" />
            </svg>
          </button>
          <button
            className={`h-7 w-7 shrink-0 flex items-center justify-center rounded hover:bg-slate-100 ${archivedMode ? "text-brand-600 bg-brand-50" : "text-slate-400"}`}
            title={archivedMode ? "返回活跃会话" : "查看已归档会话"}
            aria-label={archivedMode ? "返回活跃会话" : "查看已归档会话"}
            onClick={() => setArchivedMode((v) => !v)}
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 6h18M7 12h10M10 18h4" />
            </svg>
          </button>
        </div>

        {searchOpen && (
          <input autoComplete="off"
            autoFocus
            className="w-full rounded border border-slate-200 px-2 py-1 text-xs outline-none focus:border-brand-400"
            placeholder="搜索当前项目的会话…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        )}
        {archivedMode && (
          <div className="rounded bg-amber-50 px-2 py-1 text-[10.5px] text-amber-700">
            正在查看已归档会话
          </div>
        )}
      </div>

      <div className="ga-list-tint flex-1 overflow-auto p-1.5 space-y-0.5">
        {msg && <div className="text-[11px] text-red-600 px-1">{msg}</div>}
        {!projects.length && (
          <div className="text-xs text-slate-400 px-1 py-2 leading-relaxed">
            还没有可用的项目。请联系管理员在「团队」页创建项目并分派给你。
          </div>
        )}
        {projects.length > 0 && visible.length === 0 && (
          <div className="text-[11px] text-slate-400 px-2 py-1">
            {q ? "没有匹配的会话" : archivedMode ? "暂无已归档会话" : "暂无会话，点上方「新会话」开始"}
          </div>
        )}
        {visible.map((s) => (
          <SessionRow key={s.id} s={s} activeId={activeId} archivedMode={archivedMode}
            onPick={(row) => onPick(projectId, row)} onArchiveToggle={setStatus} onRemove={remove} />
        ))}
        {activeIsArchived && !archivedMode && (
          <div className="text-[11px] text-amber-600 px-1 pt-1">当前打开的会话已归档，可在「已归档」中恢复。</div>
        )}
      </div>
    </div>
  );
}
