import { useEffect, useRef, useState } from "react";
import { isAdminUser, useAuth } from "./store";
import { navigateKeep, paramsOf, parseHash, type Route } from "./router";
import { initTheme, usePrefs } from "./theme";
import { usePolicy } from "./assistantPolicy";
import { api } from "./api";
import Chat from "./pages/Chat";
import RunDetail from "./pages/RunDetail";
import Config from "./pages/Config";
import Usage from "./pages/Usage";
import Team from "./pages/Team";
import { ForcePasswordChange, Login } from "./pages/Login";
import Topbar from "./components/Topbar";
import ErrorBoundary from "./components/ErrorBoundary";

/** 全局背景壁纸层：外链图片 / 上传的图片或视频 / Wallpaper Engine 工坊文件。
 *  fixed + z-0 垫底，业务界面以 relative z-10 盖在上面（开背景时面板转半透明毛玻璃）。 */
function BackgroundLayer() {
  const p = usePrefs((s) => s.prefs);
  const [blob, setBlob] = useState("");
  useEffect(() => {
    if (p.bgKind !== "we" || !p.bgSrc) {
      setBlob("");
      return;
    }
    let dead = false;
    let url = "";
    api.weFileBlob(p.bgSrc)
      .then((u) => {
        if (dead) URL.revokeObjectURL(u);
        else {
          url = u;
          setBlob(u);
        }
      })
      .catch(() => {
        if (!dead) setBlob("");
      });
    return () => {
      dead = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [p.bgKind, p.bgSrc]);

  if (p.bgKind === "none" || !p.bgSrc) return null;
  if (p.bgKind === "we" && !blob) return null; // 工坊文件还在取
  const isVideo = p.bgVideo && p.bgKind !== "url";
  const src = p.bgKind === "we" ? blob : p.bgSrc;
  return (
    <div className="ga-bg-layer" aria-hidden style={{ opacity: p.bgOpacity / 100 }}>
      {isVideo ? (
        <video className="ga-bg-media" src={src} autoPlay loop muted playsInline />
      ) : (
        <img className="ga-bg-media" src={src} alt="" />
      )}
      <div
        className="absolute inset-0"
        style={{
          backdropFilter: p.bgBlur ? `blur(${p.bgBlur}px)` : undefined,
          background: p.bgDim ? `rgba(8, 12, 22, ${p.bgDim / 100})` : undefined,
        }}
      />
    </div>
  );
}

export default function App() {
  const token = useAuth((s) => s.token);
  const mustChangePassword = useAuth((s) => s.mustChangePassword);
  const cachedUser = useAuth((s) => s.user);
  const [identityLoading, setIdentityLoading] = useState(!!token && !cachedUser);
  const [route, setRoute] = useState<Route>(parseHash());
  // 「设置」是独立菜单页（#/config）；「个人设置」仍是盖在当前页上的弹窗。
  // lastView 记住进入设置前那一页，供设置页里的「返回」用。
  const lastView = useRef(route.view === "config" ? "chat" : route.view);
  const [personalOpen, setPersonalOpen] = useState(false);
  const [settingsTab, setSettingsTab] = useState<string | undefined>(undefined);

  // 令牌变化（登录/换账号）都重新拉一次该账号的外观偏好
  useEffect(() => {
    initTheme();
  }, [token]);

  // 换账号后回到干净落地页：个人设置是盖在页面上的弹窗，不随 token 重置的话，
  // 从设置里「退出登录」再登另一个账号，一进来又停在个人设置上。
  useEffect(() => {
    setPersonalOpen(false);
    setSettingsTab(undefined);
  }, [token]);

  // 启动 / 换令牌：用 /auth/me 校验并刷新身份。401 由 api 层清令牌 → 自动回登录页。
  useEffect(() => {
    if (!token) {
      setIdentityLoading(false);
      return;
    }
    let dead = false;
    setIdentityLoading(true);
    api.me()
      .then((r: any) => {
        if (!dead && r?.user) useAuth.getState().setIdentity(r.user, r.teams || []);
      })
      .catch(() => {
        /* 401 已由 api 层清掉令牌；网络错误则继续用缓存身份 */
      })
      .finally(() => {
        if (!dead) setIdentityLoading(false);
      });
    return () => {
      dead = true;
    };
  }, [token]);

  useEffect(() => {
    if (!token || mustChangePassword) return;
    usePolicy.getState().load();
  }, [token, mustChangePassword]);

  // 非管理员直接访问 #/config（手输/旧书签）：身份就绪后把 hash 回落，避免停在无效地址
  useEffect(() => {
    if (route.view === "config" && cachedUser && !isAdminUser(cachedUser)) {
      navigateKeep("chat");
    }
  }, [route.view, cachedUser]);

  useEffect(() => {
    const onHash = () => {
      const r = parseHash();
      if (r.view === "config") {
        // 非管理员不能进团队设置：直接回落对话页（设置页里不再降级成个人设置）
        if (!isAdminUser(useAuth.getState().user)) {
          navigateKeep("chat");
          return;
        }
        setRoute(r);
        return;
      }
      setRoute(r);
      lastView.current = r.view;
    };
    // 顶栏头像/用户名直接开个人设置（不写 hash，关闭后仍停在原页）；可指定初始分类
    const onOpenSettings = (e: Event) => {
      const detail = (e as CustomEvent).detail || {};
      setSettingsTab(typeof detail.tab === "string" ? detail.tab : undefined);
      setPersonalOpen(true);
    };
    window.addEventListener("hashchange", onHash);
    window.addEventListener("ga-open-settings", onOpenSettings);
    return () => {
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener("ga-open-settings", onOpenSettings);
    };
  }, []);

  // 未登录 → 登录页；首次登录 / 管理员重置后 → 强制改密页
  if (!token) return <Login />;
  if (mustChangePassword) return <ForcePasswordChange />;
  if (identityLoading && !cachedUser) {
    return (
      <div className="h-screen flex items-center justify-center text-sm text-slate-400">
        正在加载账号信息…
      </div>
    );
  }

  // 设置页与普通菜单页一样占满内容区；非管理员手输 #/config 时先按对话页渲染，
  // 同时 onHash 会把 hash 回落掉。
  const isAdmin = isAdminUser(cachedUser);
  const view = route.view === "config" && !isAdmin ? "chat" : route.view;
  const baseRoute: Route = { view, params: paramsOf(view) };
  const closeSettings = () => navigateKeep(lastView.current);

  return (
    <>
      <BackgroundLayer />
      <div className="ga-app-root relative z-10 h-screen flex flex-col bg-slate-100 text-slate-800">
        <Topbar />
        <div className="flex-1 overflow-hidden">
          {/* 页面级渲染兜底：单个页面崩了只替换内容区，不会把整站炸成黑屏 */}
          <ErrorBoundary resetKey={view}>
            {/* 旧链接 #/projects 落到「团队」页（项目已由管理员在团队页统一管理） */}
            {(view === "team" || view === "projects") && <Team />}
            {view === "usage" && <Usage />}
            {view === "config" && <Config page mode="team" onClose={closeSettings} />}
            {view === "runs" &&
              (baseRoute.params.id ? (
                <RunDetail runId={baseRoute.params.id} initialFile={baseRoute.params.file}
                           initialAsset={baseRoute.params.asset} />
              ) : (
                <div className="h-full flex flex-col items-center justify-center gap-2 text-slate-400 text-sm">
                  <span>未指定运行记录。</span>
                  <span className="text-xs">出于安全考虑，运行 ID 不进 URL——请在「对话」页右侧的运行面板里查看。</span>
                </div>
              ))}
            {view === "chat" && <Chat route={baseRoute} />}
          </ErrorBoundary>
        </div>
      </div>
      {personalOpen && (
        <Config
          key={`personal:${settingsTab || "default"}`}
          onClose={() => setPersonalOpen(false)}
          initialTab={(settingsTab as any) || undefined}
          mode="personal"
        />
      )}
    </>
  );
}
