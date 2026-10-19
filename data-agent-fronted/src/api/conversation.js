import request from './request'

/**
 * 会话管理接口（后端 Step 7）。
 * request 拦截器已统一加 Bearer token 与 401 刷新，
 * 并把 response.data 解出（后端统一响应 { code, message, data }），
 * 因此这里再取一层 .data 即业务数据。
 */

/** 列出当前用户的会话列表 */
export function listConversations(params = {}) {
  return request
    .get('/conversations', { params: { offset: 0, limit: 100, ...params } })
    .then((r) => r.data || [])
}

/** 新建会话；title 留空则后端在首轮后自动取问题前 20 字 */
export function createConversation(title) {
  return request
    .post('/conversations', { title: title || null })
    .then((r) => r.data)
}

/** 重命名会话 */
export function renameConversation(id, title) {
  return request
    .patch(`/conversations/${id}`, { title })
    .then((r) => r.data)
}

/** 删除会话（级联删除消息） */
export function deleteConversation(id) {
  return request.delete(`/conversations/${id}`).then((r) => r.data)
}

/** 获取某会话的历史消息 */
export function listMessages(conversationId, params = {}) {
  return request
    .get(`/conversations/${conversationId}/messages`, {
      params: { limit: 200, ...params },
    })
    .then((r) => r.data || [])
}
