import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { isAdminUser, useAuth } from "../store";
import ProjectConfigModal from "../components/ProjectConfigModal";
import SearchSelect from "../components/SearchSelect";

type Member = {
  id: string;
  username: string;
  display_name: string;
  avatar?: string | null;
  role: string;
  role_label: string;
  is_active: boolean;
  must_change_password: boolean;
  last_login_at: string | null;
  project_ids: string[];
};

type TeamProject = {
  id: string;
  name: string;
  slug: string;
  repo_path: string;
  archived_at: string | null;
  model_profile_key: string | null;
  model_label?: string | null;
  vcs_type?: string;
  vcs_remote?: string;
  vcs_branch?: string;
  vcs_username?: string;
  vcs_secret_ready?: boolean;
  max_concurrency?: number;
};

type ModelProfile = { key: string; label: string; model: string; provider: string; project_id?: string | null };

const ROLE_OPTIONS = [
  { value: "admin", label: "管理员", hint: "管理团队、项目与模型配置" },
  { value: "developer", label: "开发", hint: "在被分派项目里可发起修改" },
  { value: "viewer", label: "需求", hint: "只读问答，不能改代码" },
];

function fmtTime(iso: string | null): string {
  if (!iso) return "从未登录";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso.slice(0, 16);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function slugify(name: string): string {
  const s = name.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
  return s || `ws-${Date.now().toString(36)}`;
}

export default function Team() {
  const teamId = useAuth((s) => s.currentTeamId);
  const me = useAuth((s) => s.user);
  const isAdmin = isAdminUser(me);

  const [members, setMembers] = useState<Member[]>([]);
  const [projects, setProjects] = useState<TeamProject[]>([]);
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  // 新建成员
  const [createOpen, setCreateOpen] = useState(false);
  const [nu, setNu] = useState("");
  const [nd, setNd] = useState("");
  const [nr, setNr] = useState("developer");
  const [np, setNp] = useState("");
  const [creating, setCreating] = useState(false);

  // 新建项目
  const [pOpen, setPOpen] = useState(false);
  const [pn, setPn] = useState("");
  const [ps, setPs] = useState("");
  const [pr, setPr] = useState("");
  const [pCreating, setPCreating] = useState(false);

  // 展开的项目分派面板
  const [assignFor, setAssignFor] = useState<string | null>(null);
  const [assignSel, setAssignSel] = useState<Set<string>>(new Set());
  const [assignBusy, setAssignBusy] = useState(false);

  // 改名
  const [editNameId, setEditNameId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");

  // 项目配置（模型 / 仓库 / 消息通道）
  const [configFor, setConfigFor] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!teamId || !isAdmin) {
      setLoading(false);
      return;
    }
    try {
      const [ms, ps2, pf] = await Promise.all([
        api.teamMembers(), api.teamProjects(), api.teamModelProfiles(),
      ]);
      setMembers(Array.isArray(ms) ? ms : []);
      setProjects(Array.isArray(ps2) ? ps2 : []);
      setProfiles(Array.isArray(pf) ? pf : []);
      setErr("");
    } catch (e: any) {
      setErr(e?.message || "加载团队数据失败");
    } finally {
      setLoading(false);
    }
  }, [teamId, isAdmin]);

  useEffect(() => { refresh(); }, [refresh]);

  const activeProjects = projects.filter((p) => !p.archived_at);

  if (!isAdmin) {
    return (
      <div className="h-full flex items-center justify-center text-sm text-slate-400">
        团队管理仅管理员可见。
      </div>
    );
  }

  async function createMember() {
    const username = nu.trim();
    if (!username) return;
    setCreating(true);
    setErr("");
    setMsg("");
    try {
      const r: any = await api.createMember({
        username,
        display_name: nd.trim() || undefined,
        role: nr,
        password: np.trim() || undefined,
      });
      setCreateOpen(false);
      setNu(""); setNd(""); setNp(""); setNr("developer");
      setMsg(`已创建账号「${r?.username || username}」，初始密码 ${r?.initial_password || "666666"}，首次登录必须修改。`);
      await refresh();
    } catch (e: any) {
      setErr(e?.message || "创建账号失败");
    } finally {
      setCreating(false);
    }
  }

  async function changeRole(m: Member, role: string) {
    setErr(""); setMsg("");
    try {
      await api.updateMember(m.id, { role });
      await refresh();
    } catch (e: any) {
      setErr(e?.message || "修改角色失败");
    }
  }

  async function toggleActive(m: Member) {
    setErr(""); setMsg("");
    try {
      await api.updateMember(m.id, { is_active: !m.is_active });
      await refresh();
    } catch (e: any) {
      setErr(e?.message || "修改状态失败");
    }
  }

  async function saveName(m: Member) {
    const name = editName.trim();
    if (!name) { setEditNameId(null); return; }
    setErr(""); setMsg("");
    try {
      await api.updateMember(m.id, { display_name: name });
      setEditNameId(null);
      await refresh();
    } catch (e: any) {
      setErr(e?.message || "改名失败");
    }
  }

  async function resetPassword(m: Member) {
    if (!confirm(`把「${m.display_name || m.username}」的密码重置为 666666？\n该账号会被强制在下次登录时修改密码，已登录的设备会退出。`)) return;
    setErr(""); setMsg("");
    try {
      const r: any = await api.resetMemberPassword(m.id);
      setMsg(`「${m.username}」的密码已重置为 ${r?.initial_password || "666666"}，下次登录必须修改。`);
      await refresh();
    } catch (e: any) {
      setErr(e?.message || "重置密码失败");
    }
  }

  function openAssign(m: Member) {
    if (assignFor === m.id) { setAssignFor(null); return; }
    setAssignFor(m.id);
    setAssignSel(new Set(m.project_ids));
  }

  function toggleAssign(pid: string) {
    setAssignSel((cur) => {
      const next = new Set(cur);
      if (next.has(pid)) next.delete(pid); else next.add(pid);
      return next;
    });
  }

  async function saveAssign(m: Member) {
    setAssignBusy(true);
    setErr(""); setMsg("");
    try {
      await api.assignMemberProjects(m.id, Array.from(assignSel));
      setAssignFor(null);
      setMsg(`已更新「${m.username}」的项目分派。`);
      await refresh();
    } catch (e: any) {
      setErr(e?.message || "保存项目分派失败");
    } finally {
      setAssignBusy(false);
    }
  }

  async function createProject() {
    const name = pn.trim();
    if (!name || !teamId) return;
    setPCreating(true);
    setErr(""); setMsg("");
    const slug = (ps.trim() || slugify(name));
    const repoPath = pr.trim() || `.agent/projects/${slug}`;
    try {
      await api.createProject(teamId, {
        name, slug, repo_path: repoPath, project_type: "python",
      });
      setPOpen(false);
      setPn(""); setPs(""); setPr("");
      setMsg(`已创建项目「${name}」。记得给它绑定对话模型，并分派给需要的成员。`);
      await refresh();
    } catch (e: any) {
      setErr(e?.message || "创建项目失败");
    } finally {
      setPCreating(false);
    }
  }

  return (
    <div className="h-full overflow-auto p-5 space-y-4">
      <div className="flex items-center gap-3">
        <h2 className="text-lg font-semibold">团队</h2>
        <span className="text-xs text-slate-400">
          账号、项目分派、仓库与提醒由管理员统一管理；对话模型在「设置 → 智能体」里配置，成员对话里不能切换。
        </span>
        <button className="ml-auto ga-btn ga-btn-secondary ga-btn-sm" onClick={refresh}>刷新</button>
      </div>

      {err && <div className="rounded bg-red-50 border border-red-200 px-3 py-2 text-xs text-red-700">{err}</div>}
      {msg && <div className="rounded bg-emerald-50 border border-emerald-200 px-3 py-2 text-xs text-emerald-700">{msg}</div>}

      {/* ---------------- 成员 ---------------- */}
      <section className="bg-white rounded border border-slate-200">
        <div className="flex items-center gap-2 px-4 py-3 border-b border-slate-100">
          <span className="font-medium text-sm">成员</span>
          <span className="text-[11px] text-slate-400">{members.length} 个账号</span>
          <button className="ml-auto ga-btn ga-btn-primary ga-btn-sm" onClick={() => setCreateOpen((v) => !v)}>
            + 新建账号
          </button>
        </div>

        {createOpen && (
          <div className="px-4 py-3 border-b border-slate-100 bg-slate-50 grid grid-cols-1 sm:grid-cols-4 gap-2 text-xs">
            <input autoComplete="off"
              className="border rounded px-2 py-1.5 bg-white" placeholder="用户名（登录用，字母/数字）"
              value={nu} onChange={(e) => setNu(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && createMember()}
            />
            <input autoComplete="off"
              className="border rounded px-2 py-1.5 bg-white" placeholder="显示名（可选）"
              value={nd} onChange={(e) => setNd(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && createMember()}
            />
            <SearchSelect className="border rounded px-2 py-1.5 bg-white text-xs w-full"
              rootClassName="w-full"
              value={nr} onChange={setNr} title="角色" searchPlaceholder="搜索角色…"
              options={ROLE_OPTIONS.map((r) => ({
                value: r.value, label: `${r.label} — ${r.hint}`, keywords: r.hint,
              }))} />
            <input autoComplete="off"
              className="border rounded px-2 py-1.5 bg-white" placeholder="初始密码（默认 666666）"
              value={np} onChange={(e) => setNp(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && createMember()}
            />
            <div className="sm:col-span-4 flex items-center gap-2">
              <span className="text-[11px] text-slate-400">
                新账号首次登录必须修改密码；项目可在下面每行的「分派项目」里勾选。
              </span>
              <div className="ml-auto flex gap-2">
                <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={() => setCreateOpen(false)}>取消</button>
                <button className="ga-btn ga-btn-primary ga-btn-sm" disabled={creating || !nu.trim()} onClick={createMember}>
                  {creating ? "创建中…" : "创建"}
                </button>
              </div>
            </div>
          </div>
        )}

        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-slate-400 text-left border-b border-slate-100">
                <th className="px-4 py-2 font-normal">用户名 / 显示名</th>
                <th className="px-3 py-2 font-normal">角色</th>
                <th className="px-3 py-2 font-normal">状态</th>
                <th className="px-3 py-2 font-normal">项目</th>
                <th className="px-3 py-2 font-normal">最后登录</th>
                <th className="px-4 py-2 font-normal text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {members.map((m) => {
                const isSelf = m.id === me?.id;
                const isOwner = m.role === "owner";
                return (
                  <tr key={m.id} className="border-b border-slate-50 align-top hover:bg-slate-50/50">
                    <td className="px-4 py-2">
                      <div className="font-medium text-slate-700 flex items-center gap-1.5">
                        {editNameId === m.id ? (
                          <input autoComplete="off"
                            autoFocus
                            className="border rounded px-1.5 py-0.5 text-xs w-36"
                            value={editName}
                            onChange={(e) => setEditName(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") saveName(m);
                              if (e.key === "Escape") setEditNameId(null);
                            }}
                            onBlur={() => saveName(m)}
                          />
                        ) : (
                          <span>{m.display_name || m.username}</span>
                        )}
                        {isSelf && <span className="text-[10px] text-brand-600 bg-brand-50 rounded px-1">我</span>}
                      </div>
                      <div className="mono text-[11px] text-slate-400">{m.username}</div>
                    </td>
                    <td className="px-3 py-2">
                      {isOwner ? (
                        <span className="rounded bg-brand-50 text-brand-700 px-1.5 py-0.5 text-[11px]" title="初始管理员角色不可修改">
                          管理员
                        </span>
                      ) : (
                        <SearchSelect
                          className="border rounded px-1.5 py-1 bg-white text-xs min-w-[72px]"
                          value={m.role}
                          onChange={(v) => changeRole(m, v)}
                          title="角色" searchPlaceholder="搜索角色…" panelMinWidth={140}
                          options={ROLE_OPTIONS.map((r) => ({
                            value: r.value, label: r.label, keywords: r.hint,
                          }))}
                        />
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex flex-col gap-0.5">
                        <span className={m.is_active ? "text-emerald-700" : "text-slate-400"}>
                          {m.is_active ? "启用" : "已停用"}
                        </span>
                        {m.must_change_password && (
                          <span className="text-[10px] text-amber-600" title="该账号下次登录必须先修改密码">
                            待首次改密
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-3 py-2">
                      {m.role === "owner" || m.role === "admin" ? (
                        <span className="text-slate-400">全部项目</span>
                      ) : (
                        <button
                          className="text-brand-700 hover:underline"
                          onClick={() => openAssign(m)}
                          title="勾选该账号负责的项目"
                        >
                          分派项目（{m.project_ids.length}）
                        </button>
                      )}
                    </td>
                    <td className="px-3 py-2 text-slate-500 tabular-nums">{fmtTime(m.last_login_at)}</td>
                    <td className="px-4 py-2 text-right whitespace-nowrap">
                      <button
                        className="text-[11px] px-1.5 py-0.5 rounded hover:bg-slate-100 text-slate-600"
                        onClick={() => { setEditNameId(m.id); setEditName(m.display_name || m.username); }}
                      >
                        改名
                      </button>
                      <button
                        className="ml-1 text-[11px] px-1.5 py-0.5 rounded hover:bg-amber-50 text-amber-700"
                        onClick={() => resetPassword(m)}
                      >
                        重置密码
                      </button>
                      {!isSelf && (
                        <button
                          className={`ml-1 text-[11px] px-1.5 py-0.5 rounded ${m.is_active ? "hover:bg-red-50 text-red-600" : "hover:bg-emerald-50 text-emerald-700"}`}
                          onClick={() => toggleActive(m)}
                        >
                          {m.is_active ? "停用" : "启用"}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
              {members.length === 0 && !loading && (
                <tr><td colSpan={6} className="px-4 py-6 text-center text-slate-400">还没有成员（除你之外）。点「+ 新建账号」添加开发和需求。</td></tr>
              )}
            </tbody>
          </table>
        </div>

        {assignFor && (() => {
          const m = members.find((x) => x.id === assignFor);
          if (!m) return null;
          return (
            <div className="mx-4 mb-3 mt-1 rounded border border-brand-200 bg-brand-50/40 p-3">
              <div className="flex items-center gap-2 mb-2">
                <span className="text-xs font-medium">分派项目给「{m.display_name || m.username}」</span>
                <span className="text-[11px] text-slate-400">
                  成员登录后只能看到勾选的项目；需求角色在这些项目里只能只读问答。
                </span>
                <button className="ml-auto text-[11px] text-slate-500 hover:text-slate-700" onClick={() => setAssignFor(null)}>收起</button>
              </div>
              {activeProjects.length === 0 ? (
                <div className="text-xs text-slate-400">还没有可用的项目，先在右侧新建项目。</div>
              ) : (
                <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-1 mb-2">
                  {activeProjects.map((p) => (
                    <label key={p.id} className="flex items-center gap-2 text-xs px-2 py-1 rounded bg-white border border-slate-200 cursor-pointer">
                      <input autoComplete="off"
                        type="checkbox"
                        className="accent-brand-600"
                        checked={assignSel.has(p.id)}
                        onChange={() => toggleAssign(p.id)}
                      />
                      <span className="truncate" title={`${p.name}（${p.repo_path}）`}>{p.name}</span>
                    </label>
                  ))}
                </div>
              )}
              <div className="flex justify-end gap-2">
                <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={() => setAssignFor(null)}>取消</button>
                <button className="ga-btn ga-btn-primary ga-btn-sm" disabled={assignBusy} onClick={() => saveAssign(m)}>
                  {assignBusy ? "保存中…" : "保存分派"}
                </button>
              </div>
            </div>
          );
        })()}
      </section>

      {/* ---------------- 项目与模型 ---------------- */}
      <section className="bg-white rounded border border-slate-200">
        <div className="flex items-center gap-2 px-4 py-3 border-b border-slate-100">
          <span className="font-medium text-sm">项目与对话模型</span>
          <span className="text-[11px] text-slate-400">{activeProjects.length} 个可用项目</span>
          <button className="ml-auto ga-btn ga-btn-primary ga-btn-sm" onClick={() => setPOpen((v) => !v)}>
            + 新建项目
          </button>
        </div>

        {pOpen && (
          <div className="px-4 py-3 border-b border-slate-100 bg-slate-50 grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs">
            <input autoComplete="off" className="border rounded px-2 py-1.5 bg-white" placeholder="项目名称"
              value={pn} onChange={(e) => setPn(e.target.value)} />
            <input autoComplete="off" className="border rounded px-2 py-1.5 bg-white" placeholder="slug（可选，英文）"
              value={ps} onChange={(e) => setPs(e.target.value)} />
            <input autoComplete="off" className="border rounded px-2 py-1.5 bg-white" placeholder="仓库路径（可选，默认 .agent/projects/slug）"
              value={pr} onChange={(e) => setPr(e.target.value)} />
            <div className="sm:col-span-3 flex items-center gap-2">
              <span className="text-[11px] text-slate-400">智能体直接改动工作区文件；每次改动可在运行详情里逐个文件审阅、回滚。</span>
              <div className="ml-auto flex gap-2">
                <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={() => setPOpen(false)}>取消</button>
                <button className="ga-btn ga-btn-primary ga-btn-sm" disabled={pCreating || !pn.trim()} onClick={createProject}>
                  {pCreating ? "创建中…" : "创建"}
                </button>
              </div>
            </div>
          </div>
        )}

        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-400 text-left border-b border-slate-100">
              <th className="px-4 py-2 font-normal">项目</th>
              <th className="px-3 py-2 font-normal">工作区目录</th>
              <th className="px-3 py-2 font-normal">对话模型</th>
              <th className="px-3 py-2 font-normal">代码仓库</th>
              <th className="px-3 py-2 font-normal">并发</th>
              <th className="px-4 py-2 font-normal">已分派</th>
              <th className="px-4 py-2 font-normal text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            {projects.map((p) => {
              const assigned = members.filter((m) => m.project_ids.includes(p.id));
              // 同名 key 时项目专属档位优先（与运行时解析一致）
              const bound = profiles.find((f) => f.key === p.model_profile_key && f.project_id === p.id) ||
                profiles.find((f) => f.key === p.model_profile_key && !f.project_id);
              return (
                <tr key={p.id} className={`border-b border-slate-50 align-top hover:bg-slate-50/50 ${p.archived_at ? "opacity-60" : ""}`}>
                  <td className="px-4 py-2">
                    <div className="font-medium text-slate-700 flex items-center gap-1.5">
                      {p.name}
                      {p.archived_at && <span className="text-[10px] text-slate-400 bg-slate-100 rounded px-1">已移除</span>}
                    </div>
                    <div className="mono text-[11px] text-slate-400">{p.slug}</div>
                  </td>
                  <td className="px-3 py-2 mono text-[11px] text-slate-500 break-all max-w-[280px]">{p.repo_path}</td>
                  <td className="px-3 py-2">
                    {profiles.length === 0 ? (
                      <span className="text-amber-600" title="先在「设置 → 模型配置」里加供应商并拉取模型">
                        还没有可用模型（去设置 → 模型配置 添加）
                      </span>
                    ) : (
                      <div className="flex flex-col gap-0.5">
                        <span className={bound ? "text-slate-700" : "text-amber-600"}>
                          {bound ? `${bound.label} · ${bound.model}` : "未固定（用助手默认档位）"}
                        </span>
                        {!bound && (
                          <span className="text-[10px] text-amber-600"
                            title="在「设置 → 智能体」把作用范围切到本项目，再选对话模型">
                            在「设置 → 智能体」里选作用范围为本项目可固定
                          </span>
                        )}
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {(p.vcs_type || "none") === "none" ? (
                      <span className="text-slate-400">未绑定</span>
                    ) : (
                      <div className="flex flex-col gap-0.5">
                        <span className="text-slate-700">
                          {p.vcs_type === "git" ? "Git" : "SVN"}
                          {p.vcs_type === "git" && p.vcs_branch ? ` · ${p.vcs_branch}` : ""}
                        </span>
                        <span className="mono text-[10px] text-slate-400 truncate max-w-[200px]"
                          title={p.vcs_remote || ""}>{p.vcs_remote}</span>
                      </div>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    {p.max_concurrency && p.max_concurrency > 0 ? (
                      <span className="text-slate-700" title="同时执行的会话上限，超出的排队">
                        {p.max_concurrency} 个
                      </span>
                    ) : (
                      <span className="text-slate-400">不限</span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-slate-500">
                    {assigned.length === 0 ? (
                      <span className="text-slate-400">仅管理员</span>
                    ) : (
                      <span className="truncate" title={assigned.map((m) => m.display_name || m.username).join("、")}>
                        {assigned.map((m) => m.display_name || m.username).join("、")}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-right whitespace-nowrap">
                    <button className="text-[11px] px-2 py-0.5 rounded border border-slate-200 hover:bg-slate-50 text-slate-600"
                      onClick={() => setConfigFor(p.id)}>
                      配置
                    </button>
                  </td>
                </tr>
              );
            })}
            {projects.length === 0 && !loading && (
              <tr><td colSpan={7} className="px-4 py-6 text-center text-slate-400">还没有项目。点「+ 新建项目」创建第一个。</td></tr>
            )}
          </tbody>
        </table>
        {loading && <div className="px-4 py-3 text-xs text-slate-400">加载中…</div>}
      </section>

      {(() => {
        const p = projects.find((x) => x.id === configFor);
        if (!p) return null;
        return (
          <ProjectConfigModal
            project={p}
            onClose={() => setConfigFor(null)}
            onSaved={refresh}
          />
        );
      })()}
    </div>
  );
}
