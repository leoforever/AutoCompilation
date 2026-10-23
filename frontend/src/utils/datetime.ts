/**
 * 时间显示统一工具（10-23 时区修复）
 * 后端全链存 naive UTC（datetime.utcnow().isoformat()，无时区后缀）——
 * JS new Date("2026-10-23T02:37:28") 会按本地(+08)解析，显示早 8 小时。
 * 约定：无时区标记的串一律补 Z 按 UTC 解析，再由 toLocaleString('zh-CN') 转北京时间。
 */
export function parseServerDate(s?: string | null): Date | null {
  if (!s) return null
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(s)
  const d = new Date(hasTz ? s : s + 'Z')
  return isNaN(d.getTime()) ? null : d
}

export function fmtDateTime(s?: string | null, opts?: Intl.DateTimeFormatOptions): string {
  const d = parseServerDate(s)
  if (!d) return typeof s === 'string' && s ? s : ''
  return d.toLocaleString('zh-CN', opts)
}
