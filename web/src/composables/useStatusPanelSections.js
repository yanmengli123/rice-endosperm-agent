/**
 * 状态面板分区折叠状态（纯 vue，无 '@' 依赖，可裸 node spec）：
 * 各 section 折叠布尔 + 展开/收起切换。默认值集中在此，
 * AgentChatComponent 不再自带面板分区状态。
 */
import { reactive } from 'vue'

export const STATE_SECTION_DEFAULTS = {
  trace: false,
  evidence: false,
  scope: false,
  tokenUsage: false,
  todos: false,
  files: false,
  artifacts: false,
  // 本会话产物聚合视图默认折叠（主视图是「本轮产物」）
  sessionArtifacts: true,
  subagents: false
}

export function useStatusPanelSections() {
  const collapsedStateSections = reactive({ ...STATE_SECTION_DEFAULTS })
  const isStateSectionExpanded = (key) => !collapsedStateSections[key]
  const toggleStateSection = (key) => {
    collapsedStateSections[key] = !collapsedStateSections[key]
  }
  return { collapsedStateSections, isStateSectionExpanded, toggleStateSection }
}
