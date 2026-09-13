/**
 * 会话标题编目规则。
 *
 * 背景：web 端曾把首条消息直接交给 LLM 起标题（无提示词约束、无温度控制、无输出校验），
 * 模型会把原文缩写扩写成错误实体（如 Wx → 微信）。现在与桌面端已上线行为对齐
 * （rice-endosperm-desktop/src-tauri/src/database.rs）：默认标题 = 原文归一化后
 * 前 MAX_TITLE_CHARS 个 Unicode 码点；LLM 编目只是可选增强，且必须通过
 * 符号保留性门禁，未通过一律丢弃、保留确定性结果。
 */

/** 与桌面端 chars().take(28) 对齐的标题最大码点数 */
export const MAX_TITLE_CHARS = 28

/** 与后端 conversation_service.INITIAL_THREAD_TITLE 一致 */
export const INITIAL_THREAD_TITLE = '新的对话'

/** 标题来源，写入 extra_metadata.title_source；USER 为终态，任何自动路径不得覆盖 */
export const TITLE_SOURCE_USER = 'USER'
export const TITLE_SOURCE_FALLBACK = 'FALLBACK'
export const TITLE_SOURCE_AUTO = 'AUTO'

const MARKDOWN_MARK_RE = /[*`#>~]/
const QUOTE_MARK_RE = /["'「」『』《》〔〕]/
const EXPLANATORY_PREFIX_RE = /^(根据|关于|标题|摘要|以下是|这是)/
const SYMBOL_TOKEN_RE = /[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*/g

/**
 * 确定性标题：去 markdown 记号、折叠空白，再按码点截断。
 * Array.from 按 Unicode 码点切分，不会截断 emoji 等代理对（等价于桌面端 chars().take(28)）。
 */
export const deriveThreadTitle = (raw) => {
  if (typeof raw !== 'string') return ''
  const normalized = raw
    .replace(/[*`#>~]+/g, '')
    .replace(/\s+/g, ' ')
    .trim()
  if (!normalized) return ''
  return Array.from(normalized).slice(0, MAX_TITLE_CHARS).join('')
}

/**
 * 从原文中提取必须原样保留的高置信度符号（MCP、Wx、OsMYB73、RAP-MSU、mRNA 等）。
 * 抽不出符号就不校验（不误拦），抽出了就要求在标题中逐字出现（不放过）。
 */
export const extractProtectedSymbols = (raw) => {
  if (typeof raw !== 'string') return []
  const tokens = raw.match(SYMBOL_TOKEN_RE) || []
  const symbols = new Set()
  for (const token of tokens) {
    const compact = token.replace(/[-_.]/g, '')
    const hasDigit = /\d/.test(token)
    const isAllCaps = compact.length >= 2 && /^[A-Z]+$/.test(compact)
    const isShortMixedCase = token.length <= 5 && /^[A-Z][a-z0-9]/.test(token)
    const hasCaseAlternation = /[a-z][A-Z]/.test(token)
    if (hasDigit || isAllCaps || isShortMixedCase || hasCaseAlternation) {
      symbols.add(token)
    }
  }
  return [...symbols]
}

/**
 * 校验 LLM 编目结果：仅接受单行 JSON {"title":"..."}，且不得含 markdown 记号、
 * 引号书名号、解释性前缀，不得超过 maxChars 个码点，原文符号必须逐字保留。
 * 任何一条不满足即拒绝（返回 null），调用方应保留确定性标题。
 */
export const validateCatalogTitle = (candidateText, rawMessage, maxChars = MAX_TITLE_CHARS) => {
  if (typeof candidateText !== 'string') return null

  let parsed
  try {
    let text = candidateText.trim()
    const fenced = text.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/)
    if (fenced) text = fenced[1]
    parsed = JSON.parse(text)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
  if (typeof parsed.title !== 'string') return null

  const title = parsed.title.replace(/\s+/g, ' ').trim()
  if (!title) return null
  if (Array.from(title).length > maxChars) return null
  if (MARKDOWN_MARK_RE.test(title) || QUOTE_MARK_RE.test(title)) return null
  if (EXPLANATORY_PREFIX_RE.test(title)) return null

  const missingSymbols = extractProtectedSymbols(rawMessage).filter(
    (symbol) => !title.includes(symbol)
  )
  if (missingSymbols.length > 0) return null
  return title
}

/** 受限编目契约提示词：模型只做保守编目，不确定时必须复用原文措辞 */
export const buildCatalogPrompt = (rawMessage) => {
  const message = typeof rawMessage === 'string' ? rawMessage.slice(0, 2000) : ''
  return [
    '你是会话目录编目员，不是问答助手。你的唯一任务是为一段对话生成侧边栏标题。',
    '',
    '硬性规则（违反任何一条即为失败）：',
    '1. 禁止扩写、翻译、解释任何缩写、代号、符号、基因名、工具名、协议名。',
    '   原文写 Wx，标题必须原样保留 Wx；写 MCP、OsMYB73、RAP-MSU，必须原样保留。',
    '2. 禁止引入原文中未出现的任何实体、产品、物种、机构、论文名。',
    '3. 禁止输出 markdown、引号、书名号、冒号前缀、解释性词语（「根据」「标题」「摘要」「关于」等）。',
    '4. 使用「对象 + 动作」的短语结构，不要完整句子。',
    `5. 长度不超过 ${MAX_TITLE_CHARS} 个字符，宁短勿编。不确定时，直接复用原文措辞。`,
    '6. 只输出一行 JSON，形如 {"title":"..."}，不要任何其他内容。',
    '',
    '对话首条用户消息：',
    message
  ].join('\n')
}
