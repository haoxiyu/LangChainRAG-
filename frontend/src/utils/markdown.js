import MarkdownIt from 'markdown-it'

// html: false —— 知识库正文与模型输出都当作纯文本,禁止内联 HTML,避免 XSS
const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
})

const CITATION_RE = /\[(\d{1,2})\]/g

/** 用户气泡:纯文本 + 保留换行,不走 Markdown。 */
export function renderPlain(text) {
  const div = document.createElement('div')
  div.textContent = text || ''
  return div.innerHTML.replace(/\n/g, '<br>')
}

/**
 * 助手气泡:先渲染 Markdown,再把正文里的 `[n]` 变成可点击的引用角标。
 *
 * 做法是遍历文本节点而不是对 HTML 字符串做正则 —— 后者会误伤
 * `<a href="...1...">` 之类的标签属性,也会破坏代码块里的内容。
 *
 * @param {string} content  Markdown 原文
 * @param {Set<number>} validIndexes  实际存在的引用序号;
 *        模型偶尔会写出不存在的编号,这类角标置灰且不可点击。
 */
export function renderAnswer(content, validIndexes = null) {
  const html = md.render(content || '')
  if (!html) return ''

  const root = document.createElement('div')
  root.innerHTML = html

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const targets = []
  let node
  while ((node = walker.nextNode())) {
    const text = node.nodeValue
    if (!text || !/\[\d{1,2}\]/.test(text)) continue
    // 代码块/行内代码/链接里的 [n] 不当引用处理
    if (node.parentElement?.closest('code, pre, a')) continue
    targets.push(node)
  }

  for (const textNode of targets) {
    const text = textNode.nodeValue
    const frag = document.createDocumentFragment()
    let last = 0
    let m
    CITATION_RE.lastIndex = 0
    while ((m = CITATION_RE.exec(text)) !== null) {
      const idx = Number(m[1])
      if (m.index > last) {
        frag.appendChild(document.createTextNode(text.slice(last, m.index)))
      }
      const span = document.createElement('span')
      span.className = 'citation-ref'
      if (validIndexes && !validIndexes.has(idx)) {
        span.classList.add('invalid')
        span.title = '该编号没有对应的引用片段'
      } else {
        span.dataset.citation = String(idx)
      }
      span.textContent = String(idx)
      frag.appendChild(span)
      last = m.index + m[0].length
    }
    if (last < text.length) {
      frag.appendChild(document.createTextNode(text.slice(last)))
    }
    textNode.parentNode.replaceChild(frag, textNode)
  }

  return root.innerHTML
}
