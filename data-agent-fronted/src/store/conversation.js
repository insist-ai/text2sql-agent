import { defineStore } from 'pinia'
import {
  listConversations,
  createConversation,
  renameConversation,
  deleteConversation,
  listMessages,
} from '@/api/conversation'

/** 把后端消息结构归一化成前端统一形态 */
function normalizeMessage(m) {
  return {
    id: m.id,
    role: m.role, // 'user' | 'assistant'
    content: m.content || '',
    sqlText: m.sql_text || '',
    result: m.result_json || null,
    steps: [], // 历史消息无进度步骤
    error: '',
  }
}

export const useConversationStore = defineStore('conversation', {
  state: () => ({
    conversations: [],
    currentId: null,
    messages: [],
    loadingList: false,
    loadingHistory: false,
  }),

  getters: {
    currentConversation: (state) =>
      state.conversations.find((c) => c.id === state.currentId) || null,
  },

  actions: {
    async fetchConversations() {
      this.loadingList = true
      try {
        this.conversations = await listConversations()
        // 防御：当前选中的会话不在最新列表里（如换账号后残留旧会话ID），
        // 重置选中与消息，避免拿着他人会话ID去拉消息被403、且旧画面残留
        if (this.currentId && !this.conversations.some((c) => c.id === this.currentId)) {
          this.currentId = null
          this.messages = []
        }
        // 若没有选中的会话，默认选最新一条（后端按更新时间倒序，第一条即最新）
        if (!this.currentId && this.conversations.length) {
          this.currentId = this.conversations[0].id
        }
      } finally {
        this.loadingList = false
      }
    },

    /** 清空会话相关状态（登录/登出切换账号时调用，避免上一账号的画面残留） */
    resetState() {
      this.conversations = []
      this.currentId = null
      this.messages = []
    },

    async createConversation(title) {
      const conv = await createConversation(title)
      this.conversations.unshift(conv)
      this.currentId = conv.id
      this.messages = []
      return conv
    },

    async renameConversation(id, title) {
      await renameConversation(id, title)
      const conv = this.conversations.find((c) => c.id === id)
      if (conv) conv.title = title
    },

    async deleteConversation(id) {
      await deleteConversation(id)
      this.conversations = this.conversations.filter((c) => c.id !== id)
      if (this.currentId === id) {
        this.currentId = this.conversations.length ? this.conversations[0].id : null
        this.messages = []
      }
    },

    async switchConversation(id) {
      this.currentId = id
      await this.fetchMessages()
    },

    async fetchMessages() {
      if (!this.currentId) {
        this.messages = []
        return
      }
      this.loadingHistory = true
      try {
        const msgs = await listMessages(this.currentId)
        this.messages = msgs.map(normalizeMessage)
      } finally {
        this.loadingHistory = false
      }
    },

    /** 流结束后用后端落库的最终消息补齐（拿到 SQL 与结果 JSON） */
    async syncLatestMessages(sinceId = 0) {
      if (!this.currentId) return
      const msgs = await listMessages(this.currentId)
      const newer = msgs.filter((m) => m.id > sinceId).map(normalizeMessage)
      this.messages.push(...newer)
    },

    /** 流式过程中临时追加/更新的本地消息，不入库 */
    appendLocalMessage(msg) {
      this.messages.push(msg)
      return this.messages.length - 1
    },

    /** 按索引取本地消息（用于进度步骤实时更新） */
    updateLocalMessage(index, patch) {
      const target = this.messages[index]
      if (target) Object.assign(target, patch)
    },
  },
})
