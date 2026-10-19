/**
 * 基于 fetch + ReadableStream 的 SSE 客户端。
 *
 * 为什么不用 EventSource：POST /api/query 需要带 JSON body 与 Bearer 鉴权头，
 * EventSource 仅支持 GET 且无法自定义 header，故用 fetch 直接消费流式响应体。
 *
 * 解析要点：
 *  - 标准 SSE 帧以空行（\n\n）为事件边界；
 *  - fetch 分片到达时，一个事件可能被切成两块、或一个 chunk 含多个事件，
 *    因此必须维护 buffer，按 \n\n 切分后保留尾巴；
 *  - 每行以 "data: " 开头，去掉前缀后 JSON.parse；
 *  - 网络/HTTP 非 2xx / 流中断均走 onError，由调用方决定重试。
 */

/**
 * 发起 SSE 请求。
 *
 * @param {string} url  请求地址
 * @param {object} opts
 * @param {object} opts.body       POST body（会被 JSON.stringify）
 * @param {string} opts.token      access_token（会拼成 Bearer）
 * @param {(data: object) => void} opts.onEvent  每收到一条完整 data 事件回调
 * @param {(err: Error) => void}   opts.onError  错误回调
 * @param {() => void}             opts.onClose  流正常结束回调
 * @returns {{ abort: () => void }} 可用于主动取消请求
 */
export function sseRequest(url, { body, token, onEvent, onError, onClose }) {
  const controller = new AbortController()

  ;(async () => {
    let response
    try {
      response = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify(body || {}),
        signal: controller.signal,
      })
    } catch (e) {
      // fetch 层失败（网络断开、被 abort 等）
      if (e?.name === 'AbortError') return
      onError?.(e)
      return
    }

    if (!response.ok) {
      let detail = `HTTP ${response.status}`
      try {
        const text = await response.text()
        const json = JSON.parse(text)
        detail = json?.detail || json?.message || detail
      } catch {
        /* 非 JSON 错误体，忽略 */
      }
      onError?.(new Error(detail))
      return
    }

    if (!response.body) {
      onError?.(new Error('服务器未返回流'))
      return
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''

    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break

        // 累计并按事件边界切分；最后一段留在 buffer 等下一个 chunk 补齐
        buffer += decoder.decode(value, { stream: true })
        let boundary
        while ((boundary = buffer.indexOf('\n\n')) !== -1) {
          const rawEvent = buffer.slice(0, boundary)
          buffer = buffer.slice(boundary + 2)
          const dataLine = rawEvent
            .split('\n')
            .map((l) => l.trim())
            .find((l) => l.startsWith('data:'))
          if (!dataLine) continue

          const jsonText = dataLine.replace(/^data:\s*/, '')
          if (!jsonText) continue

          let data
          try {
            data = JSON.parse(jsonText)
          } catch {
            // 单条事件解析失败不影响后续事件
            continue
          }
          onEvent?.(data)
        }
      }
      onClose?.()
    } catch (e) {
      if (e?.name === 'AbortError') return
      onError?.(e)
    }
  })()

  return { abort: () => controller.abort() }
}
