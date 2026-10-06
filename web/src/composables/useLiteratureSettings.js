/**
 * 文献工作台专属设置（对标 PaperAgent 设置中心的文献子集）。
 *
 * 三组设置：
 * - parse：PDF 导入解析细配（language/is_ocr/enable_table/enable_formula/
 *   model_version）+ 篇间间隔，随 addDocuments 请求穿透到 MinerU 调用；
 * - workflow：复核门控策略（wait/auto，对标 PaperAgent「翻译时机」）；
 * - display：护眼主题 ×4 / 阅读字号 / 行宽 / 自定义 CSS（本机生效，不落服务端）。
 *
 * 持久化按用户分键（与 literature_reader.js 同模式），模块级单例供多组件共享。
 */
import { reactive, watch } from 'vue'

export const LITERATURE_THEMES = [
  { value: 'default', label: '默认' },
  { value: 'sepia', label: '米黄护眼' },
  { value: 'night', label: '夜间' },
  { value: 'paper', label: '纸质' }
]

const DEFAULTS = Object.freeze({
  // 解析细配（默认值与后端口径一致：ch / is_ocr=true / table / formula / vlm）
  parseLanguage: 'ch',
  parseIsOcr: true,
  parseEnableTable: true,
  parseEnableFormula: true,
  parseModelVersion: 'vlm',
  parseInterval: 0, // 篇间间隔秒（0-60），仅批量导入节流，不落 processing_params
  // 处理流程
  reviewPolicy: 'wait', // wait=复核门控后翻译；auto=渲染后自动跳过复核（仅 full）
  // 界面
  theme: 'default',
  fontSize: 16, // 14-24
  lineWidth: 900, // 600-1200 px
  customCss: ''
})

const clamp = (value, min, max, fallback) =>
  Number.isFinite(Number(value)) ? Math.min(max, Math.max(min, Number(value))) : fallback

const normalize = (raw) => ({
  parseLanguage: ['ch', 'en'].includes(raw?.parseLanguage)
    ? raw.parseLanguage
    : DEFAULTS.parseLanguage,
  parseIsOcr: typeof raw?.parseIsOcr === 'boolean' ? raw.parseIsOcr : DEFAULTS.parseIsOcr,
  parseEnableTable:
    typeof raw?.parseEnableTable === 'boolean' ? raw.parseEnableTable : DEFAULTS.parseEnableTable,
  parseEnableFormula:
    typeof raw?.parseEnableFormula === 'boolean'
      ? raw.parseEnableFormula
      : DEFAULTS.parseEnableFormula,
  parseModelVersion: ['vlm', 'pipeline'].includes(raw?.parseModelVersion)
    ? raw.parseModelVersion
    : DEFAULTS.parseModelVersion,
  parseInterval: clamp(raw?.parseInterval, 0, 60, DEFAULTS.parseInterval),
  reviewPolicy: ['wait', 'auto'].includes(raw?.reviewPolicy)
    ? raw.reviewPolicy
    : DEFAULTS.reviewPolicy,
  theme: LITERATURE_THEMES.some((item) => item.value === raw?.theme) ? raw.theme : DEFAULTS.theme,
  fontSize: clamp(raw?.fontSize, 14, 24, DEFAULTS.fontSize),
  lineWidth: clamp(raw?.lineWidth, 600, 1200, DEFAULTS.lineWidth),
  customCss: typeof raw?.customCss === 'string' ? raw.customCss.slice(0, 20000) : ''
})

const state = reactive({ ...DEFAULTS, _uid: '' })
let stopPersist = null

const storageKey = (uid) => `yuxi:literature:settings:${uid || 'anonymous'}`

/** 按当前用户装载设置并开启自动持久化（App 内调用一次即可，重复调用幂等切人）。 */
export function initLiteratureSettings(uid) {
  const key = storageKey(uid)
  let saved
  try {
    saved = JSON.parse(localStorage.getItem(key))
  } catch {
    saved = null
  }
  Object.assign(state, normalize(saved), { _uid: uid || '' })
  if (stopPersist) stopPersist()
  stopPersist = watch(
    () => ({ ...state }),
    (value) => {
      const rest = { ...value }
      delete rest._uid
      try {
        localStorage.setItem(key, JSON.stringify(rest))
      } catch {
        /* 存储满/隐私模式：仅本会话生效 */
      }
    },
    { deep: true }
  )
}

export function resetLiteratureSettings() {
  Object.assign(state, { ...DEFAULTS })
}

/** 解析细配请求参数（仅含与后端契约同名的键，随 addDocuments params 提交）。 */
export function parseOptionsPayload() {
  return {
    language: state.parseLanguage,
    is_ocr: state.parseIsOcr,
    enable_table: state.parseEnableTable,
    enable_formula: state.parseEnableFormula,
    model_version: state.parseModelVersion,
    parse_interval_seconds: state.parseInterval
  }
}

export function useLiteratureSettings() {
  return state
}
