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
    _pollTimer: null,
    _refreshPending: false,
    _refreshPendingWithSettings: false,
    _refreshPendingForce: false
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

    async refresh({ withSettings = false, force = false } = {}) {
      if (!this.kbId || !this.active) return
      if (this.summaryLoading) {
        // 写操作失效或切库可能与在途请求重叠；合并成一次后继请求，不能静默丢弃。
        this._refreshPending = true
        this._refreshPendingWithSettings ||= withSettings
        this._refreshPendingForce ||= force
        return
      }
      const requestedKbId = this.kbId
      this.summaryLoading = true
      try {
        const response = await graphApi.governanceSummary(requestedKbId, { refresh: force })
        // 切库期间旧请求可以完成，但绝不能污染新库状态。
        if (requestedKbId !== this.kbId || !this.active) return
        this.summary = response?.data || null
        this.summaryError = null
        if (this.summary?.settings) {
          this.settings = this.summary.settings
        }
      } catch (error) {
        if (requestedKbId === this.kbId) {
          this.summaryError = error?.response?.data?.detail || error?.message || '治理总览加载失败'
        }
      } finally {
        if (withSettings && requestedKbId === this.kbId && !this.settings) {
          await this.loadSettings(requestedKbId)
        }
        this.summaryLoading = false

        const pending = this._refreshPending
        const pendingWithSettings = this._refreshPendingWithSettings
        const pendingForce = this._refreshPendingForce
        this._refreshPending = false
        this._refreshPendingWithSettings = false
        this._refreshPendingForce = false
        if (pending && this.kbId && this.active) {
          await this.refresh({ withSettings: pendingWithSettings, force: pendingForce })
        }
      }
    },

    async loadSettings(requestedKbId = this.kbId) {
      if (!requestedKbId) return
      try {
        const response = await graphApi.governanceSettings(requestedKbId)
        if (requestedKbId === this.kbId) {
          this.settings = response?.data || null
        }
      } catch {
        if (requestedKbId === this.kbId) {
          this.settings = null
        }
      }
    },

    /** 写操作后立即失效并重取（后端写路径也会失效自己的缓存） */
    invalidate() {
      return this.refresh({ withSettings: true, force: true })
    }
  }
})
