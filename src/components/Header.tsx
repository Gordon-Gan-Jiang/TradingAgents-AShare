import { useEffect, useMemo, useRef, useState } from 'react'
import { Bell, BellOff, ChevronDown, LogOut, Monitor, Moon, Settings, Sun } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useAuthStore } from '@/stores/authStore'

type ThemeMode = 'system' | 'light' | 'dark'

function getInitials(email?: string | null): string {
    if (!email) return 'TA'
    return email.slice(0, 2).toUpperCase()
}

export default function Header() {
    const navigate = useNavigate()
    const { user, logout } = useAuthStore()
    const [themeMode, setThemeMode] = useState<ThemeMode>('system')
    const [notifPermission, setNotifPermission] = useState<NotificationPermission>('default')
    const [menuOpen, setMenuOpen] = useState(false)
    const menuRef = useRef<HTMLDivElement | null>(null)

    useEffect(() => {
        const saved = (localStorage.getItem('ta-theme') || 'system') as ThemeMode
        const mode: ThemeMode = ['system', 'light', 'dark'].includes(saved) ? saved : 'system'
        setThemeMode(mode)
        applyTheme(mode)
        if ('Notification' in window) setNotifPermission(Notification.permission)
    }, [])

    useEffect(() => {
        const onClick = (event: MouseEvent) => {
            if (!menuRef.current) return
            if (!menuRef.current.contains(event.target as Node)) {
                setMenuOpen(false)
            }
        }
        document.addEventListener('mousedown', onClick)
        return () => document.removeEventListener('mousedown', onClick)
    }, [])

    const applyTheme = (mode: ThemeMode) => {
        const root = document.documentElement
        const systemDark = window.matchMedia('(prefers-color-scheme: dark)').matches
        const shouldBeDark = mode === 'system' ? systemDark : mode === 'dark'
        root.classList.toggle('dark', shouldBeDark)
    }

    const cycleTheme = () => {
        const next: ThemeMode =
            themeMode === 'system' ? 'light' : themeMode === 'light' ? 'dark' : 'system'
        setThemeMode(next)
        localStorage.setItem('ta-theme', next)
        applyTheme(next)
    }

    const toggleNotifications = async () => {
        if (!('Notification' in window)) return
        if (Notification.permission === 'denied') {
            alert('通知权限已被浏览器拒绝，请在浏览器设置中手动开启')
            return
        }
        const perm = await Notification.requestPermission()
        setNotifPermission(perm)
    }

    const themeLabel = themeMode === 'system' ? '跟随系统' : themeMode === 'light' ? '浅色' : '深色'
    const ThemeIcon = themeMode === 'system' ? Monitor : themeMode === 'light' ? Sun : Moon
    const accountTone = useMemo(() => getInitials(user?.email), [user?.email])

    return (
        <header className="h-16 sticky top-0 z-40 border-b border-slate-200/80 dark:border-slate-800 bg-white/88 dark:bg-slate-950/78 backdrop-blur-xl">
            <div className="h-full px-6 flex items-center justify-between">
                <div className="flex items-center gap-3">
                    <div className="hidden md:flex items-center gap-4">
                        <div className="flex items-center gap-2.5">
                            <div className="w-2.5 h-2.5 rounded-full bg-emerald-500 shadow-[0_0_16px_rgba(16,185,129,0.4)]" />
                            <div className="text-sm font-semibold tracking-[0.04em] text-slate-900 dark:text-slate-100">A 股投研终端</div>
                        </div>
                        <div className="h-4 w-px bg-slate-200 dark:bg-slate-800" />
                        <div className="text-xs tracking-[0.18em] text-slate-400 dark:text-slate-500">工作台在线</div>
                    </div>
                </div>

                <div className="flex items-center gap-2">
                    <button
                        onClick={cycleTheme}
                        className="inline-flex items-center justify-center w-10 h-10 rounded-2xl border border-slate-200 dark:border-slate-800 text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-100 hover:bg-slate-100 dark:hover:bg-slate-900 transition-all"
                        title={`主题：${themeLabel}`}
                    >
                        <ThemeIcon className="w-4 h-4" />
                    </button>

                    <button
                        onClick={toggleNotifications}
                        title={notifPermission === 'granted' ? '通知已启用' : notifPermission === 'denied' ? '通知被拒绝' : '点击启用通知'}
                        className="relative inline-flex items-center justify-center w-10 h-10 rounded-2xl border border-slate-200 dark:border-slate-800 text-slate-500 dark:text-slate-400 hover:text-slate-900 dark:hover:text-slate-100 hover:bg-slate-100 dark:hover:bg-slate-900 transition-all"
                    >
                        {notifPermission === 'denied' ? <BellOff className="w-4 h-4" /> : <Bell className="w-4 h-4" />}
                        <span className={`absolute top-2.5 right-2.5 w-2 h-2 rounded-full ${
                            notifPermission === 'granted' ? 'bg-emerald-500' : notifPermission === 'denied' ? 'bg-rose-500' : 'bg-slate-400'
                        }`} />
                    </button>

                    {user && (
                        <div className="relative" ref={menuRef}>
                            <button
                                onClick={() => setMenuOpen(v => !v)}
                                className="group flex items-center gap-3 rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 px-2.5 py-2 hover:border-slate-300 dark:hover:border-slate-700 hover:bg-slate-50 dark:hover:bg-slate-900/90 transition-all"
                            >
                                <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-cyan-500 via-blue-500 to-indigo-600 text-white flex items-center justify-center text-xs font-bold shadow-[0_12px_24px_rgba(37,99,235,0.22)]">
                                    {accountTone}
                                </div>
                                <div className="hidden md:block text-left">
                                    <div className="text-[11px] tracking-[0.22em] text-slate-400 dark:text-slate-500">当前账户</div>
                                    <div className="max-w-[220px] truncate text-sm font-medium text-slate-900 dark:text-slate-100">{user.email}</div>
                                </div>
                                <ChevronDown className={`w-4 h-4 text-slate-400 transition-transform ${menuOpen ? 'rotate-180' : ''}`} />
                            </button>

                            {menuOpen && (
                                <div className="absolute right-0 top-[calc(100%+0.75rem)] w-72 rounded-3xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-950 shadow-[0_24px_80px_rgba(15,23,42,0.18)] overflow-hidden">
                                    <div className="p-4 border-b border-slate-100 dark:border-slate-900">
                                        <div className="text-[11px] tracking-[0.22em] text-slate-400 dark:text-slate-500">私有研究空间</div>
                                        <div className="mt-2 text-base font-semibold leading-6 text-slate-950 dark:text-slate-50 break-all">{user.email}</div>
                                    </div>
                                    <div className="p-2">
                                        <button
                                            onClick={() => {
                                                setMenuOpen(false)
                                                navigate('/reports')
                                            }}
                                            className="w-full flex items-center gap-3 rounded-2xl px-3 py-3 text-sm text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-900 transition-colors"
                                        >
                                            <div className="w-8 h-8 rounded-xl bg-slate-100 dark:bg-slate-900 flex items-center justify-center">
                                                <Monitor className="w-4 h-4" />
                                            </div>
                                            我的报告
                                        </button>
                                        <button
                                            onClick={() => {
                                                setMenuOpen(false)
                                                navigate('/settings')
                                            }}
                                            className="w-full flex items-center gap-3 rounded-2xl px-3 py-3 text-sm text-slate-700 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-900 transition-colors"
                                        >
                                            <div className="w-8 h-8 rounded-xl bg-slate-100 dark:bg-slate-900 flex items-center justify-center">
                                                <Settings className="w-4 h-4" />
                                            </div>
                                            模型设置
                                        </button>
                                    </div>
                                    <div className="p-2 border-t border-slate-100 dark:border-slate-900">
                                        <button
                                            onClick={() => {
                                                setMenuOpen(false)
                                                logout()
                                            }}
                                            className="w-full flex items-center gap-3 rounded-2xl px-3 py-3 text-sm text-rose-600 dark:text-rose-400 hover:bg-rose-50 dark:hover:bg-rose-500/10 transition-colors"
                                        >
                                            <div className="w-8 h-8 rounded-xl bg-rose-50 dark:bg-rose-500/10 flex items-center justify-center">
                                                <LogOut className="w-4 h-4" />
                                            </div>
                                            退出登录
                                        </button>
                                    </div>
                                </div>
                            )}
                        </div>
                    )}
                </div>
            </div>
        </header>
    )
}
