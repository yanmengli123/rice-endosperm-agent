import assert from 'node:assert/strict'
import { createApp, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import piniaPluginPersistedstate from 'pinia-plugin-persistedstate'

const values = new Map()
globalThis.localStorage = {
  getItem(key) {
    return values.has(key) ? values.get(key) : null
  },
  setItem(key, value) {
    values.set(key, String(value))
  },
  removeItem(key) {
    values.delete(key)
  },
  clear() {
    values.clear()
  }
}

const { useChatUIStore } = await import('../chatUI.js')

const createTestPinia = () => {
  const pinia = createPinia()
  pinia.use(piniaPluginPersistedstate)
  createApp({}).use(pinia)
  setActivePinia(pinia)
  return pinia
}

const firstPinia = createTestPinia()
const firstStore = useChatUIStore(firstPinia)

firstStore.setSelectedModelSpec('  siliconflow:Qwen/Qwen3-32B  ')
assert.equal(firstStore.selectedModelSpec, 'siliconflow:Qwen/Qwen3-32B')
await nextTick()

const persisted = JSON.parse(localStorage.getItem('chat-ui-store'))
assert.equal(persisted.selectedModelSpec, 'siliconflow:Qwen/Qwen3-32B')

// 模拟页面离开后重新创建应用：模型选择应从 localStorage 恢复。
const secondPinia = createTestPinia()
const secondStore = useChatUIStore(secondPinia)
assert.equal(secondStore.selectedModelSpec, 'siliconflow:Qwen/Qwen3-32B')

// 空选择代表恢复使用智能体默认模型，并同步清除持久化偏好。
secondStore.setSelectedModelSpec('')
assert.equal(secondStore.selectedModelSpec, '')
await nextTick()
assert.equal(JSON.parse(localStorage.getItem('chat-ui-store')).selectedModelSpec, '')

console.log('chatUI model preference: all assertions passed')
