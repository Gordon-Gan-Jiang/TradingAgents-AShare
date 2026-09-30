import { describe, expect, it } from 'vitest'

import { navGroups, navItems } from '@/components/sidebarNav'

describe('sidebarNav', () => {
    it('includes a dedicated tracking board entry in the sidebar', () => {
        const dashboardIndex = navItems.findIndex(item => item.path === '/')
        const trackingBoardIndex = navItems.findIndex(item => item.path === '/tracking-board')

        expect(trackingBoardIndex).toBeGreaterThan(0)
        expect(dashboardIndex).toBe(0)
        expect(navItems[trackingBoardIndex]).toMatchObject({
            path: '/tracking-board',
            label: '跟踪看板',
        })
        expect(trackingBoardIndex).toBeGreaterThan(dashboardIndex)
    })
})

describe('judgement nav entry', () => {
    it('exposes the conversational judgement entry under research', () => {
        // 新能力必须有**真实用户入口**：本仓库有过「实现了却没人用」的先例
        // （recommendation_eval_items 0 行、/v1/backtest 无前端调用者）。
        const item = navItems.find(i => i.path === '/judgement')
        expect(item).toBeDefined()
        expect(item?.label).toContain('研判')

        const research = navGroups.find(g => g.id === 'research')
        expect(research?.items.some(i => i.path === '/judgement')).toBe(true)
    })
})

describe('data sources nav entry', () => {
    it('exposes 数据源探测 under the system group', () => {
        const item = navItems.find(i => i.path === '/data-sources')
        expect(item).toBeDefined()
        expect(item?.label).toBe('数据源探测')

        const system = navGroups.find(g => g.id === 'system')
        expect(system?.items.some(i => i.path === '/data-sources')).toBe(true)
    })
})
