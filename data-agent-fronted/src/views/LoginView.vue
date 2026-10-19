<template>
  <div class="login-page">
    <el-card class="login-card" shadow="always">
      <template #header>
        <div class="card-header">
          <span class="title">智能库</span>
          <span class="subtitle">{{ isLoginMode ? '账号登录' : '注册账号' }}</span>
        </div>
      </template>

      <!-- 登录表单 -->
      <el-form
        v-if="isLoginMode"
        ref="loginFormRef"
        :model="loginForm"
        :rules="loginRules"
        label-position="top"
        size="large"
        @keyup.enter="handleLogin"
      >
        <el-form-item prop="username" label="用户名">
          <el-input v-model="loginForm.username" placeholder="请输入用户名" clearable />
        </el-form-item>
        <el-form-item prop="password" label="密码">
          <el-input v-model="loginForm.password" type="password" placeholder="请输入密码" show-password />
        </el-form-item>
        <el-button
          class="submit-btn"
          type="primary"
          size="large"
          :loading="loading"
          @click="handleLogin"
        >
          登 录
        </el-button>
        <div class="switch-line">
          还没有账号？
          <el-link type="primary" :underline="false" @click="switchMode(false)">立即注册</el-link>
        </div>
      </el-form>

      <!-- 注册表单 -->
      <el-form
        v-else
        ref="registerFormRef"
        :model="registerForm"
        :rules="registerRules"
        label-position="top"
        size="large"
        @keyup.enter="handleRegister"
      >
        <el-form-item prop="username" label="用户名">
          <el-input v-model="registerForm.username" placeholder="3-64 位，仅限字母、数字、下划线" clearable />
        </el-form-item>
        <el-form-item prop="password" label="密码">
          <el-input v-model="registerForm.password" type="password" placeholder="至少 6 位" show-password />
        </el-form-item>
        <el-form-item prop="confirmPassword" label="确认密码">
          <el-input v-model="registerForm.confirmPassword" type="password" placeholder="请再次输入密码" show-password />
        </el-form-item>
        <el-button
          class="submit-btn"
          type="primary"
          size="large"
          :loading="loading"
          @click="handleRegister"
        >
          注 册
        </el-button>
        <div class="switch-line">
          已有账号？
          <el-link type="primary" :underline="false" @click="switchMode(true)">返回登录</el-link>
        </div>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { loginUser, registerUser } from '@/api/auth'
import { getCurrentUser } from '@/api/user'
import { useUserStore } from '@/store/user'
import { useConversationStore } from '@/store/conversation'

const USERNAME_PATTERN = /^[A-Za-z0-9_]{3,64}$/

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()
const convStore = useConversationStore()

const isLoginMode = ref(true)
const loading = ref(false)
const loginFormRef = ref(null)
const registerFormRef = ref(null)

const loginForm = reactive({ username: '', password: '' })
const registerForm = reactive({ username: '', password: '', confirmPassword: '' })

const usernameRule = {
  validator: (_rule, value, callback) => {
    if (!value) {
      callback(new Error('请输入用户名'))
    } else if (!USERNAME_PATTERN.test(value)) {
      callback(new Error('用户名须为 3-64 位字母、数字或下划线'))
    } else {
      callback()
    }
  },
  trigger: 'blur',
}

const passwordRule = {
  validator: (_rule, value, callback) => {
    if (!value) {
      callback(new Error('请输入密码'))
    } else if (value.length < 6 || value.length > 128) {
      callback(new Error('密码长度须为 6-128 位'))
    } else {
      callback()
    }
  },
  trigger: 'blur',
}

const loginRules = { username: [usernameRule], password: [passwordRule] }
const registerRules = {
  username: [usernameRule],
  password: [passwordRule],
  confirmPassword: [
    {
      validator: (_rule, value, callback) => {
        if (!value) {
          callback(new Error('请再次输入密码'))
        } else if (value !== registerForm.password) {
          callback(new Error('两次输入的密码不一致'))
        } else {
          callback()
        }
      },
      trigger: 'blur',
    },
  ],
}

/** 登录：拿双 token -> 拉用户信息 -> 进首页 */
async function handleLogin() {
  const valid = await loginFormRef.value.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    const tokens = await loginUser({ ...loginForm })
    // 后端返回 snake_case（access_token/refresh_token），转换为 store 的驼峰字段
    userStore.setTokens({
      accessToken: tokens.access_token,
      refreshToken: tokens.refresh_token,
    })
    const me = await getCurrentUser()
    userStore.setUserInfo(me)
    // 换账号登录前清空上一账号的会话状态，避免旧会话ID/消息残留导致越权拉取与旧画面
    convStore.resetState()
    ElMessage.success('登录成功')
    router.push(typeof route.query.redirect === 'string' ? route.query.redirect : '/')
  } catch {
    /* 错误提示已在 axios 拦截器统一弹出 */
  } finally {
    loading.value = false
  }
}

/** 注册：成功后回登录页并预填用户名 */
async function handleRegister() {
  const valid = await registerFormRef.value.validate().catch(() => false)
  if (!valid) return
  loading.value = true
  try {
    await registerUser({ username: registerForm.username, password: registerForm.password })
    ElMessage.success('注册成功，请登录')
    loginForm.username = registerForm.username
    loginForm.password = ''
    switchMode(true)
  } catch {
    /* 错误提示已在 axios 拦截器统一弹出 */
  } finally {
    loading.value = false
  }
}

function switchMode(toLogin) {
  isLoginMode.value = toLogin
  registerForm.password = ''
  registerForm.confirmPassword = ''
}
</script>

<style scoped>
.login-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #e8f1fd 0%, #f5f7fa 60%);
}

.login-card {
  width: 380px;
}

.card-header {
  display: flex;
  align-items: baseline;
  gap: 10px;
}

.title {
  font-size: 20px;
  font-weight: 700;
  color: #303133;
}

.subtitle {
  font-size: 13px;
  color: #909399;
}

.submit-btn {
  width: 100%;
  margin-top: 4px;
}

.switch-line {
  margin-top: 14px;
  text-align: center;
  font-size: 13px;
  color: #909399;
}
</style>
