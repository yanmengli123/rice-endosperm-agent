/**
 * 知识图谱治理 store：治理头聚合总览（轮询）+ 治理设置缓存 + 工作台模式切换。
 *
 * summary 是治理工作台的数据源（队列/门禁/冲突/死信/构建/轻量完整性/发布指针），
 * 后端有 TTL 缓存，这里轮询间隔与其对齐（默认 15s，仅在 active 时轮询）。
 */
import { defineStore } from 'pinia'
import { graphApi } from '@/apis/graph_api'

const SUMMARY_POLL_INTERVAL_MS = 15000
const MODE_STORAGE_KEY = 'yuxi.graph.workbenchMode'

export const useGraphGovernanceStore = defineStore('graphGovernance', {
  state: () => ({
    kbId: '',
    active: false,
    summary: null,
    summaryLoading: false,
    summaryError: null,
    settings: null,
    mode: localStorage.getItem(MODE_STORAGE_KEY) === 'classic' ? 'classic' : 'workbench',
    _pollTimer: null
  }),

  getters: {
    pendingReviewCount: (state) =>
      Number(state.summary?.counts?.triples?.CANDIDATE ?? 0) +
      Number(state.summary?.counts?.entities?.CANDIDATE ?? 0),
    pendingGateCount: (state) => Number(state.summary?.gates?.pending?._total ?? 0),
    openConflictCount: (state) => Number(state.summary?.conflicts?.open?._total ?? 0),
    integrityViolations: (state) => {
      const integrity = state.summary?.integrity || {}
      return Object.entries(integrity)
        .filter(([key, value]) => key.startsWith('I') && Number(value) > 0)
        .map(([key, value]) => ({ key, value: Number(value) }))
    },
    reviewPolicy: (state) => state.settings?.review_policy || 'candidates_visible',
    lastRelease: (state) => state.summary?.release?.last_release || null
  },

  actions: {
    setMode(mode) {
      this.mode = mode === 'classic' ? 'classic' : 'workbench'
      localStorage.setItem(MODE_STORAGE_KEY, this.mode)
    },

    /** 绑定知识库并开始/停止轮询（切库、离开标签页时调用 stop） */
    attach(kbId, { active }) {
      if (kbId !== this.kbId) {
        this.kbId = kbId || ''
        this.summary = null
        this.settings = null
      }
      this.active = Boolean(active && kbId)
      if (this.active) {
        this.refresh({ withSettings: true })
        this._startPoll()
      } else {
        this._stopPoll()
      }
    },

    _startPoll() {
      this._stopPoll()
      this._pollTimer = setInterval(() => {
        if (!this.summaryLoading) {
          this.refresh()
        }
      }, SUMMARY_POLL_INTERVAL_MS)
    },

    _stopPoll() {
      if (this._pollTimer) {
        clearInterval(this._pollTimer)
        this._pollTimer = null
      }
    },

    async refresh({ withSettings = false } = {}) {
      if (!this.kbId || !this.active || this.summaryLoading) return
      this.summaryLoading = true
      try {
        const response = await graphApi.governanceSummary(this.kbId)
        this.summary = response?.data || null
        this.summaryError = null
        if (this.summary?.settings) {
          this.settings = this.summary.settings
        }
      } catch (error) {
        this.summaryError = error?.response?.data?.detail || error?.message || '治理总览加载失败'
      } finally {
        this.summaryLoading = false
      }
      if (withSettings && !this.settings) {
        await this.loadSettings()
      }
    },

    async loadSettings() {
      if (!this.kbId) return
      try {
        const response = await graphApi.governanceSettings(this.kbId)
        this.settings = response?.data || null
      } catch {
        this.settings = null
      }
    },

    /** 写操作后立即失效并重取（后端写路径也会失效自己的缓存） */
    invalidate() {
      this.refresh({ withSettings: true })
    }
  }
})
