import { useState } from 'react'
import { NavLink } from 'react-router-dom'
import { ChevronDown, TrendingUp } from 'lucide-react'

import { navGroups, pinnedNavItems, type SidebarNavItem } from '@/components/sidebarNav'

const buildDate = __APP_BUILD_DATE__
const buildCommit = __APP_BUILD_COMMIT__
const buildVersion = __APP_BUILD_VERSION__

export default function Sidebar() {
    const [isExpanded, setIsExpanded] = useState(false)
    // 只记录用户主动收起的分组；默认全部展开，避免首次进入就藏起功能入口。
    const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({})

    const renderItem = (item: SidebarNavItem) => (
        <NavLink
            key={item.path}
            to={item.path}
            title={item.label}
            className={({ isActive }) =>
                `flex items-center gap-3 px-3 py-3 rounded-xl transition-all duration-200 ${isActive
                    ? 'bg-gradient-to-r from-cyan-500/15 via-blue-500/20 to-violet-500/20 text-blue-300 border border-blue-400/30'
                    : 'text-slate-400 hover:bg-slate-800/50 hover:text-slate-200'
                }`
            }
        >
            <item.icon className="w-5 h-5 flex-shrink-0" />
            {isExpanded && (
                <span className="font-medium text-sm whitespace-nowrap">{item.label}</span>
            )}
        </NavLink>
    )

    return (
        <aside
            className={`on-dark-surface fixed left-0 top-0 h-full bg-slate-900/95 backdrop-blur-md border-r border-slate-700 flex flex-col z-50 transition-all duration-300 ${isExpanded ? 'w-48' : 'w-16'
                }`}
            onMouseEnter={() => setIsExpanded(true)}
            onMouseLeave={() => setIsExpanded(false)}
        >
            {/* Logo */}
            <div className="h-16 flex items-center justify-center border-b border-slate-700 px-2">
                <div className="flex items-center gap-3">
                    <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-cyan-500 via-blue-600 to-violet-600 flex items-center justify-center shadow-lg shadow-blue-600/30 flex-shrink-0">
                        <TrendingUp className="w-5 h-5 text-white" />
                    </div>
                    {isExpanded && (
                        <span className="font-bold text-base bg-gradient-to-r from-cyan-400 via-blue-400 to-violet-400 bg-clip-text text-transparent whitespace-nowrap">
                            AlphaPilot
                        </span>
                    )}
                </div>
            </div>

            {/* Navigation */}
            {/* 悬停展开时按分组呈现，并允许逐组收起；收起为图标栏时始终平铺全部图标，
                保证任何入口都不会因为分组被折叠而在图标栏里消失。 */}
            <nav className="flex-1 overflow-y-auto overflow-x-hidden py-4 px-2">
                <div className="space-y-2">{pinnedNavItems.map(renderItem)}</div>

                {navGroups.map(group => {
                    const collapsed = isExpanded && Boolean(collapsedGroups[group.id])
                    return (
                        <div key={group.id} className="pt-2">
                            {isExpanded ? (
                                <button
                                    type="button"
                                    onClick={() =>
                                        setCollapsedGroups(prev => ({ ...prev, [group.id]: !prev[group.id] }))
                                    }
                                    aria-expanded={!collapsed}
                                    className="w-full flex items-center justify-between gap-2 px-3 py-1.5 rounded-lg text-[11px] font-semibold tracking-wider text-slate-500 hover:text-slate-300 hover:bg-slate-800/40 transition-colors"
                                >
                                    <span className="whitespace-nowrap">{group.label}</span>
                                    <ChevronDown
                                        className={`w-3.5 h-3.5 flex-shrink-0 transition-transform duration-200 ${collapsed ? '-rotate-90' : ''
                                            }`}
                                    />
                                </button>
                            ) : (
                                <div className="mx-3 mb-2 border-t border-slate-700/70" />
                            )}
                            {!collapsed && (
                                <div className={`${isExpanded ? 'mt-1' : ''} space-y-2`}>
                                    {group.items.map(renderItem)}
                                </div>
                            )}
                        </div>
                    )
                })}
            </nav>

            {/* Footer */}
            <div className="p-3 border-t border-slate-700">
                {isExpanded ? (
                    <div className="text-xs text-slate-500 text-center">
                        <p className="text-slate-400 text-sm font-medium">AlphaPilot A-Share</p>
                        <p className="mt-0.5">多智能体投研系统</p>
                        <p className="mt-1 font-mono text-[11px] text-slate-400">{buildVersion}</p>
                        <p className="mt-0.5 text-[10px] text-slate-500">{buildDate} · {buildCommit}</p>
                    </div>
                ) : (
                    <div className="text-[10px] text-slate-500 text-center font-mono">{buildCommit}</div>
                )}
            </div>
        </aside>
    )
}
