import { create } from "zustand";
import { clearToken, getToken, setToken } from "./api";
import type { Team, User } from "./types";

const KEY = "ga_profile";

/**
 * 登录身份：Bearer 令牌 + 当前账号/团队。
 * 令牌放 localStorage（ga_token），这里只是内存镜像；刷新后由 /auth/me 校验。
 * 非管理员看不到团队页与模型配置（前端只做体验，后端才是安全闸门）。
 */
interface AuthState {
  user: User | null;
  teams: Team[];
  currentTeamId: string | null;
  token: string | null;
  mustChangePassword: boolean;
  setLogin: (r: { token: string; user: User; teams: Team[]; current_team?: string | null;
                   must_change_password?: boolean }) => void;
  setIdentity: (user: User, teams: Team[]) => void;
  setUser: (user: User) => void;
  setTeams: (teams: Team[]) => void;
  setMustChangePassword: (v: boolean) => void;
  clearAuth: () => void;
}

function load(): Partial<AuthState> {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) return JSON.parse(raw);
  } catch {
    /* ignore */
  }
  return {};
}

function persist(s: AuthState) {
  try {
    localStorage.setItem(
      KEY,
      JSON.stringify({ user: s.user, teams: s.teams, currentTeamId: s.currentTeamId })
    );
  } catch {
    /* 隐私模式/配额满：只是少了首屏缓存，不影响使用 */
  }
}

const initial = load();
const initialTeamId = initial.currentTeamId || initial.teams?.[0]?.id || null;

export function isAdminUser(u: User | null | undefined): boolean {
  return !!u && (u.is_admin || u.role === "owner" || u.role === "admin");
}

export const useAuth = create<AuthState>((set, get) => ({
  user: initial.user ?? null,
  teams: initial.teams ?? [],
  currentTeamId: initialTeamId,
  token: getToken(),
  mustChangePassword: !!initial.user?.must_change_password,

  setLogin: (r) => {
    setToken(r.token);
    const currentTeamId = r.current_team || r.teams[0]?.id || null;
    const next = {
      user: r.user,
      teams: r.teams,
      currentTeamId,
      token: r.token,
      mustChangePassword: !!r.must_change_password,
    };
    persist({ ...get(), ...next });
    set(next);
  },
  setIdentity: (user, teams) => {
    const cur = get();
    const next = {
      ...cur,
      user,
      teams,
      currentTeamId: cur.currentTeamId || teams[0]?.id || null,
      mustChangePassword: !!user.must_change_password,
    };
    persist(next);
    set(next);
  },
  setTeams: (teams) => {
    const cur = get();
    const next = {
      ...cur,
      teams,
      currentTeamId: cur.currentTeamId || teams[0]?.id || null,
    };
    persist(next);
    set(next);
  },
  setUser: (user) => {
    const cur = get();
    const next = { ...cur, user, mustChangePassword: !!user.must_change_password };
    persist(next);
    set(next);
  },
  setMustChangePassword: (v) => {
    const cur = get();
    set({ ...cur, mustChangePassword: v });
  },
  clearAuth: () => {
    clearToken();
    try {
      localStorage.removeItem(KEY);
    } catch {
      /* ignore */
    }
    set({ user: null, teams: [], currentTeamId: null, token: null, mustChangePassword: false });
  },
}));
