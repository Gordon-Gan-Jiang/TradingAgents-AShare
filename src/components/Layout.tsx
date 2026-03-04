import { ReactNode, useState } from 'react'
import Sidebar from './Sidebar'
import Header from './Header'

interface LayoutProps {
    children: ReactNode
}

export default function Layout({ children }: LayoutProps) {
    const [sidebarExpanded, setSidebarExpanded] = useState(false)

    return (
        <div className="flex h-screen bg-trading-bg-primary overflow-hidden">
            <Sidebar
                expanded={sidebarExpanded}
                onToggleExpand={() => setSidebarExpanded((prev) => !prev)}
            />
            <div className="flex-1 flex flex-col min-w-0">
                <Header />
                <main className="flex-1 overflow-auto p-6">
                    {children}
                </main>
            </div>
        </div>
    )
}
