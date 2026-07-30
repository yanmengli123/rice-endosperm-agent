import { ref } from 'vue'
import { defineStore } from 'pinia'
import { theme } from 'ant-design-vue'

export const useThemeStore = defineStore('theme', () => {
  // 从 localStorage 读取保存的主题，默认为浅色
  const isDark = ref(localStorage.getItem('theme') === 'dark')

  // 公共主题配置
  const commonTheme = {
    token: {
      fontFamily:
        "'HarmonyOS Sans SC', 'Noto Sans SC', Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      colorPrimary: '#2f6b4f',
      colorSuccess: '#438064',
      colorWarning: '#d6a83e',
      colorInfo: '#3277a8',
      colorLink: '#3277a8',
      colorLinkHover: '#245d86',
      colorLinkActive: '#183f5d',
      borderRadius: 10,
      borderRadiusLG: 16,
      controlHeight: 36,
      controlHeightLG: 42,
      wireframe: false
    }
  }

  // 浅色主题配置
  const lightTheme = {
    ...commonTheme,
    token: {
      ...commonTheme.token,
      colorText: '#24312b',
      colorTextSecondary: '#68746e',
      colorBgLayout: '#fffdf6',
      colorBgContainer: '#ffffff'
    }
  }

  // 深色主题配置
  const darkTheme = {
    ...commonTheme,
    algorithm: theme.darkAlgorithm,
    token: {
      ...commonTheme.token,
      colorPrimary: '#78aa8b',
      colorSuccess: '#78aa8b',
      colorWarning: '#e0be67',
      colorInfo: '#73a9cc',
      colorLink: '#73a9cc',
      colorLinkHover: '#91bdd8',
      colorLinkActive: '#b3d1e3',
      colorText: '#edf3ee',
      colorTextSecondary: '#a9b7ad',
      colorBgLayout: '#0f1712',
      colorBgContainer: '#19251d'
    }
  }

  // 当前主题配置
  const currentTheme = ref(isDark.value ? darkTheme : lightTheme)

  // 切换主题
  function toggleTheme() {
    setTheme(!isDark.value)
  }

  // 设置主题
  function setTheme(dark) {
    isDark.value = dark
    currentTheme.value = dark ? darkTheme : lightTheme
    localStorage.setItem('theme', dark ? 'dark' : 'light')
    updateDocumentTheme()
  }

  // 更新 document 的主题类
  function updateDocumentTheme() {
    if (isDark.value) {
      document.documentElement.classList.add('dark')
    } else {
      document.documentElement.classList.remove('dark')
    }
  }

  // 初始化时设置主题
  updateDocumentTheme()

  return {
    isDark,
    currentTheme,
    toggleTheme,
    setTheme
  }
})
