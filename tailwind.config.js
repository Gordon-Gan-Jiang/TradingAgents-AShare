/** @type {import('tailwindcss').Config} */
export default {
    content: [
        "./index.html",
        "./src/**/*.{js,ts,jsx,tsx}",
    ],
    theme: {
        extend: {
            colors: {
                trading: {
                    bg: {
                        primary: 'rgb(var(--trading-bg-primary) / <alpha-value>)',
                        secondary: 'rgb(var(--trading-bg-secondary) / <alpha-value>)',
                        tertiary: 'rgb(var(--trading-bg-tertiary) / <alpha-value>)',
                    },
                    accent: {
                        green: 'rgb(var(--trading-accent-green) / <alpha-value>)',
                        red: 'rgb(var(--trading-accent-red) / <alpha-value>)',
                        blue: 'rgb(var(--trading-accent-blue) / <alpha-value>)',
                        orange: 'rgb(var(--trading-accent-orange) / <alpha-value>)',
                        purple: 'rgb(var(--trading-accent-purple) / <alpha-value>)',
                        cyan: 'rgb(var(--trading-accent-cyan) / <alpha-value>)',
                    },
                    text: {
                        primary: 'rgb(var(--trading-text-primary) / <alpha-value>)',
                        secondary: 'rgb(var(--trading-text-secondary) / <alpha-value>)',
                        muted: 'rgb(var(--trading-text-muted) / <alpha-value>)',
                    },
                    border: 'rgb(var(--trading-border) / <alpha-value>)',
                },
            },
            fontFamily: {
                mono: ['JetBrains Mono', 'SF Mono', 'monospace'],
                sans: ['Inter', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
            },
            animation: {
                'pulse-slow': 'pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite',
                'spin-slow': 'spin 3s linear infinite',
            },
        },
    },
    plugins: [],
}
