import { defineStore } from 'pinia'

const USER_STORAGE_KEY = 'data-agent-user'

/** 从 localStorage 读取持久化的用户信息 */
function loadPersistedUser() {
  try {
    const raw = localStorage.getItem(USER_STORAGE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export const useUserStore = defineStore('user', {
  state: () => {
    const saved = loadPersistedUser()
    return {
      accessToken: saved?.accessToken ?? '',
      refreshToken: saved?.refreshToken ?? '',
      userInfo: saved?.userInfo ?? null,
    }
  },
  getters: {
    isLoggedIn: (state) => Boolean(state.accessToken),
  },
  actions: {
    /** 持久化到 localStorage */
    persist() {
      localStorage.setItem(
        USER_STORAGE_KEY,
        JSON.stringify({
          accessToken: this.accessToken,
          refreshToken: this.refreshToken,
          userInfo: this.userInfo,
        }),
      )
    },
    /** 保存双 token */
    setTokens({ accessToken, refreshToken }) {
      this.accessToken = accessToken
      this.refreshToken = refreshToken
      this.persist()
    },
    /** 保存用户信息 */
    setUserInfo(info) {
      this.userInfo = info
      this.persist()
    },
    /** 退出登录，清空内存与本地缓存 */
    logout() {
      this.accessToken = ''
      this.refreshToken = ''
      this.userInfo = null
      localStorage.removeItem(USER_STORAGE_KEY)
    },
  },
})
