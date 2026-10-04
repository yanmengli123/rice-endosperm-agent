<template>
  <aside class="lit-sidebar" :class="{ collapsed: !pinned }" aria-label="文献工作区侧栏">
    <div class="side-tabs" role="tablist" aria-label="工作区抽屉">
      <button
        v-for="item in drawers"
        :key="item.key"
        class="side-tab"
        role="tab"
        :class="{ active: drawer === item.key }"
        :aria-selected="drawer === item.key"
        :title="item.label"
        @click="$emit('select-drawer', item.key)"
      >
        {{ item.label }}
      </button>
      <button
        class="icon-btn side-pin"
        :class="{ active: pinned }"
        :aria-pressed="pinned"
        :title="pinned ? '已钉住：侧栏常驻' : '钉住侧栏'"
        @click="$emit('toggle-pin')"
      >
        <Pin :size="13" aria-hidden="true" />
      </button>
    </div>
    <div class="side-panel">
      <!-- 会话抽屉：常驻可见，含全选/批量删除/清空归档（对标 PaperAgent §6.4） -->
      <div v-show="drawer === 'sessions'" class="side-body">
        <p class="side-hint">四类会话共用 Yuxi 原生聊天与审批；文献来源 ACL 每次运行前复验。</p>
        <div class="side-actions">
          <button class="btn" :disabled="busy" @click="$emit('new-session', 'global-qa')">
            全库问答
          </button>
          <button class="btn" :disabled="busy" @click="$emit('new-session', 'lit')">AI 检索</button>
          <button class="btn" :disabled="busy" @click="$emit('new-session', 'manage')">管理</button>
          <button class="btn" :disabled="busy || !rid" @click="$emit('new-session', 'paper')">
            本文问答
          </button>
        </div>
        <label class="batch-check"
          ><input
            type="checkbox"
            :checked="allSelected"
            :disabled="busy || !sessions.length"
            aria-label="全选会话"
            @change="$emit('toggle-all', $event.target.checked)"
          />全选</label
        >
        <span class="muted">已选 {{ selectedIds.length }}</span>
        <div class="side-actions">
          <button
            class="btn"
            :disabled="busy || !selectedIds.length"
            @click="$emit('delete-selected')"
          >
            批量删除
          </button>
          <button
            v-if="archived"
            class="btn"
            :disabled="busy || !sessions.length"
            @click="$emit('clear-archived')"
          >
            清空已归档
          </button>
        </div>
        <label class="archive-toggle"
          ><input
            type="checkbox"
            :checked="archived"
            :disabled="busy"
            aria-label="切换归档会话"
            @change="$emit('toggle-archived', $event.target.checked)"
          />归档会话</label
        >
        <ul v-if="sessions.length" class="session-rows">
          <li v-for="session in sessions" :key="session.id" class="session-row">
            <label
              ><input
                type="checkbox"
                :checked="selectedIds.includes(session.id)"
                :disabled="busy"
                :aria-label="`选择会话 ${session.title}`"
                @change="$emit('toggle-one', session.id, $event.target.checked)"
            /></label>
            <button
              class="session-title"
              :class="{ active: session.id === selectedId }"
              :disabled="busy || archived"
              @click="$emit('select-session', session.id)"
            >
              <span class="session-name">{{ session.title }}</span>
              <span class="session-kind">{{ labels[session.session.kind] }}</span>
            </button>
            <button
              v-if="archived"
              class="btn"
              :disabled="busy"
              @click="$emit('restore', session.id)"
            >
              恢复
            </button>
            <button
              class="btn"
              :disabled="busy"
              :aria-label="`删除会话 ${session.title}`"
              @click="$emit('remove', session.id)"
            >
              删除
            </button>
          </li>
        </ul>
        <p v-else class="side-empty">{{ archived ? '没有已归档会话' : '暂无会话' }}</p>
      </div>

      <!-- 文献库抽屉：类型芯片 + 检索 + 列表 -->
      <div v-show="drawer === 'papers'" class="side-body">
        <div class="kind-chips">
          <button
            v-for="chip in kindChips"
            :key="chip.value"
            class="chip"
            :class="{ active: currentKind === chip.value }"
            @click="$emit('select-kind', chip.value)"
          >
            {{ chip.label }}
          </button>
        </div>
        <input
          :value="searchQuery"
          class="side-search"
          placeholder="🔍 标题 / DOI（支持 kind:thesis）"
          aria-label="检索文献"
          @input="$emit('update:search-query', $event.target.value)"
          @keyup.enter="$emit('search')"
        />
        <ul v-if="papers.length" class="side-paper-list">
          <li v-for="paper in papers" :key="paper.rid">
            <button
              class="side-paper"
              :class="{ active: paper.rid === selectedRid }"
              @click="$emit('open-paper', paper)"
            >
              <span class="kind-badge" :data-kind="paper.kind">{{ kindLabel(paper.kind) }}</span>
              <span class="paper-name">{{ paper.title || paper.rid }}</span>
              <span v-if="paper.pub_year" class="paper-year">{{ paper.pub_year }}</span>
            </button>
          </li>
        </ul>
        <p v-else class="side-empty">暂无可访问文献。可导入 Bib、Markdown 或迁移 PaperAgent。</p>
      </div>

      <!-- 知识库抽屉：建库与导出 -->
      <div v-show="drawer === 'workbench'" class="side-body">
        <p class="side-hint">六步建库与期刊指标复用知识源权限；笔记与向量为派生导航。</p>
        <div class="side-actions">
          <button class="btn" @click="$emit('open-workbench')">建库与导出</button>
          <button class="btn" @click="$emit('open-import')">导入文献</button>
          <button class="btn" @click="$emit('open-tasks')">任务与历史</button>
          <button class="btn" @click="$emit('open-graph')">2D / 3D 图谱</button>
        </div>
        <p v-if="libraryState" class="side-status">建库运行：{{ libraryState.status }}</p>
      </div>

      <!-- 日记抽屉 -->
      <div v-show="drawer === 'diary'" class="side-body">
        <p class="side-hint">阅读日记按日保存；清空并保存即删除该天记录。</p>
        <div class="side-actions">
          <button class="btn" @click="$emit('open-diary')">打开日记工作区</button>
        </div>
      </div>
    </div>
  </aside>
</template>
<script setup>
import { Pin } from '@lucide/vue'
defineProps({
  drawer: { type: String, default: 'papers' },
  pinned: { type: Boolean, default: true },
  rid: { type: String, default: '' },
  sessions: { type: Array, default: () => [] },
  selectedId: { type: String, default: '' },
  selectedIds: { type: Array, default: () => [] },
  allSelected: { type: Boolean, default: false },
  archived: { type: Boolean, default: false },
  busy: { type: Boolean, default: false },
  papers: { type: Array, default: () => [] },
  currentKind: { type: String, default: '' },
  searchQuery: { type: String, default: '' },
  kindChips: { type: Array, default: () => [] },
  selectedRid: { type: String, default: '' },
  libraryState: { type: Object, default: null }
})
defineEmits([
  'select-drawer',
  'toggle-pin',
  'new-session',
  'toggle-all',
  'delete-selected',
  'clear-archived',
  'toggle-archived',
  'toggle-one',
  'select-session',
  'restore',
  'remove',
  'select-kind',
  'update:search-query',
  'search',
  'open-paper',
  'open-workbench',
  'open-import',
  'open-tasks',
  'open-graph',
  'open-diary'
])
const drawers = [
  { key: 'sessions', label: '会话' },
  { key: 'papers', label: '文献库' },
  { key: 'workbench', label: '知识库' },
  { key: 'diary', label: '日记' }
]
const labels = { paper: '本文', 'global-qa': '全库问答', manage: '管理', lit: '目录检索' }
const kindLabel = (kind) => (kind ? String(kind) : '文献')
</script>
<style lang="less" scoped>
.lit-sidebar {
  display: flex;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
  background: var(--color-bg-container);
  border-right: 1px solid var(--gray-150);
}
.side-tabs {
  display: flex;
  gap: 2px;
  padding: 8px 10px 0;
  border-bottom: 1px solid var(--gray-150);
  flex-shrink: 0;
}
.side-tab {
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  padding: 6px 10px;
  border-radius: 6px 6px 0 0;
  cursor: pointer;
  font-size: 13px;
  &.active {
    color: var(--main-color);
    font-weight: 600;
    box-shadow: inset 0 -2px 0 var(--main-color);
  }
}
.icon-btn {
  border: 1px solid transparent;
  background: transparent;
  border-radius: 6px;
  cursor: pointer;
  padding: 2px 6px;
  font-size: 12px;
  &.active {
    background: var(--main-color);
    color: var(--color-bg-container);
  }
}
.side-pin {
  margin-left: auto;
}
.side-panel {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 8px;
}
.side-body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.side-hint,
.side-empty,
.side-status {
  font-size: 12px;
  color: var(--color-text-tertiary);
  margin: 0;
}
.side-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.side-search {
  width: 100%;
  box-sizing: border-box;
  padding: 6px 10px;
  background: var(--color-bg-container);
  color: var(--color-text-primary);
  border: 1px solid var(--gray-150);
  border-radius: 6px;
}
.kind-chips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.chip {
  border: 1px solid var(--gray-300);
  border-radius: 12px;
  background: transparent;
  color: var(--color-text-secondary);
  padding: 1px 10px;
  font-size: 12px;
  cursor: pointer;
  &.active {
    background: var(--main-color);
    border-color: var(--main-color);
    color: var(--color-text-primary);
  }
}
.muted {
  font-size: 12px;
  color: var(--color-text-tertiary);
}
.batch-check,
.archive-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.session-rows {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.session-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 4px;
  border-radius: 6px;
  &:hover {
    background: var(--color-bg-elevated);
  }
}
.session-title {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  text-align: left;
  border: 1px solid var(--gray-150);
  background: var(--color-bg-container);
  color: var(--color-text-primary);
  border-radius: 6px;
  padding: 3px 8px;
  cursor: pointer;
  &.active {
    border-color: var(--main-color);
  }
}
.session-name {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}
.session-kind {
  font-size: 11px;
  color: var(--color-text-tertiary);
}
.side-paper-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.side-paper {
  width: 100%;
  display: flex;
  align-items: baseline;
  gap: 6px;
  text-align: left;
  border: 1px solid transparent;
  background: transparent;
  color: var(--color-text-primary);
  border-radius: 6px;
  padding: 5px 8px;
  cursor: pointer;
  &.active {
    border-color: var(--main-color);
  }
  &:hover {
    background: var(--color-bg-elevated);
  }
}
.kind-badge {
  font-size: 11px;
  border-radius: 4px;
  padding: 0 5px;
  background: color-mix(in srgb, var(--main-color) 12%, transparent);
  flex-shrink: 0;
}
.paper-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
}
.paper-year {
  font-size: 11px;
  color: var(--color-text-tertiary);
  flex-shrink: 0;
}
</style>
