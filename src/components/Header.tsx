import { useEffect, useState } from 'react'
import { Bell, Monitor, Moon, Sun } from 'lucide-react'

type ThemeMode = 'system' | 'light' | 'dark'

export default function Header() {
    const [themeMode, setThemeMode] = useState<ThemeMode>('system')

    useEffect(() => {
        const saved = (localStorage.getItem('ta-theme') || 'system') as ThemeMode
        const mode: ThemeMode = ['system', 'light', 'dark'].includes(saved) ? saved : 'system'
        setThemeMode(mode)
        applyTheme(mode)
    }, [])

    const applyTheme = (mode: ThemeMode) => {
        const root = document.documentElement
        root.classList.remove('theme-light', 'theme-dark')
        if (mode === 'light') root.classList.add('theme-light')
        if (mode === 'dark') root.classList.add('theme-dark')
    }

    const cycleTheme = () => {
        const next: ThemeMode =
            themeMode === 'system' ? 'light' : themeMode === 'light' ? 'dark' : 'system'
        setThemeMode(next)
        localStorage.setItem('ta-theme', next)
        applyTheme(next)
    }

    const themeLabel = themeMode === 'system' ? '系统' : themeMode === 'light' ? '浅色' : '深色'
    const ThemeIcon = themeMode === 'system' ? Monitor : themeMode === 'light' ? Sun : Moon

    return (
        <header className="h-16 bg-trading-bg-secondary border-b border-trading-border flex items-center justify-end px-6">
            <div className="flex items-center gap-4">
                <button
                    onClick={cycleTheme}
                    className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-md text-sm border border-trading-border text-trading-text-secondary hover:text-trading-text-primary hover:bg-trading-bg-tertiary transition-colors"
                    title={`主题：${themeLabel}（点击切换）`}
                >
                    <ThemeIcon className="w-4 h-4" />
                    <span>{themeLabel}</span>
                </button>
                <button className="relative p-2 text-trading-text-secondary hover:text-trading-text-primary transition-colors">
                    <Bell className="w-5 h-5" />
                    <span className="absolute top-1 right-1 w-2 h-2 bg-trading-accent-red rounded-full" />
                </button>
            </div>
        </header>
    )
}
