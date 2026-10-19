<template>
  <div class="chat-layout">
    <!-- 左侧：会话列表 -->
    <aside class="sidebar">
      <div class="sidebar-header">
        <el-button type="primary" class="new-btn" @click="handleNewConversation">
          + 新建会话
        </el-button>
      </div>

      <div class="conv-list" v-loading="convStore.loadingList">
        <div
          v-for="conv in convStore.conversations"
          :key="conv.id"
          :class="['conv-item', { active: conv.id === convStore.currentId }]"
          @click="handleSwitch(conv.id)"
        >
          <template v-if="editingId === conv.id">
            <el-input
              v-model="editTitle"
              size="small"
              @click.stop
              @keyup.enter="commitRename(conv.id)"
              @blur="commitRename(conv.id)"
              ref="renameInputRef"
            />
          </template>
          <template v-else>
            <span class="conv-title" :title="conv.title || '未命名会话'">
              {{ conv.title || '未命名会话' }}
            </span>
            <span class="conv-actions" @click.stop>
              <span class="icon-btn" title="重命名" @click="startRename(conv)">✎</span>
              <el-popconfirm
                title="确定删除该会话及其所有消息？"
                confirm-button-text="删除"
                cancel-button-text="取消"
                @confirm="handleDelete(conv.id)"
              >
                <template #reference>
                  <span class="icon-btn danger" title="删除">🗑</span>
                </template>
              </el-popconfirm>
            </span>
          </template>
        </div>
        <div v-if="!convStore.loadingList && !convStore.conversations.length" class="empty-tip">
          暂无会话，点击上方「新建会话」开始
        </div>
      </div>

      <div class="sidebar-footer">
        <span class="username">{{ userStore.userInfo?.username || '用户' }}</span>
        <el-button link type="danger" @click="handleLogout">退出登录</el-button>
      </div>
    </aside>

    <!-- 右侧：对话区 -->
    <section class="main">
      <header class="main-header">
        <span class="current-title">{{ currentTitle }}</span>
      </header>

      <div ref="messagesEl" class="messages" v-loading="convStore.loadingHistory">
        <template v-for="(msg, idx) in convStore.messages" :key="msg.id || idx">
          <!-- 用户消息 -->
          <div v-if="msg.role === 'user'" class="msg-row user">
            <div class="bubble user-bubble">{{ msg.content }}</div>
            <div class="avatar user-avatar">🧑</div>
          </div>

          <!-- 助手消息 -->
          <div v-else class="msg-row assistant">
            <div class="avatar assistant-avatar">🤖</div>
            <div class="bubble assistant-bubble">
              <!-- 进度时间线 -->
              <div v-if="msg.steps && msg.steps.length" class="timeline">
                <div v-for="(step, sIdx) in msg.steps" :key="sIdx" class="timeline-item">
                  <span class="status-dot" :class="step.status">
                    <span v-if="step.status === 'running'" class="spinner" />
                    <span v-else-if="step.status === 'success'" class="tick">✓</span>
                    <span v-else class="cross">✕</span>
                  </span>
                  <span class="step-text">{{ step.text }}</span>
                </div>
              </div>

              <!-- 结果面板：表格 / 图表 / SQL / 导出 -->
              <ResultPanel
                v-if="msg.result || msg.sqlText"
                :result="msg.result"
                :sql-text="msg.sqlText"
              />

              <!-- 错误 -->
              <div v-if="msg.error" class="error-block">
                <el-alert
                  :title="msg.error"
                  type="error"
                  :closable="false"
                  show-icon
                />
                <div v-if="msg.streamingRetryable" class="retry-row">
                  <el-button type="primary" size="small" @click="retryLast">重试</el-button>
                </div>
              </div>

              <!-- 数据范围拒答：给解释而非结果表 -->
              <div v-if="msg.rejected && !msg.streaming" class="reject-block">
                <el-alert type="info" :closable="false" show-icon title="超出数据范围">
                  <div style="white-space: pre-line">{{ msg.rejectHint }}</div>
                </el-alert>
              </div>

              <!-- 流式占位：无步骤无结果无错误无拒答时显示思考中 -->
              <div v-if="!msg.steps?.length && !msg.result?.length && !msg.error && !msg.rejected && msg.streaming"
                   class="thinking">思考中…</div>
            </div>
          </div>
        </template>

        <div v-if="!convStore.messages.length && !convStore.loadingHistory" class="empty-tip center">
          开始提问吧，例如：统计华东地区的销售总额
        </div>
        <div class="bottom-spacer" />
      </div>

      <!-- 输入区 -->
      <div class="input-area">
        <el-input
          v-model="question"
          type="textarea"
          :rows="2"
          :autosize="{ minRows: 1, maxRows: 4 }"
          :disabled="sending"
          placeholder="请输入问题，Enter 发送，Shift+Enter 换行"
          @keydown="onKeyDown"
        />
        <el-button
          type="primary"
          :loading="sending"
          :disabled="!question.trim() || sending"
          @click="sendQuestion"
        >
          {{ sending ? '执行中' : '发送' }}
        </el-button>
      </div>
    </section>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { getCurrentUser } from '@/api/user'
import { useUserStore } from '@/store/user'
import { useConversationStore } from '@/store/conversation'
import { sseRequest } from '@/api/sse'
import { listMessages } from '@/api/conversation'
import ResultPanel from '@/components/ResultPanel.vue'

const router = useRouter()
const userStore = useUserStore()
const convStore = useConversationStore()

const question = ref('')
const sending = ref(false)
const messagesEl = ref(null)

const editingId = ref(null)
const editTitle = ref('')
const renameInputRef = ref(null)

let sseAbort = null
let lastQuery = ''

const currentTitle = computed(() => {
  if (!convStore.currentConversation) return '新会话'
  return convStore.currentConversation.title || '未命名会话'
})

onMounted(async () => {
  if (!userStore.userInfo && userStore.accessToken) {
    try {
      userStore.setUserInfo(await getCurrentUser())
    } catch {
      /* 拦截器已提示 */
    }
  }
  await convStore.fetchConversations()
  if (convStore.currentId) {
    await convStore.fetchMessages()
  }
})

function handleLogout() {
  userStore.logout()
  convStore.resetState()
  ElMessage.success('已退出登录')
  router.push('/login')
}

async function handleNewConversation() {
  question.value = ''
  convStore.currentId = null
  convStore.messages = []
  scrollToBottom()
}

async function handleSwitch(id) {
  if (sending.value) return
  if (id === convStore.currentId) return
  await convStore.switchConversation(id)
  scrollToBottom()
}

function startRename(conv) {
  editingId.value = conv.id
  editTitle.value = conv.title || ''
  nextTick(() => renameInputRef.value?.focus?.())
}

async function commitRename(id) {
  if (editingId.value !== id) return
  const title = editTitle.value.trim()
  editingId.value = null
  if (!title) return
  try {
    await convStore.renameConversation(id, title)
  } catch {
    /* 拦截器已提示 */
  }
}

async function handleDelete(id) {
  try {
    await convStore.deleteConversation(id)
    ElMessage.success('已删除')
  } catch {
    /* 拦截器已提示 */
  }
}

function onKeyDown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendQuestion()
  }
}

function scrollToBottom() {
  nextTick(() => {
    const el = messagesEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

async function sendQuestion() {
  const q = question.value.trim()
  if (!q || sending.value) return

  lastQuery = q
  question.value = ''
  sending.value = true

  // 若还没有选中的会话，先建一个（后端也会自动建，但提前建便于列表即时显示）
  if (!convStore.currentId) {
    try {
      const conv = await convStore.createConversation(q.slice(0, 20))
      convStore.currentId = conv.id
    } catch {
      sending.value = false
      return
    }
  }

  // 用户气泡 + 助手占位气泡
  const userMsg = { role: 'user', content: q }
  const asstIndex = convStore.appendLocalMessage(userMsg) + 1
  convStore.appendLocalMessage({
    role: 'assistant',
    steps: [],
    result: null,
    content: '',
    sqlText: '',
    error: '',
    rejected: false,
    rejectHint: '',
    streaming: true,
    streamingRetryable: false,
  })
  const lastMessageId = Math.max(0, ...convStore.messages.filter((m) => m.id).map((m) => m.id))
  scrollToBottom()

  sseAbort = sseRequest('/api/query', {
    body: { query: q, conversation_id: convStore.currentId },
    token: userStore.accessToken,
    onEvent: (data) => handleEvent(data, asstIndex),
    onError: (err) => handleStreamError(err, asstIndex),
    onClose: () => handleStreamClose(asstIndex, lastMessageId),
  })
}

function handleEvent(data, asstIndex) {
  if (data?.type === 'meta' && data.conversation_id) {
    // 新会话场景：后端返回的真实 id 同步到 store 与列表
    if (!convStore.currentId) {
      convStore.currentId = data.conversation_id
      // 列表里的临时项（id=null）替换为真实 id
      const temp = convStore.conversations.find((c) => c.id === null)
      if (temp) temp.id = data.conversation_id
    }
  } else if (data?.type === 'progress') {
    const msg = convStore.messages[asstIndex]
    if (!msg) return
    const step = msg.steps.find((s) => s.text === data.step)
    if (step) {
      step.status = data.status
    } else {
      msg.steps.push({ text: data.step, status: data.status })
    }
  } else if (data?.type === 'result') {
    convStore.updateLocalMessage(asstIndex, {
      result: Array.isArray(data.data) ? data.data : null,
    })
  } else if (data?.type === 'rejected') {
    // 数据范围拒答：展示友好提示而非结果表（后端已落库话术，多轮上下文可读）
    convStore.updateLocalMessage(asstIndex, {
      rejected: true,
      rejectHint: data.message || '该问题超出当前数据范围',
      streaming: false,
      streamingRetryable: false,
    })
  } else if (data?.type === 'error') {
    convStore.updateLocalMessage(asstIndex, {
      error: data.message || '发生错误',
      streaming: false,
      streamingRetryable: true,
    })
  }
  scrollToBottom()
}

function handleStreamError(err, asstIndex) {
  convStore.updateLocalMessage(asstIndex, {
    error: err?.message || '连接中断，请重试',
    streaming: false,
    streamingRetryable: true,
  })
  sending.value = false
  scrollToBottom()
}

async function handleStreamClose(asstIndex, lastMessageId) {
  sending.value = false
  // 用后端落库的最终消息补齐 SQL（流里没有 SQL，只有结果集）
  try {
    const msgs = await listMessages(convStore.currentId)
    const assistant = [...msgs].reverse().find(
      (m) => m.role === 'assistant' && m.id > lastMessageId,
    )
    if (assistant) {
      convStore.updateLocalMessage(asstIndex, {
        content: assistant.content || '',
        sqlText: assistant.sql_text || '',
        result: assistant.result_json || convStore.messages[asstIndex]?.result || null,
        streaming: false,
        streamingRetryable: false,
      })
    } else {
      convStore.updateLocalMessage(asstIndex, { streaming: false })
    }
  } catch {
    convStore.updateLocalMessage(asstIndex, { streaming: false })
  }
  scrollToBottom()
}

function retryLast() {
  question.value = lastQuery
  sendQuestion()
}
</script>

<style scoped>
.chat-layout {
  height: 100%;
  display: flex;
  background: #f5f7fa;
}

/* ---------- 左侧栏 ---------- */
.sidebar {
  width: 260px;
  flex: none;
  display: flex;
  flex-direction: column;
  background: #fff;
  border-right: 1px solid #ebeef5;
}

.sidebar-header {
  padding: 14px;
  border-bottom: 1px solid #ebeef5;
}

.new-btn {
  width: 100%;
}

.conv-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.conv-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px;
  margin-bottom: 4px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 14px;
  color: #303133;
  transition: background 0.15s;
}

.conv-item:hover {
  background: #f2f6fc;
}

.conv-item.active {
  background: #ecf5ff;
  color: #409eff;
  font-weight: 500;
}

.conv-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conv-actions {
  display: none;
  gap: 6px;
}

.conv-item:hover .conv-actions {
  display: flex;
}

.icon-btn {
  cursor: pointer;
  font-size: 13px;
  color: #909399;
  padding: 2px 4px;
  border-radius: 4px;
}

.icon-btn:hover {
  background: #e4e7ed;
  color: #409eff;
}

.icon-btn.danger:hover {
  color: #f56c6c;
}

.empty-tip {
  color: #909399;
  font-size: 13px;
  text-align: center;
  padding: 24px 12px;
}

.sidebar-footer {
  padding: 12px 14px;
  border-top: 1px solid #ebeef5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  color: #606266;
}

/* ---------- 右侧主区 ---------- */
.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.main-header {
  height: 52px;
  flex: none;
  display: flex;
  align-items: center;
  padding: 0 20px;
  border-bottom: 1px solid #ebeef5;
  background: #fff;
}

.current-title {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
}

.messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}

.msg-row {
  display: flex;
  margin-bottom: 18px;
  align-items: flex-start;
}

.msg-row.user {
  justify-content: flex-end;
}

.avatar {
  width: 34px;
  height: 34px;
  border-radius: 8px;
  background: #f3f4f6;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  flex: none;
}

.user-avatar {
  margin-left: 10px;
}

.assistant-avatar {
  margin-right: 10px;
}

.bubble {
  max-width: min(820px, 78%);
  padding: 12px 14px;
  border-radius: 12px;
  word-break: break-word;
}

.user-bubble {
  background: #409eff;
  color: #fff;
}

.assistant-bubble {
  background: #fff;
  border: 1px solid #ebeef5;
}

/* 进度时间线 */
.timeline {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 10px;
}

.timeline-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #606266;
}

.status-dot {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: none;
  font-size: 12px;
  color: #fff;
}

.status-dot.running {
  background: #f0f2f5;
}

.status-dot.success {
  background: #67c23a;
}

.status-dot.error {
  background: #f56c6c;
}

.spinner {
  width: 12px;
  height: 12px;
  border: 2px solid #c0c4cc;
  border-top-color: #409eff;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* 错误 */
.error-block {
  margin-top: 8px;
}

/* 数据范围拒答提示：与错误块保持一致的间距观感 */
.reject-block {
  margin-top: 8px;
  max-width: 720px;
}

.retry-row {
  margin-top: 10px;
}

.thinking {
  color: #909399;
  font-size: 13px;
}

.empty-tip.center {
  text-align: center;
  padding-top: 80px;
}

.bottom-spacer {
  height: 120px;
}

/* ---------- 输入区 ---------- */
.input-area {
  flex: none;
  display: flex;
  align-items: flex-end;
  gap: 10px;
  padding: 14px 20px 18px;
  background: #fff;
  border-top: 1px solid #ebeef5;
}

.input-area :deep(.el-textarea) {
  flex: 1;
}
</style>
