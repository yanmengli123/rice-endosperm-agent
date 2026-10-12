import { createApp } from 'vue'
import { createPinia } from 'pinia'
import piniaPluginPersistedstate from 'pinia-plugin-persistedstate'
import { message } from 'ant-design-vue'

import App from './App.vue'
import router from './router'
import { installRuntimeErrorHandling } from '@/utils/runtimeErrors'

import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'
import '@/assets/css/main.css'

const app = createApp(App)
const pinia = createPinia()
pinia.use(piniaPluginPersistedstate)

// 分类诊断与限频提示，不保留原始异常；错误钩子并不能替代组件的恢复 UI。
const disposeRuntimeErrors = installRuntimeErrorHandling(app, {
  development: import.meta.env.DEV,
  notify: (text) => message.error(text)
})
if (import.meta.hot) import.meta.hot.dispose(disposeRuntimeErrors)

app.use(pinia)
app.use(router)
app.use(Antd)

// 预加载信息配置
import { useInfoStore } from '@/stores/info'
const infoStore = useInfoStore()
infoStore.loadInfoConfig()

app.mount('#app')
