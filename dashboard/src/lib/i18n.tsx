import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

/**
 * Bilingual (English / 简体中文) UI layer.
 *
 * A tiny, dependency-free i18n: a flat message dictionary per locale, a
 * provider that persists the choice to localStorage and stamps
 * `<html lang>` / `data-lang` (so CSS can switch the CJK font stack), and a
 * `t(key, vars)` lookup with `{name}` interpolation. Missing keys fall back to
 * the key itself (visible in dev, never a crash).
 *
 * Data values (loan ids, product/region strings, risk bands Q1–Q5) are NOT
 * translated — only chrome and labels are.
 */

export type Lang = "en" | "zh";

const STORAGE_KEY = "waspada.lang";

type Dict = Record<string, string>;

const EN: Dict = {
  // --- app chrome / topbar ---
  "brand.name": "WASPADA",
  "brand.sub": "Early-Warning Collections",
  "nav.worklist": "Work List",
  "nav.health": "Portfolio Health",
  "nav.alerts": "Alerts",
  "nav.debate": "Agent Debate",
  "top.fixtureDemo": "Fixture demo",
  "top.signOut": "Sign out",
  "top.loading": "Loading portfolio…",
  "top.loadError": "Couldn’t load the dashboard payload: {message}",
  "top.contestedOne": "⚖ {count} account contested — see debate ↓",
  "top.contestedMany": "⚖ {count} accounts contested — see debate ↓",
  "lang.toggle": "中文",
  "lang.label": "Language",

  // --- work list ---
  "wl.title": "Work List",
  "wl.showing": "Showing {shown} of {total} ranked accounts",
  "wl.top": "Top",
  "wl.showCount": "Number of accounts to show",
  "wl.col.rank": "#",
  "wl.col.loan": "Loan",
  "wl.col.segment": "Segment",
  "wl.col.pdefault": "P(default)",
  "wl.col.band": "Band",
  "wl.col.el": "Exp. loss",
  "wl.col.action": "Action",
  "wl.caption": "Ranked collections work-list, sortable by probability of default. Select a row to view account detail.",
  "wl.contested": "⚖ contested",
  "wl.contested.title": "This account was contested in the Agent Society debate",
  "wl.contested.aria": "Account {id} was contested — jump to debate",
  "wl.assumptions.label": "Expected loss assumptions:",
  "wl.assumptions.body": "LGD = 45% (Basel foundation-IRB, unsecured consumer). EAD = outstanding_principal (amortizing installment). EL = PD × LGD × EAD.",

  // --- portfolio health ---
  "ph.title": "Portfolio Health",
  "ph.npl.label": "NPL ratio",
  "ph.npl.hint": "Fraction of accounts in delinquent or default status.",
  "ph.el.label": "Total expected loss",
  "ph.el.assumptions": "EL = PD × LGD × EAD · LGD=45% (Basel foundation-IRB, unsecured consumer) · EAD=outstanding_principal (amortizing installment).",
  "ph.vintage.title": "Vintage default rate",
  "ph.vintage.empty": "No vintage data.",
  "ph.status.title": "Status mix",
  "ph.status.empty": "No status mix data.",

  // --- alerts ---
  "al.title": "Alerts",
  "al.breachOne": "{count} active breach",
  "al.breachMany": "{count} active breaches",
  "al.empty": "No alerts. Portfolio within tolerance.",
  "al.value": "Value",
  "al.threshold": "Threshold",
  "sev.critical": "Critical",
  "sev.high": "High",
  "sev.moderate": "Moderate",
  "sev.low": "Low",
  "sev.info": "Info",
  "metric.npl_ratio": "NPL Ratio",
  "metric.vintage_default_rate": "Vintage Default Rate",
  "segment.portfolioWide": "Portfolio-wide",

  // --- account drawer ---
  "dr.eyebrow": "Account",
  "dr.close": "Close account detail",
  "dr.pdefault": "Probability of default",
  "dr.action": "Recommended action",
  "dr.segment": "Segment",
  "dr.product": "Product",
  "dr.region": "Region",
  "dr.band": "Risk band",

  // --- action / band badges ---
  "action.call": "Call",
  "action.watch": "Watch",
  "action.auto-cure": "Auto-cure",
  "action.aria": "Recommended action: {label}",
  "band.aria": "Risk band {band}",

  // --- agent dialogue ---
  "ad.title": "Agent Society · Risk Debate",
  "ad.subtitle": "The Risk Auditor audits the riskiest scores; contested calls are argued, ruled, and — when unresolved — escalated to the analyst.",
  "ad.streaming": "Streaming",
  "ad.live": "Live",
  "ad.streaming.title": "Watching the live stream",
  "ad.live.title": "Showing a live Qwen run",
  "ad.disputeOne": "{count} dispute",
  "ad.disputeMany": "{count} disputes",
  "ad.escalated": " · {count} escalated",
  "ad.stopStream": "Stop stream",
  "ad.backToFixture": "Back to fixture",
  "ad.watchLive": "Watch live",
  "ad.connecting": "Connecting…",
  "ad.runLive": "Run live (Qwen)",
  "ad.running": "Running…",
  "ad.runError": "Couldn’t run live:",
  "ad.watchError": "Couldn’t watch live:",
  "ad.debatingBody": "The Risk Auditor, Actuary, and Credit Arbiter are debating the riskiest accounts over Qwen…",
  "ad.empty.streamDone": "No disputes this stream — the auditor agreed with every audited score.",
  "ad.empty.streamWait": "Waiting for the first round…",
  "ad.empty.live": "No disputes this live run — the auditor agreed with every audited score.",
  "ad.empty.fixture": "No disputes this run — the auditor agreed with every audited score.",
  "ad.streamingMore": "Streaming more rounds…",
  "ad.sr.opening": "Opening the live debate stream…",
  "ad.sr.complete": "Live debate complete.",
  "ad.sr.streaming": "Streaming the live debate…",
  "ad.sr.running": "Running the live Qwen debate…",
  "ad.sr.loaded": "Live debate loaded.",
  "ad.vs": "vs",
  "ad.resolvedBy": "Resolved by {resolver}:",
  "speaker.risk_auditor": "Risk Auditor",
  "speaker.risk_model": "Actuary",
  "speaker.arbiter": "Credit Arbiter",
  "speaker.human": "Analyst",
  "res.upheld": "Upheld",
  "res.overridden": "Overridden",
  "res.escalated_approved": "Escalated · approved",
  "res.escalated_rejected": "Escalated · rejected",

  // --- auth ---
  "auth.sub": "Early-warning collections",
  "auth.signIn": "Sign in",
  "auth.createAccount": "Create account",
  "auth.forgot": "Forgot password",
  "auth.reset": "Reset password",
  "auth.updateBtn": "Update password",
  "auth.checkEmail": "Check your email",
  "auth.updated": "Password updated",
  "auth.email": "Email",
  "auth.password": "Password",
  "auth.confirm": "Confirm password",
  "auth.newPassword": "New password",
  "auth.confirmNew": "Confirm new password",
  "auth.token": "Reset token",
  "auth.demo": "Demo analyst",
  "auth.createLink": "Create an account",
  "auth.forgotLink": "Forgot password?",
  "auth.backToSignIn": "← Back to sign in",
  "auth.backToSignInPlain": "Back to sign in",
  "auth.backToSignInFwd": "→ Back to sign in",
  "auth.sendToken": "Send reset token",
  "auth.haveToken": "I have a token →",
  "auth.minChars": "Minimum 8 characters.",
  "auth.signInNew": "Sign in with your new password.",
  "auth.checkEmailBody": "If {email} is registered, a reset token has been issued. In production it would land in your inbox; for this demo the token is shown below.",
  "auth.tokenLabel": "Reset token (demo delivery)",
  "auth.fillDemo": "Fill demo analyst credentials",
  "auth.err.mismatch": "Passwords don’t match.",
  "auth.err.invalidLogin": "Invalid email or password.",
  "auth.err.signInFailed": "Sign-in failed.",
  "auth.err.dup": "That email is already registered. Try signing in.",
  "auth.err.regFailed": "Registration failed.",
  "auth.err.tokenFailed": "Couldn’t send reset token.",
  "auth.err.invalidToken": "Invalid or expired reset token.",
  "auth.err.resetFailed": "Reset failed.",
  "auth.err.network": "Couldn’t reach the server. Check it’s running on :8080.",
};

const ZH: Dict = {
  // --- app chrome / topbar ---
  "brand.name": "威思塔",
  "brand.sub": "早期预警 · 催收系统",
  "nav.worklist": "工作清单",
  "nav.health": "组合健康",
  "nav.alerts": "预警",
  "nav.debate": "智能体辩论",
  "top.fixtureDemo": "样例演示",
  "top.signOut": "退出登录",
  "top.loading": "正在加载组合…",
  "top.loadError": "无法加载看板数据：{message}",
  "top.contestedOne": "⚖ {count} 个账户存在争议 — 查看辩论 ↓",
  "top.contestedMany": "⚖ {count} 个账户存在争议 — 查看辩论 ↓",
  "lang.toggle": "EN",
  "lang.label": "语言",

  // --- work list ---
  "wl.title": "工作清单",
  "wl.showing": "共 {total} 个排序账户，显示 {shown} 个",
  "wl.top": "前",
  "wl.showCount": "显示账户数量",
  "wl.col.rank": "#",
  "wl.col.loan": "贷款",
  "wl.col.segment": "细分",
  "wl.col.pdefault": "违约概率",
  "wl.col.band": "评级",
  "wl.col.el": "预期损失",
  "wl.col.action": "处置",
  "wl.caption": "催收工作清单，按违约概率排序。点击行查看账户详情。",
  "wl.contested": "⚖ 争议",
  "wl.contested.title": "该账户在智能体辩论中受到质疑",
  "wl.contested.aria": "账户 {id} 存在争议 — 跳转至辩论",
  "wl.assumptions.label": "预期损失假设：",
  "wl.assumptions.body": "违约损失率 = 45%（巴塞尔基础内评法，无担保消费信贷）。违约风险敞口 = 剩余本金（分期摊还）。预期损失 = 违约概率 × 违约损失率 × 风险敞口。",

  // --- portfolio health ---
  "ph.title": "组合健康",
  "ph.npl.label": "不良率",
  "ph.npl.hint": "处于逾期或违约状态的账户占比。",
  "ph.el.label": "预期损失总额",
  "ph.el.assumptions": "预期损失 = 违约概率 × 违约损失率 × 风险敞口 · 违约损失率=45%（巴塞尔基础内评法，无担保消费信贷）· 风险敞口=剩余本金（分期摊还）。",
  "ph.vintage.title": "账龄违约率",
  "ph.vintage.empty": "暂无账龄数据。",
  "ph.status.title": "状态构成",
  "ph.status.empty": "暂无状态构成数据。",

  // --- alerts ---
  "al.title": "预警",
  "al.breachOne": "{count} 项触发",
  "al.breachMany": "{count} 项触发",
  "al.empty": "暂无预警。组合处于容忍范围内。",
  "al.value": "当前值",
  "al.threshold": "阈值",
  "sev.critical": "严重",
  "sev.high": "高",
  "sev.moderate": "中",
  "sev.low": "低",
  "sev.info": "提示",
  "metric.npl_ratio": "不良率",
  "metric.vintage_default_rate": "账龄违约率",
  "segment.portfolioWide": "全组合",

  // --- account drawer ---
  "dr.eyebrow": "账户",
  "dr.close": "关闭账户详情",
  "dr.pdefault": "违约概率",
  "dr.action": "建议处置",
  "dr.segment": "细分",
  "dr.product": "产品",
  "dr.region": "地区",
  "dr.band": "风险评级",

  // --- action / band badges ---
  "action.call": "催收",
  "action.watch": "观察",
  "action.auto-cure": "自动结清",
  "action.aria": "建议处置：{label}",
  "band.aria": "风险评级 {band}",

  // --- agent dialogue ---
  "ad.title": "智能体协作 · 风险辩论",
  "ad.subtitle": "风险审计员审查最高风险评分；有争议的判定经辩论、裁决，若无法解决则上报分析师。",
  "ad.streaming": "实时推送",
  "ad.live": "实时",
  "ad.streaming.title": "正在观看实时推送",
  "ad.live.title": "展示一次实时 Qwen 运行",
  "ad.disputeOne": "{count} 项争议",
  "ad.disputeMany": "{count} 项争议",
  "ad.escalated": " · {count} 项上报",
  "ad.stopStream": "停止推送",
  "ad.backToFixture": "返回样例",
  "ad.watchLive": "实时观看",
  "ad.connecting": "连接中…",
  "ad.runLive": "实时运行（Qwen）",
  "ad.running": "运行中…",
  "ad.runError": "实时运行失败：",
  "ad.watchError": "实时观看失败：",
  "ad.debatingBody": "风险审计员、精算师与信贷仲裁者正在通过 Qwen 就最高风险账户展开辩论…",
  "ad.empty.streamDone": "本次推送无争议 — 审计员认可每一个受审评分。",
  "ad.empty.streamWait": "等待第一轮…",
  "ad.empty.live": "本次实时运行无争议 — 审计员认可每一个受审评分。",
  "ad.empty.fixture": "本次运行无争议 — 审计员认可每一个受审评分。",
  "ad.streamingMore": "正在推送更多轮次…",
  "ad.sr.opening": "正在打开实时辩论推送…",
  "ad.sr.complete": "实时辩论已完成。",
  "ad.sr.streaming": "正在推送实时辩论…",
  "ad.sr.running": "正在运行实时 Qwen 辩论…",
  "ad.sr.loaded": "实时辩论已加载。",
  "ad.vs": "对",
  "ad.resolvedBy": "由{resolver}裁定：",
  "speaker.risk_auditor": "风险审计员",
  "speaker.risk_model": "精算师",
  "speaker.arbiter": "信贷仲裁者",
  "speaker.human": "分析师",
  "res.upheld": "维持",
  "res.overridden": "推翻",
  "res.escalated_approved": "上报 · 批准",
  "res.escalated_rejected": "上报 · 驳回",

  // --- auth ---
  "auth.sub": "早期预警催收",
  "auth.signIn": "登录",
  "auth.createAccount": "注册账户",
  "auth.forgot": "忘记密码",
  "auth.reset": "重置密码",
  "auth.updateBtn": "更新密码",
  "auth.checkEmail": "请查收邮件",
  "auth.updated": "密码已更新",
  "auth.email": "邮箱",
  "auth.password": "密码",
  "auth.confirm": "确认密码",
  "auth.newPassword": "新密码",
  "auth.confirmNew": "确认新密码",
  "auth.token": "重置令牌",
  "auth.demo": "演示分析师",
  "auth.createLink": "注册新账户",
  "auth.forgotLink": "忘记密码？",
  "auth.backToSignIn": "← 返回登录",
  "auth.backToSignInPlain": "返回登录",
  "auth.backToSignInFwd": "→ 返回登录",
  "auth.sendToken": "发送重置令牌",
  "auth.haveToken": "我已有令牌 →",
  "auth.minChars": "至少 8 个字符。",
  "auth.signInNew": "请使用新密码登录。",
  "auth.checkEmailBody": "若 {email} 已注册，系统已签发重置令牌。生产环境会发送至邮箱；本演示直接在下方显示令牌。",
  "auth.tokenLabel": "重置令牌（演示投递）",
  "auth.fillDemo": "填入演示分析师凭据",
  "auth.err.mismatch": "两次输入的密码不一致。",
  "auth.err.invalidLogin": "邮箱或密码错误。",
  "auth.err.signInFailed": "登录失败。",
  "auth.err.dup": "该邮箱已注册，请尝试登录。",
  "auth.err.regFailed": "注册失败。",
  "auth.err.tokenFailed": "无法发送重置令牌。",
  "auth.err.invalidToken": "重置令牌无效或已过期。",
  "auth.err.resetFailed": "重置失败。",
  "auth.err.network": "无法连接服务器，请确认其运行在 :8080。",
};

const MESSAGES: Record<Lang, Dict> = { en: EN, zh: ZH };

function interpolate(template: string, vars?: Record<string, string | number>): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (_, k: string) =>
    k in vars ? String(vars[k]) : `{${k}}`,
  );
}

export type TFunc = (key: string, vars?: Record<string, string | number>) => string;

interface I18nValue {
  lang: Lang;
  setLang: (l: Lang) => void;
  toggle: () => void;
  t: TFunc;
}

const I18nContext = createContext<I18nValue | null>(null);

function detectInitial(): Lang {
  if (typeof window === "undefined") return "en";
  const stored = window.localStorage.getItem(STORAGE_KEY);
  if (stored === "en" || stored === "zh") return stored;
  return (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
}

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(detectInitial);

  useEffect(() => {
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
    document.documentElement.setAttribute("data-lang", lang);
    try {
      window.localStorage.setItem(STORAGE_KEY, lang);
    } catch {
      /* private-mode / storage-disabled — the choice just doesn't persist */
    }
  }, [lang]);

  const setLang = useCallback((l: Lang) => setLangState(l), []);
  const toggle = useCallback(() => setLangState((l) => (l === "en" ? "zh" : "en")), []);

  const t = useCallback<TFunc>(
    (key, vars) => {
      const table = MESSAGES[lang];
      const template = table[key] ?? MESSAGES.en[key] ?? key;
      return interpolate(template, vars);
    },
    [lang],
  );

  const value = useMemo<I18nValue>(() => ({ lang, setLang, toggle, t }), [lang, setLang, toggle, t]);
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error("useI18n must be used within <I18nProvider>");
  return ctx;
}
