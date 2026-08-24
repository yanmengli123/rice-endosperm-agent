<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { authApi } from '@/apis/auth_api'

const router = useRouter()
const form = reactive({ uid: '', username: '', password: '', inviteCode: '' })
const loading = ref(false)
const error = ref('')
const success = ref(false)
const inviteRequired = ref(false)

async function submit() {
  if (form.password.length < 8) {
    error.value = '密码至少需要 8 位'
    return
  }
  loading.value = true
  error.value = ''
  try {
    await authApi.register({
      uid: form.uid.trim(),
      username: form.username.trim(),
      password: form.password,
      invite_code: form.inviteCode.trim() || null
    })
    success.value = true
    setTimeout(() => router.push('/login'), 1200)
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    const config = await authApi.getRegisterConfig()
    if (!config.enabled) {
      await router.replace('/login')
      return
    }
    inviteRequired.value = Boolean(config.invite_required)
  } catch (e) {
    error.value = e.message
  }
})
</script>

<template>
  <div class="login-view">
    <main class="login-main">
      <div class="login-card">
        <div class="login-content">
          <div class="login-form">
            <h1 class="login-title">注册稻芯智析账号</h1>
            <p class="register-lead">注册后请联系管理员绑定部门并开通使用权限。</p>
            <template v-if="!success">
              <label>
                <span>登录 ID</span>
                <input v-model="form.uid" placeholder="3-20 位字母/数字/下划线" autocomplete="username" required>
              </label>
              <label>
                <span>显示名称</span>
                <input v-model="form.username" placeholder="你的姓名或昵称" required>
              </label>
              <label>
                <span>密码（至少 8 位）</span>
                <input v-model="form.password" type="password" autocomplete="new-password" required>
              </label>
              <label v-if="inviteRequired">
                <span>邀请码</span>
                <input v-model="form.inviteCode" placeholder="如系统要求请填写" autocomplete="off">
              </label>
              <p v-if="error" class="register-error" role="alert">{{ error }}</p>
              <button class="login-button" :disabled="loading || !form.uid || !form.username || !form.password" @click="submit">
                {{ loading ? '正在注册…' : '注册' }}
              </button>
            </template>
            <template v-else>
              <p class="register-success">注册成功，正在跳转登录…</p>
            </template>
            <p class="register-footer">
              已有账号？
              <router-link to="/login">返回登录</router-link>
            </p>
          </div>
        </div>
      </div>
    </main>
  </div>
</template>

<style scoped>
.register-lead {
  font-size: 12px;
  color: #8a948d;
  margin: 0 0 10px;
}

.register-error {
  color: #c2554d;
  font-size: 13px;
  margin: 4px 0 0;
}

.register-success {
  color: var(--main-color, #2f6b4f);
  font-size: 14px;
  font-weight: 600;
}

.register-footer {
  font-size: 12px;
  color: #8a948d;
  margin-top: 4px;
}

.register-footer a {
  color: var(--main-color, #2f6b4f);
}
</style>
