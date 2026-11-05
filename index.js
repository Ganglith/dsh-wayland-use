/**
 * dsh-wayland-use —— 给 Wayland（GNOME/Mutter）用的原生桌面工具
 *
 * 和旧 computer-use 的区别不是「换了个驱动」，而是换了套动作模型：
 *   1. 常驻后端。会话、PipeWire 流、剪贴板所有权只建一次，之后每一步都是
 *      个位数毫秒的 D-Bus 调用，不再每次冷启动。
 *   2. 一次调用干一串活。wayland_act 收一组步骤，本地连着跑完只回一张最终
 *      截图 —— 模型不必为「点一下、等一下、再点一下」付三次往返。
 *   3. 能按文字点。tesseract 把截图变成带坐标的文字，wayland_click_text
 *      直接命中目标，省掉「看一眼猜坐标」的回合。
 *
 * 不 import 任何外部包：profile 上下文里解析不到 @deepseek-ai/dsh-tools，
 * 一旦失败整个插件会静默挂掉，所以工具对象在这里按它的产出形状手搓。
 */

import { spawn } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const DAEMON = path.join(HERE, 'lib', 'daemon.py')

// ── 后端进程 ────────────────────────────────────────────────────────────────

let proc = null
let seq = 0
let bootedAt = 0
const pending = new Map()
let buf = ''
let lastError = ''

const PY =
  process.env.DSH_WAYLAND_PYTHON ||
  ['/usr/bin/python3', 'python3'].find((p) => {
    try {
      if (p.startsWith('/')) return fs.existsSync(p)
      return true
    } catch {
      return false
    }
  }) ||
  'python3'

function env() {
  // 让 GStreamer/tesseract 之类少打点无关噪音
  return { ...process.env, GST_DEBUG: process.env.GST_DEBUG || '1' }
}

function killDaemon() {
  if (!proc) return
  try {
    proc.stdin.end()
  } catch {}
  try {
    proc.kill()
  } catch {}
  proc = null
}

function boot() {
  if (proc && !proc.killed) return proc
  const child = spawn(PY, [DAEMON], {
    env: env(),
    stdio: ['pipe', 'pipe', 'pipe'],
    detached: false,
  })
  proc = child
  bootedAt = Date.now()
  buf = ''

  child.stdout.setEncoding('utf8')
  child.stdout.on('data', (chunk) => {
    buf += chunk
    let nl
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl)
      buf = buf.slice(nl + 1)
      if (!line.trim()) continue
      let msg
      try {
        msg = JSON.parse(line)
      } catch {
        continue
      }
      const slot = pending.get(msg.id)
      if (!slot) continue
      pending.delete(msg.id)
      clearTimeout(slot.timer)
      if (msg.ok) slot.resolve(msg.result)
      else slot.reject(new Error(msg.error || '后端出错'))
    }
  })

  child.stderr.setEncoding('utf8')
  child.stderr.on('data', (d) => {
    lastError = String(d).slice(-2000)
    // GStreamer 的 INFO 级日志没完没了，只留真的错误
    const bad = String(d)
      .split('\n')
      .filter((l) => /ERROR|Traceback|Error|错误/.test(l))
      .slice(-8)
      .join('\n')
    if (bad) trace(bad)
  })

  const die = (why) => {
    if (proc !== child) return
    proc = null
    for (const [id, slot] of pending) {
      pending.delete(id)
      clearTimeout(slot.timer)
      slot.reject(new Error(`后端退出（${why}）：${lastError.slice(-400)}`))
    }
  }
  child.on('exit', (code, sig) => die(sig || `code ${code}`))
  child.on('error', (e) => die(e.message))
  return child
}

/** 一次调用 = 一行 JSON 进来、一行 JSON 出去 */
function rpc(method, params = {}, timeoutMs = 30000) {
  const child = boot()
  const id = ++seq
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id)
      reject(new Error(`${method} 超时（${timeoutMs}ms）`))
    }, timeoutMs)
    pending.set(id, { resolve, reject, timer })
    try {
      child.stdin.write(JSON.stringify({ id, method, params }) + '\n')
    } catch (e) {
      pending.delete(id)
      clearTimeout(timer)
      reject(e)
    }
  })
}

function trace(line) {
  try {
    const dir = path.join(os.tmpdir(), `dsh-wayland-${process.getuid?.() ?? 'u'}`)
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 })
    fs.appendFileSync(path.join(dir, 'daemon.log'),
      `${new Date().toISOString()} ${line}\n`, { mode: 0o600 })
  } catch {}
}

// ── 工具定义（等价于 dsh-tools 的 defineTool，只借它的形状）────────────────

function specToJsonSchema(spec) {
  const properties = {}
  const required = []
  for (const [key, node] of Object.entries(spec)) {
    const { required: isRequired, ...rest } = node
    properties[key] = rest
    if (isRequired) required.push(key)
  }
  return { type: 'object', properties, ...(required.length ? { required } : {}) }
}

function defineTool(options) {
  return {
    name: options.name,
    description: options.description,
    parameters: specToJsonSchema(options.parameters),
    output: { schema: options.output.schema, render: options.output.render },
    ...(options.presentCall ? { presentCall: options.presentCall } : {}),
    execute(args) {
      return options.execute(args || {})
    },
  }
}

const present = (title) => (args) => ({ card: 'generic', title, kind: 'other', rawInput: args })

const textOutput = {
  schema: {
    type: 'object',
    additionalProperties: false,
    properties: { text: { type: 'string' } },
    required: ['text'],
  },
  render: (_a, v) => [{ type: 'text', text: v.text }],
}

const shotOutput = {
  schema: {
    type: 'object',
    additionalProperties: false,
    properties: { text: { type: 'string' }, token: { type: 'string' } },
    required: ['text', 'token'],
  },
  render: (_a, v) => {
    const blocks = [{ type: 'text', text: v.text }]
    const ref = shots.get(v.token)
    if (ref) {
      shots.delete(v.token)
      blocks.push({ type: 'image', attachment: ref })
    }
    return blocks
  },
}

const shots = new Map()

/** 把 JPEG 交给附件库；拿不到就退回文件，别让截图功能整个挂掉 */
async function deliver(ctx, data, name, note) {
  const attachments = ctx.get('attachments')
  if (attachments) {
    try {
      const [ref] = await attachments.saveImages([{
        data,
        mediaType: 'image/jpeg',
        name: name || `screen-${Date.now()}.jpg`,
      }])
      const token = crypto.randomUUID()
      shots.set(token, ref)
      if (shots.size > 20) shots.delete(shots.keys().next().value)
      return { text: note, token }
    } catch {}
  }
  const file = path.join(os.tmpdir(), `dsh-wayland-${Date.now()}.jpg`)
  fs.writeFileSync(file, data, { mode: 0o600 })
  return { text: `${note}\n（附件库不可用，已存到 ${file}，可以用 read_image 看）`, token: '' }
}

const SIZE_NOTE = (r) =>
  `坐标空间 ${r.logical?.[0] ?? '?'}×${r.logical?.[1] ?? '?'}（逻辑像素），` +
  `画面 ${r.physical?.[0] ?? '?'}×${r.physical?.[1] ?? '?'}`

/** 把后端返回的 base64 变成 Buffer，顺手拼一句人话 */
function unpack(r) {
  const buf = Buffer.from(r.data || '', 'base64')
  return { buf, kb: Math.round(buf.length / 1024) }
}

const STEP_DESC =
  '一步操作：{op:"click",x,y,button,count} | {op:"clickText",text,nth,scroll} | {op:"type",text} | ' +
  '{op:"key",combo} | {op:"move",x,y} | {op:"scroll",dx,dy} | {op:"wait",ms} | ' +
  '{op:"waitText",text,gone,timeoutMs} | {op:"launch",command} | {op:"sleep",seconds}'

const STEP_SPEC = {
  type: 'array',
  description: STEP_DESC,
  items: {
    type: 'object',
    additionalProperties: true,
    properties: {
      op: {
        type: 'string',
        enum: ['click', 'clickText', 'type', 'key', 'move', 'scroll', 'wait', 'waitText', 'launch', 'sleep'],
      },
      x: { type: 'number' },
      y: { type: 'number' },
      button: { type: 'string', enum: ['left', 'middle', 'right'] },
      count: { type: 'integer' },
      text: { type: 'string' },
      nth: { type: 'integer' },
      scroll: { type: 'boolean', description: 'clickText 找不到时自动翻页（默认 true）' },
      window: { type: 'string', description: 'clickText 前先把这个标题的窗口拉到前台（子串匹配）' },
      dismissDialog: {
        type: 'boolean',
        description: 'clickText 找不到时顺手处理挡路的屏幕共享授权弹窗（默认 true）',
      },
      acceptShare: {
        type: 'boolean',
        description: '处理授权弹窗时按哪一边：true（默认）按「共享/允许」放行，' +
          'false 按「取消」。远控（HopToDesk/ToDesk）要靠这个框拿画面，所以默认放行',
      },
      combo: { type: 'string' },
      dx: { type: 'number' },
      dy: { type: 'number' },
      ms: { type: 'number' },
      seconds: { type: 'number' },
      gone: { type: 'boolean' },
      timeoutMs: { type: 'number' },
      command: { type: 'string' },
      settleMs: { type: 'number', description: '这一步做完额外等多久（默认 90ms）' },
    },
    required: ['op'],
  },
}

export const name = 'wayland-use'
export const inject = ['tools']

export function apply(ctx) {
  const tools = [
    // ── 看 ──────────────────────────────────────────────────────────────────
    defineTool({
      name: 'wayland_screenshot',
      description:
        '截当前屏幕，图片直接给你看。屏幕静止时取的是缓存帧（约 2ms），刚操作过则是操作后的新画面。' +
        'region 可以只截一块（省 token，也更清楚）。' +
        '纯看文字用 wayland_read 更省，按文字点用 wayland_click_text 更快。',
      parameters: {
        region: {
          type: 'object',
          description: '只截这块：{x,y,w,h}，逻辑坐标',
          properties: {
            x: { type: 'number' }, y: { type: 'number' },
            w: { type: 'number' }, h: { type: 'number' },
          },
          required: ['x', 'y', 'w', 'h'],
        },
        scale: { type: 'number', description: '再缩放多少（0.5 = 一半大）。放大看细节用 2' },
        maxWidth: { type: 'integer', description: '宽度上限，默认 1536' },
        freshMs: { type: 'integer', description: '最多等多久去等一帧更新的画面（默认 150）' },
      },
      output: shotOutput,
      presentCall: present('Wayland screenshot'),
      async execute(args) {
        if (args.region) {
          const r = await rpc('shot_crop', {
            x: args.region.x, y: args.region.y,
            w: args.region.w, h: args.region.h,
            scale: args.scale ?? 1,
            maxWidth: args.maxWidth ?? 1536,
            freshMs: args.freshMs ?? 150,
          })
          const { buf, kb } = unpack(r)
          return deliver(ctx, buf, null,
            `截图（局部 ${args.region.w}×${args.region.h}，${kb}KB，帧龄 ${r.ageMs}ms）— ${SIZE_NOTE(r)}`)
        }
        let r = await rpc('shot', { freshMs: args.freshMs ?? 150 })
        let { buf, kb } = unpack(r)
        if (args.scale && args.scale !== 1) {
          // 缩放交给 ImageMagick：这里不值得为一次缩放拉起 PIL
          const tmp = path.join(os.tmpdir(), `dsh-wl-s-${process.pid}-${Date.now()}.jpg`)
          fs.writeFileSync(tmp, buf)
          const out = tmp.replace('.jpg', `-x${args.scale}.jpg`)
          await new Promise((res) => {
            const c = spawn('magick', [tmp, '-resize', `${Math.round(args.scale * 100)}%`, out])
            c.on('exit', res)
            c.on('error', res)
          })
          try {
            buf = fs.readFileSync(out)
            kb = Math.round(buf.length / 1024)
            fs.unlinkSync(out)
          } catch {}
          try { fs.unlinkSync(tmp) } catch {}
        }
        return deliver(ctx, buf, null, `截图（${kb}KB，帧龄 ${r.ageMs}ms）— ${SIZE_NOTE(r)}`)
      },
    }),

    defineTool({
      name: 'wayland_read',
      description:
        '把屏幕上的文字识别出来，带坐标。想知道界面写了什么、或者要确认某个操作生效了，用它比截图省 token。' +
        '第一次调用会慢一点（tesseract 冷启动约 300ms），之后更快。',
      parameters: {
        region: {
          type: 'object',
          description: '只看这块：{x,y,w,h}，逻辑坐标',
          properties: {
            x: { type: 'number' }, y: { type: 'number' },
            w: { type: 'number' }, h: { type: 'number' },
          },
          required: ['x', 'y', 'w', 'h'],
        },
        lang: { type: 'string', description: 'tesseract 语言包，默认 chi_sim+eng' },
        contains: { type: 'string', description: '只返回含这段文字的行' },
      },
      output: textOutput,
      presentCall: present('Wayland read'),
      async execute(args) {
        const r = await rpc('ocr', {
          region: args.region, lang: args.lang,
        }, 40000)
        let items = r.items || []
        if (args.contains) {
          const n = String(args.contains).toLowerCase()
          items = items.filter((i) => i.text.toLowerCase().includes(n))
        }
        if (!items.length) return { text: '屏幕上没识别到文字（或者都写在图片里了，用 wayland_screenshot 看）' }
        const lines = items
          .slice(0, 80)
          .map((i) => `(${i.cx}, ${i.cy})  ${i.text}`)
        return {
          text: `识别到 ${items.length} 段文字（${r.ms}ms，坐标是逻辑像素，可直接喂给 wayland_act 的 click）：\n` +
            lines.join('\n'),
        }
      },
    }),

    defineTool({
      name: 'wayland_windows',
      description:
        '列出当前窗口：标题和矩形。想直接对某个窗口动手（比如先点它、截它那一块）时用它，比看截图猜快。' +
        '注意 Wayland 下拿不到「窗口在最前面的顺序」，active 表示当前有焦点。',
      parameters: {},
      output: textOutput,
      presentCall: present('Wayland windows'),
      async execute() {
        const r = await rpc('windows', {}, 15000)
        const ws = r.windows || []
        if (!ws.length) return { text: '没读到窗口（目标应用可能没开无障碍）' }
        const lines = ws.map((w) => {
          const t = w.title || '(无标题)'
          return `${w.active ? '*' : ' '} ${t}\n    ${w.width}×${w.height} @ (${w.x}, ${w.y})  [${w.role}]`
        })
        return { text: `${ws.length} 个窗口（* = 有焦点），${r.ms}ms：\n${lines.join('\n')}` }
      },
    }),

    // ── 动 ──────────────────────────────────────────────────────────────────
    defineTool({
      name: 'wayland_act',
      description:
        '一次做一串操作，只回最终截图。这是主力工具：点输入框、打字、回车、再点按钮，一次调用全干完，' +
        '不用一步步来。步骤在本地连续执行，不产生额外往返。\n' +
        '坐标用截图里看到的逻辑像素。shot=false 可以不要截图（纯键盘操作时更快）。\n' +
        '例子：[{op:"click",x:400,y:60},{op:"type",text:"你好"},{op:"key",combo:"Return"}]',
      parameters: {
        steps: { ...STEP_SPEC, required: true },
        window: {
          type: 'string',
          description: '整串动作开始前先把标题含这段字的窗口拉到前台（子串匹配）；clickText 步没单独给 window 时沿用它',
        },
        shot: { type: 'boolean', description: '结束后要不要截图，默认 true' },
        freshMs: { type: 'integer', description: '等新画面最多多久（默认 250）' },
        acceptShare: {
          type: 'boolean',
          description: 'clickText 步遇到屏幕共享授权弹窗时按哪一边，默认 true（共享/放行）',
        },
      },
      output: shotOutput,
      presentCall: present('Wayland act'),
      async execute(args) {
        const steps = args.steps || []
        if (!steps.length) throw new Error('steps 不能为空')
        const r = await rpc('act', {
          steps,
          window: args.window,
          freshMs: args.freshMs ?? 250,
          acceptShare: args.acceptShare ?? true,
        }, 120000)
        const did = (r.steps || []).join(' → ')
        if (args.shot === false) return { text: `已执行：${did}（${r.ms}ms）`, token: '' }
        const { buf, kb } = unpack(r)
        return deliver(ctx, buf, null,
          `已执行：${did}（动作 ${r.ms}ms，${kb}KB，帧龄 ${r.ageMs}ms）`)
      },
    }),

    defineTool({
      name: 'wayland_click_text',
      description:
        '按屏幕上的文字点：识别 → 找到那段字 → 点它中心。不用先截图猜坐标。' +
        '找到多个时用 nth 选第几个（按从上到下、从左到右）。' +
        '当前视野没有就自己翻页继续找；要翻的是某个窗口里的内容时，用 window 给它标题字样。',
      parameters: {
        text: { type: 'string', required: true, description: '要点的文字（模糊包含即可，比如按钮上的字）' },
        nth: { type: 'integer', description: '第几个匹配，默认 0' },
        button: { type: 'string', enum: ['left', 'middle', 'right'], description: '默认 left' },
        count: { type: 'integer', description: '点几次，默认 1（2 = 双击）' },
        region: {
          type: 'object',
          description: '只在屏幕这块里找：{x,y,w,h}',
          properties: {
            x: { type: 'number' }, y: { type: 'number' },
            w: { type: 'number' }, h: { type: 'number' },
          },
          required: ['x', 'y', 'w', 'h'],
        },
        scroll: {
          type: 'boolean',
          description: '当前视野没有就自己滚动翻页继续找，默认 true（在长列表、长文档里找东西时靠它）',
        },
        maxPages: { type: 'integer', description: '最多翻几屏，默认 24' },
        delta: { type: 'number', description: '每屏滚多少，默认按区域高度自适应' },
        window: {
          type: 'string',
          description: '先把标题含这段字的窗口拉到前台再找（子串匹配，比如 "long.txt"）。' +
            '窗口被别的窗口盖住时必须给，否则滚轮翻的是上层窗口、永远找不到',
        },
        dismissDialog: {
          type: 'boolean',
          description: '找不到时顺手处理挡路的「屏幕共享授权」弹窗再找一遍，默认 true。' +
            '默认按「共享/允许」放行 —— 那个框是 HopToDesk/ToDesk 这类远控拿画面用的，' +
            '按掉它等于掐断别人的远程连接，所以只在确实要掐断时才把 acceptShare 设成 false',
        },
        acceptShare: {
          type: 'boolean',
          description: '处理授权弹窗时按哪一边，默认 true（共享/放行）。' +
            '设 false 则按「取消」—— 会掐断正在等授权的远控连接',
        },
      },
      output: textOutput,
      presentCall: present('Wayland click text'),
      async execute(args) {
        const r = await rpc('click_text', {
          text: args.text,
          nth: args.nth ?? 0,
          button: args.button || 'left',
          count: args.count ?? 1,
          region: args.region,
          scroll: args.scroll ?? true,
          maxPages: args.maxPages ?? 24,
          delta: args.delta,
          window: args.window,
          dismissDialog: args.dismissDialog ?? true,
          acceptShare: args.acceptShare ?? true,
        }, 120000)
        const pages = r.pages ? `，翻了 ${r.pages} 屏` : ''
        const fuzzy = r.fuzzy ? '（模糊命中，可能不是完全一样的那一条）' : ''
        return { text: `已点「${r.text}」@ (${r.x}, ${r.y})${pages}${fuzzy}（${r.ms}ms）` }
      },
    }),

    defineTool({
      name: 'wayland_key',
      description:
        '按一个组合键。键名用英文：ctrl / shift / alt / super、a-z、0-9、Return、Escape、Tab、' +
        'BackSpace、Delete、Up/Down/Left/Right、Home/End、PageUp/PageDown、F1-F12、space。' +
        '组合用 + 连：ctrl+shift+t。打中文/长文本用 wayland_act 的 type（走剪贴板，绕过输入法）。',
      parameters: {
        combo: { type: 'string', required: true, description: '比如 ctrl+v、super、Escape' },
      },
      output: textOutput,
      presentCall: present('Wayland key'),
      async execute(args) {
        await rpc('key', { combo: args.combo })
        return { text: `已按 ${args.combo}` }
      },
    }),

    defineTool({
      name: 'wayland_launch',
      description: '启动一个程序（在用户桌面上，不是后台守护）。比如 "nautilus"、"gnome-text-editor"、"firefox"。',
      parameters: {
        command: { type: 'string', required: true, description: '要跑的命令，可以带参数' },
      },
      output: textOutput,
      presentCall: present('Wayland launch'),
      async execute(args) {
        const r = await rpc('launch', { command: args.command }, 15000)
        return { text: `已启动 ${r.argv.join(' ')}（pid ${r.pid}）` }
      },
    }),

    defineTool({
      name: 'wayland_status',
      description: '后端状态自检：会话是否活着、屏幕尺寸、缩放、已收帧数。工具行为诡异时先看它。' +
        'reset=true 会重建会话（快捷键卡住、剪贴板被抢、点了没反应时可试）。',
      parameters: {
        reset: { type: 'boolean', description: '重建会话' },
      },
      output: textOutput,
      presentCall: present('Wayland status'),
      async execute(args) {
        if (args.reset) {
          const r = await rpc('reset', {}, 30000)
          return { text: `会话已重建：坐标空间 ${r.logical?.[0]}×${r.logical?.[1]}` }
        }
        const r = await rpc('status', {}, 15000)
        const agents = (r.remoteAgents || []).filter(
          (a, i, all) => all.findIndex((b) => b.agent === a.agent) === i,
        )
        return {
          text: [
            `会话：${r.alive ? '活着' : '没起来'}`,
            `坐标空间：${r.logical?.join('×') ?? '?'}（逻辑）/ ${r.physical?.join('×') ?? '?'}（物理），缩放 ${Number(r.scale).toFixed(3)}`,
            `PipeWire 节点：${r.node ?? '-'}，已收 ${r.frames} 帧`,
            `JPEG 质量：${r.quality}`,
            `后端进程：${proc ? proc.pid : '未启动'}，运行 ${Math.round((Date.now() - bootedAt) / 1000)}s`,
            agents.length
              ? `本机远控在跑：${agents.map((a) => `${a.agent}(pid ${a.pid})`).join('、')}`
                + ' —— 「接受远程控制/共享屏幕」那个弹窗是它们调 portal 时弹的，不是本插件；'
                + '老是弹就打开它们的「无人值守/Wayland 免提示」设置，或直接关掉它们的传入连接。'
              : '',
            lastError ? `最近的后端错误：\n${lastError.slice(-500)}` : '',
          ].filter(Boolean).join('\n'),
        }
      },
    }),

    defineTool({
      name: 'wayland_dialogs',
      description:
        '查/关挡在最前面的系统授权弹窗（Wayland 屏幕共享、远程控制这类）。' +
        'close=true 会按「取消」把它关掉（不会按「共享」，不会把屏幕交出去）。' +
        '不带参数只查不动。注意：屏幕文字里出现「远程控制」不等于有弹窗，' +
        '必须看到授权框的取消/共享按钮行才算。',
      parameters: {
        close: { type: 'boolean', description: '认出来就直接关掉' },
      },
      output: textOutput,
      presentCall: present('Wayland dialogs'),
      async execute(args) {
        const r = await rpc('dialogs', { close: !!args.close }, 60000)
        const agents = (r.agents || []).map((a) => a.agent).filter((v, i, a) => a.indexOf(v) === i)
        if (!r.found) {
          return {
            text: [
              '屏幕上没有屏幕共享/远程控制授权弹窗。',
              agents.length ? `（本机在跑的远控：${agents.join('、')}）` : '',
              r.weak?.length
                ? `屏幕上只是出现了「${r.weak.join('、')}」这些字，但没看到授权框的按钮行 —— ` +
                  '那多半是聊天正文/网页里的字，不是弹窗。'
                : '',
            ].filter(Boolean).join(''),
          }
        }
        if (r.dismissed) {
          return { text: `已关掉弹窗（点的按钮是「${r.button}」）。触发它的提示：${(r.mark || []).join('、')}` }
        }
        if (!args.close) {
          return {
            text: [
              `屏幕上确实有授权弹窗（${(r.mark || []).join('、')}）`,
              r.button ? `，按钮行是「${r.button}」` : '',
              '。要关掉就把 close 设为 true。',
              agents.length ? `\n本机在跑的远控：${agents.join('、')} —— 这类弹窗基本是它们调 portal 弹的。` : '',
            ].filter(Boolean).join(''),
          }
        }
        return {
          text: [
            `认到弹窗（${(r.mark || []).join('、')}），但没找到「取消/关闭」按钮，没敢乱点。`,
            r.texts?.length ? `屏幕上的文字：\n${r.texts.join('\n')}` : '',
          ].filter(Boolean).join('\n'),
        }
      },
    }),
  ]

  for (const tool of tools) {
    try {
      ctx.tools.register(tool)
    } catch (e) {
      trace(`注册 ${tool.name} 失败: ${e && e.message}`)
      throw e
    }
  }
  trace(`已注册 ${tools.length} 个工具: ${tools.map((t) => t.name).join(', ')}`)

  // 插件卸载时收掉后端，别留孤儿子进程
  const stop = () => killDaemon()
  if (typeof ctx.on === 'function') {
    try {
      ctx.on('dispose', stop)
    } catch {}
  }
  process.on('exit', stop)
}

trace('模块已加载')
