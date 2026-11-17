import { create } from "zustand";
import { api } from "./api";

/**
 * 助手行为设置：指导项。
 * 团队版权限固定「仅工作区」，不再有「新会话默认权限」选项（后端同样强制）。
 * 指导项是给模型看的操作要点，由后端注入每一轮的系统提示。
 */
export interface GuidanceItem {
  id: string;
  title: string;
  text: string;
  enabled: boolean;
}

interface PolicyState {
  guidance: GuidanceItem[];
  defaults: GuidanceItem[];
  loaded: boolean;
  load: () => Promise<void>;
  save: (patch: { guidance?: GuidanceItem[] }) => Promise<void>;
}

export const usePolicy = create<PolicyState>((set, get) => ({
  guidance: [],
  defaults: [],
  loaded: false,
  load: async () => {
    try {
      const r: any = await api.assistantPolicy();
      set({
        guidance: Array.isArray(r?.guidance) ? r.guidance : [],
        defaults: Array.isArray(r?.defaults) ? r.defaults : [],
        loaded: true,
      });
    } catch {
      set({ loaded: true });
    }
  },
  save: async (patch) => {
    const next = { guidance: patch.guidance ?? get().guidance };
    const r: any = await api.saveAssistantPolicy(next);
    set({
      guidance: Array.isArray(r?.guidance) ? r.guidance : next.guidance,
    });
  },
}));
