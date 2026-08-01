<script setup>
import zhCN from 'ant-design-vue/es/locale/zh_CN'
import { useAgentStore } from '@/stores/agent'
import { useUserStore } from '@/stores/user'
import { useThemeStore } from '@/stores/theme'
import SettingsModal from '@/components/SettingsModal.vue'
import { onMounted, provide, ref } from 'vue'

const agentStore = useAgentStore()
const userStore = useUserStore()
const themeStore = useThemeStore()
const showSettingsModal = ref(false)
const settingsInitialTab = ref('')

const openSettingsModal = (tab) => {
  settingsInitialTab.value = tab || (userStore.isAdmin ? 'base' : 'account')
  showSettingsModal.value = true
}

provide('settingsModal', {
  openSettingsModal
})

onMounted(async () => {
  if (userStore.isLoggedIn) {
    await agentStore.initialize()
  }
})
</script>
<template>
  <a-config-provider :theme="themeStore.currentTheme" :locale="zhCN">
    <router-view />
    <SettingsModal
      v-model:visible="showSettingsModal"
      :initial-tab="settingsInitialTab"
      @close="showSettingsModal = false"
    />
  </a-config-provider>
</template>
