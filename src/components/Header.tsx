import { Bell, User } from 'lucide-react'

export default function Header() {
    return (
        <header className="h-16 bg-trading-bg-secondary border-b border-trading-border flex items-center justify-between px-6">
            <div>
                <h2 className="text-sm font-semibold text-trading-text-primary">A-Share Agent Terminal</h2>
                <p className="text-xs text-trading-text-muted">多智能体研究与交易分析</p>
            </div>

            {/* Right Actions */}
            <div className="flex items-center gap-4">
                <button className="relative p-2 text-trading-text-secondary hover:text-trading-text-primary transition-colors">
                    <Bell className="w-5 h-5" />
                    <span className="absolute top-1 right-1 w-2 h-2 bg-trading-accent-red rounded-full" />
                </button>

                <div className="flex items-center gap-3 pl-4 border-l border-trading-border">
                    <div className="w-8 h-8 rounded-full bg-trading-bg-tertiary flex items-center justify-center">
                        <User className="w-4 h-4 text-trading-text-secondary" />
                    </div>
                    <span className="text-sm font-medium text-trading-text-primary hidden sm:block">
                        Trader
                    </span>
                </div>
            </div>
        </header>
    )
}
