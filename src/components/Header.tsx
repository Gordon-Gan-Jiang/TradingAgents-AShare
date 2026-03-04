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
        const systemDark = window.matchMedia('(prefers-color-scheme: dark)').matches

        const shouldBeDark = mode === 'system' ? systemDark : mode === 'dark'

        if (shouldBeDark) {
            root.classList.add('dark')
        } else {
            root.classList.remove('dark')
        }
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
        <header className="h-16 bg-white dark:bg-slate-900 border-b border-slate-200 dark:border-slate-700 flex items-center justify-end px-6 sticky top-0 z-40">
            <div className="flex items-center gap-4">
                <button
                    onClick={cycleTheme}
                    className="inline-flex items-center gap-2 px-3 py-2 rounded-lg border border-slate-200 dark:border-slate-600 text-slate-600 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-200 hover:bg-slate-100 dark:hover:bg-slate-800 transition-all"
                    title={`主题：${themeLabel}（点击切换）`}
                >
                    <ThemeIcon className="w-4 h-4" />
                    <span className="text-sm">{themeLabel}</span>
                </button>

                <button className="relative p-2 text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-200 transition-colors rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800">
                    <Bell className="w-5 h-5" />
                    <span className="absolute top-1.5 right-1.5 w-2 h-2 bg-red-500 rounded-full" />
                </button>
            </div>
        </header>
    )
}
