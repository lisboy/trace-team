import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useAuth } from "../store";
import Avatar from "./Avatar";

/** 账号资料：显示名 / 头像 / 改密码 / 退出登录。
 *  用户名（登录账号）由管理员创建，不能在个人设置里改；外观偏好只作用于当前账号。 */
const ROLE_LABEL: Record<string, string> = {
  owner: "管理员", admin: "管理员", developer: "开发", viewer: "需求",
};

export default function ProfileTab() {
  const user = useAuth((s) => s.user);
  const setUser = useAuth((s) => s.setUser);
  const clearAuth = useAuth((s) => s.clearAuth);
  const fileRef = useRef<HTMLInputElement>(null);

  const [name, setName] = useState(user?.display_name || "");
  const [avatar, setAvatar] = useState<string | null>(user?.avatar || null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const [oldPw, setOldPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [pwMsg, setPwMsg] = useState("");
  const [pwErr, setPwErr] = useState("");
  const [pwBusy, setPwBusy] = useState(false);

  useEffect(() => {
    setName(user?.display_name || "");
    setAvatar(user?.avatar || null);
  }, [user?.display_name, user?.avatar]);

  async function saveProfile() {
    setMsg("");
    setErr("");
    const trimmed = name.trim();
    if (!trimmed) {
      setErr("名称不能为空");
      return;
    }
    setBusy(true);
    try {
      const r = await api.updateMe({ display_name: trimmed, avatar });
      setUser(r.user);
      setMsg("已保存");
    } catch (e: any) {
      setErr(e?.message || "保存失败");
    } finally {
      setBusy(false);
    }
  }

  async function pickAvatar(file: File | null | undefined) {
    if (!file) return;
    setMsg("");
    setErr("");
    if (!file.type.startsWith("image/")) {
      setErr("请选择图片文件");
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      setErr("头像不能超过 8MB");
      return;
    }
    try {
      const r = await api.uploadAvatar(file);
      setAvatar(r.avatar);
      // 后端上传时已经落库，但名称可能还没保存：这里只刷新头像
      const me = await api.me();
      if (me?.user) setUser(me.user);
      setMsg("头像已更新");
    } catch (e: any) {
      setErr(e?.message || "头像上传失败");
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function removeAvatar() {
    setMsg("");
    setErr("");
    try {
      const r = await api.updateMe({ avatar: "" });
      setAvatar(null);
      setUser(r.user);
      setMsg("已恢复默认头像");
    } catch (e: any) {
      setErr(e?.message || "操作失败");
    }
  }

  async function changePassword() {
    setPwMsg("");
    setPwErr("");
    if (!oldPw) {
      setPwErr("请输入当前密码");
      return;
    }
    if (newPw.length < 6) {
      setPwErr("新密码至少 6 位");
      return;
    }
    if (newPw === "666666") {
      setPwErr("新密码不能继续使用 666666");
      return;
    }
    if (newPw !== confirmPw) {
      setPwErr("两次输入的新密码不一致");
      return;
    }
    setPwBusy(true);
    try {
      await api.changePassword(oldPw, newPw);
      setOldPw("");
      setNewPw("");
      setConfirmPw("");
      setPwMsg("密码已更新（其它设备上的登录已失效）");
    } catch (e: any) {
      setPwErr(e?.message || "修改密码失败");
    } finally {
      setPwBusy(false);
    }
  }

  async function logout() {
    try {
      await api.logout();
    } catch {
      /* 令牌失效也照样退出 */
    }
    clearAuth();
  }

  return (
    <div className="max-w-2xl space-y-4">
      <div className="bg-white rounded border border-slate-200 p-4">
        <div className="text-sm font-medium mb-3">个人资料</div>
        <div className="flex items-center gap-4">
          <div className="relative group">
            <Avatar src={avatar} name={name} size={72} className="ring-2 ring-slate-200" />
            <button
              type="button"
              className="absolute inset-0 rounded-full bg-slate-900/45 text-white text-[11px] opacity-0 group-hover:opacity-100 transition-opacity"
              onClick={() => fileRef.current?.click()}
              title="更换头像"
            >
              更换
            </button>
          </div>
          <div className="space-y-1.5">
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="text-xs px-2.5 py-1.5 rounded border border-slate-200 hover:bg-slate-50"
                onClick={() => fileRef.current?.click()}
              >
                上传新头像
              </button>
              {avatar && (
                <button
                  type="button"
                  className="text-xs px-2.5 py-1.5 rounded border border-slate-200 hover:bg-slate-50 text-slate-500"
                  onClick={removeAvatar}
                >
                  恢复默认
                </button>
              )}
            </div>
            <div className="text-[11px] text-slate-400">支持 JPG / PNG / WebP / GIF，不超过 8MB</div>
            <input autoComplete="off"
              ref={fileRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => pickAvatar(e.target.files?.[0])}
            />
          </div>
        </div>

        <div className="mt-4 grid grid-cols-[88px_1fr] gap-2 items-center text-sm">
          <label className="text-slate-500 text-xs">用户名</label>
          <div className="flex items-center gap-2">
            <span className="mono text-slate-700">{user?.username || "—"}</span>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500">
              {ROLE_LABEL[user?.role || ""] || user?.role || "成员"}
            </span>
          </div>
          <label className="text-slate-500 text-xs">名称</label>
          <input autoComplete="off"
            className="border rounded px-2 py-1.5 max-w-sm"
            value={name}
            maxLength={120}
            onChange={(e) => setName(e.target.value)}
            placeholder="你的显示名称"
          />
        </div>

        <div className="mt-3 flex items-center gap-2">
          <button className="ga-btn ga-btn-primary" disabled={busy} onClick={saveProfile}>
            {busy ? "保存中…" : "保存资料"}
          </button>
          {msg && <span className="text-xs text-green-700">{msg}</span>}
          {err && <span className="text-xs text-red-600">{err}</span>}
        </div>
      </div>

      <div className="bg-white rounded border border-slate-200 p-4">
        <div className="text-sm font-medium mb-1">修改密码</div>
        <div className="text-[11px] text-slate-400 mb-3">
          用户名由管理员创建和分配项目；密码可以自己修改。
        </div>
        <div className="grid grid-cols-[88px_1fr] gap-2 items-center text-sm">
          <label className="text-slate-500 text-xs">当前密码</label>
          <input
            className="border rounded px-2 py-1.5 max-w-sm"
            type="password"
            autoComplete="new-password"
            value={oldPw}
            onChange={(e) => setOldPw(e.target.value)}
          />
          <label className="text-slate-500 text-xs">新密码</label>
          <input
            className="border rounded px-2 py-1.5 max-w-sm"
            type="password"
            autoComplete="new-password"
            placeholder="至少 6 位"
            value={newPw}
            onChange={(e) => setNewPw(e.target.value)}
          />
          <label className="text-slate-500 text-xs">确认新密码</label>
          <input
            className="border rounded px-2 py-1.5 max-w-sm"
            type="password"
            autoComplete="new-password"
            value={confirmPw}
            onChange={(e) => setConfirmPw(e.target.value)}
          />
        </div>
        <div className="mt-3 flex items-center gap-2">
          <button className="ga-btn ga-btn-primary" disabled={pwBusy} onClick={changePassword}>
            {pwBusy ? "保存中…" : "修改密码"}
          </button>
          {pwMsg && <span className="text-xs text-green-700">{pwMsg}</span>}
          {pwErr && <span className="text-xs text-red-600">{pwErr}</span>}
        </div>
      </div>

      <div className="bg-white rounded border border-slate-200 p-4 flex items-center gap-3">
        <div className="text-sm">
          <div className="font-medium">退出登录</div>
          <div className="text-[11px] text-slate-400 mt-0.5">
            退出后需要重新输入用户名和密码；外观偏好只保存在当前账号下。
          </div>
        </div>
        <button className="ml-auto ga-btn ga-btn-secondary" onClick={logout}>
          退出登录
        </button>
      </div>
    </div>
  );
}
