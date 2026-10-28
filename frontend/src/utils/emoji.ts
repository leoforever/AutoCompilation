/** 彩色 emoji → 自带 SVG 资产（P2 2026-10-28：图标不依赖系统字体，
 *  任何裸 Linux/浏览器像素一致）。素材 twemoji 14.0.2 (CC-BY 4.0) 已入库
 *  public/emoji/，文件名=码位小写连字符，VS16(\ufe0f) 规范化剥离。
 *  文本内残余 emoji（日志/文案）不受影响：装字体照常显示，未装仍是方框。*/
const ALIAS: Record<string, string> = { "270e": "270f", "2713": "2714" }  // ✎→✏️ ✔ 替代字形
export function emojiSrc(ch: string): string | null {
  const cp = [...ch].map((c) => (c === "\ufe0f" ? "" : c.codePointAt(0)!.toString(16))).join("")
  const name = ALIAS[cp] || cp
  return /\.(svg|png)$/.test(name) || /^[0-9a-f-]+$/.test(name) ? `/emoji/${name}.svg` : null
}
