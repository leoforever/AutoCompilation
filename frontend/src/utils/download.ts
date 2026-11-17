// 产物下载单源（11-16）：fetch 流式收包 + 进度回调 + 后端展示名
// Tasks/Workbench 两处共用——此前各页自拼名/自收 blob 是同款双源病
export async function fetchArtifactDownload(
  url: string,
  fallbackName: string,
  onProgress?: (pct: number, recvBytes: number) => void,
): Promise<{ blob: Blob; name: string }> {
  const token = localStorage.getItem('token') || ''
  const r = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
  if (!r.ok) {
    // 后端人话透传（410 历史清理/404 无产物），别只剩 "HTTP 410" 天书
    let msg = `HTTP ${r.status}`
    try { msg = (await r.json()).detail || msg } catch { /* 非 JSON 错体保留状态码 */ }
    throw new Error(msg)
  }
  const cd = r.headers.get('content-disposition') || ''
  const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i)
  const name = m ? decodeURIComponent(m[1]) : fallbackName
  const total = Number(r.headers.get('content-length') || 0)
  if (!r.body || !Number.isFinite(total) || total <= 0) {
    const b = await r.blob()
    onProgress?.(100, b.size)
    return { blob: b, name }
  }
  const reader = r.body.getReader()
  const chunks: Uint8Array[] = []
  let recv = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    if (value) { chunks.push(value); recv += value.length; onProgress?.(Math.min(99, Math.round((recv / total) * 100)), recv) }
  }
  onProgress?.(100, recv)
  return { blob: new Blob(chunks as BlobPart[]), name }
}
