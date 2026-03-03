import { NavLink } from 'react-router-dom'
import {
    LayoutDashboard,
    Activity,
    FileText,
    Settings,
    TrendingUp
} from 'lucide-react'

const navItems = [
    { path: '/', icon: LayoutDashboard, label: '控制台' },
    { path: '/analysis', icon: Activity, label: '分析' },
    { path: '/reports', icon: FileText, label: '报告' },
    { path: '/settings', icon: Settings, label: '设置' },
]

export default function Sidebar() {
    return (
        <aside className="w-16 lg:w-64 bg-trading-bg-secondary border-r border-trading-border flex flex-col">
            {/* Logo */}
            <div className="h-16 flex items-center justify-center lg:justify-start lg:px-6 border-b border-trading-border">
                <div className="flex items-center gap-3">
                    <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-trading-accent-blue to-trading-accent-cyan flex items-center justify-center">
                        <TrendingUp className="w-5 h-5 text-white" />
                    </div>
                    <span className="hidden lg:block font-bold text-lg text-gradient">
                        TradingAgents
                    </span>
                </div>
            </div>

            {/* Navigation */}
            <nav className="flex-1 py-4 px-2 lg:px-3 space-y-1">
                {navItems.map((item) => (
                    <NavLink
                        key={item.path}
                        to={item.path}
                        className={({ isActive }) =>
                            `flex items-center gap-3 px-3 py-2.5 rounded-lg transition-all duration-200 group ${isActive
                                ? 'bg-trading-accent-blue/10 text-trading-accent-blue border border-trading-accent-blue/30'
                                : 'text-trading-text-secondary hover:bg-trading-bg-tertiary hover:text-trading-text-primary'
                            }`
                        }
                    >
                        <item.icon className="w-5 h-5 flex-shrink-0" />
                        <span className="hidden lg:block font-medium">{item.label}</span>
                    </NavLink>
                ))}
            </nav>

            {/* Footer */}
            <div className="p-4 border-t border-trading-border">
                <div className="hidden lg:block text-xs text-trading-text-muted text-center">
                    <p>TradingAgents Dashboard</p>
                    <p className="mt-1">v0.1.0</p>
                </div>
            </div>
        </aside>
    )
}