/** @type {import('tailwindcss').Config} */
export default {
    darkMode: 'class',
    content: [
        "./index.html",
        "./src/**/*.{js,ts,jsx,tsx}",
    ],
    theme: {
        extend: {
            colors: {
                // 保留 trading 命名空间用于特定业务颜色
                trading: {
                    accent: {
                        green: '#22c55e',
                        red: '#ef4444',
                        blue: '#3b82f6',
                        cyan: '#06b6d4',
                        purple: '#8b5cf6',
                        orange: '#f97316',
                        yellow: '#eab308',
                        pink: '#ec4899',
                    },
                },
                // 品牌色：与登录页 CTA / 标题渐变同源（cyan → blue → violet）
                brand: {
                    teal: '#0e7490',
                    cyan: '#06b6d4',
                    'cyan-bright': '#22d3ee',
                    blue: '#2563eb',
                    'blue-deep': '#1d4ed8',
                    violet: '#7c3aed',
                    'violet-deep': '#6d28d9',
                },
                // OLED 深色底：卡片比页面底更亮，形成正向层级
                ink: {
                    950: '#020617',
                    900: '#0b1120',
                    850: '#0e1223',
                    800: '#1a1e2f',
                    700: '#334155',
                },
                // 注意：slate-400 不在这里覆盖。
                // 它的对比度必须按主题分别取值（浅色需要更深、深色需要更亮），
                // 单一色值无法同时满足两套主题。改在 index.css 用 CSS 变量按主题切换：
                //   :root { --color-slate-400: #64748b }  → 白底 4.76:1
                //   .dark { --color-slate-400: #94a3b8 }  → slate-900 上 6.96:1
                // 这样 text-slate-400 的 374 处调用无需改动，且不与 dark:text-* 冲突。
            },
            boxShadow: {
                // 浅色卡片：贴近的高光边 + 中距 + 远距大扩散
                card: '0 1px 2px rgba(15, 23, 42, 0.04), 0 8px 20px -12px rgba(15, 23, 42, 0.18)',
                'card-hover': '0 2px 4px rgba(15, 23, 42, 0.06), 0 16px 32px -14px rgba(15, 23, 42, 0.26)',
                // 深色面板：顶部内高光 + 深投影
                panel: 'inset 0 1px 0 rgba(255, 255, 255, 0.05), 0 1px 2px rgba(0, 0, 0, 0.3), 0 12px 28px -16px rgba(0, 0, 0, 0.6)',
                glow: '0 8px 24px -10px rgba(37, 99, 235, 0.55)',
                'glow-strong': '0 12px 30px -10px rgba(37, 99, 235, 0.65)',
                'inset-top': 'inset 0 1px 0 rgba(255, 255, 255, 0.06)',
            },
            backgroundImage: {
                // 品牌渐变（每个色标与白字对比度均 ≥5:1）
                'brand-gradient': 'linear-gradient(101deg, #0e7490 0%, #1d4ed8 48%, #6d28d9 100%)',
                'brand-gradient-soft': 'linear-gradient(101deg, rgba(14,116,144,0.14) 0%, rgba(29,78,216,0.14) 50%, rgba(109,40,217,0.14) 100%)',
                // 卡片表面：极轻微的上亮下暗，避免纯平色块
                'surface-card': 'linear-gradient(180deg, #ffffff 0%, #fbfcfe 100%)',
                'surface-card-dark': 'linear-gradient(180deg, rgba(148,163,184,0.085) 0%, rgba(148,163,184,0.035) 100%)',
            },
            keyframes: {
                apRise: {
                    from: { opacity: '0', transform: 'translateY(8px)' },
                    to: { opacity: '1', transform: 'translateY(0)' },
                },
                auroraDrift: {
                    '0%, 100%': { transform: 'translate3d(0, 0, 0) scale(1)' },
                    '50%': { transform: 'translate3d(2%, -2%, 0) scale(1.06)' },
                },
            },
            animation: {
                'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
                'spin-slow': 'spin 3s linear infinite',
                rise: 'apRise 0.42s cubic-bezier(0.22, 1, 0.36, 1) both',
                aurora: 'auroraDrift 22s ease-in-out infinite',
            },
            fontFamily: {
                mono: ['JetBrains Mono', 'SF Mono', 'monospace'],
                sans: ['Inter', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
            },
        },
    },
    plugins: [],
}
