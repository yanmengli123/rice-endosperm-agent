<template>
  <section class="literature-markdown" @click="handleLink">
    <details v-if="headings.length" class="reader-toc">
      <summary>目录 · {{ headings.length }} 节</summary>
      <button
        v-for="heading in headings"
        :key="heading.id"
        :style="{ marginLeft: `${(heading.level - 1) * 12}px` }"
        @click="scrollTo(heading.id)"
      >
        {{ heading.title }}
      </button>
    </details>
    <p v-if="anchorNotice" role="status">{{ anchorNotice }}</p>
    <div ref="host">
      <MarkdownPreview :content="content" :knowledge-base-id="knowledgeBaseId" />
    </div>
  </section>
</template>
<script setup>
import { onMounted, onBeforeUnmount, ref, useId, watch, nextTick } from 'vue'
import { normalizeReaderAnchor } from '@/utils/literature_reader'
import MarkdownPreview from '@/components/common/MarkdownPreview.vue'
const props = defineProps({
  content: { type: String, default: '' },
  knowledgeBaseId: { type: String, default: '' },
  anchor: { type: String, default: '' },
  navigationToken: { type: [String, Number], default: '' }
})
const emit = defineEmits(['open-link', 'open-image'])
const host = ref(null),
  headings = ref([]),
  prefix = `literature-${useId()}`
const anchorNotice = ref('')
let observer
let appliedAnchor = ''
const hydrate = () => {
  if (!host.value) return
  observer?.disconnect()
  const walker = document.createTreeWalker(host.value, NodeFilter.SHOW_TEXT)
  const nodes = []
  while (walker.nextNode()) {
    const node = walker.currentNode
    if (!node.parentElement?.closest('pre,code,a,script,style') && node.textContent.includes('[['))
      nodes.push(node)
  }
  for (const node of nodes) {
    const text = node.textContent,
      matches = [...text.matchAll(/\[\[([^\]\n]{1,500})\]\]/g)]
    if (!matches.length) continue
    const fragment = document.createDocumentFragment()
    let start = 0
    for (const match of matches) {
      fragment.append(document.createTextNode(text.slice(start, match.index)))
      const [target, ...label] = match[1].split('|'),
        anchor = document.createElement('a')
      anchor.textContent = label.join('|') || target
      anchor.dataset.wikiTarget = target
      anchor.href = '#'
      fragment.append(anchor)
      start = match.index + match[0].length
    }
    fragment.append(document.createTextNode(text.slice(start)))
    node.replaceWith(fragment)
  }
  headings.value = [...host.value.querySelectorAll('h1,h2,h3,h4,h5,h6')].map((node, index) => {
    if (node.id && !node.id.startsWith(prefix)) node.dataset.originalId = node.id
    node.id = `${prefix}-${index}`
    return { id: node.id, title: node.textContent, level: Number(node.tagName.slice(1)) }
  })
  observer?.observe(host.value, { childList: true, subtree: true })
  applyAnchor()
}
const scrollTo = (id) => {
  const heading = [...(host.value?.querySelectorAll('h1,h2,h3,h4,h5,h6') || [])].find(
    (node) => node.id === id
  )
  heading?.scrollIntoView({ block: 'start', behavior: 'smooth' })
}
const navigateAnchor = (value) => {
  const wanted = normalizeReaderAnchor(value)
  if (!wanted || !host.value) return false
  const counts = new Map()
  const matches = [...host.value.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li')].filter((node) => {
    if (wanted.startsWith('^')) return node.textContent.trim().endsWith(wanted)
    if (!/^H[1-6]$/.test(node.tagName)) return false
    const title = normalizeReaderAnchor(node.textContent)
    const count = counts.get(title) || 0
    counts.set(title, count + 1)
    return (
      wanted === normalizeReaderAnchor(node.dataset.originalId) ||
      wanted === (count ? `${title}-${count}` : title) ||
      wanted === node.id
    )
  })
  const target = matches.length === 1 ? matches[0] : null
  if (target) target.scrollIntoView({ block: 'start', behavior: 'smooth' })
  return !!target
}
const applyAnchor = () => {
  const key = JSON.stringify([props.anchor, props.navigationToken, props.content])
  if (!props.anchor || key === appliedAnchor) return
  if (navigateAnchor(props.anchor)) {
    appliedAnchor = key
    anchorNotice.value = ''
  } else anchorNotice.value = '该章节或块锚点不存在于当前版本；未跳转到其他位置。'
}
watch(
  () => [props.anchor, props.navigationToken, props.content],
  async () => {
    appliedAnchor = ''
    anchorNotice.value = ''
    await nextTick()
    hydrate()
  }
)
const handleLink = (event) => {
  const image = event.target.closest?.('img')
  if (image && host.value?.contains(image)) {
    event.preventDefault()
    emit('open-image', image.currentSrc || image.src)
    return
  }
  const anchor = event.target.closest?.('a')
  if (!anchor || !host.value?.contains(anchor)) return
  const target = anchor.dataset.wikiTarget
  const href = anchor.getAttribute('href') || ''
  if ((target || href).startsWith('#')) {
    event.preventDefault()
    anchorNotice.value = navigateAnchor(target || href) ? '' : '当前版本未找到该章节或块锚点。'
    return
  }
  if (target || (!/^(?:[a-z]+:|\/|#)/i.test(href) && /\.md(?:#.*)?$/i.test(href))) {
    event.preventDefault()
    emit('open-link', target || href)
  }
}
onMounted(() => {
  observer = new MutationObserver(hydrate)
  hydrate()
})
onBeforeUnmount(() => observer?.disconnect())
</script>
<style scoped>
.reader-toc button {
  display: block;
  border: 0;
  background: transparent;
  color: var(--color-text-primary);
  padding: 5px;
  cursor: pointer;
  text-align: left;
}
</style>
