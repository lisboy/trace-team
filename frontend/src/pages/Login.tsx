import { useState } from "react";
import { api } from "../api";
import { useAuth } from "../store";

function Brand({ small }: { small?: boolean }) {
  return (
    <div className="flex items-center gap-2 justify-center">
      <svg className={small ? "h-6 w-6" : "h-8 w-8"} viewBox="0 0 1024 1024" aria-hidden="true">
        <defs>
          <linearGradient id="login-brand-tile" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#3B8BFF" />
            <stop offset="0.55" stopColor="#2F7DDF" />
            <stop offset="1" stopColor="#19C2A8" />
          </linearGradient>
        </defs>
        <rect x="48" y="48" width="928" height="928" rx="210" fill="url(#login-brand-tile)" />
        <rect x="236" y="352" width="552" height="320" rx="160" fill="#eef4ff" />
        <g fill="#1E5FC8">
          <rect x="386" y="404" width="76" height="216" rx="26" />
          <rect x="316" y="474" width="216" height="76" rx="26" />
        </g>
        <circle cx="650" cy="512" r="66" fill="#1E5FC8" />
      </svg>
      <span className={small ? "font-bold text-slate-700" : "text-lg font-bold text-slate-800"}>
        循迹 Trace
      </span>
    </div>
  );
}

export function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const setLogin = useAuth((s) => s.setLogin);

  async function submit() {
    if (!username.trim() || !password) {
      setErr("请输入用户名和密码");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      const r = await api.login(username.trim(), password);
      setLogin(r);
    } catch (e: any) {
      setErr(e?.message || "登录失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-100 px-4">
      <div className="w-full max-w-sm bg-white rounded-xl border border-slate-200 shadow-sm p-7">
        <Brand />
        <div className="mt-5 mb-4 text-center text-[12px] text-slate-400">
          团队版 · 账号由管理员创建
        </div>
        <div className="space-y-3 text-sm">
          <input
            autoFocus
            className="w-full border border-slate-200 rounded px-3 py-2 outline-none focus:border-brand-400"
            placeholder="用户名"
            autoComplete="off"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />
          <input
            className="w-full border border-slate-200 rounded px-3 py-2 outline-none focus:border-brand-400"
            placeholder="密码"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />
          {err && <div className="text-xs text-red-600">{err}</div>}
          <button
            className="ga-btn ga-btn-primary w-full py-2"
            disabled={busy}
            onClick={submit}
          >
            {busy ? "登录中…" : "登录"}
          </button>
        </div>
      </div>
    </div>
  );
}

/** 首次登录 / 管理员重置密码后：必须先改密才能进入工作区。 */
export function ForcePasswordChange() {
  const user = useAuth((s) => s.user);
  const setUser = useAuth((s) => s.setUser);
  const setMustChangePassword = useAuth((s) => s.setMustChangePassword);
  const clearAuth = useAuth((s) => s.clearAuth);
  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setErr("");
    if (!oldPw) {
      setErr("请输入当前密码（初始密码为 666666）");
      return;
    }
    if (newPw.length < 6) {
      setErr("新密码至少 6 位");
      return;
    }
    if (newPw === "666666") {
      setErr("新密码不能继续使用 666666");
      return;
    }
    if (newPw !== confirm) {
      setErr("两次输入的新密码不一致");
      return;
    }
    setBusy(true);
    try {
      await api.changePassword(oldPw, newPw);
      try {
        const me = await api.me();
        if (me?.user) setUser(me.user);
      } catch {
        /* /me 失败也不挡：下面直接放行 */
      }
      setMustChangePassword(false);
    } catch (e: any) {
      setErr(e?.message || "修改密码失败");
    } finally {
      setBusy(false);
    }
  }

  async function relogin() {
    try {
      await api.logout();
    } catch {
      /* ignore */
    }
    clearAuth();
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-100 px-4">
      <div className="w-full max-w-md bg-white rounded-xl border border-slate-200 shadow-sm p-7">
        <Brand small />
        <div className="mt-4 text-base font-semibold text-slate-800">先设置一个新密码</div>
        <div className="mt-1 text-[12px] leading-relaxed text-slate-500">
          账号 <span className="mono">{user?.username || user?.display_name || ""}</span>{" "}
          正在使用初始密码登录（或密码刚被管理员重置），必须修改后才能继续使用。
        </div>
        <div className="mt-4 space-y-3 text-sm">
          <input
            autoFocus
            className="w-full border border-slate-200 rounded px-3 py-2 outline-none focus:border-brand-400"
            placeholder="当前密码（初始密码 666666）"
            type="password"
            autoComplete="new-password"
            value={oldPw}
            onChange={(e) => setOldPw(e.target.value)}
          />
          <input
            className="w-full border border-slate-200 rounded px-3 py-2 outline-none focus:border-brand-400"
            placeholder="新密码（至少 6 位）"
            type="password"
            autoComplete="new-password"
            value={newPw}
            onChange={(e) => setNewPw(e.target.value)}
          />
          <input
            className="w-full border border-slate-200 rounded px-3 py-2 outline-none focus:border-brand-400"
            placeholder="再次输入新密码"
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />
          {err && <div className="text-xs text-red-600">{err}</div>}
          <button className="ga-btn ga-btn-primary w-full py-2" disabled={busy} onClick={submit}>
            {busy ? "保存中…" : "保存并进入"}
          </button>
          <button className="text-[11px] text-slate-400 hover:text-slate-600" onClick={relogin}>
            换个账号登录
          </button>
        </div>
      </div>
    </div>
  );
}
