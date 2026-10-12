<template>
  <div class="review-panel">
    <div class="review-head">
      <span v-if="meta.total">待复核 {{ meta.blocking_pending }} / {{ meta.total }}</span>
      <label><input v-model="hideResolved" type="checkbox" />隐藏已处理</label>
      <button class="btn" :disabled="loading || applying || busyItem !== null" @click="load">
        刷新清单
      </button>
      <button class="btn" :disabled="dualLoading" @click="triggerDual">
        {{ dualLoading ? '双通道渲染入队中…' : '触发双通道渲染' }}
      </button>
      <button
        class="btn"
        :disabled="applying || loading || busyItem !== null || !meta.total"
        @click="applyDecisions"
      >
        {{ applying ? '回放中…' : '应用裁决（修正回放）' }}
      </button>
    </div>
    <div v-if="notice" class="review-notice">{{ notice }}</div>

    <div v-if="loading" class="review-empty">复核清单加载中…</div>
    <div v-else-if="!meta.total" class="review-empty">
      {{
        meta.render_digest
          ? '双通道比对已完成，无需复核。'
          : '暂无复核清单（离线渲染不产生双通道差异；需先触发双通道渲染）'
      }}
    </div>
    <div v-else-if="!visibleItems.length" class="review-empty">
      已处理项已隐藏；可取消勾选查看全部裁决。
    </div>

    <ul v-else class="review-list">
      <li
        v-for="item in visibleItems"
        :key="item.report_idx"
        class="review-item"
        :class="{ resolved: item.decision, blocking: item.blocking && !item.decision }"
      >
        <div class="item-head">
          <span class="item-idx">#{{ item.report_idx }}</span>
          <span v-for="pid in item.para_ids" :key="pid" class="item-para">{{ pid }}</span>
          <span v-if="item.decision" class="item-decided">
            已裁决：{{ item.decision.choice }}
          </span>
        </div>
        <div class="item-bodies">
          <div v-if="item.mineru_text" class="body-mineru">
            <label>MinerU (A)</label>{{ item.mineru_text }}
          </div>
          <div v-if="item.paddleocr_text" class="body-paddle">
            <label>Paddle (B)</label>{{ item.paddleocr_text }}
          </div>
        </div>
        <!-- 页图对照（对标上游整页 PNG + 红框）：仅人眼核对用，不是证据 -->
        <div class="item-pageview">
          <button
            v-if="item.page"
            class="pageview-toggle"
            :disabled="pageBusy"
            @click="togglePage(item)"
          >
            {{ expanded === item.item_id ? '收起页图对照' : `查看第 ${item.page} 页对照` }}
          </button>
          <span v-else class="pageview-none">该差异项无页码定位</span>
          <div v-if="expanded === item.item_id" class="pageview-body">
            <div v-if="pageLoading" class="pageview-hint">页图加载中…</div>
            <div v-else-if="pageError" class="pageview-hint error">{{ pageError }}</div>
            <template v-else-if="pageData">
              <div class="pageview-canvas">
                <img
                  v-if="!pageImageFailed"
                  :src="pageImageUrl"
                  :alt="`第 ${item.page} 页原文对照`"
                  class="page-img"
                  @error="pageImageFailed = true"
                />
                <div v-else class="pageview-hint">页图不可用（该版本未产出页图）</div>
                <span
                  v-for="box in ownBoxes"
                  :key="box.item_id"
                  class="bbox-box"
                  :style="boxStyle(box)"
                ></span>
              </div>
              <div class="pageview-legend">
                红框为本差异项在第 {{ item.page }} 页的定位范围（仅供人工核对）
              </div>
            </template>
          </div>
        </div>
        <div class="item-choices">
          <button
            v-for="choice in choices"
            :key="choice.key"
            class="choice-btn"
            :disabled="busyItem !== null || applying"
            @click="decide(item, choice.key)"
          >
            {{ choice.label }}
          </button>
          <button
            class="choice-btn suggest"
            :disabled="busyItem !== null || applying"
            @click="suggest(item)"
          >
            ✨ AI 建议
          </button>
        </div>
        <div v-if="item.suggestion" class="item-suggestion">
          <label>AI 建议（仅预填，仍需人工确认）：</label>
          <textarea v-model="item.suggestion" rows="2" class="suggest-input"></textarea>
        </div>
      </li>
    </ul>
  </div>
</template>

<script>
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { apiGet, apiPost } from '@/apis/base'
import { buildLiteratureAssetUrl } from '@/apis/literature_api'
import { confirmAction } from '@/composables/useLiteratureSessions'

export default {
  name: 'ReviewPanel',
  props: {
    rid: { type: String, required: true },
    versionToken: { type: String, default: '' }
  },
  emits: ['changed'],
  setup(props, { emit }) {
    const meta = ref({ total: 0, blocking_pending: 0, items: [] })
    const notice = ref('')
    const dualLoading = ref(false)
    const applying = ref(false)
    const loading = ref(false)
    const hideResolved = ref(false)
    const busyItem = ref(null)
    // 页图对照：同一时刻只展开一项，避免一次拉取整篇 PDF 的全部页图
    const expanded = ref('')
    const pageData = ref(null)
    const pageImageUrl = ref('')
    const pageLoading = ref(false)
    const pageError = ref('')
    const pageImageFailed = ref(false)
    const pageBusy = computed(
      () => pageLoading.value || dualLoading.value || applying.value || busyItem.value !== null
    )
    // 只把本差异项的红框画出来（同页其他项的框会误导对照）
    const ownBoxes = computed(() =>
      (pageData.value?.boxes || []).filter((box) => box.item_id === expanded.value)
    )
    /**
     * bbox 为 PDF 点坐标，页图渲染分辨率未知 → 用图片实际像素尺寸换算成百分比，
     * 这样红框随页图缩放自适应，且不依赖前端知道渲染 DPI。
     */
    const boxStyle = (box) => {
      const canvas = document.querySelector('.pageview-canvas .page-img')
      if (!canvas) return {}
      const width = canvas.clientWidth
      const height = canvas.clientHeight
      if (!width || !height) return {}
      const [x0, y0, x1, y1] = box.bbox || []
      if (!Array.isArray(box.bbox) || box.bbox.length !== 4) return {}
      const boxWidth = Math.abs(x1 - x0)
      const boxHeight = Math.abs(y1 - y0)
      return {
        left: `${(x0 / width) * 100}%`,
        top: `${(y0 / height) * 100}%`,
        width: `${(boxWidth / width) * 100}%`,
        height: `${(boxHeight / height) * 100}%`
      }
    }
    const togglePage = async (item) => {
      if (expanded.value === item.item_id) {
        expanded.value = ''
        return
      }
      expanded.value = item.item_id
      pageData.value = null
      pageImageUrl.value = ''
      pageError.value = ''
      pageImageFailed.value = false
      pageLoading.value = true
      try {
        const data = await apiGet(
          `/api/literature/papers/${enc(props.rid)}/review/page-image?page=${encodeURIComponent(item.page)}`
        )
        pageData.value = data
        // 页图字节仍走鉴权代理，不裸取 MinIO URL
        pageImageUrl.value = buildLiteratureAssetUrl(data.uri)
      } catch (err) {
        pageError.value = err?.message || '页图加载失败'
      } finally {
        pageLoading.value = false
      }
    }
    const visibleItems = computed(() =>
      (meta.value.items || []).filter((item) => !hideResolved.value || !item.decision)
    )
    let generation = 0
    let disposed = false
    const choices = [
      { key: 'A', label: 'A 保留/还原 MinerU' },
      { key: 'B', label: 'B 采用 PaddleOCR' },
      { key: 'C', label: 'C 两者皆可' },
      { key: 'D', label: 'D 都有误（存疑）' }
    ]

    const enc = (v) => encodeURIComponent(v)
    const load = async () => {
      const ticket = ++generation
      loading.value = true
      try {
        const data = await apiGet(`/api/literature/papers/${enc(props.rid)}/review`)
        if (!disposed && ticket === generation) {
          // Keep local suggestions only while the immutable review item is unchanged.
          const suggestions = new Map(
            (meta.value.items || []).map((item) => [
              `${item.item_id}:${item.render_digest}`,
              item.suggestion
            ])
          )
          for (const item of data.items || []) {
            if (item.item_id && item.render_digest)
              item.suggestion = suggestions.get(`${item.item_id}:${item.render_digest}`) || ''
          }
          meta.value = data
        }
      } catch (err) {
        if (disposed || ticket !== generation) return
        meta.value = { total: 0, blocking_pending: 0, items: [] }
        notice.value = `复核结果读取失败：${err?.message || err}`
      } finally {
        if (!disposed && ticket === generation) loading.value = false
      }
    }
    const triggerDual = async () => {
      dualLoading.value = true
      notice.value = ''
      try {
        await apiPost(`/api/literature/papers/${enc(props.rid)}/review/dual-channel`)
        notice.value = '双通道渲染已入队（不表示完成）；任务完成后自动刷新差异。'
        emit('changed')
      } catch (err) {
        notice.value = `触发失败：${err?.message || err}`
      } finally {
        dualLoading.value = false
      }
    }
    const suggest = async (item) => {
      if (busyItem.value !== null || applying.value) return
      busyItem.value = item.report_idx
      try {
        const data = await apiPost(`/api/literature/papers/${enc(props.rid)}/review/suggest`, {
          report_idx: item.report_idx
        })
        item.suggestion = data?.suggestion || ''
      } catch (err) {
        notice.value = `AI 建议失败：${err?.message || err}`
      } finally {
        busyItem.value = null
      }
    }
    const applyDecisions = async () => {
      if (applying.value || busyItem.value !== null || loading.value) return
      applying.value = true
      notice.value = ''
      try {
        const manifest = await apiPost(`/api/literature/papers/${enc(props.rid)}/review/apply`)
        notice.value = `修正回放完成：${manifest.applied_decisions} 条裁决（替换 ${manifest.actions.replaced} / 还原 ${manifest.actions.restored} / 标记 ${manifest.actions.marked} / 保留 ${manifest.actions.kept}）`
        await load()
        emit('changed')
      } catch (err) {
        notice.value = `回放失败：${err?.message || err}`
      } finally {
        applying.value = false
      }
    }
    const decide = async (item, choice) => {
      if (busyItem.value !== null || applying.value) return
      const legacyAck = item.stale_decision?.contract_version === 'legacy_v1'
      if (
        legacyAck &&
        !(await confirmAction(
          '此项有旧契约裁决。确认按当前 A/B/C/D 语义重新裁决？历史文本不会被当作删除/合并指令。'
        ))
      )
        return
      if (busyItem.value !== null || applying.value) return
      // 兼容语义（review_item_v2）：仅 B 携带替换文本（AI 建议优先，缺省 Paddle 文本）；A/C/D 不改写
      const resolvedText = choice === 'B' ? item.suggestion || item.paddleocr_text || '' : null
      busyItem.value = item.report_idx
      try {
        await apiPost(`/api/literature/papers/${enc(props.rid)}/review/decide`, {
          report_idx: item.report_idx,
          item_id: item.item_id,
          render_id: item.render_id,
          render_digest: item.render_digest,
          legacy_ack: legacyAck,
          para_id: item.para_id || '',
          choice,
          resolved_text: resolvedText
        })
        await load()
      } catch (err) {
        notice.value = `裁决失败：${err?.message || err}`
      } finally {
        busyItem.value = null
      }
    }

    watch(() => props.rid, load, { immediate: true })
    watch(
      () => props.versionToken,
      () => {
        if (!applying.value && busyItem.value === null) load()
      }
    )
    onBeforeUnmount(() => {
      disposed = true
      generation++
    })

    return {
      meta,
      hideResolved,
      visibleItems,
      busyItem,
      loading,
      load,
      notice,
      dualLoading,
      applying,
      choices,
      triggerDual,
      decide,
      suggest,
      applyDecisions,
      // 页图对照
      expanded,
      pageData,
      pageImageUrl,
      pageLoading,
      pageError,
      pageImageFailed,
      pageBusy,
      ownBoxes,
      boxStyle,
      togglePage
    }
  }
}
</script>

<style lang="less" scoped>
.item-pageview {
  margin-top: 8px;
}
.pageview-toggle {
  background: transparent;
  border: 1px solid var(--el-border-color, #dcdfe6);
  border-radius: 4px;
  padding: 3px 10px;
  font-size: 12px;
  cursor: pointer;
  color: var(--el-text-color-regular, #606266);
}
.pageview-toggle:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.pageview-none,
.pageview-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary, #909399);
}
.pageview-hint.error {
  color: var(--el-color-danger, #f56c6c);
}
.pageview-body {
  margin-top: 8px;
  max-width: 640px;
}
// 页图容器必须相对定位，红框才能按百分比叠加
.pageview-canvas {
  position: relative;
  display: inline-block;
  border: 1px solid var(--el-border-color, #dcdfe6);
  border-radius: 4px;
  overflow: hidden;
  line-height: 0;
}
.page-img {
  display: block;
  max-width: 100%;
  height: auto;
}
.bbox-box {
  position: absolute;
  border: 2px solid var(--el-color-danger, #f56c6c);
  background: rgba(245, 108, 108, 0.12);
  border-radius: 2px;
  pointer-events: none;
  box-sizing: border-box;
}
.pageview-legend {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary, #909399);
}

.review-head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}
.review-notice {
  padding: 8px 12px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--main-color) 10%, transparent);
  margin-bottom: 10px;
  font-size: 13px;
}
.review-empty {
  color: var(--color-text-tertiary);
  padding: 16px 0;
}
.review-list {
  list-style: none;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.review-item {
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  padding: 10px 12px;
  &.blocking {
    border-color: color-mix(in srgb, #e6a23c 60%, transparent);
  }
  &.resolved {
    color: var(--color-text-secondary);
  }
}
.item-head {
  display: flex;
  gap: 10px;
  font-size: 12px;
  margin-bottom: 6px;
}
.item-decided {
  color: var(--main-color);
}
.item-bodies {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  label {
    display: inline-block;
    font-weight: 600;
    margin-right: 6px;
    color: var(--color-text-secondary);
  }
}
.item-choices {
  margin-top: 8px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.choice-btn {
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: transparent;
  padding: 3px 10px;
  cursor: pointer;
  &:hover {
    border-color: var(--main-color);
  }
}
</style>
