/**
 * 知识源契约前端能力判定（单一事实源，fail-closed）。
 *
 * 原则：UI 的功能可见性由契约快照的 allowed_commands 派生，而不是按
 * contract_key 硬编码——契约升级（如 managed_graph 1.0→1.1 新增
 * graph_mindmap_generate）时页面自动跟随，杜绝「入口可见但调用被契约拒绝」
 * 的版本断层。
 *
 * fail-closed 语义：契约信息未加载（snapshot 缺失）时一律返回 false——
 * 宁可按钮晚出现，也不显示一个必然撞契约的入口。
 */

/** 该库契约是否放行指定命令（以建库时冻结的 contract_snapshot 为准） */
export function contractAllows(database, command) {
  const commands = database?.contract_snapshot?.allowed_commands
  return Array.isArray(commands) && commands.includes(command)
}

/**
 * 按契约 key 从注册中心快照选取契约条目：锚定 latest_version，
 * 快照未提供时按 semver 客户端取最大。
 *
 * 禁止依赖数组顺序取首条——注册顺序曾是 managed_graph@1.0.0 在前，
 * 导致新建向导把库冻结到旧版本。
 */
export function pickLatestContract(contracts, key) {
  const candidates = (contracts || []).filter(
    (contract) => contract && contract.contract_key === key
  )
  if (!candidates.length) return null
  const latest = candidates.find((contract) => contract.latest_version)?.latest_version
  if (latest) {
    const anchored = candidates.find((contract) => contract.version === latest)
    if (anchored) return anchored
  }
  return [...candidates].sort((a, b) => compareSemver(b.version, a.version))[0]
}

/** 逐段数值比较（1.10.0 > 1.9.0）；非数值段按字符串比较（legacy "0" 等） */
function compareSemver(a, b) {
  const pa = String(a || '').split('.')
  const pb = String(b || '').split('.')
  for (let i = 0; i < Math.max(pa.length, pb.length); i += 1) {
    const na = Number(pa[i])
    const nb = Number(pb[i])
    const aIsNum = pa[i] !== undefined && Number.isFinite(na)
    const bIsNum = pb[i] !== undefined && Number.isFinite(nb)
    if (aIsNum && bIsNum) {
      if (na !== nb) return na > nb ? 1 : -1
    } else {
      const sa = pa[i] ?? ''
      const sb = pb[i] ?? ''
      if (sa !== sb) return sa > sb ? 1 : -1
    }
  }
  return 0
}
