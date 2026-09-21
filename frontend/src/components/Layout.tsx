import { ReactNode } from 'react'
import Sidebar from './Sidebar'
import Header from './Header'

interface LayoutProps {
    children: ReactNode
}

export default function Layout({ children }: LayoutProps) {
    return (
        <div className="min-h-screen bg-slate-100 dark:bg-slate-950 text-slate-900 dark:text-slate-100">
            <Sidebar />
            <div className="ml-16 min-h-screen flex flex-col">
                <Header />
                {/* 深色下内容区必须比卡片更深：原先 from-slate-900 to-slate-800 比卡片
                    的 slate-800/70 还亮，卡片在右下角会和背景融成一片、失去层级。 */}
                <main className="flex-1 p-6 bg-gradient-to-b from-slate-100 to-slate-50 dark:from-slate-950 dark:via-slate-950 dark:to-slate-900/50">
                    {children}
                </main>
            </div>
        </div>
    )
}
