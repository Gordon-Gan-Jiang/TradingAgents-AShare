import type { LucideIcon } from 'lucide-react'
import {
    Activity,
    Briefcase,
    FileText,
    LayoutDashboard,
    MessageSquare,
    BarChart3,
    Sparkles,
    Settings,
    Wallet,
    CandlestickChart,
    TrendingUp,
    Cpu,
    Compass,
} from 'lucide-react'

export interface SidebarNavItem {
    path: string
    icon: LucideIcon
    label: string
}

/** 分组：折叠后只显示组标题，用于收敛左侧菜单的信息密度。 */
export interface SidebarNavGroup {
    id: string
    label: string
    items: SidebarNavItem[]
}

/** 置顶项：不参与折叠，始终单独展示。 */
export const pinnedNavItems: SidebarNavItem[] = [
    { path: '/', icon: LayoutDashboard, label: '控制台' },
]

export const navGroups: SidebarNavGroup[] = [
    {
        id: 'research',
        label: '研究分析',
        items: [
            { path: '/analysis', icon: Activity, label: '智能分析' },
            { path: '/mainline', icon: Compass, label: '市场主线' },
            { path: '/recommendations', icon: Sparkles, label: '股票推荐' },
        ],
    },
    {
        id: 'positions',
        label: '持仓跟踪',
        items: [
            { path: '/portfolio', icon: Briefcase, label: '自选 & 定时' },
            { path: '/tracking-board', icon: Wallet, label: '跟踪看板' },
            { path: '/paper-trading', icon: CandlestickChart, label: '每日操盘' },
        ],
    },
    {
        id: 'review',
        label: '复盘统计',
        items: [
            { path: '/reports', icon: FileText, label: '历史报告' },
            { path: '/recommendation-insights', icon: BarChart3, label: '选股统计' },
            { path: '/quality-insights', icon: TrendingUp, label: 'T+1 质量' },
        ],
    },
    {
        id: 'system',
        label: '系统',
        items: [
            { path: '/model-profiles', icon: Cpu, label: '模型管理' },
            { path: '/feedback', icon: MessageSquare, label: '反馈留言' },
            { path: '/settings', icon: Settings, label: '设置' },
        ],
    },
]

/** 扁平列表：保持既有契约（控制台始终为第一项），供既有测试与顺序检索使用。 */
export const navItems: SidebarNavItem[] = [
    ...pinnedNavItems,
    ...navGroups.flatMap(group => group.items),
]

/** 当前路径所属的分组 id；不属于任何分组时返回 null（例如置顶项）。 */
export function groupIdForPath(pathname: string): string | null {
    const group = navGroups.find(g => g.items.some(item => item.path === pathname))
    return group ? group.id : null
}
