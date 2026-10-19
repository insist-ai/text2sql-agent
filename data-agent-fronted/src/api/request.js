import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'
import { useUserStore } from '@/store/user'

/** 无需携带 token、也不参与 401 刷新重放的接口 */
const WHITE_LIST = ['/auth/login', '/auth/register', '/auth/refresh']

const request = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

// ---------- 请求拦截器：自动携带 access_token ----------
request.interceptors.request.use((config) => {
  const userStore = useUserStore()
  if (userStore.accessToken) {
    config.headers.Authorization = `Bearer ${userStore.accessToken}`
  }
  return config
})

// ---------- 401 时用 refresh_token 换新 token 并重放原请求 ----------
let refreshing = false
let pendingQueue = []

function flushPendingQueue(token) {
  pendingQueue.forEach((cb) => cb(token))
  pendingQueue = []
}

/** 用 refresh_token 换新的 access_token（走原生 axios，避免触发本实例拦截器死循环） */
async function refreshAccessToken() {
  const userStore = useUserStore()
  if (!userStore.refreshToken) {
    throw new Error('no refresh token')
  }
  const { data } = await axios.post('/api/auth/refresh', {
    refresh_token: userStore.refreshToken,
  })
  // 后端统一响应 { code, message, data }，业务数据在 data.data
  const body = data?.data ?? data
  userStore.setTokens({
    accessToken: body.access_token,
    refreshToken: body.refresh_token ?? userStore.refreshToken,
  })
  return body.access_token
}

/** 刷新失败：清空登录态并跳回登录页 */
function forceLogout(message) {
  const userStore = useUserStore()
  userStore.logout()
  ElMessage.error(message)
  router.push('/login')
}

request.interceptors.response.use(
  (response) => response.data,
  async (error) => {
    const { response, config } = error
    if (!response || !config) {
      ElMessage.error('网络异常，请稍后重试')
      return Promise.reject(error)
    }

    const isWhiteListed = WHITE_LIST.some((url) => config.url.includes(url))

    // 非 401，或登录/注册/刷新接口本身的失败：直接提示后端 message 并抛出
    if (response.status !== 401 || isWhiteListed) {
      const detail = response.data?.message ?? response.data?.detail
      ElMessage.error(typeof detail === 'string' ? detail : `请求失败（${response.status}）`)
      return Promise.reject(error)
    }

    // 401：进入刷新流程
    if (refreshing) {
      // 已有刷新在途，当前请求排队等待新 token
      return new Promise((resolve, reject) => {
        pendingQueue.push((token) => {
          if (token) {
            config.headers.Authorization = `Bearer ${token}`
            resolve(request(config))
          } else {
            reject(error)
          }
        })
      })
    }

    refreshing = true
    try {
      const token = await refreshAccessToken()
      flushPendingQueue(token)
      config.headers.Authorization = `Bearer ${token}`
      return request(config)
    } catch {
      flushPendingQueue(null)
      forceLogout('登录已过期，请重新登录')
      return Promise.reject(error)
    } finally {
      refreshing = false
    }
  },
)

export default request
