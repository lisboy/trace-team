import { useEffect, useState } from "react";
import { usePolicy, type GuidanceItem } from "../assistantPolicy";

/**
 * 设置 → 助手行为
 * 1) 权限固定「仅工作区」（团队版不给选项，后端同样强制）。
 * 2) 指导项：给模型看的操作要点（如：jar 包里的源码要先解压查看）。
 *    内置若干默认项，可停用/编辑；也能新增自己的条目。
 */
export default function AssistantTab() {
  const { guidance, defaults, loaded, load, save } = usePolicy();
  const [items, setItems] = useState<GuidanceItem[]>(guidance);
  const [dirty, setDirty] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!loaded) load();
  }, [loaded, load]);

  useEffect(() => {
    setItems(guidance);
    setDirty(false);
  }, [guidance]);

  function patch(k: Partial<{ items: GuidanceItem[] }>) {
    if (k.items !== undefined) setItems(k.items);
    setDirty(true);
    setMsg("");
    setErr("");
  }

  function setItem(id: string, patchObj: Partial<GuidanceItem>) {
    patch({ items: items.map((it) => (it.id === id ? { ...it, ...patchObj } : it)) });
  }
  function removeItem(id: string) {
    patch({ items: items.filter((it) => it.id !== id) });
  }
  function addItem() {
    patch({
      items: [...items, { id: `u-${Date.now().toString(36)}`, title: "", text: "", enabled: true }],
    });
  }
  function restoreDefaults() {
    // 把没在列表里的内置项补回来（保留用户已改的措辞与停用状态）
    const have = new Set(items.map((it) => it.id));
    const add = defaults.filter((d) => !have.has(d.id)).map((d) => ({ ...d }));
    patch({ items: [...add, ...items] });
  }

  async function doSave() {
    setBusy(true);
    setMsg("");
    setErr("");
    try {
      const clean = items
        .map((it) => ({ ...it, title: it.title.trim(), text: it.text.trim() }))
        .filter((it) => it.text);
      await save({ guidance: clean });
      setMsg("已保存：指导项从下一轮开始生效");
      setDirty(false);
    } catch (e: any) {
      setErr(e?.message || "保存失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4 max-w-3xl">
      <div className="bg-white rounded border border-slate-200 p-4">
        <div className="text-sm font-medium mb-1">权限范围：仅工作区</div>
        <div className="text-[11.5px] text-slate-500 leading-relaxed">
          团队版固定为「仅工作区」：文件工具只能读写/解析当前项目工作区内的内容，
          越界会被拦下。这样每个成员能改到的范围可预期，也不会碰到别的项目或本机其它目录。
        </div>
      </div>

      <div className="bg-white rounded border border-slate-200 p-4">
        <div className="flex items-center gap-2 mb-1">
          <div className="text-sm font-medium">指导项</div>
          <span className="text-[10px] text-slate-400">
            已启用 {items.filter((i) => i.enabled && i.text.trim()).length} / {items.length}
          </span>
          <div className="ml-auto flex items-center gap-2">
            <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={restoreDefaults}>补回内置项</button>
            <button className="ga-btn ga-btn-secondary ga-btn-sm" onClick={addItem}>+ 新增指导项</button>
          </div>
        </div>
        <div className="text-[11px] text-slate-500 mb-3">
          这些条目会原样写进每轮的系统提示，用来纠正反复踩坑的操作习惯（例如：jar 包里的源码要先解压再查看）。
          写清「遇到什么、该怎么做」最有效；只写原则、不写具体动作的条目容易被忽略。
        </div>
        <div className="space-y-2">
          {items.map((it) => {
            const isDefault = defaults.some((d) => d.id === it.id);
            return (
              <div key={it.id} className="rounded border border-slate-200 p-2.5">
                <div className="flex items-center gap-2 mb-1.5">
                  <input autoComplete="off"
                    type="checkbox"
                    checked={it.enabled}
                    onChange={(e) => setItem(it.id, { enabled: e.target.checked })}
                    title="停用后不再注入系统提示"
                  />
                  <input autoComplete="off"
                    className="flex-1 text-[12.5px] font-medium border border-slate-200 rounded px-2 py-1"
                    value={it.title}
                    placeholder="标题（如：jar 包先解压）"
                    onChange={(e) => setItem(it.id, { title: e.target.value })}
                  />
                  {isDefault && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 shrink-0">内置</span>
                  )}
                  <button
                    className="text-[11px] text-slate-400 hover:text-red-600 shrink-0"
                    onClick={() => removeItem(it.id)}
                    title="删除这条指导项"
                  >
                    删除
                  </button>
                </div>
                <textarea
                  className="w-full border border-slate-200 rounded px-2 py-1.5 text-[12px] leading-relaxed resize-y min-h-[54px]"
                  value={it.text}
                  placeholder="例：遇到 .jar 先解压到工作区临时目录，再 read_file / grep 查看其中的源码与配置；只有 .class 时用反编译工具查看。"
                  onChange={(e) => setItem(it.id, { text: e.target.value })}
                />
              </div>
            );
          })}
          {!items.length && (
            <div className="text-[12px] text-slate-400 py-4 text-center">
              当前没有任何指导项。点「补回内置项」恢复默认清单，或「+ 新增指导项」自己写一条。
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center gap-3">
        <button className="ga-btn ga-btn-primary" disabled={busy || !dirty} onClick={doSave}>
          {busy ? "保存中…" : "保存"}
        </button>
        {dirty && <span className="text-[11px] text-amber-600">有未保存的修改</span>}
        {msg && <span className="text-[11px] text-green-700">{msg}</span>}
        {err && <span className="text-[11px] text-red-600">{err}</span>}
      </div>
    </div>
  );
}
