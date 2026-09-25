import assert from 'node:assert/strict'

import { useStatusPanelSections } from '../useStatusPanelSections.js'

// vue 在宿主裸 node 可解析（composables 不 import '@' 别名是本 spec 的前提）
const { isStateSectionExpanded, toggleStateSection } = useStatusPanelSections()

// 默认折叠语义：主视图展开、会话产物聚合默认折叠
assert.strictEqual(isStateSectionExpanded('trace'), true)
assert.strictEqual(isStateSectionExpanded('evidence'), true)
assert.strictEqual(isStateSectionExpanded('scope'), true)
assert.strictEqual(isStateSectionExpanded('sessionArtifacts'), false)

// 切换往返
toggleStateSection('trace')
assert.strictEqual(isStateSectionExpanded('trace'), false)
toggleStateSection('trace')
assert.strictEqual(isStateSectionExpanded('trace'), true)

// 未注册 key：折叠态视为展开（falsy → !undefined = true，静默容错不抛错）
assert.strictEqual(isStateSectionExpanded('nonexistent'), true)
assert.doesNotThrow(() => toggleStateSection('nonexistent'))

// 两次 use 得到独立状态实例（组件间不共享）
const second = useStatusPanelSections()
second.toggleStateSection('trace')
assert.strictEqual(second.isStateSectionExpanded('trace'), false)
assert.strictEqual(isStateSectionExpanded('trace'), true)

console.log('useStatusPanelSections: all assertions passed')
