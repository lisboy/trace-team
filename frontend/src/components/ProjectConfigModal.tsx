import { useEffect, useState } from "react";
import { api } from "../api";
import ChannelsTab from "./ChannelsTab";
import SearchSelect from "./SearchSelect";

export type TeamProjectLite = {
  id: string;
  name: string;
  slug: string;
  repo_path: string;
  model_profile_key: string | null;
  model_label?: string | null;
  vcs_type?: "none" | "git" | "svn" | string;
  vcs_remote?: string;
  vcs_branch?: string;
  vcs_username?: string;
  vcs_secret_ready?: boolean;
  max_concurrency?: number;
};

type VcsDraft = {
  type: "none" | "git" | "svn";
  remote: string;
  branch: string;
  username: string;
  secret: string;
  clearSecret: boolean;
};

/**
 * 项目配置：代码仓库（Git/SVN）、并发限制、消息通道集中在一个弹窗里，都是项目级配置，
 * 只有管理员能进来（团队页本身已限管理员）。对话模型不在这里：统一在「设置 → 智能体」
 * 把作用范围切到某个项目后设置，避免同一条设定有两个入口。
 */
export default function ProjectConfigModal({ project, onClose, onSaved }: {
  project: TeamProjectLite;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [vcs, setVcs] = useState<VcsDraft>({
    type: (project.vcs_type as VcsDraft["type"]) || "none",
    remote: project.vcs_remote || "",
    branch: project.vcs_branch || "main",
    username: project.vcs_username || "",
    secret: "",
    clearSecret: false,
  });
  const [secretReady, setSecretReady] = useState(!!project.vcs_secret_ready);
  const [limit, setLimit] = useState<number>(project.max_concurrency ?? 0);
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  async function saveVcs(testAfter: boolean) {
    setBusy(testAfter ? "test" : "vcs"); setErr(""); setMsg("");
    try {
      const body: any = {
        vcs_type: vcs.type,
        vcs_remote: vcs.remote.trim(),
        vcs_branch: vcs.branch.trim() || "main",
        vcs_username: vcs.username.trim(),
      };
      if (vcs.clearSecret) body.vcs_secret = "";
      else if (vcs.secret.trim()) body.vcs_secret = vcs.secret.trim();
      const r: any = await api.setProjectVcs(project.id, body);
      setSecretReady(!!r?.vcs_secret_ready);
      setVcs((v) => ({ ...v, secret: "", clearSecret: false }));
      onSaved();
      if (!testAfter) {
        setMsg("仓库配置已保存");
        return;
      }
      const t: any = await api.testProjectVcs(project.id);
      if (t?.ok) {
        setMsg(`已保存并同步成功${t?.action ? `（${t.action}）` : ""}${t?.message ? `：${t.message}` : ""}`);
      } else {
        setErr(`已保存，但同步失败：${t?.message || "未知原因"}`);
      }
    } catch (e: any) {
      setErr(e?.message || "保存仓库配置失败");
    } finally {
      setBusy("");
    }
  }

  async function saveLimit() {
    setBusy("limit"); setErr(""); setMsg("");
    try {
      const n = Math.max(0, Math.min(50, Math.floor(limit || 0)));
      await api.setProjectConcurrency(project.id, n);
      setLimit(n);
      setMsg(n === 0 ? "并发限制已关闭（不限制同时执行）" : `已限制为同时最多 ${n} 个会话`);
      onSaved();
    } catch (e: any) {
      setErr(e?.message || "保存并发限制失败");
    } finally {
      setBusy("");
    }
  }

  async function testVcs() {
    setBusy("test"); setErr(""); setMsg("");
    try {
      const t: any = await api.testProjectVcs(project.id);
      if (t?.ok) setMsg(`同步成功${t?.action ? `（${t.action}）` : ""}${t?.message ? `：${t.message}` : ""}`);
      else setErr(`同步失败：${t?.message || "未知原因"}`);
    } catch (e: any) {
      setErr(e?.message || "同步失败");
    } finally {
      setBusy("");
    }
  }

  const input = "border border-slate-300 rounded px-2 py-1.5 text-sm w-full bg-white";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4" onClick={onClose}>
      <div className="w-[min(860px,96vw)] h-[88vh] bg-white rounded-lg shadow-xl flex flex-col overflow-hidden"
        onClick={(e) => e.stopPropagation()}>
        <div className="shrink-0 flex items-center gap-2 px-4 h-12 border-b border-slate-200 bg-slate-50">
          <span className="font-semibold text-slate-700">项目配置 · {project.name}</span>
          <span className="text-[11px] text-slate-400 mono">{project.repo_path}</span>
          <button className="ml-auto p-1 rounded hover:bg-slate-200 text-slate-500"
            onClick={onClose} aria-label="关闭" title="关闭（Esc）">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
              strokeWidth="2" strokeLinecap="round"><path d="M18 6 6 18M6 6l12 12" /></svg>
          </button>
        </div>

        <div className="flex-1 min-h-0 overflow-auto p-5 space-y-4">
          {err && <div className="rounded bg-red-50 border border-red-200 px-3 py-2 text-xs text-red-700">{err}</div>}
          {msg && <div className="rounded bg-emerald-50 border border-emerald-200 px-3 py-2 text-xs text-emerald-700">{msg}</div>}

          {/* 代码仓库 */}
          <section className="bg-white rounded border border-slate-200 p-4">
            <div className="text-sm font-medium mb-1">代码仓库</div>
            <div className="text-[11px] text-slate-500 mb-3">
              绑定后，每次开始对话先把最新代码同步到项目工作区，再排查/修改。
              Git 用令牌（HTTPS），SVN 用账号密码；同步失败不会中断对话，只在本轮提示里说明。
            </div>
            <div className="grid grid-cols-2 gap-2">
              <label className="text-xs text-slate-600">
                <span>仓库类型</span>
                <SearchSelect className={input + " mt-1"} rootClassName="w-full" value={vcs.type}
                  onChange={(v) => setVcs((cur) => ({ ...cur, type: v as VcsDraft["type"] }))}
                  title="仓库类型" searchPlaceholder="搜索类型…" panelMinWidth={160}
                  options={[
                    { value: "none", label: "不绑定" },
                    { value: "git", label: "Git" },
                    { value: "svn", label: "SVN" },
                  ]} />
              </label>
              {vcs.type === "git" && (
                <label className="text-xs text-slate-600">
                  <span>分支</span>
                  <input autoComplete="off" className={input + " mt-1"} value={vcs.branch}
                    placeholder="main"
                    onChange={(e) => setVcs((v) => ({ ...v, branch: e.target.value }))} />
                </label>
              )}
              {vcs.type !== "none" && (
                <label className="text-xs text-slate-600 col-span-2">
                  <span>{vcs.type === "git" ? "Git 仓库地址（HTTPS）" : "SVN URL"}</span>
                  <input autoComplete="off" className={input + " mt-1"} value={vcs.remote}
                    placeholder={vcs.type === "git" ? "https://git.example.com/group/repo.git" : "https://svn.example.com/repos/project"}
                    onChange={(e) => setVcs((v) => ({ ...v, remote: e.target.value }))} />
                </label>
              )}
              {vcs.type === "git" && (
                <>
                  <label className="text-xs text-slate-600">
                    <span>账号（选填，多数平台填用户名或 token 名）</span>
                    <input autoComplete="off" className={input + " mt-1"} value={vcs.username}
                      placeholder="git"
                      onChange={(e) => setVcs((v) => ({ ...v, username: e.target.value }))} />
                  </label>
                  <label className="text-xs text-slate-600">
                    <span className="flex items-center gap-2">
                      Git 令牌
                      {secretReady && <span className="text-[10px] text-emerald-700">已配置</span>}
                    </span>
                    <input autoComplete="new-password" className={input + " mt-1"} type="password" value={vcs.secret}
                      placeholder={secretReady ? "••••••（留空表示不修改）" : "Personal Access Token"}
                      onChange={(e) => setVcs((v) => ({ ...v, secret: e.target.value }))} />
                    {secretReady && (
                      <label className="mt-1 flex items-center gap-1 text-[11px] text-slate-500">
                        <input autoComplete="off" type="checkbox" checked={vcs.clearSecret}
                          onChange={(e) => setVcs((v) => ({ ...v, clearSecret: e.target.checked }))} />
                        清除已保存的令牌
                      </label>
                    )}
                  </label>
                </>
              )}
              {vcs.type === "svn" && (
                <>
                  <label className="text-xs text-slate-600">
                    <span>SVN 账号</span>
                    <input autoComplete="off" className={input + " mt-1"} value={vcs.username}
                      placeholder="username"
                      onChange={(e) => setVcs((v) => ({ ...v, username: e.target.value }))} />
                  </label>
                  <label className="text-xs text-slate-600">
                    <span className="flex items-center gap-2">
                      SVN 密码
                      {secretReady && <span className="text-[10px] text-emerald-700">已配置</span>}
                    </span>
                    <input autoComplete="new-password" className={input + " mt-1"} type="password" value={vcs.secret}
                      placeholder={secretReady ? "••••••（留空表示不修改）" : "password"}
                      onChange={(e) => setVcs((v) => ({ ...v, secret: e.target.value }))} />
                    {secretReady && (
                      <label className="mt-1 flex items-center gap-1 text-[11px] text-slate-500">
                        <input autoComplete="off" type="checkbox" checked={vcs.clearSecret}
                          onChange={(e) => setVcs((v) => ({ ...v, clearSecret: e.target.checked }))} />
                        清除已保存的密码
                      </label>
                    )}
                  </label>
                </>
              )}
            </div>
            <div className="mt-3 flex items-center gap-2">
              <button className="ga-btn ga-btn-primary ga-btn-sm" disabled={!!busy}
                onClick={() => saveVcs(false)}>
                {busy === "vcs" ? "保存中…" : "保存仓库配置"}
              </button>
              <button className="ga-btn ga-btn-secondary ga-btn-sm" disabled={!!busy || vcs.type === "none"}
                onClick={() => saveVcs(true)}>
                {busy === "test" ? "同步中…" : "保存并立即同步"}
              </button>
              <button className="ga-btn ga-btn-secondary ga-btn-sm" disabled={!!busy || vcs.type === "none"}
                onClick={testVcs} title="按已保存的配置再同步一次">
                仅测试同步
              </button>
            </div>
          </section>

          {/* 并发限制 */}
          <section className="bg-white rounded border border-slate-200 p-4">
            <div className="text-sm font-medium mb-1">并发限制</div>
            <div className="text-[11px] text-slate-500 mb-3">
              同时执行的会话（run）上限。达到上限后，新发的消息留在队列里排队，
              前面的做完再按顺序开始。0 = 不限制。
            </div>
            <div className="flex items-center gap-2">
              <input autoComplete="off"
                className={input + " max-w-[120px]"}
                type="number" min={0} max={50} step={1}
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value) || 0)}
              />
              <span className="text-xs text-slate-500">个会话同时执行</span>
              <button className="ga-btn ga-btn-primary ga-btn-sm ml-2" disabled={!!busy}
                onClick={saveLimit}>
                {busy === "limit" ? "保存中…" : "保存并发限制"}
              </button>
              <span className="text-[11px] text-slate-400">
                {limit === 0 ? "当前：不限制" : `当前：最多 ${limit} 个，其余排队`}
              </span>
            </div>
          </section>

          {/* 消息通道 */}
          <section className="bg-white rounded border border-slate-200 p-4">
            <ChannelsTab projectId={project.id} />
          </section>
        </div>
      </div>
    </div>
  );
}
