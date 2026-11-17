"""配置口（设计 §4.1 / §7.2 / §8）：智能体配置、模型档位、用量、审计。

个人版：不再有「成员 / 账号管理」——工作区只属于当前登录用户。
底层 teams / team_members 表仍保留，仅作为数据分区 id（个人空间）使用。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select, text

from game_agent.config import LLM_USER_AGENT
from game_agent.llm.openai_compat import (
    _new_llm_client, _proxy_env_disabled, _system_proxy,
)
from game_agent.api.deps import is_admin_role, require, require_admin
from game_agent.auth.rbac import (
    PERM_AGENT_CONFIG, PERM_VIEW,
)
from game_agent.core.context import RequestContext
from game_agent.agents.roles import ROLE_PROMPTS, ROLE_TOOLS
from game_agent.llm.context_window import DEFAULT_CONTEXT_WINDOW
from game_agent.db.models import (
    Agent, AuditLog, LlmCall, LlmProvider, ModelProfile, Project,
    Session, UsageLedger, User,
)
from game_agent.db.session import SessionLocal
from game_agent.orchestration import policy as policy_mod

router = APIRouter(prefix="/api", tags=["config"])

ROLES = ["main"]


class LlmModelsIn(BaseModel):
    base_url: str = ""
    api_key: str = ""
    provider_id: str | None = None  # 已配好的供应商：直接用它存着的 base_url/key/UA
    protocol: str = ""  # 空=按供应商/自动探测；anthropic 时优先 /v1/models + x-api-key


# 当前真正适配的协议。google/ollama 尚未实现，收口掉避免用户以为能用。
_SUPPORTED_PROTOCOLS = ("openai", "anthropic")


def _model_url_candidates(base: str) -> list[str]:
    """模型列表候选地址：先按用户填的原样，再补 /v1（多数中转站/官方都在 /v1 下）。

    用户经常只填根域名（如 https://ai.jxbw.com），真实接口却在 https://ai.jxbw.com/v1；
    这里把两种情况都试一遍，省掉「地址少个 /v1 就报不是 JSON」的无效排查。"""
    b = (base or "").strip().rstrip("/")
    if not b:
        return []
    cands = [f"{b}/models"]
    if not b.endswith("/v1"):
        cands.append(f"{b}/v1/models")
    seen: set[str] = set()
    out: list[str] = []
    for u in cands:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _models_headers(auth: str, key: str, ua: str) -> dict:
    headers: dict[str, str] = {"Accept": "application/json"}
    if ua:
        headers["User-Agent"] = ua
    if key:
        if auth == "anthropic":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {key}"
    return headers


def _extract_model_ids(data: dict) -> list[str]:
    """从各家 /models 响应里取模型 id：OpenAI/Anthropic 用 data[]，Google 用 models[]。"""
    items = data.get("data")
    if not isinstance(items, list):
        items = data.get("models")
    if not isinstance(items, list):
        return []
    ids: set[str] = set()
    for m in items:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("name")
        if isinstance(mid, str) and mid:
            ids.add(mid[len("models/"):] if mid.startswith("models/") else mid)
    return sorted(ids)


_WAF_HINT_TPL = (
    "被站点前置的 Cloudflare 人机验证拦下了（HTTP {code} + HTML 挑战页，不是 Key 无效）："
    "直接再点一次重试通常就能过；若反复出现，关掉/切换本机代理，"
    "或向供应商要一个不挂验证的 API 域名。")


def _looks_like_waf(status: int, content_type: str, cf_mitigated: str | None,
                    text: str) -> bool:
    if status not in (200, 403, 429, 503):
        return False
    if (cf_mitigated or "").lower() == "challenge":
        return True
    if "html" not in (content_type or "").lower():
        return False
    low = (text or "")[:800].lower()
    if ("just a moment" in low or "cf-chl" in low
            or "challenge-platform" in low):
        return True
    # 200 + 普通 HTML 不算挑战（地址指向控制台/登录页也会这样），
    # 只有非 2xx 的整页 HTML 才按人机验证拦截处理。
    return status in (403, 429, 503) and ("<!doctype html" in low or "<html" in low)


def _is_waf_challenge(r: httpx.Response) -> bool:
    """识别 Cloudflare 一类人机验证响应（403/429/503 + HTML 挑战页）。

    中转站挂在 CF 后面时，出口 IP 声誉差 / 客户端指纹像脚本就会被判给人机验证页。
    这不是 Key 问题，换条连接重试或改直连往往就过——所以单独识别出来重试。"""
    return _looks_like_waf(r.status_code, r.headers.get("content-type") or "",
                           r.headers.get("cf-mitigated"), r.text or "")


async def _with_waf_retry(factory, attempts: int = 3, delays=(0.6, 1.6)):
    """WAF 挑战页短退避重试：同一出口换一条连接常落到另一个 CF 节点，往往就能过。"""
    r = None
    for i in range(attempts):
        r = await factory()
        if not _is_waf_challenge(r):
            return r
        if i < attempts - 1:
            await asyncio.sleep(delays[min(i, len(delays) - 1)])
    return r


@router.post("/llm/models")
async def list_llm_models(body: LlmModelsIn,
                          ctx: RequestContext = Depends(require_admin())):
    """代理拉取模型列表（服务端发起，避开浏览器 CORS 与 key 泄露）。

    地址容错：先试 {base}/models、再试 {base}/v1/models；鉴权先 Bearer，遇 401/403 再试
    x-api-key（Anthropic 风格），命中即用。命中的 base 会回填到供应商，之后对话也走同一
    条可用地址。全部失败时逐条列出「试过哪个地址、返回什么」，而不是只丢一句「不是 JSON」。"""
    ua = LLM_USER_AGENT
    protocol = (body.protocol or "").strip().lower()
    if body.provider_id:
        async with SessionLocal() as s:
            pv = await s.get(LlmProvider, _u(body.provider_id))
            if pv is None or str(pv.team_id) != ctx.team_id:
                raise HTTPException(status_code=404, detail="供应商不存在")
            base = (body.base_url or pv.base_url or "").strip().rstrip("/")
            key = "".join((body.api_key or pv.api_key or "").split())
            ua = pv.user_agent or LLM_USER_AGENT
            protocol = protocol or (pv.protocol or "")
            provider_row = pv
    else:
        provider_row = None
        base = (body.base_url or "").strip().rstrip("/")
        key = "".join((body.api_key or "").split())  # 去掉空白/换行，避免粘贴带入不可见字符
    if not base:
        raise HTTPException(status_code=400, detail="Base URL 必填")
    if not base.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Base URL 需以 http(s):// 开头")

    attempts: list[str] = []
    ids: list[str] = []
    used_url = ""
    saw_waf = False
    conn_fail = 0
    statuses: dict[int, str] = {}
    # 协议已知就先只用对应鉴权，避免多打一轮；协议未知（自动探测）两种都试。
    if protocol == "anthropic":
        auths = ["anthropic"] + (["bearer"] if key else [])
    elif protocol == "openai":
        auths = ["bearer"] + (["anthropic"] if key else [])
    else:
        auths = ["bearer"] + (["anthropic"] if key else [])

    async def _probe(client: httpx.AsyncClient) -> dict:
        p_attempts: list[str] = []
        p_ids: list[str] = []
        p_used = ""
        p_waf = False
        p_conn = 0
        p_nonjson = False
        p_status: dict[int, str] = {}
        for url in _model_url_candidates(base):
            if p_ids:
                break
            for auth in auths:
                try:
                    r = await _with_waf_retry(
                        lambda: client.get(url, headers=_models_headers(auth, key, ua)))
                except Exception as e:  # noqa: BLE001
                    p_conn += 1
                    p_attempts.append(f"· {url}（{auth}）→ 连接失败：{e}")
                    break  # 同一地址换鉴权也不会通，直接换下一个地址
                ct = (r.headers.get("content-type") or "").split(";")[0].strip()
                p_status[r.status_code] = r.text[:200].replace("\n", " ")
                if r.status_code != 200:
                    snippet = r.text[:140].replace("\n", " ")
                    p_attempts.append(f"· {url}（{auth}）→ HTTP {r.status_code}"
                                      f"{(' ' + ct) if ct else ''}：{snippet}")
                    if _is_waf_challenge(r):
                        p_waf = True
                        continue  # 人机验证页：换鉴权方式也一样，交给上层改直连重试
                    if r.status_code in (401, 403):
                        continue  # 可能只是鉴权方式不对，换 x-api-key 再试
                    break  # 404/5xx 等：换地址
                try:
                    data = r.json()
                except Exception:  # noqa: BLE001
                    p_nonjson = True
                    snippet = (r.text or "")[:120].replace("\n", " ").strip()
                    via = f"（被重定向到 {r.url}）" if r.history and str(r.url) != url else ""
                    p_attempts.append(f"· {url}（{auth}）→ HTTP 200 但不是 JSON（{ct or '未知类型'}）"
                                      f"{via}：{snippet}")
                    break
                got = _extract_model_ids(data if isinstance(data, dict) else {})
                if not got:
                    p_attempts.append(f"· {url}（{auth}）→ HTTP 200 但没有模型列表")
                    break
                p_ids, p_used = got, url
                break
        return {"attempts": p_attempts, "ids": p_ids, "used_url": p_used,
                "saw_waf": p_waf, "conn_fail": p_conn, "nonjson": p_nonjson,
                "statuses": p_status}

    # 代理自动模式（与真实对话一致）：系统配了代理先走代理，连不上自动改直连再试；
    # 没配代理就直接直连。只连不通（纯连接失败、拿不到任何 HTTP 响应）才降级，
    # 拿到 401/403/404 说明网络是通的，不降级（那是 key/地址问题，换网也白搭）。
    proxy = None if _proxy_env_disabled() else await asyncio.to_thread(_system_proxy)
    async with _new_llm_client(proxy, ua, timeout=15, follow_redirects=True) as client:
        res = await _probe(client)
    # 代理出口被 CF 判成人机验证、或代理直接回了 HTML 提示页时，改直连往往就正常。
    if proxy and not res["ids"] and (res["saw_waf"] or res["nonjson"]
                                     or (res["conn_fail"] and not res["statuses"])):
        async with _new_llm_client(None, ua, timeout=15, follow_redirects=True) as client:
            res2 = await _probe(client)
        if res2["ids"] or res2["statuses"]:
            if res["saw_waf"]:
                why = "被人机验证拦截"
            elif res["nonjson"]:
                why = "返回的不是 API 数据"
            else:
                why = "连不上"
            res2["attempts"].insert(0, f"· 经系统代理 {proxy} {why}，已自动改直连重试")
        res = res2
    attempts = res["attempts"]
    ids = res["ids"]
    used_url = res["used_url"]
    saw_waf = res["saw_waf"]
    conn_fail = res["conn_fail"]
    nonjson = res["nonjson"]
    statuses = res["statuses"]

    if not ids:
        if conn_fail and not statuses:
            hint = ("连不上这个地址（域名解析失败或网络不通）：请确认 Base URL 拼写正确"
                    "（例如 ai.xxx.com 其实是 api.xxx.com？），以及本机/服务器能否访问该域名。")
        elif saw_waf:
            hint = ("返回的是 Cloudflare 一类人机验证页（HTML，不是 JSON，也不是 Key 问题）："
                    "已经自动重试过并尝试改直连；仍失败就关掉/切换本机代理再试，"
                    "或改用厂商提供的不挂验证的 API 域名。")
        elif 401 in statuses:
            hint = (f"Key 不被接受（共 {len(key)} 字符）：可能无效/未兑换/无该渠道权限。"
                    "可先「+ 添加模型」手填模型名直接保存；对话若报同样错误，说明 Key 确实无权。")
        elif 403 in statuses:
            hint = ("服务器认得请求但拒绝了这个账户（常见于订阅过期 / 分组的 Key 无 /models 权限）。"
                    "可先「+ 添加模型」手填模型名直接保存，或在厂商后台确认套餐与分组。")
        elif nonjson:
            hint = ("返回的是网页（HTML）而不是 API 数据：多半是地址指向了控制台/登录页、"
                    "http 被网关重定向到网页，或者中间代理插了提示页。"
                    "请核对地址（多数是 https:// 且以 /v1 结尾，可从别的能拉取的机器对照），"
                    "也可以直接「+ 添加模型」手填模型名保存，不影响对话使用。")
        else:
            hint = ("确认 Base URL 是接口地址（一般以 /v1 结尾，可访问 /v1/models）；"
                    "也可以先「+ 添加模型」手填模型名直接保存，不影响对话使用。")
        raise HTTPException(status_code=502, detail="拉取模型失败，尝试结果：\n"
                                                  + "\n".join(attempts) + "\n" + hint)

    used_base = used_url[:-len("/models")] if used_url.endswith("/models") else base
    merged: list[dict] = []
    if provider_row is not None:
        async with SessionLocal() as s:
            pv = await s.get(LlmProvider, provider_row.id)
            if pv is not None:
                # 合并而不是覆盖：用户已设置的上下文窗口要保留；新拉到的模型按默认窗口（256k）建
                old = {m["name"]: m for m in _norm_models(pv.models_json)}
                for mid in ids:
                    merged.append(old.get(mid) or {"name": mid, "context_window": DEFAULT_CONTEXT_WINDOW})
                pv.models_json = merged
                if used_base and used_base != (pv.base_url or "").rstrip("/"):
                    pv.base_url = used_base  # 记下真正可用的地址，对话走同一条
                await s.commit()
    if not merged:
        merged = [{"name": mid, "context_window": DEFAULT_CONTEXT_WINDOW} for mid in ids]
    return {"models": merged, "base_url": used_base, "requested_base": base}


class LlmSpeedTestIn(BaseModel):
    provider_id: str | None = None  # 已保存的供应商：用它的 base_url/key/UA/协议
    base_url: str = ""  # 未保存的新供应商表单：直接带地址+key 来测
    api_key: str = ""
    protocol: str = ""
    models: list[str] = []


@router.post("/llm/speed-test")
async def speed_test_llm_models(body: LlmSpeedTestIn,
                                ctx: RequestContext = Depends(require_admin())):
    """一键测速：给目录里的每个模型发一条短请求，按 产出tokens/耗时 算 t/s。

    与真实对话同一条网络路径（同 UA、同系统代理自动降级）。计时是端到端的
    （含首字等待），推理型模型的思考 token 也计入产出，所以 t/s 偏低是正常现象，
    横向对比同一批模型仍然有效。"""
    ua = LLM_USER_AGENT
    protocol = (body.protocol or "").strip().lower()
    if body.provider_id:
        async with SessionLocal() as s:
            pv = await s.get(LlmProvider, _u(body.provider_id))
            if pv is None or str(pv.team_id) != ctx.team_id:
                raise HTTPException(status_code=404, detail="供应商不存在")
            base = (body.base_url or pv.base_url or "").strip().rstrip("/")
            key = "".join((body.api_key or pv.api_key or "").split())
            ua = pv.user_agent or LLM_USER_AGENT
            protocol = protocol or (pv.protocol or "")
    else:
        base = (body.base_url or "").strip().rstrip("/")
        key = "".join((body.api_key or "").split())
    names = [n.strip() for n in body.models if isinstance(n, str) and n.strip()]
    if not base or not base.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Base URL 必填且以 http(s):// 开头")
    if not names:
        raise HTTPException(status_code=400, detail="没有可测的模型")
    if protocol not in _SUPPORTED_PROTOCOLS:
        protocol = "openai"

    proxy = None if _proxy_env_disabled() else await asyncio.to_thread(_system_proxy)
    url = f"{base}/messages" if protocol == "anthropic" else f"{base}/chat/completions"
    if protocol == "anthropic":
        headers = {"Content-Type": "application/json", "x-api-key": key,
                   "anthropic-version": "2023-06-01", "Authorization": f"Bearer {key}"}
    else:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    if ua:
        headers["User-Agent"] = ua

    # 提示词刻意要求一段确定长度的输出：产出太少时 t/s 波动大，太长又拖慢测速
    PROMPT = "请从 1 数到 50，用顿号分隔，直接开始，不要任何解释。"
    MAX_TOKENS = 300

    async def _one(model: str, client: httpx.AsyncClient) -> dict:
        if protocol == "anthropic":
            payload = {"model": model, "max_tokens": MAX_TOKENS,
                       "messages": [{"role": "user", "content": PROMPT}]}
        else:
            payload = {"model": model, "max_tokens": MAX_TOKENS, "stream": False,
                       "messages": [{"role": "user", "content": PROMPT}]}
        t0 = time.perf_counter()
        try:
            r = await _with_waf_retry(
                lambda: client.post(url, headers=headers, json=payload))
        except Exception as e:  # noqa: BLE001
            return {"model": model, "ok": False, "error": f"请求失败：{e}"}
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code != 200:
            if _is_waf_challenge(r):
                return {"model": model, "ok": False, "waf": True, "latency_ms": latency_ms,
                        "error": _WAF_HINT_TPL.format(code=r.status_code)}
            return {"model": model, "ok": False, "latency_ms": latency_ms,
                    "error": f"HTTP {r.status_code}：{r.text[:160]}"}
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            return {"model": model, "ok": False, "latency_ms": latency_ms,
                    "error": "HTTP 200 但响应不是 JSON"}
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        out_tok = usage.get("completion_tokens")
        if out_tok is None:
            out_tok = usage.get("output_tokens")
        out_tok = int(out_tok or 0)
        if out_tok <= 0:
            # 不少中转站非流式响应不带 usage。改走流式重测：每个 SSE 块 ≈ 1 个 token，
            # 数块也能算出 t/s（比 usage 略有误差，但横向对比够用）。
            if protocol == "openai":
                return await _one_stream(model, client)
            snippet = r.text[:200].replace("\n", " ")
            return {"model": model, "ok": False, "latency_ms": latency_ms,
                    "error": f"响应没有用量统计，无法计算 t/s。响应片段：{snippet}"}
        tps = round(out_tok / max(latency_ms, 1) * 1000, 1)
        return {"model": model, "ok": True, "latency_ms": latency_ms,
                "completion_tokens": out_tok, "tok_per_s": tps}

    async def _one_stream(model: str, client: httpx.AsyncClient) -> dict:
        t0 = time.perf_counter()
        try:
            async with client.stream(
                "POST", url, headers=headers,
                json={"model": model, "max_tokens": MAX_TOKENS, "stream": True,
                      "stream_options": {"include_usage": True},
                      "messages": [{"role": "user", "content": PROMPT}]},
            ) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", "replace")
                    if _looks_like_waf(r.status_code, r.headers.get("content-type") or "",
                                       r.headers.get("cf-mitigated"), body):
                        return {"model": model, "ok": False, "waf": True,
                                "error": _WAF_HINT_TPL.format(code=r.status_code)}
                    return {"model": model, "ok": False,
                            "error": f"流式重测 HTTP {r.status_code}：{body[:160]}"}
                chunks = 0
                err = ""
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        evt = json.loads(payload)
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(evt.get("error"), dict):
                        err = str(evt["error"].get("message") or evt["error"])[:160]
                        break
                    u = evt.get("usage") or {}
                    if u.get("completion_tokens"):
                        chunks = max(chunks, int(u["completion_tokens"]))
                        break  # 有 usage 块就以它为准
                    for ch in evt.get("choices") or []:
                        d = ch.get("delta") or {}
                        if (d.get("content") or d.get("reasoning_content")
                                or ch.get("text")):
                            chunks += 1
        except Exception as e:  # noqa: BLE001
            return {"model": model, "ok": False, "error": f"流式重测请求失败：{e}"}
        ms = int((time.perf_counter() - t0) * 1000)
        if err:
            return {"model": model, "ok": False, "error": f"流式重测报错：{err}"}
        if chunks <= 0:
            return {"model": model, "ok": False,
                    "error": "流式重测也没拿到任何产出块，无法计算 t/s"}
        return {"model": model, "ok": True, "completion_tokens": chunks,
                "latency_ms": ms, "tok_per_s": round(chunks / max(ms, 1) * 1000, 1),
                "via": "stream"}

    async def _run_all(p: str | None) -> list[dict]:
        async with _new_llm_client(p, ua, timeout=90, follow_redirects=True) as client:
            sem = asyncio.Semaphore(4)  # 并发太猛会被限流，限 4 路同时测

            async def _guarded(m: str) -> dict:
                async with sem:
                    return await _one(m, client)

            return list(await asyncio.gather(*[_guarded(m) for m in names]))

    results = await _run_all(proxy)
    # 代理出口 IP 被人机验证拦时，代理才是根源：这批被拦的模型改直连再测一遍。
    if proxy and any(x.get("waf") for x in results):
        direct = await _run_all(None)
        merged: list[dict] = []
        for old, new in zip(results, direct):
            if old.get("waf") and not new.get("waf"):
                new = {**new, "via": "direct"}
                merged.append(new)
            else:
                merged.append(old)
        results = merged
    return {"results": results, "protocol": protocol, "base_url": base}


# ---------------------------------------------------------------- LLM 供应商
class ProviderModelIn(BaseModel):
    """供应商下的一个可加载模型：模型名 + 上下文窗口（tokens，0=未设置）+ 是否支持视觉输入。

    vision 是「这个模型能不能看图」的档位开关：关掉时适配层会把附图替换成一句提示
    （见 llm/openai_compat.NO_VISION_NOTE），模型看不到画面。以前这个字段只在后端
    存在、前端没有任何入口，用户对着「图看不到」只能猜——现在每个模型可单独勾选。"""
    name: str
    context_window: int = 0
    vision: bool = False


class LlmProviderIn(BaseModel):
    key: str
    label: str = ""
    protocol: str = "openai"  # openai | anthropic | google | ollama
    base_url: str | None = None
    api_key: str | None = None
    user_agent: str | None = None
    enabled: bool = True
    models: list[ProviderModelIn] | None = None  # 模型目录（新建时一并保存）


class LlmProviderPatch(BaseModel):
    label: str | None = None
    protocol: str | None = None
    base_url: str | None = None
    api_key: str | None = None  # None=保持原值；""=清空；其它=替换（GET 不回传明文，故必须显式给）
    user_agent: str | None = None
    enabled: bool | None = None
    models: list[ProviderModelIn] | None = None  # 模型目录（覆盖）


def _norm_models(raw: list | None) -> list[dict]:
    """统一模型目录格式：兼容存量字符串列表与 {name, context_window[, vision]} 对象列表。

    这里刻意**不**给没填窗口的模型补默认值：0 表示「目录对该模型没有意见」，
    merge_model_catalog_params 据此保留档位里显式配过的窗口；默认 256k 的口径在
    「拉取新模型」与「自动建档位」两处落地，运行时兜底见 DEFAULT_CONTEXT_WINDOW。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw or []:
        if isinstance(item, str):
            name, window, vision = item.strip(), 0, False
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("id") or "").strip()
            try:
                window = int(item.get("context_window") or 0)
            except (TypeError, ValueError):
                window = 0
            vision = bool(item.get("vision"))
        else:
            continue
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "context_window": max(window, 0), "vision": vision})
    return out


def _key_hint(k: str | None) -> tuple[bool, str | None]:
    """密钥只回摘要，绝不回明文：这个接口是登录态可读的，不能把凭据发给浏览器。"""
    if not k:
        return False, None
    if len(k) <= 12:
        return True, f"共 {len(k)} 位"
    return True, f"{k[:6]}…{k[-4]}（共 {len(k)} 位）"


def _provider_out(p: LlmProvider, bound: int = 0) -> dict:
    has, hint = _key_hint(p.api_key)
    return {"id": str(p.id), "key": p.key, "label": p.label, "protocol": p.protocol,
            "base_url": p.base_url, "has_key": has, "key_hint": hint,
            "user_agent": p.user_agent, "enabled": bool(p.enabled),
            "models": _norm_models(p.models_json), "bound_profiles": bound}


@router.get("/llm-providers")
async def list_llm_providers(ctx: RequestContext = Depends(require_admin())):
    async with SessionLocal() as s:
        rows = (await s.execute(select(LlmProvider).where(
            LlmProvider.team_id == _u(ctx.team_id)).order_by(LlmProvider.created_at)
        )).scalars().all()
        counts = dict((await s.execute(
            select(ModelProfile.provider_id, func.count())
            .where(ModelProfile.team_id == _u(ctx.team_id))
            .group_by(ModelProfile.provider_id))).all())
        return {"providers": [_provider_out(p, int(counts.get(p.id, 0) or 0)) for p in rows]}


@router.post("/llm-providers")
async def create_llm_provider(body: LlmProviderIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    key = (body.key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="标识必填")
    if body.protocol not in _SUPPORTED_PROTOCOLS:
        raise HTTPException(
            status_code=400,
            detail=(f"暂不支持「{body.protocol}」协议。当前支持：openai（OpenAI 兼容 /v1/chat/completions）、"
                    "anthropic（Anthropic /v1/messages）。google/ollama 尚未适配。"))
    url = (body.base_url or "").strip() or None
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Base URL 需以 http(s):// 开头")
    async with SessionLocal() as s:
        dup = await s.scalar(select(LlmProvider).where(
            LlmProvider.team_id == _u(ctx.team_id), LlmProvider.key == key))
        if dup:
            raise HTTPException(status_code=409, detail="同标识的供应商已存在")
        p = LlmProvider(team_id=_u(ctx.team_id), key=key,
                        label=(body.label or "").strip() or url or key,
                        protocol=body.protocol, base_url=url,
                        api_key=(body.api_key or "").strip() or None,
                        user_agent=(body.user_agent or "").strip() or None,
                        enabled=bool(body.enabled),
                        models_json=_norm_models([m.model_dump() for m in body.models])
                        if body.models is not None else [])
        s.add(p)
        await s.commit()
        await s.refresh(p)
        s.add(AuditLog(team_id=_u(ctx.team_id), actor_id=_u(ctx.user_id),
                       action="provider.add", target=p.key,
                       detail_json={"protocol": p.protocol, "base_url": p.base_url}))
        await s.commit()
        return {"id": str(p.id)}


@router.put("/llm-providers/{provider_id}")
async def update_llm_provider(provider_id: str, body: LlmProviderPatch,
                              ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """改供应商：只动显式提交的字段（api_key 省略=不动），避免掩码回填把密钥擦成空。"""
    async with SessionLocal() as s:
        p = await s.get(LlmProvider, _u(provider_id))
        if p is None or str(p.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="不存在")
        if body.label is not None:
            p.label = body.label.strip() or p.label
        if body.protocol is not None:
            if body.protocol not in _SUPPORTED_PROTOCOLS:
                raise HTTPException(
                    status_code=400,
                    detail=(f"暂不支持「{body.protocol}」协议。当前支持：openai（OpenAI 兼容 /v1/chat/completions）、"
                            "anthropic（Anthropic /v1/messages）。google/ollama 尚未适配。"))
            p.protocol = body.protocol
        if body.base_url is not None:
            url = body.base_url.strip() or None
            if url and not url.startswith(("http://", "https://")):
                raise HTTPException(status_code=400, detail="Base URL 需以 http(s):// 开头")
            if url != p.base_url:
                p.models_json = []  # 换地址后旧列表不再可信
            p.base_url = url
        if body.api_key is not None:
            k = "".join(body.api_key.split())
            if k != (p.api_key or ""):
                p.models_json = []  # 换 Key 后模型可见范围可能不同，强制重新拉取
            p.api_key = k or None
        if body.user_agent is not None:
            p.user_agent = body.user_agent.strip() or None
        if body.enabled is not None:
            p.enabled = bool(body.enabled)
        if body.models is not None:
            # 模型目录是「供应商 → 可加载模型」的唯一来源：保存后把同一供应商下
            # 已存在档位的上下文窗口 / 视觉开关同步过去，否则档位还按旧值猜。
            models = _norm_models([m.model_dump() for m in body.models])
            p.models_json = models
            windows = {m["name"]: m["context_window"] for m in models}
            visions = {m["name"]: m["vision"] for m in models}
            profs = (await s.execute(select(ModelProfile).where(
                ModelProfile.team_id == _u(ctx.team_id),
                ModelProfile.provider_id == p.id,
                ModelProfile.kind == "llm"))).scalars().all()
            for prof in profs:
                win = windows.get(prof.model)
                if win is None:
                    continue
                params = dict(prof.params_json or {})
                if win:
                    params["context_window"] = win
                elif "context_window" in params:
                    params.pop("context_window", None)
                # vision 只要模型目录里有这一项就同步（True/False 都写），保证关掉能生效
                params["vision"] = bool(visions.get(prof.model))
                prof.params_json = params
        await s.commit()
        s.add(AuditLog(team_id=_u(ctx.team_id), actor_id=_u(ctx.user_id),
                       action="provider.update", target=p.key,
                       detail_json={"fields": sorted(k for k, v in body.model_dump().items()
                                                     if v is not None)}))
        await s.commit()
        return {"ok": True}


@router.delete("/llm-providers/{provider_id}")
async def delete_llm_provider(provider_id: str, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """删除供应商：连同它模型目录下自动生成的对话档位一起清理。

    供应商模型目录是加模型的唯一入口；对话档位只是输入框选择器为了记住「选过哪个
    模型」自动生成的内部记录，用户看不到也不需要管理，不能反过来卡住供应商删除。
    级联删除该供应商下的 llm 档位并解绑引用它们的智能体；媒体档位不绑供应商，不受
    影响。历史 run 的 options_json 只存 key，删除后按「跟随默认」处理，不会报错。
    """
    async with SessionLocal() as s:
        p = await s.get(LlmProvider, _u(provider_id))
        if p is None or str(p.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="不存在")
        profs = (await s.execute(select(ModelProfile).where(
            ModelProfile.team_id == _u(ctx.team_id),
            ModelProfile.provider_id == p.id,
            ModelProfile.kind == "llm"))).scalars().all()
        keys = [pr.key for pr in profs]
        if keys:
            for a in (await s.execute(select(Agent).where(
                    Agent.team_id == _u(ctx.team_id),
                    Agent.model_profile_key.in_(keys)))).scalars().all():
                a.model_profile_key = None
            for pr in profs:
                await s.delete(pr)
        await s.delete(p)
        await s.commit()
        s.add(AuditLog(team_id=_u(ctx.team_id), actor_id=_u(ctx.user_id),
                       action="provider.delete", target=p.key,
                       detail_json={"deleted_profiles": keys}))
        await s.commit()
        return {"ok": True, "deleted_profiles": len(keys)}


# ---------------------------------------------------------------- 模型档位
class ModelProfileIn(BaseModel):
    key: str
    label: str
    kind: str = "llm"  # 仅支持对话档位（媒体生成已移除）
    provider: str = "openai"
    model: str = ""
    provider_id: str | None = None  # 对话档位绑定的 LLM 供应商
    base_url: str | None = None
    api_key_ref: str | None = None
    params_json: dict = {}
    # 非空 = 建该项目专属档位；空 = 团队默认档位（所有项目可用）
    project_id: str | None = None


class ModelProfilePatch(BaseModel):
    label: str | None = None
    model: str | None = None
    provider_id: str | None = None  # ""=解绑供应商
    base_url: str | None = None
    api_key: str | None = None  # None=不动；""=清空；其它=替换（档位的 inline key）
    params_json: dict | None = None  # 合并覆盖（不含 api_key）


@router.get("/model-profiles")
async def list_profiles(project_id: str = Query(""),
                        ctx: RequestContext = Depends(require_admin())):
    """档位列表：始终带团队默认；带 project_id 时再并入该项目的专属档位。

    同名 key 在项目内优先解析项目行（orchestrator._load_profile 同口径），
    这里两条都返回，界面用 project_id 区分「团队默认 / 本项目」。
    """
    async with SessionLocal() as s:
        pid = await _scoped_project(s, ctx, project_id) if project_id else None
        cond = [ModelProfile.team_id == _u(ctx.team_id)]
        if pid is not None:
            cond.append(or_(ModelProfile.project_id.is_(None), ModelProfile.project_id == pid))
        else:
            cond.append(ModelProfile.project_id.is_(None))
        rows = (await s.execute(select(ModelProfile).where(*cond)
                                .order_by(ModelProfile.created_at))).scalars().all()
        provs = {p.id: p for p in (await s.execute(select(LlmProvider).where(
            LlmProvider.team_id == _u(ctx.team_id)))).scalars().all()}
        # 角色绑定关系：配置页要显示"这个档位正被谁用"，删档位前得看得见。
        # 团队默认档位统计团队级角色；项目档位统计该项目内的角色绑定。
        binds: dict[tuple[str, str], list[str]] = {}
        for a in (await s.execute(select(Agent).where(
                Agent.team_id == _u(ctx.team_id)))).scalars().all():
            if a.model_profile_key:
                scope = str(a.project_id) if a.project_id else ""
                binds.setdefault((scope, a.model_profile_key), []).append(a.key)
        out = []
        for p in rows:
            params = dict(p.params_json or {})
            inline_key = params.pop("api_key", None) or p.api_key_ref
            has, hint = _key_hint(inline_key)
            pv = provs.get(p.provider_id) if p.provider_id else None
            scope = str(p.project_id) if p.project_id else ""
            inherited = bool(pid is not None and p.project_id is None)
            out.append({"id": str(p.id), "key": p.key, "label": p.label,
                        "kind": p.kind or "llm", "provider": p.provider, "model": p.model,
                        "provider_id": str(p.provider_id) if p.provider_id else None,
                        "provider_label": pv.label if pv else None,
                        "base_url": p.base_url, "has_key": has, "key_hint": hint,
                        "params_json": params,
                        "project_id": scope or None,
                        "inherited": inherited,
                        "bound_roles": binds.get((scope, p.key), [])})
        return {"profiles": out}


class ModelProfileAutoIn(BaseModel):
    provider_id: str
    model: str
    project_id: str | None = None  # 非空 = 优先复用/创建该项目的专属档位


def _auto_key(key: str, model: str) -> str:
    """生成档位 key：供应商标识 + 清洗后的模型名，限长并去尾部分隔符。"""
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in (model or ""))
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    cleaned = cleaned.strip("-").lower()
    return (f"{key}-{cleaned}"[:60]).strip("-") or f"{key}-model"


@router.post("/model-profiles/auto")
async def auto_profile(body: ModelProfileAutoIn,
                       ctx: RequestContext = Depends(require_admin())):
    """选「供应商 → 模型」时复用/创建对话档位：同供应商+同模型直接复用，否则自动建档位。
    这样输入框旁就能直接选模型，不必先进设置页手工建档位。"""
    model = (body.model or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="模型名必填")
    async with SessionLocal() as s:
        pv = await s.get(LlmProvider, _u(body.provider_id))
        if pv is None or str(pv.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="供应商不存在")
        if not pv.enabled:
            raise HTTPException(status_code=400, detail="该供应商已停用")
        team = _u(ctx.team_id)
        pid = await _scoped_project(s, ctx, body.project_id)
        # 先找同作用域（项目优先，再团队默认）里同供应商+同模型的档位，能复用就不新建
        base_q = select(ModelProfile).where(
            ModelProfile.team_id == team, ModelProfile.provider_id == pv.id,
            ModelProfile.model == model, ModelProfile.kind == "llm")
        existing = None
        if pid is not None:
            existing = await s.scalar(base_q.where(ModelProfile.project_id == pid))
        if existing is None:
            existing = await s.scalar(base_q.where(ModelProfile.project_id.is_(None)))
        if existing is not None:
            return {"key": existing.key, "created": False}
        key = _auto_key(pv.key, model)
        n = 2
        while await s.scalar(select(ModelProfile.id).where(
                ModelProfile.team_id == team, ModelProfile.project_id == pid,
                ModelProfile.key == key)):
            key = f"{_auto_key(pv.key, model)}-{n}"
            n += 1
        # 供应商模型目录里配了窗口/视觉就直接带进档位，选模型即生效；
        # 目录没配窗口时按默认 256k 落，避免档位「未配置」让运行时按更小值保守压缩。
        pm = next((m for m in _norm_models(pv.models_json) if m["name"] == model), None)
        win = (pm["context_window"] if pm else 0) or DEFAULT_CONTEXT_WINDOW
        vision = bool(pm and pm.get("vision"))
        params: dict = {}
        if win:
            params["context_window"] = win
        if vision:
            params["vision"] = True
        p = ModelProfile(team_id=team, project_id=pid, key=key, label=model, kind="llm",
                         provider=pv.protocol or "openai", model=model,
                         provider_id=pv.id, params_json=params)
        s.add(p)
        await s.commit()
        s.add(AuditLog(team_id=team, actor_id=_u(ctx.user_id),
                       action="profile.auto", target=key,
                       detail_json={"provider": pv.key, "model": model,
                                    "project_id": str(pid) if pid else None}))
        await s.commit()
        return {"key": key, "created": True}


@router.post("/model-profiles")
async def create_profile(body: ModelProfileIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    if not body.key.strip():
        raise HTTPException(status_code=400, detail="key 必填")
    if not body.model.strip():
        raise HTTPException(status_code=400, detail="key/model 必填")
    if body.kind != "llm":
        raise HTTPException(status_code=400, detail="kind 必须是 llm（媒体生成已移除）")
    async with SessionLocal() as s:
        pid = await _scoped_project(s, ctx, body.project_id)
        provider_id = None
        if body.provider_id:
            pv = await s.get(LlmProvider, _u(body.provider_id))
            if pv is None or str(pv.team_id) != ctx.team_id:
                raise HTTPException(status_code=404, detail="所选供应商不存在")
            provider_id = pv.id
        exists = await s.scalar(select(ModelProfile).where(
            ModelProfile.team_id == _u(ctx.team_id), ModelProfile.project_id == pid,
            ModelProfile.key == body.key))
        if exists:
            raise HTTPException(status_code=409, detail="同名档位已存在")
        p = ModelProfile(team_id=_u(ctx.team_id), project_id=pid,
                         key=body.key.strip(), label=body.label or body.key,
                         kind=body.kind, provider=body.provider, model=body.model,
                         provider_id=provider_id,
                         base_url=body.base_url, api_key_ref=body.api_key_ref,
                         params_json=body.params_json)
        s.add(p)
        await s.commit()
        await s.refresh(p)
        s.add(AuditLog(team_id=_u(ctx.team_id), actor_id=_u(ctx.user_id),
                       action="profile.add", target=p.key,
                       detail_json={"kind": p.kind, "model": p.model,
                                    "project_id": str(pid) if pid else None,
                                    "provider_id": str(provider_id) if provider_id else None}))
        await s.commit()
        return {"id": str(p.id)}


@router.put("/model-profiles/{profile_id}")
async def update_profile(profile_id: str, body: ModelProfilePatch,
                         ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """改档位：标识不可改（角色按 key 字符串绑定，改 key 会把绑定全部甩空）。"""
    async with SessionLocal() as s:
        p = await s.get(ModelProfile, _u(profile_id))
        if p is None or str(p.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="不存在")
        if body.provider_id is not None:
            if body.provider_id == "":
                p.provider_id = None
            else:
                pv = await s.get(LlmProvider, _u(body.provider_id))
                if pv is None or str(pv.team_id) != ctx.team_id:
                    raise HTTPException(status_code=404, detail="所选供应商不存在")
                p.provider_id = pv.id
        if body.label is not None:
            p.label = body.label.strip() or p.label
        if body.model is not None:
            p.model = body.model.strip()
        if body.base_url is not None:
            p.base_url = body.base_url.strip() or None
        params = dict(p.params_json or {})
        if body.params_json is not None:
            params.update(body.params_json)
        if body.api_key is not None:
            k = "".join(body.api_key.split())
            if k:
                params["api_key"] = k
            else:
                params.pop("api_key", None)
                p.api_key_ref = None
        p.params_json = params
        if not p.model:
            raise HTTPException(status_code=400, detail="模型名不能为空")
        await s.commit()
        s.add(AuditLog(team_id=_u(ctx.team_id), actor_id=_u(ctx.user_id),
                       action="profile.update", target=p.key,
                       detail_json={"model": p.model, "kind": p.kind}))
        await s.commit()
        return {"ok": True}


@router.delete("/model-profiles/{profile_id}")
async def delete_profile(profile_id: str, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    async with SessionLocal() as s:
        p = await s.get(ModelProfile, _u(profile_id))
        if p is None or str(p.team_id) != ctx.team_id:
            raise HTTPException(status_code=404, detail="不存在")
        key, pid = p.key, p.project_id
        await s.delete(p)
        await s.flush()
        # 删档位时清理仍绑定到它、且删除后已无任何可解析档位的智能体：
        # 悬空的 model_profile_key 会让 _load_profile 找不到档位 → 静默回退到
        # 陈旧的环境变量（OPENAI_*），极易表现为莫名其妙的 HTTP 403。
        # 同 key 的其它作用域档位仍在时不解除绑定（项目行删除会回落到团队默认）。
        remaining = (await s.execute(select(ModelProfile).where(
            ModelProfile.team_id == _u(ctx.team_id),
            ModelProfile.key == key))).scalars().all()
        have_team = any(x.project_id is None for x in remaining)
        project_scopes = {str(x.project_id) for x in remaining if x.project_id}
        bound = (await s.execute(select(Agent).where(
            Agent.team_id == _u(ctx.team_id), Agent.model_profile_key == key))).scalars().all()
        unbound: list[dict] = []
        for a in bound:
            resolvable = (have_team if a.project_id is None
                          else (have_team or str(a.project_id) in project_scopes))
            if not resolvable:
                a.model_profile_key = None
                unbound.append({"role": a.key,
                                "project_id": str(a.project_id) if a.project_id else None})
        await s.commit()
        s.add(AuditLog(team_id=_u(ctx.team_id), actor_id=_u(ctx.user_id),
                       action="profile.delete", target=key,
                       detail_json={"project_id": str(pid) if pid else None,
                                    "unbound_roles": unbound}))
        await s.commit()
    return {"ok": True}


def _u(v):  # type: ignore[no-untyped-def]
    if isinstance(v, str):
        try:
            return uuid.UUID(v)
        except ValueError:
            return v
    return v


# ---------------------------------------------------------------- 智能体配置
class AgentIn(BaseModel):
    system_prompt: str | None = None
    model_profile_key: str | None = None
    params_json: dict | None = None  # 扩展参数（如 reasoning_effort）
    # 非空 = 写该项目的覆盖行；空 = 写团队默认行
    project_id: str | None = None


async def _scoped_project(s, ctx: RequestContext, project_id: str | None):
    if not project_id:
        return None
    proj = await s.get(Project, _u(project_id))
    if proj is None or str(proj.team_id) != ctx.team_id:
        raise HTTPException(status_code=404, detail="项目不存在")
    return proj.id


@router.get("/agents")
async def list_agents(project_id: str = Query(""), ctx: RequestContext = Depends(require_admin())):
    """角色配置：返回项目作用域下的生效值（项目覆盖 → 团队默认 → 出厂）。

    `inherited` 表示该项目没有覆盖、当前展示的是团队默认/出厂词，界面据此提示。
    """
    async with SessionLocal() as s:
        pid = await _scoped_project(s, ctx, project_id) if project_id else None
        rows = (await s.execute(select(Agent).where(
            Agent.team_id == _u(ctx.team_id), Agent.project_id.is_(None)))).scalars().all()
        defaults = {a.key: a for a in rows}
        overrides: dict[str, Agent] = {}
        if pid is not None:
            prows = (await s.execute(select(Agent).where(
                Agent.team_id == _u(ctx.team_id), Agent.project_id == pid))).scalars().all()
            overrides = {a.key: a for a in prows}
        out = []
        for key in ROLES:
            base = defaults.get(key)
            ov = overrides.get(key)
            base_prompt = (base.system_prompt if base else "") or ROLE_PROMPTS.get(key, "")
            out.append({
                "key": key,
                "system_prompt": (ov.system_prompt if ov and ov.system_prompt else base_prompt),
                "model_profile_key": ((ov.model_profile_key if ov else None) or
                                      (base.model_profile_key if base else None)),
                "params_json": {**((base.params_json if base else None) or {}),
                                **((ov.params_json if ov else None) or {})},
                "tools": ROLE_TOOLS.get(key, []),
                "is_default": base is None and ov is None,
                "inherited": ov is None,
                "prompt_inherited": ov is None or not ov.system_prompt,
                "model_inherited": ov is None or not ov.model_profile_key,
                "project_id": project_id or None,
            })
        return {"agents": out}


@router.put("/agents/{key}")
async def update_agent(key: str, body: AgentIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    if key not in ROLES:
        raise HTTPException(status_code=404, detail="未知角色")
    async with SessionLocal() as s:
        pid = await _scoped_project(s, ctx, body.project_id)
        row = await s.scalar(select(Agent).where(
            Agent.team_id == _u(ctx.team_id), Agent.project_id == pid, Agent.key == key))
        if row is None:
            # 新建覆盖行：
            # - 团队默认行必须带完整出厂词（它是该项目未覆盖时的兜底正文）；
            # - 项目行未显式给提示词时存空串 = 继承团队默认（之后团队词升级能跟着走，
            #   只覆盖模型绑定的项目不会被一份旧拷贝钉死）。
            if pid is None:
                prompt = (body.system_prompt if body.system_prompt is not None
                          else ROLE_PROMPTS.get(key, ""))
            else:
                prompt = body.system_prompt or ""
            row = Agent(team_id=_u(ctx.team_id), project_id=pid, key=key,
                        system_prompt=prompt, model_profile_key=None, params_json={})
            s.add(row)
        if body.system_prompt is not None:
            row.system_prompt = body.system_prompt
            # 存过和出厂词不同的正文就打「自定义」标记：启动时的出厂词升级迁移据此跳过这一行
            pj = dict(row.params_json or {})
            if body.system_prompt != ROLE_PROMPTS.get(key, ""):
                pj["prompt_custom"] = True
            else:
                pj.pop("prompt_custom", None)
            row.params_json = pj
        if body.model_profile_key is not None:
            row.model_profile_key = body.model_profile_key or None
        if body.params_json is not None:
            # 合并更新（键级覆盖，不清空其它扩展参数）
            row.params_json = {**(row.params_json or {}), **body.params_json}
        await s.commit()
        changed = [k for k, v in {
            "system_prompt": body.system_prompt, "model_profile_key": body.model_profile_key,
            "params_json": body.params_json}.items() if v is not None]
        s.add(AuditLog(team_id=_u(ctx.team_id), actor_id=_u(ctx.user_id),
                       action="agent.update", target=key,
                       detail_json={"fields": changed, "project_id": body.project_id or None,
                                    "model_profile_key": row.model_profile_key}))
        await s.commit()
        return {"ok": True}


@router.delete("/agents/{key}/override")
async def drop_agent_override(key: str, project_id: str = Query(""),
                              ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """删除该项目的覆盖行，恢复继承团队默认（团队默认行不受影响）。"""
    if key not in ROLES:
        raise HTTPException(status_code=404, detail="未知角色")
    if not project_id:
        raise HTTPException(status_code=400, detail="缺少 project_id")
    async with SessionLocal() as s:
        pid = await _scoped_project(s, ctx, project_id)
        await s.execute(delete(Agent).where(
            Agent.team_id == _u(ctx.team_id), Agent.project_id == pid, Agent.key == key))
        await s.commit()
    return {"ok": True}


@router.post("/agents/{key}/reset")
async def reset_agent(key: str, project_id: str = Query(""),
                      ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """恢复出厂 prompt。带 project_id = 只删该项目的覆盖；不带 = 删团队默认覆盖。"""
    if key not in ROLES:
        raise HTTPException(status_code=404, detail="未知角色")
    async with SessionLocal() as s:
        pid = await _scoped_project(s, ctx, project_id) if project_id else None
        await s.execute(delete(Agent).where(
            Agent.team_id == _u(ctx.team_id), Agent.project_id == pid, Agent.key == key))
        await s.commit()
    return {"ok": True}


# ---------------------------------------------------------------- 助手行为：指导项
class PolicyIn(BaseModel):
    guidance: list[dict] | None = None


@router.get("/assistant-policy")
async def get_assistant_policy(ctx: RequestContext = Depends(require_admin())):
    """设置 → 助手行为：指导项（含内置默认项）。

    团队版助手行为固定「仅工作区」，permission_scope 只读返回，不再是可选项。
    """
    pol = await policy_mod.load_policy()
    return {**pol, "permission_scope": policy_mod.DEFAULT_SCOPE,
            "defaults": policy_mod.DEFAULT_GUIDANCE}


@router.put("/assistant-policy")
async def put_assistant_policy(body: PolicyIn,
                               ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    cur = await policy_mod.load_policy()
    pol = await policy_mod.save_policy(
        policy_mod.DEFAULT_SCOPE,
        body.guidance if body.guidance is not None else cur["guidance"],
    )
    return {"ok": True, **pol}


# 个人版已移除「成员管理」：不再有邀请/添加账号、改角色、移除成员。
# 一个工作区只属于当前登录用户，账号本身由登录体系（/api/auth）管理。

# ---------------------------------------------------------------- 用量
def _local_offset_hours() -> int:
    """服务器所在时区相对 UTC 的整小时偏移（按本地日切统计用量，否则用户看到的"今天"差 8 天）。"""
    off = datetime.now().astimezone().utcoffset()
    return int(round((off.total_seconds() if off else 0) / 3600))


# 用量页是「看一眼」的仪表盘，没必要每次重新扫描整个窗口：
# 同一 (工作区, 窗口, 筛选) 的响应缓存 15s，切窗口/切筛选/切页面再回来都是秒开。
# 15s 的陈旧度对统计页无感（它本来就不是实时流），换来的是不再重复扫 3 万行。
_USAGE_CACHE: dict[tuple, tuple[float, dict]] = {}
_USAGE_CACHE_TTL = 15.0


def _hex(v) -> str:
    """把 UUID / hex 字符串统一成 32 位 hex（库里的 uuid 列就是 hex 文本）。"""
    return v.hex if hasattr(v, "hex") else str(v).replace("-", "")


@router.get("/usage")
async def usage(days: int = Query(30, ge=1, le=365),
                agent: str | None = Query(None),
                model: str | None = Query(None),
                project_id: str | None = Query(None),
                user_id: str | None = Query(None),
                ctx: RequestContext = Depends(require(PERM_VIEW))):
    """用量台账。

    可见范围：管理员看全团队，可再按项目 / 成员筛选；其他角色强制只看自己
    （project_id 只能选自己被分派的项目）。agent / model 仍是类型筛选。
    facets 永远按未筛选的全集返回，避免选中后下拉只剩自己。
    """
    admin = is_admin_role(ctx.role)
    if not admin:
        # facets 回的是 32 位 hex，ctx 里是带连字符的 UUID：统一成 hex 再比，
        # 否则用户在下拉里选自己/自己项目也会被判成越权。
        if user_id and _hex(user_id) != _hex(ctx.user_id):
            raise HTTPException(status_code=403, detail="只能查看自己的用量")
        user_id = _hex(ctx.user_id)
        if project_id and _hex(project_id) not in {_hex(x) for x in ctx.project_ids}:
            raise HTTPException(status_code=403, detail="无权查看该项目")
    ck = (str(ctx.team_id), days, agent or "", model or "",
          project_id or "", user_id or "", "all" if admin else "self")
    hit = _USAGE_CACHE.get(ck)
    if hit and time.monotonic() - hit[0] < _USAGE_CACHE_TTL:
        return hit[1]
    data = await _usage_payload(ctx, days, agent, model, project_id, user_id, admin)
    if len(_USAGE_CACHE) >= 32:  # 兜底：别让筛选组合把内存撑起来
        _USAGE_CACHE.clear()
    _USAGE_CACHE[ck] = (time.monotonic(), data)
    return data


async def _usage_payload(ctx: RequestContext, days: int, agent: str | None,
                         model: str | None, project_id: str | None,
                         user_id: str | None, admin: bool) -> dict:
    """把窗口内的 llm_calls 一次扫进来，总量/按模型/按角色/按天/按项目/按会话/按成员
    全部在内存里累加。

    以前每个维度一条 SQL：同样是这几万行，被 SQLite 反复扫描 6 遍（实测 3.4 万行库上
    单条 17~84ms，一页串行 ≈260ms），而一次扫描 + Python 累加只要一份 IO。
    cast/累加的都是整数列，内存里做没有精度问题；365 天窗口也只会读一次。
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    tz_off = _local_offset_hours()
    tid = _u(ctx.team_id)

    # run 维度的可见范围：非管理员只能看自己发起的调用；项目筛选也落在 run 上。
    run_where = ["team_id = :rtid", "created_at >= :rsince"]
    run_params: dict = {"rtid": _hex(ctx.team_id), "rsince": since}
    if project_id:
        run_where.append("project_id = :rpid")
        run_params["rpid"] = _hex(project_id)
    elif not admin:
        scoped = [_hex(x) for x in ctx.project_ids]
        if not scoped:
            run_where.append("1 = 0")
        else:
            marks = ",".join(f":rp{i}" for i in range(len(scoped)))
            run_where.append(f"project_id IN ({marks})")
            run_params.update({f"rp{i}": h for i, h in enumerate(scoped)})
    if user_id:
        run_where.append("triggered_by = :ruid")
        run_params["ruid"] = _hex(user_id)
    run_scope_sql = ("run_id IN (SELECT id FROM runs WHERE "
                     + " AND ".join(run_where) + ")")

    def _rate(cached: int, prompt: int, reported: int):
        return round(cached / prompt * 100, 1) if prompt > 0 and reported > 0 else None

    def _tps(completion: int, ms: int):
        return round(completion / (ms / 1000), 1) if ms > 0 and completion > 0 else None

    async with SessionLocal() as s:
        ledger_where = "team_id = :tid AND created_at >= :since"
        if not admin:
            ledger_where += " AND " + run_scope_sql
        rows = (await s.execute(text(
            "SELECT kind, COALESCE(SUM(amount), 0) FROM usage_ledger"
            " WHERE " + ledger_where + " GROUP BY kind"),
            {"tid": _hex(tid), "since": since, **run_params})).all()
        # 一次扫描把窗口内的调用全取回来，各维度在内存里累加。
        # 以前是 8 条各自带 team_id+created_at 条件的聚合，SQLite 把同几万行反复扫了 8 遍
        # （3.4 万行库实测 287ms）；一次扫描只要 ~80ms。这里直接下发原生 SQL 而不是走 ORM：
        # ORM 的 Uuid/UTCDateTime 列会逐行做类型转换，能加回来几百毫秒。
        where = ["team_id = :tid", "created_at >= :since"]
        params: dict = {"tid": _hex(tid), "since": since}
        if agent:
            where.append("agent_key = :agent")
            params["agent"] = agent
        if model:
            where.append("model = :model")
            params["model"] = model
        if not admin or project_id or user_id:
            where.append(run_scope_sql)
            params.update(run_params)
        scan = (await s.execute(text(
            "SELECT created_at, prompt_tokens, completion_tokens,"
            " cached_tokens, cache_reported, latency_ms, model, agent_key, run_id"
            " FROM llm_calls WHERE " + " AND ".join(where)), params)).all()
        # 本地日切：created_at 是 UTC 文本，按服务器时区偏移归日（否则「今天」会差一天）。
        # 逐行做偏移解析太贵，这里只解一次基准日，用「日期 → 偏移后日期」的缓存摊掉。
        _day_cache: dict[str, str] = {}

        acc: dict[str, list[int]] = {}       # model -> [calls, prompt, completion, ms, cached, reported]
        ag: dict[str, list[int]] = {}        # agent_key -> [calls, tokens, prompt, cached, reported, completion, ms]
        dy: dict[str, list[int]] = {}        # 本地日 -> [calls, tokens, completion, ms]
        totals = [0, 0, 0, 0, 0, 0]          # calls, prompt, completion, ms, cached, reported
        run_ids: set = set()
        for (created_at, p, ct, ca, rep, ms, m, ak, rid) in scan:
            p, ct, ca, rep, ms = int(p or 0), int(ct or 0), int(ca or 0), int(rep or 0), int(ms or 0)
            totals[0] += 1
            totals[1] += p
            totals[2] += ct
            totals[3] += ms
            totals[4] += ca
            totals[5] += rep
            b = acc.setdefault(m or "unknown", [0, 0, 0, 0, 0, 0])
            b[0] += 1
            b[1] += p
            b[2] += ct
            b[3] += ms
            b[4] += ca
            b[5] += rep
            a = ag.setdefault(ak or "unknown", [0, 0, 0, 0, 0, 0, 0])
            a[0] += 1
            a[1] += p + ct
            a[2] += p
            a[3] += ca
            a[4] += rep
            a[5] += ct
            a[6] += ms
            # 本地日切：同一毫秒级的 created_at 前缀会大量重复，按「UTC 小时」缓存归日结果。
            # created_at 的类型随方言变：SQLite 的原生 SQL 回来是文本，MySQL/aiomysql 回来是
            # datetime 对象——直接切片会 TypeError（线上 MySQL 版 /api/usage 一直 500 的原因）。
            # 两种都 str() 后取前 13 位：'YYYY-MM-DD HH' 与 'YYYY-MM-DDTHH' 都能被 fromisoformat 吃下。
            utc_hour = str(created_at)[:13]
            dkey = _day_cache.get(utc_hour)
            if dkey is None:
                dkey = (datetime.fromisoformat(utc_hour + ":00:00")
                        + timedelta(hours=tz_off)).strftime("%Y-%m-%d")
                _day_cache[utc_hour] = dkey
            d = dy.setdefault(dkey, [0, 0, 0, 0])
            d[0] += 1
            d[1] += p + ct
            d[2] += ct
            d[3] += ms
            if rid is not None:
                run_ids.add(rid)
        by_day = [(k, *v) for k, v in sorted(dy.items())]
        # 下拉候选项按「未筛选全集」算：选中某项后下拉不能只剩它自己。
        # 筛选生效时补一次同一窗口的轻列扫描（只在用户真用了筛选时发生，且有 15s 缓存）。
        facet_agent: dict[str, int] = {}
        facet_model: dict[str, int] = {}
        if agent or model:
            facet_where = ["team_id = :tid", "created_at >= :since"]
            facet_params: dict = {"tid": _hex(tid), "since": since}
            if not admin or project_id or user_id:
                facet_where.append(run_scope_sql)
                facet_params.update(run_params)
            full = (await s.execute(text(
                "SELECT agent_key, model, prompt_tokens FROM llm_calls"
                " WHERE " + " AND ".join(facet_where)), facet_params)).all()
            for ak, m, p in full:
                if ak is not None:
                    facet_agent[ak] = facet_agent.get(ak, 0) + int(p or 0)
                if m is not None:
                    facet_model[m] = facet_model.get(m, 0) + int(p or 0)
        else:
            facet_agent = {a: v[2] for a, v in ag.items()}
            facet_model = {m: v[1] for m, v in acc.items()}
        agent_facets = [k for k, _ in sorted(facet_agent.items(), key=lambda x: -x[1])]
        model_facets = [k for k, _ in sorted(facet_model.items(), key=lambda x: -x[1])]
        # 按项目/按会话：run → session → project 的归属关系一次性取回（只查用到的 id），
        # 不再对每行 llm_call 做三重 join 再 group by。
        run_owner: dict = {}
        if run_ids:
            id_list = list(run_ids)
            for i in range(0, len(id_list), 500):  # SQLite 绑定参数有上限，分批查
                marks = ",".join(f":r{j}" for j in range(len(id_list[i:i + 500])))
                for rid, pid, sid, uid in (await s.execute(text(
                        "SELECT id, project_id, session_id, triggered_by FROM runs"
                        " WHERE id IN (" + marks + ")"),
                        {f"r{j}": h for j, h in enumerate(id_list[i:i + 500])})).all():
                    run_owner[_hex(rid)] = (pid, sid, uid)
        names = {_hex(r[0]): r[1] for r in (await s.execute(text(
            "SELECT id, name FROM projects WHERE team_id = :tid"),
            {"tid": _hex(tid)})).all()}
        sid_list = list({_hex(v[1]) for v in run_owner.values()})
        sess_title: dict = {}
        sess_pid: dict = {}
        if sid_list:
            for i in range(0, len(sid_list), 500):
                chunk = sid_list[i:i + 500]
                marks = ",".join(f":s{j}" for j in range(len(chunk)))
                for sid, title, pid in (await s.execute(text(
                        "SELECT id, title, project_id FROM sessions WHERE id IN (" + marks + ")"),
                        {f"s{j}": h for j, h in enumerate(chunk)})).all():
                    k = _hex(sid)
                    sess_title[k] = title
                    sess_pid[k] = _hex(pid)
        by_project: dict = {}
        by_session: dict = {}
        by_user: dict = {}
        # run → (项目hex, 会话hex, 成员hex) 先算一次：如果按每一行调用去转 UUID，
        # 3.4 万行就是 21 万次字符串转换（实测占总耗时 1/3）。
        owner_hex = {rid: (_hex(p), _hex(s), _hex(u))
                     for rid, (p, s, u) in run_owner.items()}
        for (created_at, p, ct, ca, rep, ms, m, ak, rid) in scan:
            owner = owner_hex.get(rid) if rid is not None else None
            if not owner:
                continue          # 没有 run 归属的调用不计入「按项目/按会话」（与旧口径一致）
            pid, sid, uid = owner
            p, ct, ca, rep, ms = int(p or 0), int(ct or 0), int(ca or 0), int(rep or 0), int(ms or 0)
            bp = by_project.setdefault(pid, [0, 0, 0, 0])
            bp[0] += 1
            bp[1] += p
            bp[2] += ct
            bp[3] += ms
            bs = by_session.setdefault(sid, [0, 0, 0, 0, 0, 0])
            bs[0] += 1
            bs[1] += p
            bs[2] += ct
            bs[3] += ms
            bs[4] += ca
            bs[5] += rep
            bu = by_user.setdefault(uid, [0, 0, 0, 0, 0, 0])
            bu[0] += 1
            bu[1] += p
            bu[2] += ct
            bu[3] += ms
            bu[4] += ca
            bu[5] += rep
        by_session = sorted(by_session.items(), key=lambda kv: -kv[1][1])[:50]
        # 成员名：用量表只有用户 id，界面要显示「谁在用」
        user_names: dict = {}
        if by_user:
            uid_list = list(by_user)
            for i in range(0, len(uid_list), 500):
                chunk = uid_list[i:i + 500]
                marks = ",".join(f":u{j}" for j in range(len(chunk)))
                for uid, username, display in (await s.execute(text(
                        "SELECT id, username, display_name FROM users WHERE id IN (" + marks + ")"),
                        {f"u{j}": h for j, h in enumerate(chunk)})).all():
                    user_names[_hex(uid)] = display or username or "未知成员"

        # 模型名 → 档位/供应商：用量表里只有裸模型名，光看名字猜不出走的哪个端点
        prov_labels = {r[0]: r[1] for r in (await s.execute(text(
            "SELECT id, label FROM llm_providers WHERE team_id = :tid"),
            {"tid": _hex(tid)})).all()}
        prof_by_model: dict[str, dict] = {}
        for model_name, label, kind, prov_id in (await s.execute(text(
                "SELECT model, label, kind, provider_id FROM model_profiles"
                " WHERE team_id = :tid"), {"tid": _hex(tid)})).all():
            if model_name:
                prof_by_model.setdefault(model_name, {
                    "profile": label, "kind": kind or "llm",
                    "provider": prov_labels.get(prov_id) if prov_id else None})

        prompt_total = totals[1]
        completion = totals[2]
        total_ms = totals[3]
        models = [{"model": m or "unknown", "calls": v[0], "prompt": v[1],
                   "completion": v[2], "latency_ms": v[3],
                   "cached": v[4], "cache_hit_rate": _rate(v[4], v[1], v[5]),
                   "tokens_per_sec": _tps(v[2], v[3]),
                   **(prof_by_model.get(m) or {})}
                  for m, v in acc.items()]
        models.sort(key=lambda x: -(x["calls"] + x["prompt"] + x["completion"]))
        projects = [{"project_id": pid, "project": names.get(pid, pid),
                     "calls": v[0], "prompt": v[1], "completion": v[2],
                     "latency_ms": v[3], "tokens_per_sec": _tps(v[2], v[3])}
                    for pid, v in by_project.items()]
        projects.sort(key=lambda x: -(x["calls"] + x["prompt"] + x["completion"]))
        days_out = [{"date": str(d), "calls": v[0], "tokens": v[1],
                     "completion": v[2], "tokens_per_sec": _tps(v[2], v[3])}
                    for d, *v in by_day]
        sessions = [{"session_id": str(sid),
                     "title": sess_title.get(sid) or names.get(sess_pid.get(sid, ""), "") or "未命名会话",
                     "calls": v[0], "prompt": v[1], "completion": v[2],
                     "latency_ms": v[3], "cached": v[4],
                     "cache_hit_rate": _rate(v[4], v[1], v[5]),
                     "tokens_per_sec": _tps(v[2], v[3])}
                    for sid, v in by_session]
        # 按成员：管理员看排行，其他人只会有自己这一行（scope 已在 run_where 里收紧）
        members = [{"user_id": uid,
                    "user": user_names.get(uid, "未知成员"),
                    "calls": v[0], "prompt": v[1], "completion": v[2],
                    "latency_ms": v[3], "cached": v[4],
                    "cache_hit_rate": _rate(v[4], v[1], v[5]),
                    "tokens_per_sec": _tps(v[2], v[3])}
                   for uid, v in by_user.items()]
        members.sort(key=lambda x: -(x["calls"] + x["prompt"] + x["completion"]))
        # 筛选下拉：项目/成员按「可见范围全集」返回，不随 agent/model 收窄
        if admin:
            user_rows = (await s.execute(text(
                "SELECT u.id, u.username, u.display_name FROM users u"
                " JOIN team_members m ON m.user_id = u.id"
                " WHERE m.team_id = :tid"), {"tid": _hex(tid)})).all()
        else:
            user_rows = (await s.execute(text(
                "SELECT id, username, display_name FROM users WHERE id = :uid"),
                {"uid": _hex(ctx.user_id)})).all()
        user_facets = sorted(
            ({"user_id": _hex(r[0]), "user": r[2] or r[1] or "未知成员"}
             for r in user_rows), key=lambda x: x["user"])
        if admin:
            project_facets = [{"project_id": pid, "project": nm}
                              for pid, nm in names.items()]
        else:
            visible = {_hex(x) for x in ctx.project_ids}
            project_facets = [{"project_id": pid, "project": nm}
                              for pid, nm in names.items() if pid in visible]
        project_facets.sort(key=lambda x: x["project"])
        # run 成功率：光看 token 看不出这批钱花得值不值。
        # 必须和 token 口径同源，否则成员会看到全团队的 run 计数。
        run_stats = (await s.execute(text(
            "SELECT status, COUNT(*) FROM runs WHERE " + " AND ".join(run_where)
            + " GROUP BY status"), run_params)).all()
        return {
            "days": days,
            "tokens": {
                "prompt": prompt_total, "completion": completion, "calls": totals[0],
                "latency_ms": total_ms,
                "cached": totals[4],
                "cache_hit_rate": _rate(totals[4], prompt_total, totals[5]),
                "tokens_per_sec": _tps(completion, total_ms),
            },
            "runs": {k or "unknown": int(v) for k, v in run_stats},
            "ledger": {k: int(v) for k, v in rows},
            "by_model": models,
            "by_project": projects,
            "by_user": members,
            "by_day": days_out,
            "by_session": sessions,
            "agent_facets": agent_facets,
            "model_facets": model_facets,
            "project_facets": project_facets,
            "user_facets": user_facets,
            "scope": "all" if admin else "self",
            "by_agent": {k: {
                "calls": v[0], "tokens": v[1], "prompt": v[2], "cached": v[3],
                "cache_hit_rate": _rate(v[3], v[2], v[4]),
                "tokens_per_sec": _tps(v[5], v[6])}
                for k, v in ag.items()},
        }


# ---------------------------------------------------------------- 审计
@router.get("/audit")
async def audit(limit: int = Query(50, ge=1, le=500),
                q: str | None = Query(None, max_length=40),
                ctx: RequestContext = Depends(require_admin())):
    """操作留痕（仅管理员）。q 按动作/对象模糊过滤（配置类记录一多，光翻页找不到那次改了什么）。"""
    async with SessionLocal() as s:
        stmt = select(AuditLog).where(AuditLog.team_id == _u(ctx.team_id))
        if q:
            like = f"%{q.strip()}%"
            stmt = stmt.where(or_(AuditLog.action.like(like), AuditLog.target.like(like)))
        rows = (await s.execute(stmt.order_by(AuditLog.created_at.desc()).limit(limit))).scalars().all()
        actors = {u.id: u.display_name for u in (await s.execute(
            select(User).where(User.id.in_([a.actor_id for a in rows if a.actor_id]))
        )).scalars().all()} if any(a.actor_id for a in rows) else {}
        return {"logs": [{
            "id": str(a.id), "action": a.action, "target": a.target,
            "actor": actors.get(a.actor_id) if a.actor_id else None,
            "ip": a.ip, "detail": a.detail_json, "created_at": str(a.created_at),
        } for a in rows]}
