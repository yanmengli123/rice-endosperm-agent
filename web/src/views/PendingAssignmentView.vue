<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()
const checking = ref(false)
const statusText = ref('')

async function refreshStatus() {
  checking.value = true
  statusText.value = ''
  try {
    await userStore.getCurrentUser()
    if (userStore.departmentId) {
      await router.replace('/agent')
      return
    }
    statusText.value = '账号仍在等待管理员分配部门。'
  } catch (error) {
    statusText.value = error.message || '状态检查失败'
  } finally {
    checking.value = false
  }
}

function logout() {
  userStore.logout()
  router.replace('/login')
}
</script>

<template>
  <main class="pending-page">
    <section class="pending-card">
      <img src="/brand/rice-endosperm/indexlogo.png" alt="稻芯智析" />
      <h1>账号等待开通</h1>
      <p>你的账号已经注册成功。管理员为你分配部门后，即可使用知识库与智能问答。</p>
      <p v-if="statusText" class="status-text">{{ statusText }}</p>
      <div class="actions">
        <a-button type="primary" :loading="checking" @click="refreshStatus">刷新开通状态</a-button>
        <a-button @click="logout">退出登录</a-button>
      </div>
    </section>
  </main>
</template>

<style lang="less" scoped>
.pending-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background: var(--gray-10);
}

.pending-card {
  width: min(460px, 100%);
  padding: 36px;
  text-align: center;
  border: 1px solid var(--gray-200);
  border-radius: 18px;
  background: var(--gray-0);
  box-shadow: var(--shadow-2);

  img { width: 96px; height: 96px; object-fit: contain; }
  h1 { margin: 18px 0 10px; }
  p { color: var(--gray-600); line-height: 1.7; }
}

.status-text { color: var(--main-color); }
.actions { display: flex; justify-content: center; gap: 12px; margin-top: 24px; }
</style>
