import { readFileSync } from 'node:fs'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import DataSources from '@/pages/DataSources'

/**
 * 路由接线检查。
 *
 * 本仓库有过「实现了却没有真实入口」的先例（见 sidebarNav.test.ts 的注释），
 * 所以除了展示逻辑的单测之外，这里直接断言路由确实注册在鉴权区块内。
 * vitest 以 `frontend/` 为工作目录运行，因此可以用相对路径读取源码。
 */
describe('数据源探测路由接线', () => {
    const appSource = readFileSync('src/App.tsx', 'utf8')

    it('导入了 DataSources 页面', () => {
        expect(appSource).toContain("import DataSources from './pages/DataSources'")
    })

    it('在 <RequireAuth> 区块内注册了 /data-sources', () => {
        const routeIndex = appSource.indexOf('path="/data-sources"')
        expect(routeIndex).toBeGreaterThan(-1)
        expect(appSource).toContain('element={<DataSources />}')
        // 必须在 <RequireAuth> 之后，保证和其他页面一样需要登录
        expect(routeIndex).toBeGreaterThan(appSource.indexOf('<RequireAuth>'))
        // 且不能跑到 /login 那一条（未鉴权）路由前面去
        expect(routeIndex).toBeGreaterThan(appSource.indexOf('path="/login"'))
    })
})

/**
 * 渲染冒烟测试：用 react-dom/server 静态渲染一次。
 * SSR 不执行 useEffect，因此这里既不会发请求，也不需要 jsdom
 * （本仓库没有安装 jsdom/happy-dom，且不允许新增依赖）。
 */
describe('DataSources 页面渲染', () => {
    it('首屏渲染标题与加载占位且不抛错', () => {
        const html = renderToStaticMarkup(createElement(DataSources))
        expect(html).toContain('数据源探测报告')
        expect(html).toContain('立即探测')
        expect(html).toContain('正在加载探测状态')
        // 能力矩阵默认展开，标题里带上维度计数
        expect(html).toContain('能力矩阵')
    })
})

/**
 * 接线检查：SSR 首屏只有加载态，渲染不出摘要卡片与明细表，
 * 因此这两条新契约（超时状态、row_unit）只能从页面源码上确认确实被接上了，
 * 避免「display 层改了但页面忘了透传」这种静默失效。
 */
describe('数据源探测页面：超时与行数单位的接线', () => {
    const pageSource = readFileSync('src/pages/DataSources.tsx', 'utf8')

    it('把 row_unit 透传给 formatRowCount，而不是只取 row_count', () => {
        expect(pageSource).toContain('formatRowCount(row.row_count, row.row_unit)')
        expect(pageSource).not.toContain('formatRowCount(row.row_count)')
        // lines 单位的含义靠悬浮提示解释，避免「≈12 行」被读成 12 条记录
        expect(pageSource).toContain('isApproxRowUnit(row.row_unit)')
    })

    it('摘要卡片仍由 PROBE_SUMMARY_CARDS 驱动且栅格能放下 6 张卡片', () => {
        expect(pageSource).toContain('PROBE_SUMMARY_CARDS.map')
        expect(pageSource).toContain('lg:grid-cols-6')
    })

    it('页面上出现「超时」的说明与配色，且不再把状态列表写死成四类', () => {
        expect(pageSource).toContain('超时')
        expect(pageSource).toContain('text-violet-600 dark:text-violet-400')
    })
})
