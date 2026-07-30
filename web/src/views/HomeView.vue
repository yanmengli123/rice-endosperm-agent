<template>
  <div class="rice-home-page">
    <div v-if="isLoading" class="page-state" aria-live="polite">
      <div class="loading-mark" aria-hidden="true"></div>
      <p>正在连接科研工作台…</p>
    </div>

    <div v-else-if="error" class="page-state">
      <div class="state-card" role="alert">
        <CircleAlert :size="24" />
        <h1>{{ error.title }}</h1>
        <p>{{ error.message }}</p>
        <a-button type="primary" @click="loadData">重新连接</a-button>
      </div>
    </div>

    <template v-else>
      <header class="site-header">
        <button class="brand-link" type="button" aria-label="返回首页" @click="goHome">
          <img :src="brandLogo" alt="" class="brand-logo" />
          <span class="brand-copy">
            <strong>{{ brandName }}</strong>
            <small>水稻胚乳证据型科研智能体</small>
          </span>
        </button>

        <div class="header-actions">
          <ThemeToggle />
          <UserInfoComponent v-if="userStore.isLoggedIn" />
          <a-button v-else type="text" @click="goToLogin">登录</a-button>
        </div>
      </header>

      <main>
        <section class="hero-section" aria-labelledby="hero-title">
          <div class="hero-copy">
            <p class="eyebrow">
              <Sprout :size="16" aria-hidden="true" />
              Plant Science Evidence Interface
            </p>

            <h1 id="hero-title">
              <span v-for="part in heroTitleParts" :key="part" class="hero-title-line">
                {{ part }}
              </span>
            </h1>
            <p class="hero-subtitle">{{ heroSubtitle }}</p>

            <div class="hero-actions">
              <a-button type="primary" size="large" class="primary-action" @click="goToChat()">
                开始科研问答
                <ArrowRight :size="18" aria-hidden="true" />
              </a-button>
            </div>

            <p class="research-note">
              <ShieldCheck :size="16" aria-hidden="true" />
              回答用于科研信息辅助，关键结论请结合原始文献与实验数据核验。
            </p>
          </div>

          <div class="hero-visual" aria-label="水稻胚乳知识网络示意图">
            <div class="visual-frame">
              <img
                src="/brand/rice-endosperm/brand-seal.svg"
                alt="稻芯智析：水稻籽粒剖面与知识节点"
              />
              <span class="visual-tag visual-tag--gene">Gene ID</span>
              <span class="visual-tag visual-tag--paper">文献来源</span>
              <span class="visual-tag visual-tag--trait">品质性状</span>
            </div>
          </div>
        </section>

        <section class="capability-section" aria-labelledby="capability-title">
          <div class="section-heading">
            <p>当前可用能力</p>
            <h2 id="capability-title">为水稻胚乳问题建立清晰的研究入口</h2>
          </div>

          <div class="capability-grid">
            <article v-for="item in capabilities" :key="item.title" class="capability-card">
              <span class="capability-icon">
                <component :is="item.icon" :size="22" aria-hidden="true" />
              </span>
              <h3>{{ item.title }}</h3>
              <p>{{ item.description }}</p>
            </article>
          </div>
        </section>

        <section class="question-section" aria-labelledby="question-title">
          <div class="question-heading">
            <div>
              <p>研究问题示例</p>
              <h2 id="question-title">从一个明确的问题开始</h2>
            </div>
            <span>点击后进入工作台，问题仍可继续编辑</span>
          </div>

          <div class="question-grid">
            <button
              v-for="item in exampleQuestions"
              :key="item.category"
              type="button"
              class="question-card"
              @click="goToChat(item.question)"
            >
              <span>{{ item.category }}</span>
              <strong>{{ item.question }}</strong>
              <ArrowRight :size="17" aria-hidden="true" />
            </button>
          </div>
        </section>
      </main>

      <footer class="site-footer">
        <p>{{ infoStore.footer?.copyright || '© 稻芯智析 2026 · 基于 Yuxi 构建' }}</p>
        <p>Rice Endosperm Intelligence</p>
      </footer>
    </template>
  </div>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { useInfoStore } from '@/stores/info'
import { healthApi } from '@/apis/system_api'
import UserInfoComponent from '@/components/UserInfoComponent.vue'
import ThemeToggle from '@/components/ThemeToggle.vue'
import {
  ArrowRight,
  BookOpenCheck,
  Bot,
  CircleAlert,
  Quote,
  ShieldCheck,
  Sprout
} from 'lucide-vue-next'

const DRAFT_QUESTION_KEY = 'rice-endosperm-draft-question'

const router = useRouter()
const userStore = useUserStore()
const infoStore = useInfoStore()
const isLoading = ref(true)
const error = ref(null)

const brandName = computed(() => infoStore.organization?.name || '稻芯智析')
const brandLogo = computed(
  () => infoStore.organization?.logo || '/brand/rice-endosperm/logo-mark.svg'
)
const heroTitle = computed(
  () => infoStore.branding?.title || '从基因到证据，理解水稻胚乳发育'
)
const heroTitleParts = computed(() => {
  const parts = heroTitle.value.split(/[，,]/, 2)
  return parts.length === 2 ? [`${parts[0]}，`, parts[1]] : [heroTitle.value]
})
const heroSubtitle = computed(
  () =>
    infoStore.branding?.subtitle ||
    '聚焦胚乳发育、籽粒灌浆、淀粉与储藏蛋白积累，为科研问题提供可追溯的文献与实验依据。'
)

const capabilities = [
  {
    title: '文献与知识库问答',
    description: '围绕水稻胚乳发育、灌浆和品质形成组织研究问题与领域资料。',
    icon: BookOpenCheck
  },
  {
    title: '回答来源引用',
    description: '沿用工作台现有来源展示，帮助用户返回文献和知识片段核验结论。',
    icon: Quote
  },
  {
    title: '智能体研究工作台',
    description: '在统一会话中使用模型、知识库、文件和已有工具完成科研信息整理。',
    icon: Bot
  }
]

const exampleQuestions = [
  {
    category: '基因功能',
    question: 'OsbZIP58 在水稻胚乳中的功能及直接证据是什么？'
  },
  {
    category: '品质性状',
    question: '总结影响水稻胚乳垩白形成的关键基因和调控通路。'
  },
  {
    category: '发育过程',
    question: '梳理水稻胚乳灌浆过程中淀粉合成相关基因的表达变化。'
  },
  {
    category: '实验设计',
    question: '如何验证一个转录因子是否直接调控淀粉合成基因？'
  }
]

const loadData = async () => {
  isLoading.value = true
  error.value = null

  try {
    const response = await healthApi.checkHealth()
    if (response.status !== 'ok') {
      throw new Error('服务不可用')
    }
    await infoStore.loadInfoConfig(true)
  } catch (loadError) {
    console.error('首页加载失败:', loadError)
    error.value = {
      title: '科研工作台暂时无法连接',
      message: '后端服务没有正常响应，请确认服务状态后重试。'
    }
  } finally {
    isLoading.value = false
  }
}

const goHome = () => router.push('/')
const goToLogin = () => router.push('/login')

const goToChat = (question = '') => {
  if (question) {
    sessionStorage.setItem(DRAFT_QUESTION_KEY, question)
  }

  if (!userStore.isLoggedIn) {
    sessionStorage.setItem('redirect', '/')
    router.push('/login')
    return
  }

  router.push('/agent')
}

onMounted(loadData)
</script>

<style lang="less" scoped>
.rice-home-page {
  min-height: 100vh;
  color: var(--rice-text);
  background:
    radial-gradient(circle at 88% 9%, color-mix(in srgb, var(--rice-gold) 10%, transparent), transparent 26%),
    var(--rice-page-bg);
}

.page-state {
  display: flex;
  min-height: 100vh;
  align-items: center;
  justify-content: center;
  padding: 24px;
  color: var(--rice-text-secondary);
}

.loading-mark {
  width: 22px;
  height: 38px;
  margin-right: 12px;
  background: var(--main-color);
  border-radius: 70% 30% 70% 30%;
  animation: loadingPulse 1.4s ease-in-out infinite;
}

.state-card {
  width: min(420px, 100%);
  padding: 24px;
  text-align: center;
  background: var(--rice-surface);
  border: 1px solid var(--rice-border);
  border-radius: 16px;

  svg {
    color: var(--color-error-500);
  }

  h1 {
    margin: 12px 0 6px;
    color: var(--rice-text);
    font-size: 20px;
    font-weight: 600;
  }

  p {
    margin: 0 0 20px;
  }
}

.site-header {
  display: flex;
  position: sticky;
  top: 0;
  z-index: 20;
  align-items: center;
  justify-content: space-between;
  min-height: 68px;
  padding: 10px clamp(20px, 5vw, 72px);
  background: color-mix(in srgb, var(--rice-page-bg) 90%, transparent);
  border-bottom: 1px solid var(--rice-border);
  backdrop-filter: blur(14px);
}

.brand-link {
  display: inline-flex;
  align-items: center;
  min-width: 0;
  padding: 0;
  color: inherit;
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: 8px;

  &:focus-visible {
    outline: 3px solid color-mix(in srgb, var(--main-color) 30%, transparent);
    outline-offset: 4px;
  }
}

.brand-logo {
  width: 42px;
  height: 42px;
  flex: 0 0 auto;
}

.brand-copy {
  display: flex;
  flex-direction: column;
  min-width: 0;
  margin-left: 10px;

  strong {
    color: var(--rice-text);
    font-size: 17px;
    font-weight: 650;
    line-height: 1.3;
  }

  small {
    color: var(--rice-text-secondary);
    font-size: 11px;
    line-height: 1.4;
  }
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.hero-section {
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(360px, 0.92fr);
  gap: clamp(44px, 7vw, 96px);
  align-items: center;
  width: min(1180px, calc(100% - 40px));
  min-height: 640px;
  margin: 0 auto;
  padding: 76px 0 64px;
}

.hero-copy {
  max-width: 690px;
}

.eyebrow {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  margin: 0 0 18px;
  color: var(--main-700);
  font-size: 13px;
  font-weight: 650;
  letter-spacing: 0.045em;
}

.hero-copy h1 {
  max-width: 680px;
  margin: 0;
  color: var(--rice-text);
  font-size: clamp(38px, 5.2vw, 64px);
  font-weight: 750;
  line-height: 1.12;
  letter-spacing: -0.035em;
  text-wrap: balance;
}

.hero-title-line {
  display: block;
}

.hero-subtitle {
  max-width: 660px;
  margin: 24px 0 0;
  color: var(--rice-text-secondary);
  font-size: clamp(16px, 1.7vw, 20px);
  line-height: 1.8;
}

.hero-actions {
  margin-top: 32px;
}

.primary-action {
  display: inline-flex;
  min-width: 172px;
  align-items: center;
  justify-content: center;
  gap: 8px;
}

.research-note {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin: 22px 0 0;
  color: var(--rice-text-secondary);
  font-size: 12px;
  line-height: 1.65;

  svg {
    flex: 0 0 auto;
    margin-top: 2px;
    color: var(--main-600);
  }
}

.hero-visual {
  display: flex;
  align-items: center;
  justify-content: center;
}

.visual-frame {
  position: relative;
  width: min(430px, 100%);
  aspect-ratio: 1;
  padding: 30px;
  background:
    linear-gradient(var(--rice-surface), var(--rice-surface)) padding-box,
    linear-gradient(145deg, var(--main-100), var(--rice-border)) border-box;
  border: 1px solid transparent;
  border-radius: 32px;

  img {
    display: block;
    width: 100%;
    height: 100%;
  }
}

.visual-tag {
  position: absolute;
  padding: 6px 10px;
  color: var(--rice-text-secondary);
  font-size: 12px;
  font-weight: 600;
  background: var(--rice-surface);
  border: 1px solid var(--rice-border);
  border-radius: 999px;
}

.visual-tag--gene {
  top: 17%;
  left: -16px;
  color: var(--main-700);
}

.visual-tag--paper {
  top: 31%;
  right: -18px;
  color: var(--rice-evidence);
}

.visual-tag--trait {
  right: 7%;
  bottom: 10%;
  color: var(--color-accent-700);
}

.capability-section,
.question-section {
  width: min(1180px, calc(100% - 40px));
  margin: 0 auto;
  padding: 72px 0;
}

.capability-section {
  border-top: 1px solid var(--rice-border);
}

.section-heading,
.question-heading {
  margin-bottom: 26px;

  > p,
  > div > p {
    margin: 0 0 7px;
    color: var(--main-700);
    font-size: 12px;
    font-weight: 650;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  h2 {
    margin: 0;
    color: var(--rice-text);
    font-size: clamp(24px, 3vw, 34px);
    font-weight: 680;
    line-height: 1.35;
  }
}

.capability-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}

.capability-card {
  min-height: 196px;
  padding: 24px;
  background: var(--rice-surface);
  border: 1px solid var(--rice-border);
  border-radius: 14px;

  h3 {
    margin: 18px 0 8px;
    color: var(--rice-text);
    font-size: 17px;
    font-weight: 650;
  }

  p {
    margin: 0;
    color: var(--rice-text-secondary);
    font-size: 14px;
    line-height: 1.75;
  }
}

.capability-icon {
  display: inline-flex;
  width: 42px;
  height: 42px;
  align-items: center;
  justify-content: center;
  color: var(--main-700);
  background: var(--main-50);
  border: 1px solid var(--main-100);
  border-radius: 10px;
}

.question-section {
  padding-top: 48px;
}

.question-heading {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 24px;

  > span {
    max-width: 280px;
    color: var(--rice-text-secondary);
    font-size: 12px;
    text-align: right;
  }
}

.question-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.question-card {
  display: grid;
  grid-template-columns: 88px minmax(0, 1fr) 20px;
  gap: 16px;
  align-items: center;
  min-height: 92px;
  padding: 18px 20px;
  color: inherit;
  text-align: left;
  cursor: pointer;
  background: var(--rice-surface);
  border: 1px solid var(--rice-border);
  border-radius: 12px;
  transition:
    background-color 0.18s ease,
    border-color 0.18s ease;

  > span {
    color: var(--main-700);
    font-size: 12px;
    font-weight: 650;
  }

  strong {
    color: var(--rice-text);
    font-size: 14px;
    font-weight: 550;
    line-height: 1.65;
  }

  svg {
    color: var(--rice-text-secondary);
  }

  &:hover {
    background: var(--main-10);
    border-color: var(--main-200);
  }

  &:focus-visible {
    outline: 3px solid color-mix(in srgb, var(--main-color) 28%, transparent);
    outline-offset: 2px;
  }
}

.site-footer {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  width: min(1180px, calc(100% - 40px));
  margin: 0 auto;
  padding: 26px 0 32px;
  color: var(--rice-text-secondary);
  font-size: 12px;
  border-top: 1px solid var(--rice-border);

  p {
    margin: 0;
  }
}

@keyframes loadingPulse {
  0%,
  100% {
    opacity: 0.55;
  }
  50% {
    opacity: 1;
  }
}

@media (max-width: 900px) {
  .hero-section {
    grid-template-columns: 1fr;
    min-height: auto;
    padding-top: 62px;
  }

  .hero-copy {
    max-width: none;
  }

  .hero-visual {
    padding: 0 32px;
  }

  .visual-frame {
    width: min(390px, 100%);
  }

  .capability-grid {
    grid-template-columns: 1fr;
  }

  .capability-card {
    min-height: 0;
  }
}

@media (max-width: 640px) {
  .site-header {
    min-height: 60px;
    padding: 8px 16px;
  }

  .brand-logo {
    width: 36px;
    height: 36px;
  }

  .brand-copy small {
    display: none;
  }

  .hero-section,
  .capability-section,
  .question-section,
  .site-footer {
    width: min(100% - 32px, 1180px);
  }

  .hero-section {
    gap: 42px;
    padding: 48px 0;
  }

  .hero-copy h1 {
    font-size: clamp(32px, 8.7vw, 36px);
  }

  .hero-visual {
    padding: 0 20px;
  }

  .visual-tag--gene {
    left: -8px;
  }

  .visual-tag--paper {
    right: -8px;
  }

  .question-heading {
    align-items: flex-start;
    flex-direction: column;

    > span {
      text-align: left;
    }
  }

  .question-grid {
    grid-template-columns: 1fr;
  }

  .question-card {
    grid-template-columns: 76px minmax(0, 1fr) 18px;
    gap: 10px;
    min-height: 88px;
    padding: 16px;
  }

  .site-footer {
    flex-direction: column;
  }
}

@media (prefers-reduced-motion: reduce) {
  .loading-mark {
    animation: none;
  }
}
</style>
